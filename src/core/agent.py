import os
import sys
import time
import re
import signal
import subprocess
import difflib
from pathlib import Path
from collections import defaultdict
import argparse
import json
import shutil
from datetime import datetime

try:
    from openai import OpenAI
except ImportError:  # unit tests and LLM-free jobs import this module without the package
    OpenAI = None

from svaparser import *

CORE_DIR = Path(__file__).resolve().parent
SRC_DIR = CORE_DIR.parent
REPO_ROOT = SRC_DIR.parent
ANALYSIS_DIR = SRC_DIR / 'analysis'
FPV_SCRIPTS_DIR = REPO_ROOT / 'fpv_app_scripts'
for _path in (REPO_ROOT, ANALYSIS_DIR, CORE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from assertion_template import (
    TEMPLATE_RULES as rules,
    STRUCTURED_OUTPUT_EXAMPLE,
    extract_assertions_from_response as _extract_assertions_from_response,
    split_designer_assertion_blocks,
    get_assertion_name,
    rename_assertion,
    assertion_property_span,
    assertion_property_text,
    normalize_assertion_id as _normalize_assertion_name,
)
from instantiation_params import read_context_file
import rtl_clocking
from svalint_wrapper import (
    run_svalint,
    format_violations_for_llm,
    format_violation_summary,
    resolve_svalint_root,
)
import proof_status
from proof_status import Qualification, ProofStatus, VacuityStatus
from llm_cost import CostLedger, extract_usage, load_price_overrides
from adaptive_stop import (
    AdaptiveStopConfig,
    RepairOutcome,
    StopReason,
    StoppingPolicy,
)
import coi as coi_analysis
import provenance
import assumption_gen
import bind_internals
import cex_context
from metrics_report import metrics_from_run
from svapshot_ui.snapshot import build_snapshot, write_run_status, write_snapshot

# Re-export helpers that unit tests import from this module.
_looks_like_sv_literal_fragment = bind_internals.looks_like_sv_literal_fragment
_mask_sv_comments_and_strings = bind_internals.mask_sv_comments_and_strings
_sub_in_sv_code = bind_internals.sub_in_sv_code


def _module_prefix_pattern(module_name, identifier):
    """Backward-compatible name for the bare-identifier matcher used in tests."""
    return bind_internals.unqualified_identifier_pattern(identifier)

# Default cap for ad-hoc ``main.py`` runs. DATE cells export
# ``SVAPSHOT_MAX_ASSERTIONS`` from ``generation.max_assertions``. Tests still
# patch this name.
MAX_ASSERTIONS = 50

# Assumption generation is a gate before semantic repair. An empty Responses
# API final message is treated as a failed call, not as "no assumptions".
ASSUMPTION_PROPOSAL_ATTEMPTS = 3


def responses_output_text(completion) -> str:
    """Visible text from a Responses API object.

    ``output_text`` is empty when a reasoning model spends its budget on
    hidden reasoning and writes no final message. Walk the output items
    before giving up so a non-empty message part is not dropped.
    """
    text = getattr(completion, 'output_text', None)
    if text and str(text).strip():
        return str(text)
    chunks = []
    for item in getattr(completion, 'output', None) or ():
        contents = getattr(item, 'content', None)
        if isinstance(contents, str) and contents.strip():
            chunks.append(contents)
            continue
        for part in contents or ():
            part_text = getattr(part, 'text', None)
            if part_text and str(part_text).strip():
                chunks.append(str(part_text))
    return ''.join(chunks)


def assertion_cap():
    """Active assertion cap, or ``None`` when the run is uncapped.

    ``SVAPSHOT_MAX_ASSERTIONS=0`` / ``unlimited`` disables the cap. Any other
    positive integer overrides :data:`MAX_ASSERTIONS`.
    """
    raw = os.environ.get('SVAPSHOT_MAX_ASSERTIONS')
    if raw not in (None, ''):
        token = str(raw).strip().lower()
        if token in {'0', 'unlimited', 'none'}:
            return None
        try:
            return max(1, int(token))
        except ValueError:
            pass
    return MAX_ASSERTIONS

# SV type keywords mistaken as signal names (e.g. logic unsigned [...])
SV_PREFIX_EXCLUDE = frozenset({
    'unsigned', 'signed', 'logic', 'bit', 'int', 'byte', 'shortint', 'longint',
})

_INTERNAL_DECL_SKIP_PREFIXES = (
    'assign', 'always', 'function', 'endfunction', 'task', 'endtask',
    'case', 'endcase', 'endmodule', 'import', 'typedef', 'if', 'else',
    'for', 'begin', 'end', 'default', 'unique', 'priority', 'return',
)


def _collect_internal_signal_names_from_decl(line):
    """Extract declared signal identifiers from an internal RTL declaration line."""
    line = line.strip()
    if not line.endswith(';'):
        return []

    if line.startswith('logic'):
        names_part = line[len('logic'):].split(';', 1)[0]
    elif line.startswith(_INTERNAL_DECL_SKIP_PREFIXES):
        return []
    else:
        decl_match = re.match(
            r'^(?:\w+(?:\s*\[[^\]]+\])?\s+)+(.+?)\s*;\s*(?://.*)?$',
            line,
        )
        if not decl_match:
            return []
        names_part = decl_match.group(1)

    names = []
    for signal_name in re.findall(r'\b([A-Za-z_]\w*)\b', names_part):
        if signal_name in SV_PREFIX_EXCLUDE:
            continue
        names.append(signal_name)
    return names


def _deduplicate_assertion_names(assertions):
    """Return a collision-free copy while preserving every assertion.

    All names already present in the batch are reserved before suffixes are
    chosen. Without that first pass, ``a_x, a_x, a_x_1`` becomes
    ``a_x, a_x_1, a_x_1`` and the collision "fix" creates another collision.
    """
    original_names = [get_assertion_name(item) for item in assertions]
    reserved = {name for name in original_names if name}
    seen = set()
    occurrences = {}
    corrected = []
    renamed = {}
    fix_count = 0

    for assertion, name in zip(assertions, original_names):
        if not name:
            corrected.append(assertion)
            continue
        occurrence = occurrences.get(name, 0)
        occurrences[name] = occurrence + 1
        if name not in seen:
            seen.add(name)
            corrected.append(assertion)
            continue

        suffix = 1
        candidate = f'{name}_{suffix}'
        while candidate in reserved or candidate in seen:
            suffix += 1
            candidate = f'{name}_{suffix}'
        seen.add(candidate)
        reserved.add(candidate)
        corrected.append(rename_assertion(assertion, candidate))
        renamed[f'{name}_occurrence_{occurrence}'] = candidate
        fix_count += 1

    return corrected, fix_count, renamed


def _format_cnf_clauses(clauses):
    """Format CNF clause lists as (A ∨ ¬B) ∧ (C) notation."""
    def _format_literal(lit):
        lit = str(lit)
        if lit.startswith('!('):
            return '¬' + lit[1:]
        if lit.startswith('!'):
            return '¬' + lit[1:]
        return lit

    return ' ∧ '.join(
        '(' + ' ∨ '.join(_format_literal(lit) for lit in clause) + ')'
        for clause in clauses
    )


def _cnf_imperfect(clauses, postcondition=None):
    """True when CNF extraction left placeholders or unbalanced literals."""
    if clauses is None:
        return True
    if postcondition:
        for tok in postcondition:
            if '__TERM_' in str(tok):
                return True
    for clause in clauses:
        for lit in clause:
            s = str(lit)
            if '__TERM_' in s:
                return True
            # A stringified expression tree is not a literal: it means the
            # conversion gave up somewhere and kept the tree as text.
            if "['" in s or '[[' in s:
                return True
            body = s[1:] if s.startswith('!') else s
            if body.count('(') != body.count(')'):
                return True
    return False


# Model configurations
default_temperature = 0.2
models = [
    # NVIDIA Models
    {"name": "nvdev/nvidia/nemotron-4-340b-instruct-128k", "max_completion_tokens": 128*1024, "temperature": default_temperature, "type": "nvidia", "supports_top_p": True},
    {"name": "nvdev/meta/llama-4-maverick-17b-128e-instruct", "max_completion_tokens": 128*1024, "temperature": default_temperature, "type": "nvidia", "supports_top_p": True},
    {"name": "nvdev/deepseek-ai/deepseek-r1", "max_completion_tokens": 128*1024, "temperature": 0.5, "type": "nvidia", "supports_top_p": True},
    {"name": "deepseek-ai/deepseek-r1-0528", "max_completion_tokens": 128*1024, "temperature": 0.5, "type": "nvidia", "supports_top_p": True},
    {"name": "meta/llama-3.1-405b-instruct", "max_completion_tokens": 8192, "temperature": default_temperature, "type": "nvidia", "supports_top_p": True},
    {"name": "meta/llama-3.3-70b-instruct", "max_completion_tokens": 8192, "temperature": default_temperature, "type": "nvidia", "supports_top_p": True},
    {"name": "moonshotai/kimi-k2.5", "max_completion_tokens": 128*1024, "temperature": default_temperature, "type": "nvidia", "supports_top_p": True},
    # OpenAI Models (responses endpoint — v1/responses)
    {"name": "gpt-5.3-codex", "max_completion_tokens": 4096, "temperature": default_temperature, "type": "openai_responses", "supports_top_p": False},
    {"name": "gpt-5.6-terra", "max_completion_tokens": 16384, "temperature": None, "type": "openai_responses", "supports_top_p": False},
    {"name": "gpt-5.6-luna", "max_completion_tokens": 16384, "temperature": None, "type": "openai_responses", "supports_top_p": False},
    {"name": "gpt-5.6-sol", "max_completion_tokens": 16384, "temperature": None, "type": "openai_responses", "supports_top_p": False},
    # Copilot CLI — inference via `copilot -p "..."` subprocess, no API key needed
    {"name": "copilot", "max_completion_tokens": None, "temperature": None, "type": "copilot_cli", "supports_top_p": False},
    # Cursor SDK — any catalog model via `cursor:<id>` or `composer-*`.
    # Bare `cursor` / `cursor-sdk` uses $CURSOR_MODEL or composer-2.5.
    {"name": "cursor", "max_completion_tokens": 16384, "temperature": None, "type": "cursor_sdk", "supports_top_p": False},
    {"name": "cursor-sdk", "max_completion_tokens": 16384, "temperature": None, "type": "cursor_sdk", "supports_top_p": False},
    {"name": "composer-2.5", "max_completion_tokens": 16384, "temperature": None, "type": "cursor_sdk", "supports_top_p": False},
    {"name": "grok-4.6", "max_completion_tokens": 16384, "temperature": None, "type": "cursor_sdk", "supports_top_p": False},
    {"name": "cursor:grok-4.6", "max_completion_tokens": 16384, "temperature": None, "type": "cursor_sdk", "supports_top_p": False},
    # Legacy Cursor CLI — `agent -p --mode ask --trust`
    {"name": "cursor-cli", "max_completion_tokens": None, "temperature": None, "type": "cursor_cli", "supports_top_p": False},
    # Google Cloud Vertex AI
    {"name": "gemini-3.1-pro-preview", "max_completion_tokens": 16384, "temperature": 0.2, "type": "vertex", "supports_top_p": False},
    {"name": "gemini-3.8-flash", "max_completion_tokens": 16384, "temperature": 0.2, "type": "vertex", "supports_top_p": False},
    {"name": "gemini-2.5-pro", "max_completion_tokens": 16384, "temperature": 0.2, "type": "vertex", "supports_top_p": False},
    {"name": "gemini-2.5-flash", "max_completion_tokens": 16384, "temperature": 0.2, "type": "vertex", "supports_top_p": False},
    # OpenAI Models (chat endpoint — v1/chat/completions)
    {"name": "gpt-4-turbo-preview", "max_completion_tokens": 32768, "temperature": default_temperature, "type": "openai", "supports_top_p": True},
    {"name": "gpt-4.1-mini", "max_completion_tokens": 32768, "temperature": default_temperature, "type": "openai", "supports_top_p": True},
    {"name": "o4-mini-2025-04-16", "max_completion_tokens": 32768, "temperature": 1, "type": "openai", "supports_top_p": False},
    {"name": "o4-mini", "max_completion_tokens": 32768, "temperature": 1, "type": "openai", "supports_top_p": False},
    {"name": "o3", "max_completion_tokens": 32768, "temperature": 1, "type": "openai", "supports_top_p": False},
    {"name": "o3-mini", "max_completion_tokens": 32768, "temperature": 1, "type": "openai", "supports_top_p": False},
]


def resolve_model_config(name):
    """Look up ``name`` in the model table, or synthesise Vertex/Cursor rows.

    Unknown names stay unresolved so ``main.py`` can keep sending them to the
    chat endpoint. Cursor catalog ids use ``cursor:<id>`` or ``composer-*``.
    """
    found = next((entry for entry in models if entry['name'] == name), None)
    if found:
        config = dict(found)
        if config.get('type') == 'cursor_sdk':
            import cursor_llm
            config['cursor_model'] = (
                cursor_llm.catalog_model_id(name)
                or cursor_llm.DEFAULT_CURSOR_MODEL)
        return config
    if str(name).startswith('gemini-'):
        return {
            'name': name,
            'max_completion_tokens': 16384,
            'temperature': 0.2,
            'type': 'vertex',
            'supports_top_p': False,
        }
    import cursor_llm
    cursor_id = cursor_llm.catalog_model_id(name)
    if cursor_id:
        return {
            'name': name,
            'cursor_model': cursor_id,
            'max_completion_tokens': 16384,
            'temperature': None,
            'type': 'cursor_sdk',
            'supports_top_p': False,
        }
    return None


class CodingAgent:

    # Logging levels mapping
    LOG_LEVELS = {'low': 1, 'medium': 2, 'high': 3, 'debug': 4}

    def __init__(self, rtl_source, assertions_file, assertions_dir, llm, prompting_strategy, module_type, verbosity, api_key, execution='golden', formal_tool='jaspergold', stop_after_extension=False, resume_from_semantic=False, resume_from_extension=False):
        # Run options
        self.rtl_source = rtl_source
        self.assertions_file = assertions_file
        self.assertions_dir = assertions_dir
        self.llm = llm
        self.prompting_strategy = prompting_strategy
        self.module_type = module_type
        self.verbosity = verbosity
        self.api_key = api_key
        self.execution = execution  # New parameter for execution mode
        self.formal_tool = formal_tool  # Formal verification tool ('jaspergold' or 'vcformal')
        if resume_from_semantic and resume_from_extension:
            raise ValueError(
                'resume_from_semantic and resume_from_extension are mutually exclusive')
        if stop_after_extension and resume_from_semantic:
            raise ValueError(
                'stop_after_extension and resume_from_semantic are mutually exclusive')
        self.stop_after_extension = bool(stop_after_extension)
        self.resume_from_semantic = bool(resume_from_semantic)
        self.resume_from_extension = bool(resume_from_extension)

        # Network retry configuration for LLM calls
        self.llm_max_retries = 3
        self.llm_base_delay = 2  # Base delay in seconds
        self.llm_max_delay = 60  # Maximum delay in seconds

        # Rate limiting specific configuration
        self.rate_limit_max_retries = 5  # More retries for rate limits
        self.rate_limit_base_delay = 5   # Longer base delay for rate limits
        self.rate_limit_max_delay = 120  # Longer max delay for rate limits

        # Clean up previous error tracking data
        self._cleanup_error_tracking()

        # Initialize log file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file_path = os.path.join(self.assertions_dir, f'agent_log_{timestamp}.log')

        # Write initial log entry
        with open(self.log_file_path, 'w') as log_file:
            log_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Log file created\n")

        # Load RTL
        self.rtl = self._load_rtl()
        self.rtl_module = os.path.basename(rtl_source)

        # Parent instantiation guidance written by main.py for direct
        # submodules. Empty for a top-level run or when the parent left no
        # overrides worth targeting.
        self.instantiation_context = read_context_file(
            os.path.splitext(self.rtl_module)[0])

        # Load initial assertions
        self.assertions = self._load_assertions()

        self.checker_code = self._load_checker_code()
        if getattr(self, 'module_type', 'sequential') == 'combinational':
            self.checker_code = rtl_clocking.ensure_combinational_clocking(
                self.checker_code)

        self._log('debug', f'Loaded checker code in class intialization: {self.checker_code}')

        # Create SVA parser
        self.parser = SVAParser()

        # Interaction storage
        self.interactions = []

        # Initialize metrics
        self.metrics = {
            'syntax_iterations': 0,
            'extension_iterations': 0,
            'semantic_iterations': 0,
            'total_assertions_added': 0,
            'total_assertions_removed': 0,
            'llm_calls': 0,
            'total_input_tokens': 0,
            'total_output_tokens': 0,
            # Add missing metrics used in GUI and run_agent
            'initial_assertions': 0,
            'final_assertions': 0,
            'syntax_corrected': 0,
            'semantic_corrected': 0,
            'semantic_failed': 0,
            'syntax_api_requests': 0,
            'semantic_api_requests': 0,
            'internal_port_binds': 0,
            'duplicate_name_fixes': 0,
            'assertions_after_syntax_correction': 0,
            'assertions_added_during_expansion': 0,
            'assertions_after_property_expansion': 0,
            'unique_submodules': 0,
            'total_submodule_instances': 0,
            'rtl_line_count': 0,
            'module_type': 'Unknown',
            # Verification results metrics
            'passing_assertions_before_semantic': 0,
            'failing_assertions_before_semantic': 0,
            'undefined_assertions_before_semantic': 0,
            'passing_assertions_after_semantic': 0,
            'failing_assertions_after_semantic': 0,
            'undefined_assertions_after_semantic': 0,
            # Fine-grained timing metrics
            'total_formal_time': 0.0,
            'total_llm_time': 0.0,
            'total_llm_cost': 0.0,
            # Assumption-aware qualification of the final property set
            'proved_non_vacuous': 0,
            'proved_vacuous': 0,
            'failing_property_mismatch': 0,
            'failing_missing_assumption': 0,
            'inconclusive': 0,
            'proven_assumption_dependent': 0,
            'formal_runs': 0,
        }

        # Token/monetary accounting. Stages are attributed via self.llm_stage,
        # which each pipeline phase sets before it starts issuing LLM calls.
        load_price_overrides()
        self.cost_ledger = CostLedger(model=self.llm)
        self.llm_stage = 'preprocessing'

        # Adaptive semantic-repair budget (replaces the fixed 5 attempts).
        self.stopping_policy = StoppingPolicy(AdaptiveStopConfig())
        self.ablation = os.environ.get('SVAPSHOT_ABLATION', '').strip()
        if self.ablation == 'fixed_five_repairs':
            self.stopping_policy = StoppingPolicy(AdaptiveStopConfig(
                max_attempts_per_property=5,
                min_attempts_per_property=5,
                success_probability_floor=0.0,
            ))

        # Latest qualified formal result, refreshed by every _get_prop_result call.
        self.formal_result = None

        # Environment assumptions currently in force, and every candidate ever
        # proposed together with the verdict of each screen it went through.
        self.active_assumptions = []
        self.assumption_candidates = []
        self.assumption_screening = {}
        self.enable_assumption_generation = (
            self.ablation != 'no_generated_assumptions')
        self.assumption_mutation_screen = False

        # Initialize property versions tracking
        self.property_versions = {}
        self.commented_assertions = []

        # Initialize timing
        self.timing = {
            'total_start': 0,
            'total_end': 0,
            'preprocessing_start': 0,
            'preprocessing_end': 0,
            'syntax_correction_start': 0,
            'syntax_correction_end': 0,
            'set_extension_start': 0,
            'set_extension_end': 0,
            'semantic_correction_start': 0,
            'semantic_correction_end': 0,
            'gui_start': 0,
            'gui_end': 0
        }

        # Create interactions directory if it doesn't exist
        interactions_dir = os.path.join(self.assertions_dir, 'interactions')
        os.makedirs(interactions_dir, exist_ok=True)

        # Create versions directory if it doesn't exist
        versions_dir = os.path.join(self.assertions_dir, 'versions')
        os.makedirs(versions_dir, exist_ok=True)

        # Create error tracking directory
        error_tracking_dir = os.path.join(self.assertions_dir, 'error_tracking')
        os.makedirs(error_tracking_dir, exist_ok=True)

        # Log the initialization
        self._log('medium', f'CodingAgent initialized with execution mode: {self.execution}')
        self._log('medium', f'RTL source: {rtl_source}')
        self._log('medium', f'Assertions file: {assertions_file}')
        self._log('medium', f'Assertions directory: {assertions_dir}')
        self._log('medium', f'LLM: {llm}')
        self._log('medium', f'Module type: {module_type}')
        self._log('medium', f'Verbosity: {verbosity}')
        self._log('medium', f'Execution mode: {execution}')
        if self.stop_after_extension:
            self._log('medium', 'Agent will stop after set extension')
        if self.resume_from_semantic:
            self._log('medium', 'Agent will resume at semantic correction')
        if self.resume_from_extension:
            self._log('medium', 'Agent will resume at set extension')
        if self.instantiation_context:
            self._log('medium',
                'Parent instantiation context loaded for parameter-aware prompts')

        # Syntax
        self.initial_syntax_iterations = 0
        self.syntax_iterations = 0
        self.error_threshold = 3  # Number of times an error can occur before removing the assertion

        self.assertions_list = []

        self.current_error = ''  # Current iteration error
        self.errors = []        # Total list of errors

        # Coverage
        self.initial_assertions = []
        self.new_assertions = ''
        self.assertion_count = 0
        self.coverage = []
        #: Consolidated SnapshotMetrics, filled in by _finalize_reports.
        self.snapshot_metrics = None

        # Find the model configuration
        model_config = resolve_model_config(self.llm)
        if not model_config:
            raise ValueError(f"Model {self.llm} not found in configuration")

        # Initialize the appropriate client based on model type
        if model_config["type"] in ("nvidia", "openai", "openai_responses"):
            if OpenAI is None:
                raise ImportError(
                    'The openai package is required for LLM clients; '
                    'install build/requirements.txt')
        if model_config["type"] == "nvidia":
            self.client = OpenAI(
                api_key=self.api_key,
                base_url='https://integrate.api.nvidia.com/v1'
            )
        elif model_config["type"] in ("openai", "openai_responses"):
            self.client = OpenAI(
                api_key=self.api_key
            )
        elif model_config["type"] in (
            "copilot_cli", "cursor_cli", "cursor_sdk", "vertex",
        ):
            self.client = None  # Vertex uses ADC; Cursor SDK / CLIs are lazy
        else:
            raise ValueError(f"Unsupported model type: {model_config['type']}")

        # Store model configuration
        self.model_config = model_config

    def _instantiation_context_block(self):
        """Parent parameter guidance, or '' when this module is not a leaf under a parent."""
        context = getattr(self, 'instantiation_context', '') or ''
        if not context:
            return ''
        return '\n' + context.strip() + '\n'

    def _extract_module_name(self, file_path):
        """
        Extract module name from file path, handling both .sv (SystemVerilog) and .v (Verilog) extensions.

        Args:
            file_path (str): Path to the RTL file

        Returns:
            str: Module name without extension
        """
        basename = os.path.basename(file_path)
        # Remove both .sv and .v extensions
        if basename.endswith('.sv'):
            return basename[:-3]  # Remove .sv
        elif basename.endswith('.v'):
            return basename[:-2]  # Remove .v
        else:
            # Fallback: use basename as-is
            return basename

    # -------------------------------------------------------------------------
    # Formal tool abstraction helpers
    # -------------------------------------------------------------------------

    def _run_formal_tool(self):
        """Run the configured formal verification tool and return the completed process.

        For JasperGold the batch script starts JG in the background and exits immediately
        (exit code 0); the agent polls for the log file.  For VC Formal the script runs
        synchronously and always exits 0 (non-zero VC Formal codes are suppressed by the
        script); the log file is complete when the call returns.

        Returns:
            subprocess.CompletedProcess: Result of the tool invocation.

        Raises:
            ValueError: If the configured formal_tool is not supported.
        """
        module_name = self._extract_module_name(self.rtl_module)
        if self.formal_tool == 'jaspergold':
            process = self._run_batch_script('run_jg_batch.sh', module_name)
            if process.returncode:
                raise subprocess.CalledProcessError(
                    process.returncode, process.args, process.stdout, process.stderr)
            return process
        elif self.formal_tool == 'vcformal':
            from vcf_session import VcfSessionBusy, prepare_vcf_session
            try:
                prepare_vcf_session(os.getcwd(), module_name)
            except VcfSessionBusy as exc:
                raise RuntimeError(str(exc)) from exc
            return self._run_vcformal_with_license_retry(module_name)
        else:
            raise ValueError(f"Unsupported formal tool: {self.formal_tool}")

    #: Waits between attempts when the license server has no free token. A
    #: refused license is not a result, and on a shared server it is usually
    #: gone for minutes rather than hours.
    LICENSE_RETRY_DELAYS_S = (30, 90, 180, 300)

    #: How every formal tool is launched. The tools fork a tree of processes
    #: (vc_static_shell, svi, vcs, comelab) that inherit whatever stdin they are
    #: given. If that is a terminal and the group is not the terminal's
    #: foreground one — a backgrounded run, or one whose launching shell has
    #: exited — the first read earns the whole tree a SIGTTIN and the kernel
    #: stops it. Nothing is printed, nothing exits, the log simply ends
    #: mid-proof and this process waits on a child that will never run again.
    #: A session of its own means there is no controlling terminal to be stopped
    #: by, and /dev/null on stdin means there is nothing to read from either.
    DETACHED_FROM_TERMINAL = {
        'stdin': subprocess.DEVNULL,
        'start_new_session': True,
    }

    #: Wall-clock backstop for one invocation of a formal tool. The generated
    #: TCL bounds each check command, but nothing bounds elaboration, a licence
    #: server that never answers, or a command the budget does not reach, and a
    #: run that never returns costs the whole experiment rather than one module.
    #: SVAPSHOT_FORMAL_TIMEOUT_S overrides it; 0 means wait indefinitely.
    FORMAL_RUN_TIMEOUT_S = 6 * 60 * 60

    def _formal_run_timeout_s(self):
        """The wall-clock backstop for one formal invocation, or None for no limit."""
        raw = os.environ.get('SVAPSHOT_FORMAL_TIMEOUT_S')
        if raw is None:
            return self.FORMAL_RUN_TIMEOUT_S
        try:
            seconds = float(raw)
        except ValueError:
            self._log('low',
                f'⚠️  SVAPSHOT_FORMAL_TIMEOUT_S={raw!r} is not a number; '
                f'using {self.FORMAL_RUN_TIMEOUT_S}s')
            return self.FORMAL_RUN_TIMEOUT_S
        return seconds if seconds > 0 else None

    def _run_batch_script(self, script_name, module_name):
        """Run one formal batch script, detached from any terminal and bounded.

        The script forks a tree of tool processes, so a timeout has to signal
        the whole group: killing the script alone would leave VC Formal holding
        its licence and the project directory, stalling every later run.
        Iterative VC Formal calls need compilation and FPV results, not coverage
        instrumentation and the formal-core/density/COI follow-up. Default that
        script to the fast path while preserving an explicit environment
        override. Calling run_vcf_batch.sh directly retains the TCL's full-
        coverage default.

        Returns:
            subprocess.CompletedProcess: The outcome, with returncode 124 (the
            timeout convention) when the run was killed for exceeding its time.
        """
        command = [str(FPV_SCRIPTS_DIR / script_name), module_name]
        environment = os.environ.copy()
        if script_name == 'run_vcf_batch.sh':
            environment.setdefault('SVAPSHOT_COVERAGE', '0')
            environment.setdefault('SVAPSHOT_HIERARCHICAL_COVERAGE', '0')
            # Host Cursor sets TERM=dumb; batch FPV goes through vcformal-dev
            # (run_vcf_batch.sh) which needs a real terminfo entry.
            environment.setdefault('TERM', 'vt100')
            environment.setdefault('TERMINFO', '/usr/share/terminfo')
            environment.setdefault('SVAPSHOT_VCF_DOCKER', 'vcformal-dev')
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, **self.DETACHED_FROM_TERMINAL)
        timeout_s = self._formal_run_timeout_s()
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            self._log('low',
                f'⏱️  {script_name} exceeded {timeout_s}s and was killed. '
                'Anything it wrote is partial and is not evidence about the design.')
            self._kill_process_group(process)
            try:
                # Reap the corpse and close the pipes; the output is whatever
                # the tool managed before it was killed.
                stdout, stderr = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                stdout, stderr = b'', b''
            returncode = 124
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    @staticmethod
    def _kill_process_group(process):
        """Signal the run's whole process group, then insist."""
        try:
            group = os.getpgid(process.pid)
        except OSError:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(group, sig)
            except OSError:
                return
            try:
                process.wait(timeout=15)
                return
            except subprocess.TimeoutExpired:
                continue

    def _run_vcformal_with_license_retry(self, module_name):
        """Run VC Formal, waiting for a token if the license server refuses one.

        Without this the pipeline would carry on and record zero proofs for a
        design the tool never read, which is indistinguishable in the reports
        from a property set that proves nothing.

        Args:
            module_name (str): Module whose batch script should be run.

        Returns:
            subprocess.CompletedProcess: Result of the last invocation.
        """
        fail_fast = os.environ.get('SVAPSHOT_VCF_FAIL_FAST', '').strip().lower() in {
            '1', 'true', 'yes', 'on',
        }
        attempts = (0,) if fail_fast else (0,) + self.LICENSE_RETRY_DELAYS_S
        for attempt, delay in enumerate(attempts):
            if delay:
                self._log('low',
                    f'⏳ No VC Formal license available; waiting {delay}s before '
                    f'attempt {attempt + 1} of {len(attempts)}')
                time.sleep(delay)

            # The return code is not consulted: VC Formal exits non-zero on
            # compile and proof failures alike, and the agent determines what
            # happened by inspecting the log file.
            process = self._run_batch_script('run_vcf_batch.sh', module_name)

            try:
                with open(self._get_formal_log_path(), 'r') as handle:
                    log_text = handle.read()
            except OSError:
                return process

            if not proof_status.license_failure_in_log(log_text):
                if attempt:
                    self._log('low', f'✅ License obtained on attempt {attempt + 1}')
                return process

        self._log('low',
            '❌ VC Formal never obtained a license within '
            f'{sum(self.LICENSE_RETRY_DELAYS_S)}s of waiting. Nothing below is '
            'evidence about the design.')
        return process

    def _get_formal_project_dir(self):
        """Return the project/output directory used by the current formal tool.

        Returns:
            str: Path to the project directory.
        """
        module_name = self._extract_module_name(self.rtl_module)
        if self.formal_tool == 'jaspergold':
            return os.path.join('projs', module_name)
        elif self.formal_tool == 'vcformal':
            return os.path.join('vcf_projs', module_name)
        else:
            raise ValueError(f"Unsupported formal tool: {self.formal_tool}")

    def _get_formal_log_path(self):
        """Return the path to the main log file produced by the formal tool.

        Returns:
            str: Full path to the log file.
        """
        project_dir = self._get_formal_project_dir()
        if self.formal_tool == 'jaspergold':
            return os.path.join(project_dir, 'jg.log')
        elif self.formal_tool == 'vcformal':
            return os.path.join(project_dir, 'vcf.log')
        else:
            raise ValueError(f"Unsupported formal tool: {self.formal_tool}")

    def _wait_for_formal_log(self, file_path):
        """Poll for the tool log, then fail instead of waiting forever."""
        from vcf_session import wait_for_file
        timeout = float(os.environ.get('SVAPSHOT_FORMAL_LOG_WAIT_S', 120))
        if wait_for_file(
                file_path, timeout,
                log=lambda message: self._log('medium', message)):
            return
        raise RuntimeError(
            f'Formal tool produced no log at {file_path} within {timeout}s. '
            'Treating the session as dead so later seeds are not stalled.')

    def _record_llm_usage(self, input_tokens, output_tokens, seconds, exact=False):
        """Book one LLM call into both the legacy metrics and the cost ledger.

        Args:
            input_tokens (int): Prompt tokens.
            output_tokens (int): Completion tokens.
            seconds (float): Wall time of the call.
            exact (bool): True when the provider reported the counts itself.

        Returns:
            float: Monetary cost of the call in the ledger's currency.
        """
        cost = self.cost_ledger.record(
            stage=self.llm_stage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            seconds=seconds,
            exact=exact,
        )
        self._update_metrics(
            'total_input_tokens', self.metrics['total_input_tokens'] + input_tokens,
            increment=False)
        self._update_metrics(
            'total_output_tokens', self.metrics['total_output_tokens'] + output_tokens,
            increment=False)
        self._update_metrics('llm_calls')
        self.metrics['total_llm_time'] += seconds
        self.metrics['total_llm_cost'] = self.cost_ledger.total_cost
        publication_usage_log = os.environ.get('PUBLICATION_LLM_USAGE_LOG')
        if publication_usage_log:
            os.makedirs(
                os.path.dirname(os.path.abspath(publication_usage_log)),
                exist_ok=True,
            )
            with open(publication_usage_log, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({
                    'stage': self.llm_stage,
                    'model': self.llm,
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'seconds': round(seconds, 3),
                    'exact': exact,
                }, sort_keys=True) + '\n')
        self._log('debug',
            f'LLM call: {input_tokens} in / {output_tokens} out tokens, '
            f'{seconds:.2f}s, {cost:.4f}{self.cost_ledger.currency[:1]} '
            f'({"exact" if exact else "estimated"}), stage={self.llm_stage}')
        return cost

    def _get_llm_completion(self, messages, temperature=None, top_p=None, max_tokens=None):
        """
        Get completion from LLM with proper configuration based on model type.
        Includes retry logic for network errors with exponential backoff.

        Args:
            messages: List of message dictionaries for the chat completion
            temperature: Optional temperature override
            top_p: Optional top_p override
            max_tokens: Optional max_tokens override

        Returns:
            The processed response string
        """
        import time
        import random

        # Import error classes with fallback handling
        retryable_exceptions = [ConnectionError]  # Always available

        try:
            from httpx import RemoteProtocolError, ConnectError, TimeoutException
            retryable_exceptions.extend([RemoteProtocolError, ConnectError, TimeoutException])
        except ImportError:
            pass  # httpx not available, skip these exceptions

        try:
            from openai import APIError, APIConnectionError, RateLimitError, APITimeoutError
        except ImportError:
            # Fallback for older OpenAI versions
            try:
                from openai.error import APIError, APIConnectionError, RateLimitError, Timeout as APITimeoutError
            except ImportError:
                # If we can't import these, define minimal fallbacks
                class APIError(Exception): pass
                class APIConnectionError(Exception): pass
                class RateLimitError(Exception): pass
                class APITimeoutError(Exception): pass

        # Additional rate limiting and quota error handling
        rate_limit_exceptions = [RateLimitError]

        # Add HTTP status-based rate limiting detection
        try:
            from httpx import HTTPStatusError
            rate_limit_exceptions.append(HTTPStatusError)
        except ImportError:
            pass

        rate_limit_exceptions = tuple(rate_limit_exceptions)

        retryable_exceptions.extend([APIConnectionError, APITimeoutError])
        retryable_exceptions = tuple(retryable_exceptions)

        # Use instance retry configuration
        max_retries = self.llm_max_retries
        base_delay = self.llm_base_delay
        max_delay = self.llm_max_delay

        # Use model defaults if not specified
        temperature = temperature if temperature is not None else self.model_config["temperature"]
        top_p = top_p if top_p is not None else 0.7 if self.model_config["supports_top_p"] else None
        max_tokens = max_tokens if max_tokens is not None else self.model_config["max_completion_tokens"]

        self._log('debug', f"\nDebug: Model configuration:")
        self._log('debug', f"Model: {self.llm}")
        self._log('debug', f"Temperature: {temperature}")
        self._log('debug', f"Top_p: {top_p}")
        self._log('debug', f"Max tokens: {max_tokens}")

        # Count input tokens with the model's own tokenizer where available; the
        # len//4 rule under-counts SystemVerilog badly and the cost figures in
        # the snapshot manifest are derived from these numbers.
        input_tokens = sum(
            self.cost_ledger.count_tokens(message['content']) for message in messages
        )
        self._log('medium', f"Input tokens ({self.cost_ledger.accounting_method}): {input_tokens}")

        # --- Vertex AI (Google Cloud) ------------------------------------------
        if self.model_config["type"] == "vertex":
            import vertex_llm
            system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            prompt_text = f"{system_msg}\n\n{user_msg}" if system_msg else user_msg
            for attempt in range(max_retries + 1):
                try:
                    llm_start_time = time.time()
                    response = vertex_llm.generate(
                        self.llm, prompt_text, max_output_tokens=max_tokens or 16384,
                    )
                    llm_call_time = time.time() - llm_start_time
                    out_tokens = self.cost_ledger.count_tokens(response)
                    self._record_llm_usage(input_tokens, out_tokens, llm_call_time)
                    return response
                except Exception:
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                        time.sleep(delay)
                    else:
                        raise
        # --- Cursor SDK path ----------------------------------------------------
        if self.model_config["type"] == "cursor_sdk":
            import cursor_llm
            system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            prompt_text = f"{system_msg}\n\n{user_msg}" if system_msg else user_msg
            sdk_model = self.model_config.get("cursor_model") or self.llm
            for attempt in range(max_retries + 1):
                try:
                    llm_start_time = time.time()
                    response = cursor_llm.generate(sdk_model, prompt_text)
                    llm_call_time = time.time() - llm_start_time
                    out_tokens = self.cost_ledger.count_tokens(response)
                    self._record_llm_usage(input_tokens, out_tokens, llm_call_time)
                    return response
                except Exception:
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                        time.sleep(delay)
                    else:
                        raise
        # --- Copilot CLI path ---------------------------------------------------
        if self.model_config["type"] == "copilot_cli":
            system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
            user_msg   = next((m["content"] for m in messages if m["role"] == "user"),   "")
            prompt_text = f"{system_msg}\n\n{user_msg}" if system_msg else user_msg

            for attempt in range(max_retries + 1):
                try:
                    llm_start_time = time.time()
                    self._log('debug', f"\nDebug: Calling copilot CLI (attempt {attempt + 1}/{max_retries + 1})...")
                    result = subprocess.run(
                        ['copilot', '-p', prompt_text],
                        capture_output=True, text=True
                    )
                    llm_call_time = time.time() - llm_start_time

                    if result.returncode != 0:
                        raise RuntimeError(
                            f"copilot CLI exited with code {result.returncode}:\n{result.stderr.strip()}"
                        )

                    response = result.stdout.strip()
                    self._record_llm_usage(
                        input_tokens, self.cost_ledger.count_tokens(response), llm_call_time)

                    if not response:
                        self._log('medium', "\nWarning: Empty response received from copilot CLI")
                        if attempt < max_retries:
                            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                            self._log(
                                'medium',
                                f'Retrying empty copilot reply in {delay:.1f}s '
                                f'(attempt {attempt + 2}/{max_retries + 1})...')
                            time.sleep(delay)
                            continue
                    return response

                except Exception as e:
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                        self._log('medium', f"Copilot CLI error (attempt {attempt + 1}): {e}. Retrying in {delay:.1f}s...")
                        time.sleep(delay)
                    else:
                        self._log('low', f"Unexpected error in _get_llm_completion (copilot CLI): {type(e).__name__}: {e}")
                        raise
        # --- End Copilot CLI path -----------------------------------------------

        # --- Cursor CLI path ----------------------------------------------------
        if self.model_config["type"] == "cursor_cli":
            system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
            user_msg   = next((m["content"] for m in messages if m["role"] == "user"),   "")
            prompt_text = f"{system_msg}\n\n{user_msg}" if system_msg else user_msg

            for attempt in range(max_retries + 1):
                try:
                    llm_start_time = time.time()
                    result = subprocess.run(
                        ['agent', '-p', '--mode', 'ask', '--trust', prompt_text],
                        capture_output=True, text=True
                    )
                    llm_call_time = time.time() - llm_start_time

                    if result.returncode != 0:
                        raise RuntimeError(
                            f"cursor CLI exited with code {result.returncode}:\n{result.stderr.strip()}"
                        )

                    response = result.stdout.strip()
                    self._record_llm_usage(
                        input_tokens, self.cost_ledger.count_tokens(response), llm_call_time)

                    return response

                except Exception as e:
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                        time.sleep(delay)
                    else:
                        raise
        # --- End Cursor CLI path ------------------------------------------------

        is_responses_model = self.model_config["type"] == "openai_responses"

        if is_responses_model:
            system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
            user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
            prompt_text = f"{system_msg}\n\n{user_msg}" if system_msg else user_msg
            completion_params = {
                "model": self.llm,
                "reasoning": {"effort": "medium"},
                "input": prompt_text,
                "max_output_tokens": max_tokens,
            }
        else:
            # v1/chat/completions
            completion_params = {
                "model": self.llm,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            }
            if self.model_config["type"] == "openai":
                # Ask the provider for the authoritative token counts; without
                # this a streamed response carries no usage block and the cost
                # would have to be estimated. Only requested for endpoints known
                # to accept the option, since others reject unknown parameters.
                completion_params["stream_options"] = {"include_usage": True}
            # Add max tokens parameter with correct name based on model
            if self.model_config["type"] == "openai" and ("o4" in self.llm or "o3" in self.llm):
                completion_params["max_completion_tokens"] = max_tokens
            else:
                completion_params["max_tokens"] = max_tokens
            # Add top_p only if the model supports it
            if self.model_config["supports_top_p"] and top_p is not None:
                completion_params["top_p"] = top_p

        # Retry loop with exponential backoff
        for attempt in range(max_retries + 1):
            try:
                self._log('debug', f"\nDebug: Calling API (attempt {attempt + 1}/{max_retries + 1})...")
                # Start timing LLM call
                llm_start_time = time.time()
                usage = None
                if is_responses_model:
                    completion = self.client.responses.create(**completion_params)
                    response = responses_output_text(completion)
                    usage = extract_usage(completion)
                    chunk_count = 0
                else:
                    completion = self.client.chat.completions.create(**completion_params)
                    self._log('debug', "Debug: API call completed")

                    response = ''
                    chunk_count = 0

                    # Process streaming response with error handling
                    try:
                        for chunk in completion:
                            chunk_count += 1
                            # The usage block arrives on the final chunk, which
                            # carries no choices when stream_options was accepted.
                            chunk_usage = extract_usage(chunk)
                            if chunk_usage:
                                usage = chunk_usage
                            if not chunk.choices:
                                continue
                            if hasattr(chunk.choices[0].delta, 'content'):
                                if chunk.choices[0].delta.content is not None:
                                    response += chunk.choices[0].delta.content
                            else:
                                self._log('debug', f"Debug: Chunk has no content attribute: {chunk}")

                    except retryable_exceptions as stream_error:
                        # If streaming fails, we still want to retry the entire request
                        raise stream_error

                llm_call_time = time.time() - llm_start_time

                if usage:
                    self._record_llm_usage(
                        usage['input_tokens'], usage['output_tokens'],
                        llm_call_time, exact=True)
                else:
                    self._record_llm_usage(
                        input_tokens, self.cost_ledger.count_tokens(response),
                        llm_call_time, exact=False)

                self._log('debug', f"\nDebug: Response summary:")
                self._log('debug', f"Total chunks processed: {chunk_count}")
                self._log('debug', f"Final response length: {len(response)}")

                if not response:
                    self._log('medium', "\nWarning: Empty response received from API")
                    self._log('medium', "Debug: Full completion object:")
                    self._log('medium', str(completion))
                    if attempt < max_retries:
                        delay = min(
                            base_delay * (2 ** attempt) + random.uniform(0, 1),
                            max_delay)
                        self._log(
                            'medium',
                            f'Retrying empty API response in {delay:.1f}s '
                            f'(attempt {attempt + 2}/{max_retries + 1})...')
                        time.sleep(delay)
                        continue

                return response  # Return the response string instead of the completion object

            except rate_limit_exceptions as e:
                # Handle rate limiting with more aggressive delays
                # Use rate limit specific retry count (higher than regular retries)
                rate_limit_max_retries = max(max_retries, self.rate_limit_max_retries)
                if attempt < rate_limit_max_retries:
                    # More aggressive backoff for rate limits using rate limit specific settings
                    rate_limit_delay = min(
                        self.rate_limit_base_delay * (3 ** attempt) + random.uniform(1, 3),
                        self.rate_limit_max_delay
                    )

                    # Check for specific rate limit types
                    error_msg = str(e).lower()
                    is_quota_exceeded = any(keyword in error_msg for keyword in [
                        "quota", "billing", "usage limit", "exceeded your current quota",
                        "insufficient_quota", "quota_exceeded"
                    ])
                    is_too_many_requests = any(keyword in error_msg for keyword in [
                        "too many requests", "rate limit", "429", "requests per"
                    ])

                    # Check if it's an HTTPStatusError with 429 status
                    is_http_429 = False
                    if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                        is_http_429 = e.response.status_code == 429

                    if is_quota_exceeded:
                        self._log('medium', f"\n⚠️  QUOTA EXCEEDED: Your API quota has been exceeded")
                        self._log('medium', f"Error: {str(e)}")
                        self._log('medium', f"This usually means you need to check your billing/usage limits")
                        self._log('medium', f"Waiting {rate_limit_delay:.1f} seconds before retry {attempt + 2}/{rate_limit_max_retries + 1}...")
                        self._log('debug', f"API quota exceeded on attempt {attempt + 1}. Retrying in {rate_limit_delay:.1f}s")
                    elif is_too_many_requests or is_http_429:
                        self._log('medium', f"\n⚠️  TOO MANY REQUESTS (HTTP 429): Rate limit exceeded")
                        self._log('medium', f"Error: {str(e)}")
                        self._log('medium', f"Waiting {rate_limit_delay:.1f} seconds before retry {attempt + 2}/{rate_limit_max_retries + 1}...")
                        self._log('debug', f"Rate limit (429) error on attempt {attempt + 1}. Retrying in {rate_limit_delay:.1f}s")
                    else:
                        self._log('medium', f"\n⚠️  RATE LIMIT ERROR: {type(e).__name__}")
                        self._log('medium', f"Error: {str(e)}")
                        self._log('medium', f"Waiting {rate_limit_delay:.1f} seconds before retry {attempt + 2}/{rate_limit_max_retries + 1}...")
                        self._log('debug', f"Rate limit error on attempt {attempt + 1}. Retrying in {rate_limit_delay:.1f}s")

                    time.sleep(rate_limit_delay)
                    continue
                else:
                    error_msg = str(e).lower()
                    if any(keyword in error_msg for keyword in ["quota", "billing", "usage limit"]):
                        self._log('medium', f"\n❌ API QUOTA EXCEEDED after {rate_limit_max_retries + 1} attempts")
                        self._log('medium', f"Error: {str(e)}")
                        self._log('medium', f"Please check your OpenAI billing and usage limits.")
                        self._log('medium', f"You may need to add payment method or upgrade your plan.")
                    else:
                        self._log('medium', f"\n❌ Rate limit error after {rate_limit_max_retries + 1} attempts")
                        self._log('medium', f"Error: {str(e)}")
                        self._log('medium', f"Consider reducing request frequency or upgrading your API plan.")
                        self._log('medium', f"You may also try running the agent during off-peak hours.")
                    raise

            except retryable_exceptions as e:
                # Handle network/connection errors
                if attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                    self._log('medium', f"\nNetwork error encountered: {type(e).__name__}")
                    self._log('medium', f"Error message: {str(e)}")
                    self._log('medium', f"Waiting {delay:.1f} seconds before retry {attempt + 2}/{max_retries + 1}...")
                    self._log('debug', f"Network error on attempt {attempt + 1}: {type(e).__name__}. Retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                else:
                    self._log('medium', f"\nNetwork error after {max_retries + 1} attempts")
                    self._log('medium', f"Error type: {type(e).__name__}")
                    self._log('medium', f"Error message: {str(e)}")
                    raise

            except APIError as e:
                # Check if this is actually a rate limit error in disguise
                error_msg = str(e).lower()
                is_hidden_rate_limit = any(keyword in error_msg for keyword in [
                    "429", "too many requests", "rate limit", "quota", "billing"
                ])

                # Use rate limit specific retry count for hidden rate limits too
                rate_limit_max_retries = max(max_retries, self.rate_limit_max_retries)
                if is_hidden_rate_limit and attempt < rate_limit_max_retries:
                    # Treat as rate limit error with enhanced settings
                    delay = min(
                        self.rate_limit_base_delay * (3 ** attempt) + random.uniform(1, 3),
                        self.rate_limit_max_delay
                    )
                    self._log('medium', f"\n⚠️  API ERROR (Rate Limit): {type(e).__name__}")
                    self._log('medium', f"Error: {str(e)}")
                    self._log('medium', f"Treating as rate limit error. Waiting {delay:.1f} seconds before retry {attempt + 2}/{rate_limit_max_retries + 1}...")
                    self._log('debug', f"API error (rate limit) on attempt {attempt + 1}. Retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                else:
                    # Handle as regular API error (usually not retryable)
                    self._log('medium', f"\nAPI Error in _get_llm_completion:")
                    self._log('medium', f"Error type: {type(e).__name__}")
                    self._log('medium', f"Error message: {str(e)}")
                    raise

            except Exception as e:
                # Handle any other unexpected errors
                self._log('medium', f"\nUnexpected error in _get_llm_completion:")
                self._log('medium', f"Error type: {type(e).__name__}")
                self._log('medium', f"Error message: {str(e)}")
                import traceback
                self._log('medium', "Full traceback:")
                traceback.print_exc()

                # For unknown errors, try once more if we have retries left
                if attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1), max_delay)
                    self._log('medium', f"Attempting retry in {delay:.1f} seconds...")
                    self._log('debug', f"Unexpected error on attempt {attempt + 1}: {type(e).__name__}. Retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                else:
                    raise

    # Logging
    def _write_to_log_file(self, level, message):
        '''
        Writes a message to the log file without timestamps or level labels.

        Args:
            level (str): The log level (unused, kept for compatibility).
            message (str): The message to write.
        '''
        try:
            with open(self.log_file_path, 'a') as log_file:
                log_file.write(f"{message}\n")
        except Exception as e:
            # If log file writing fails, just print to stderr to avoid recursion
            import sys
            print(f"Warning: Failed to write to log file: {e}", file=sys.stderr)

    def _finalize_log(self):
        '''
        Writes a final entry to the log file indicating completion.
        '''
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(self.log_file_path, 'a') as log_file:
                log_file.write(f"[{timestamp}] [INFO] Agent execution completed\n")
                log_file.write("=" * 80 + "\n")
        except Exception as e:
            self._log('medium', f"Warning: Failed to write final log entry: {e}")

    def _log(self, level='medium', message=None, action=None):
        '''
        Handles logging or executes an action based on verbosity level.

        Args:
            level (str): The log level ('low', 'medium', 'high').
            message (str, optional): The log message (if applicable).
            action (callable, optional): A function to execute instead of or in addition to printing.
        '''
        if self.LOG_LEVELS[level] <= self.verbosity:
            # Print message if provided
            if message:
                print(f'{message}')
                # Also write to log file
                self._write_to_log_file(level, message)

            # Call the provided function if it's given
            if callable(action):
                if message:
                    # Call with message (e.g., logging to file)
                    action(message)
                else:
                    action()  # Call with no arguments (e.g., display UI)

    def _print_log_separator(self, msg):
        print('=' * 70)
        print(msg)
        print('=' * 70)

    # Attribute loading
    def _load_checker_code(self):
        with open(self.assertions_dir + self.assertions_file, 'r') as f:
            contents = f.read()

        # Remove initial comments before module interface definition
        # Look for the first 'module' keyword which indicates start of actual RTL
        lines = contents.split('\n')
        module_start_idx = -1

        for i, line in enumerate(lines):
            # Find line that starts with 'module' (possibly with whitespace)
            stripped_line = line.strip()
            if stripped_line.startswith('module ') or (stripped_line == 'module' and i + 1 < len(lines)):
                module_start_idx = i
                break

        if module_start_idx != -1:
            # Keep content from module definition onwards
            contents = '\n'.join(lines[module_start_idx:])

        # First remove generate blocks that contain assertions
        generate_pattern = r'generate\s+for\s*\([^)]*\)\s*begin(?:\s*:\s*[^:]*)?\s*\n\s*.*?end\s+endgenerate'
        contents = re.sub(generate_pattern, '', contents, flags=re.DOTALL)

        # Find the header line
        header_line = '//====DESIGNER-ADDED-SVA====//'
        header_index = contents.find(header_line)

        if header_index == -1:
            return contents  # Return original if no header found

        # Keep only content up to and including the header
        res = contents[:header_index + len(header_line)]

        return res

    def _load_rtl(self):
        # Loads the RTL with path validation
        rtl_path = os.path.abspath(self.rtl_source)

        if not os.path.exists(rtl_path):
            raise FileNotFoundError(f"RTL source file not found: {rtl_path}")

        self._log('medium', f'Loading RTL from: {rtl_path}')

        try:
            with open(rtl_path, 'r') as f:
                contents = f.read()
        except Exception as e:
            raise IOError(f"Failed to read RTL file {rtl_path}: {str(e)}")

        # Remove initial comments before module interface definition
        # Look for the first 'module' keyword which indicates start of actual RTL
        lines = contents.split('\n')
        module_start_idx = -1

        for i, line in enumerate(lines):
            # Find line that starts with 'module' (possibly with whitespace)
            stripped_line = line.strip()
            if stripped_line.startswith('module ') or (stripped_line == 'module' and i + 1 < len(lines)):
                module_start_idx = i
                break

        if module_start_idx != -1:
            # Keep content from module definition onwards
            processed_content = '\n'.join(lines[module_start_idx:])
            self._log('high', f'RTL content loaded successfully, found module at line {module_start_idx + 1}')
            return processed_content
        else:
            # If no module found, return original content
            self._log('medium', 'Warning: No module declaration found in RTL file')
            return contents

    # Loads assertions as list of strings
    def _load_assertions(self):
        with open(self.assertions_dir + self.assertions_file, 'r') as f:
            contents = f.read()

        sva_assertions = split_designer_assertion_blocks(contents)

        '''
        # QUICK TEST LIMIT: Restrict to at most 10 assertions for faster testing
        if len(sva_assertions) > 10:
            self._log('medium', f'QUICK TEST MODE: Limiting assertions from {len(sva_assertions)} to 10 for faster testing')
            sva_assertions = sva_assertions[:10]
        '''

        self._log('medium', 'Initial Assertions:\n')
        for a in sva_assertions:
            self._log('medium', a)
        self._log('medium', '\n')

        self._log('debug', '\nComplete Set:')
        self._log('debug', f'Size of sva_assertions: {len(sva_assertions)}')
        self._log('debug', sva_assertions)

        return sva_assertions

    # Others

    # Write new assertions to property file
    def _write_assertions(self):
        # Final name collision check. Use the same suffix-reserving helper as
        # preprocessing so a late-added a_x_1 cannot collide with a renamed a_x.
        self._log('debug', '\n=== FINAL NAME COLLISION CHECK ===')
        corrected, fix_count, renamed = _deduplicate_assertion_names(
            self.assertions)
        if fix_count:
            self._log(
                'medium',
                f'⚠️  WARNING: Found and fixed {fix_count} duplicate '
                'assertion name(s) in final list',
            )
            self._log('medium', 'Applying final name collision fix...')
            for old_key, new_name in renamed.items():
                self._log('debug', f'Final rename: {old_key} -> {new_name}')
            self.assertions = corrected
            self._log('high', 'Final name collision fix completed')
        else:
            self._log('high', 'No duplicate assertion names found in final list')

        #print(self.checker_code)

        content = self.checker_code
        if getattr(self, 'module_type', 'sequential') == 'combinational':
            content = rtl_clocking.ensure_combinational_clocking(content)
            self.checker_code = content

        for a in self.assertions:
            if a and not content.endswith('\n'):
                content += '\n'
            content += a
            if not a.endswith('\n'):
                content += '\n'
            content += '\n'

        for commented in getattr(self, 'commented_assertions', []):
            if commented and not content.endswith('\n'):
                content += '\n'
            content += commented
            if not commented.endswith('\n'):
                content += '\n'
            content += '\n'

        # Environment assumptions in force. The canonical artifact is the
        # generated *_assume.svh file recorded in the manifest; the same text is
        # inlined here so the constraints reach the tool without depending on
        # the property directory being on the include path.
        if getattr(self, 'active_assumptions', None):
            content += '\n// ---- SVApshot environment assumptions ----\n'
            # Two assumptions with one name is an elaboration error that costs a
            # whole formal run to discover, so the writer is the last guard.
            written = set()
            for assumption in self.active_assumptions:
                if assumption.name in written:
                    continue
                written.add(assumption.name)
                if assumption.rationale:
                    content += f'// {assumption.rationale}\n'
                content += assumption.to_sv() + '\n\n'
            content += (
                f'{assumption_gen.REACHABILITY_COVER_NAME}: cover property '
                f"(1'b1);\n\n")

        # Add endmodule only if it's not already present
        if 'endmodule' not in content:
            content += '\nendmodule\n'

        self._log('debug', '\nWriting to property file:')
        self._log('debug', content)

        with open(self.assertions_dir + self.assertions_file, 'w') as f:
            f.write(content)
        self._log('debug', f'New assertions written to {self.assertions_file}')

    # Save current assertions as list of strings
    def _retrieve_assertions(self, property_file):

        filtered_string = '\n'.join(line for line in self.assertions.splitlines() if (
            '// Check that' in line or 'assert property' in line))
        return filtered_string
        # if(self.verbosity == 3):
        #     print(filtered_string)

    def _dump_json(self, payload, filename):
        """Write a report next to the property file, tolerating I/O failures.

        Reports are diagnostics: a full run must not be lost because a report
        could not be written.

        Args:
            payload (dict): JSON-serialisable report body.
            filename (str): File name inside the assertions directory.

        Returns:
            str or None: Path written, or None on failure.
        """
        path = os.path.join(self.assertions_dir, filename)
        try:
            with open(path, 'w') as handle:
                json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            self._log('medium', f'Wrote {path}')
            return path
        except OSError as error:
            self._log('medium', f'Could not write {path}: {error}')
            return None

    def _get_formal_report_dir(self):
        """Return the directory holding the auxiliary VC Formal reports.

        Returns:
            str: Path to the reports directory (may not exist yet).
        """
        return os.path.join(self._get_formal_project_dir(), 'reports')

    def _qualify_formal_run(self):
        """Parse the latest formal-tool log into qualified property records.

        Every property is placed in one of the five snapshot qualifications
        (proved non-vacuously, proved vacuously, failing due to a property
        mismatch, failing for a missing assumption, inconclusive) rather than
        the binary pass/fail the first version of the tool reported.  For
        VC Formal the auxiliary reports written by FPV_vcf.tcl are merged in,
        which adds the vacuity verdict, the cone of influence, and the
        constraints each proof depended on.

        Returns:
            proof_status.FormalRunResult: The qualified run, also cached on
            ``self.formal_result``.
        """
        file_path = self._get_formal_log_path()
        self._wait_for_formal_log(file_path)

        with open(file_path, 'r', errors='replace') as handle:
            log_text = handle.read()

        module_name = self._extract_module_name(self.rtl_module)
        result = proof_status.parse_formal_log(log_text, self.formal_tool, module_name)

        if self.formal_tool == 'vcformal':
            report_dir = self._get_formal_report_dir()
            if os.path.isdir(report_dir):
                reports = proof_status.load_vcformal_reports(report_dir)
                result = proof_status.merge_reports_into_result(result, reports)
            else:
                self._log('medium',
                    f'No auxiliary reports at {report_dir}; qualification falls back to '
                    f'the streaming log, so vacuity and assumption dependence may be '
                    f'incomplete. Regenerate FPV_vcf.tcl to enable them.')

        self.formal_result = result
        self._update_metrics('formal_runs')

        if result.summary.no_per_property_results:
            self._warn_missing_per_property_results(log_text)

        # 'no_constraints' is the script reporting that it skipped the check
        # because the property set declares no assumptions, which is a clean
        # result rather than a conflict.
        if result.summary.constraint_conflict_status not in (
                '', 'no_conflict', 'no_constraints'):
            self._log('medium',
                f'⚠️  Constraint conflict reported by check_constraints '
                f'({result.summary.constraint_conflict_status}). Every proof in this run '
                f'is suspect until the conflicting assumptions are removed.')

        return result

    def _counterexample_signature(self, assertion_name):
        """Return a coarse fingerprint of why an assertion currently fails.

        The isolated repair run dumps a per-property FSDB under
        ``reports/cex/``; the prompt shrinks that (or a VCD/text sibling) into
        a cycle table. This signature stays the coarse
        ``status@depth/engine`` fingerprint the stopping policy already uses.

        Args:
            assertion_name (str): Short name of the assertion.

        Returns:
            str or None: Signature, or None when no result is available yet.
        """
        if self.formal_result is None:
            return None
        record = self.formal_result.records.get(assertion_name)
        if record is None:
            return None
        return f'{record.proof_status.value}@{record.bound_depth}/{record.engine}'

    def _property_record(self, assertion_name):
        """Return the latest qualified record for ``assertion_name``, if any."""
        result = getattr(self, 'formal_result', None)
        if result is None:
            return None
        return result.records.get(assertion_name)

    def _load_repair_cycle_table(self, assertion_name, assertion_text, record=None):
        """Load a dumped CEX, or a depth-only skeleton for a falsified property."""
        if record is None:
            record = self._property_record(assertion_name)
        wanted = cex_context.signals_from_property(assertion_text)
        wanted.extend(cex_context.core_and_coi_signals(record))
        fail_cycle = None
        if record is not None and record.bound_depth is not None and record.bound_depth >= 0:
            fail_cycle = record.bound_depth
        table = None
        try:
            table = cex_context.load_cycle_table(
                self._get_formal_report_dir(),
                assertion_name,
                wanted,
                fail_cycle=fail_cycle,
            )
        except (OSError, ValueError, TypeError):
            table = None
        if table is not None:
            return table
        if record is not None and record.proof_status is ProofStatus.FALSIFIED:
            return cex_context.skeleton_cycle_table(
                wanted, fail_cycle, fail_signal='')
        return None

    def _include_cex_in_prompt(self):
        """False for the no_cex_in_prompt publication ablation."""
        return getattr(self, 'ablation', '') != 'no_cex_in_prompt'

    def _semantic_repair_context_block(
            self, target_name, target_assertion,
            proven_assertions=(), previous_signature=None):
        """Assumptions, failure kind, core, siblings, solver progress, CEX table."""
        record = self._property_record(target_name)
        include_cex = self._include_cex_in_prompt()
        cycle_table = None
        if include_cex:
            cycle_table = self._load_repair_cycle_table(
                target_name, target_assertion, record)
        return cex_context.build_repair_context_block(
            record=record,
            assumptions=getattr(self, 'active_assumptions', None) or (),
            proven_assertions=proven_assertions,
            previous_signature=previous_signature,
            latest_signature=self._counterexample_signature(target_name),
            cycle_table=cycle_table,
            property_text=target_assertion,
            include_cex=include_cex,
        )

    def _focused_rtl_for_repair(self, target_assertion):
        """RTL lines that mention the property or its formal-core signals."""
        name = get_assertion_name(target_assertion) or coi_analysis.assertion_name(
            target_assertion)
        record = self._property_record(name) if name else None
        signals = cex_context.signals_from_property(target_assertion)
        signals.extend(cex_context.core_and_coi_signals(record))
        return cex_context.rtl_focus(self.rtl, signals)

    def _warn_missing_per_property_results(self, log_text):
        """Explain why a formal run produced no per-property results."""
        if self.formal_tool != 'vcformal':
            self._log('medium', 'No per-property results found in the formal log.')
            return

        if proof_status._vcf_license_failed(log_text):
            self._log('low',
                '❌ VC Formal could not check out a license, so no property was '
                'examined. This run establishes nothing about the design: the '
                'zero counts below are the absence of evidence, not evidence of '
                'absence. Wait for a token on '
                f'{os.environ.get("SNPSLMD_LICENSE_FILE", "the license server")} '
                'and re-run.')
            return

        has_summary = bool(re.search(r'Property Summary:\s*FPV', log_text, re.MULTILINE))
        if has_summary:
            self._log('medium',
                'VC Formal run completed but no per-property results are available.\n'
                '  ACTION REQUIRED: FPV_vcf.tcl must use "check_fv -block" (not plain '
                '"check_fv") to produce the PROP_I_RESULT messages the agent parses.')
        else:
            self._log('medium',
                'No PROP_I_RESULT entries and no summary found in the VC Formal log. '
                'The run may have ended early due to a compile error or a missing '
                '"check_fv -block" call in FPV_vcf.tcl.')

    # Returns dictionary of SVA Name - Proof Result pairs
    def _get_prop_result(self):
        """Return the legacy {assertion: 'proven'|'cex'|'undef'} mapping.

        The mapping is derived from the qualified run so the rest of the agent
        keeps its existing control flow.  One deliberate change in meaning: a
        vacuous proof maps to 'cex', not 'proven', because a property whose
        antecedent is unreachable checks nothing and must be repaired or dropped
        rather than banked as verified behaviour.
        """
        try:
            result = self._qualify_formal_run()
            for name, record in sorted(result.records.items()):
                self._log('debug',
                    f'{name}: {record.qualification.value} '
                    f'(proof={record.proof_status.value}, '
                    f'vacuity={record.vacuity_status.value})')
            return result.legacy_results()
        except subprocess.CalledProcessError as e:
            self._log('medium', f'Program execution failed: {e}')
            return e

    # Run the formal tool to collect syntax errors
    def _get_syntax_errors(self):
        try:
            directory = self._get_formal_project_dir()
            file_path = self._get_formal_log_path()

            self._log('debug', directory)

            # Remove files from previous run
            p = subprocess.run(['rm', '-rf', directory], check=True)

            # Call the formal tool
            formal_start_time = time.time()
            p = self._run_formal_tool()
            formal_end_time = time.time()
            formal_call_time = formal_end_time - formal_start_time
            self.metrics['total_formal_time'] += formal_call_time
            self._log('debug', f"Formal tool call took {formal_call_time:.2f} seconds")

            self._wait_for_formal_log(file_path)

            # Look for syntax errors in the log (tool-specific parsing)
            with open(file_path, 'r') as f:
                log_content = f.read()

            if (self.formal_tool == 'vcformal'
                    and proof_status.infrastructure_failure_in_log(log_content)):
                self.current_error = (
                    'VC Formal did not reach compilation because another session '
                    'owns vcst_rtdb or the VC Static server failed to start.'
                )
                self._log('medium', self.current_error)
                raise RuntimeError(self.current_error)

            # Expand to detect equivalent errors in different assertions and also to gather the names of assertions
            # that are failing due to the same error to tell the LLM the specific names of the assertions
            if self.formal_tool == 'jaspergold':
                for line in log_content.splitlines():
                    if 'ERROR' in line and 'prop.sv' in line:
                        self.errors.append(line)
                        self.current_error = line
                        self._log('medium', line)
                        self._log('medium', 'Assertions contain syntax errors')
                        return False

            elif self.formal_tool == 'vcformal':
                # VC Formal uses VCS to compile the property file.  VCS compile errors
                # appear at column 0 and are multi-line blocks, e.g.:
                #
                #   Error-[SVPS] SVA Property Syntax error
                #     "prop.sv", 42: token is '...'
                #     near "..."
                #
                # or single-line variants:
                #   Error: syntax error near '...' ("prop.sv", 42)
                #
                # Warnings start with [Warning] or Warning-[...] and are NOT errors.
                #
                # Successful elaboration marker (observed in actual vcf.log):
                #   Verdi KDB elaboration done and the database successfully generated:
                #     0 error(s), 0 warning(s)
                # When N > 0 errors are reported here (even without explicit Error lines
                # referencing prop.sv), compilation failed.
                #
                # Strategy:
                #  1. Scan for Error-[...] / Error: blocks that reference prop.sv.
                #  2. If none found but the elaboration summary reports N > 0 errors,
                #     capture the first Error block (any file) as the root cause.
                #  3. If the elaboration success line is present with 0 errors and no
                #     Error blocks referencing prop.sv were found → success.

                lines = log_content.splitlines()

                # Step 1 – find VCS error blocks that reference the property file
                for i, line in enumerate(lines):
                    if re.match(r'^Error', line, re.IGNORECASE):
                        # Collect block: this line plus up to 10 following lines
                        context_end = min(len(lines), i + 11)
                        block_lines = lines[i:context_end]
                        block = '\n'.join(block_lines)

                        if 'prop.sv' in block:
                            self.errors.append(block)
                            self.current_error = block
                            self._log('medium', block)
                            self._log('medium', 'Assertions contain syntax errors')
                            return False

                # Also catch late-stage elaboration failures: VC Formal reports
                # "Error: Elaboration failed." without a direct prop.sv reference.
                # In that case any earlier VCS Error block is the real cause.
                if re.search(r'^Error:.*[Ee]laboration\s+failed', log_content, re.MULTILINE):
                    # Find the first Error line that looks like a VCS compile error
                    for i, line in enumerate(lines):
                        if re.match(r'^Error', line, re.IGNORECASE):
                            context_end = min(len(lines), i + 11)
                            block = '\n'.join(lines[i:context_end])
                            self.errors.append(block)
                            self.current_error = block
                            self._log('medium', block)
                            self._log('medium', 'Assertions contain syntax errors (elaboration failed)')
                            return False

            self._log('medium', 'Assertions compiled successfully')
            return True

        except subprocess.CalledProcessError as e:
            self._log('medium', f'Program execution failed: {e}')
            return e

    @staticmethod
    def _parser_input(assertion):
        """What the assertion database is given: the property and nothing else.

        The parser converts a property expression into clauses.  A failure
        action, a comment or a trailing message is not part of the property, so
        it is removed here rather than tolerated there.  Text that is not a
        labelled assertion is passed on unchanged, which leaves the parser to
        refuse it.
        """
        property_text = assertion_property_text(assertion) or assertion
        return re.sub(r'\s+', '', property_text)

    # Processes batch of properties with SVA Parser module
    def _process_batch(self, limit=None):
        """Store the batch's new properties, at most ``limit`` of them.

        A property stored beyond what the assertion cap allows is dropped from
        the property file but stays in the database, where it answers 'already
        got this one' for a property no later stage can see.  The limit stops
        the batch instead, so what the database holds is what was kept.
        """
        # names = re.findall(r'^\s*\w*:\s*assert\s+property\s*\(.*\);|^\s*assert\s+property\s*\(.*\);', self.assertions, re.MULTILINE)
        i = 0
        new = 0
        new_list = []
        duplicate_list = []  # List to track duplicate assertions

        # Initialize assertions_list if it doesn't exist
        if not hasattr(self, 'assertions_list'):
            self.assertions_list = []

        if self.verbosity >= 2:
            self._log('medium', '================================== PROCESSING PROPERTIES ==================================\n')

        # Store parser state before processing this batch
        parser_state_before = {
            'sva_string': len(self.parser.sva_string),
            'delay_d': len(self.parser.delay_d),
            'postcondition_d': len(self.parser.postcondition_d),
            'clauses_d': len(self.parser.clauses_d),
            'prop_result': len(self.parser.prop_result),
            'unprocessable_props_dict': len(self.parser.unprocessable_props_dict)
        }

        for assertion in self.assertions:
            if limit is not None and new >= limit:
                self._log(
                    'medium',
                    f'⚠️  Assertion cap reached after {new} new properties; '
                    f'{len(self.assertions) - i} of this batch were not offered '
                    'to the database')
                break

            assertion_clean = assertion.replace('\n', '').strip()

            # if self.verbosity >= 2:
            self._log('medium', f'\nProperty {i}: {assertion_clean}')

            assertion_normalized = self._parser_input(assertion_clean)

            if self.parser.process_property(assertion_normalized):
                new += 1
                new_list.append(assertion)
                if assertion not in self.assertions_list:  # Only add if not already present
                    self.assertions_list.append(assertion)
            else:
                # This assertion was a duplicate, add it to the duplicate list
                duplicate_list.append(assertion)

            i += 1

        # Print parser state statistics with context
        self._log('medium', f'\nParser state statistics (processed {len(self.assertions)} assertions in this batch):')
        self._log('medium', f'  Before this batch - Total stored properties: {parser_state_before["sva_string"]}')
        self._log('medium', f'  After this batch  - Total stored properties: {len(self.parser.sva_string)}')
        self._log('medium', f'  New properties added in this batch: {new}')
        self._log('medium', f'  Duplicates found in this batch: {len(duplicate_list)}')
        self._log('medium', f'\nCumulative parser dictionaries:')
        self._log('medium', f'  SVA String dictionary: {len(self.parser.sva_string)}')
        self._log('medium', f'  Delay dictionary: {len(self.parser.delay_d)}')
        self._log('medium', f'  Postcondition dictionary: {len(self.parser.postcondition_d)}')
        self._log('medium', f'  Clauses dictionary: {len(self.parser.clauses_d)}')
        self._log('medium', f'  Results dictionary: {len(self.parser.prop_result)}')
        self._log('medium', f'  Unprocessable properties: {len(self.parser.unprocessable_props_dict)}')

        # Validate no duplicates remain
        validation_passed = self.parser.validate_no_duplicates()
        if not validation_passed:
            self._log('medium', "\n⚠️  WARNING: Duplicate validation failed! This may cause compilation errors.")
        else:
            self._log('medium', "\n✅ Duplicate validation passed!")

        return new, new_list, duplicate_list

    def _check_pre_syntax_duplicates(self, new_assertions):
        """
        Check for duplicates between new assertions and stored ones before syntax correction.
        This method does NOT store any properties, it only filters out duplicates.

        Args:
            new_assertions: List of new assertion strings to check

        Returns:
            tuple: (unique_assertions, duplicate_assertions)
        """
        unique_assertions = []
        duplicate_assertions = []
        # A batch can restate itself. Those copies are duplicates of each other
        # rather than of the database, so nothing before this point has seen
        # them, and only one of them could ever be stored: correcting and
        # proving the rest is work spent on a property already in hand.
        seen_in_batch = {}

        self._log('medium', '\n=== PRE-SYNTAX DUPLICATE CHECK ===')

        for i, assertion in enumerate(new_assertions):
            assertion_clean = assertion.replace('\n', '').strip()
            self._log('medium', f'Checking assertion {i+1}: {assertion_clean}')

            assertion_normalized = self._parser_input(assertion_clean)

            # Check if this assertion is a duplicate
            if self.parser.check_property_duplicate(assertion_normalized):
                self._log('medium', f'  -> DUPLICATE (already exists)')
                duplicate_assertions.append(assertion)
                continue

            signature = self.parser.property_signature(assertion_normalized)
            if signature is not None and signature in seen_in_batch:
                self._log(
                    'medium',
                    f'  -> DUPLICATE (repeats assertion '
                    f'{seen_in_batch[signature]} of this batch)')
                duplicate_assertions.append(assertion)
                continue

            if signature is not None:
                seen_in_batch[signature] = i + 1
            self._log('medium', f'  -> UNIQUE (new assertion)')
            unique_assertions.append(assertion)

        self._log('medium', f'\nPre-syntax check results:')
        self._log('medium', f'  Unique assertions: {len(unique_assertions)}')
        self._log('medium', f'  Duplicate assertions: {len(duplicate_assertions)}')
        self._log('medium', '=' * 50)

        return unique_assertions, duplicate_assertions

    def _display_assertions_gui(self):
        '''Display processed assertions in a GUI window for easier browsing.'''
        try:
            print('Starting GUI initialization...')
            import tkinter as tk
            print('Tkinter imported successfully')
            from tkinter import ttk
            print('ttk imported successfully')
            from tkinter.scrolledtext import ScrolledText
            print('ScrolledText imported successfully')

            try:
                import matplotlib.pyplot as plt
                print('Matplotlib imported successfully')
                from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
                print('FigureCanvasTkAgg imported successfully')
                import numpy as np
                print('Numpy imported successfully')
            except ImportError as e:
                print(
                    f'Warning: Matplotlib or related packages not available: {e}')
                print('GUI will be launched without coverage plot')
                MATPLOTLIB_AVAILABLE = False
            else:
                MATPLOTLIB_AVAILABLE = True

        except ImportError as e:
            print(f'Error: Required packages not available: {e}')
            print('GUI display skipped')
            return

        try:
            # Create the main window
            root = tk.Tk()
            print('Main window created')
            root.title(f'Assertions Browser - {self.rtl_module}')
            # Increased window size to accommodate new tab
            root.geometry('1200x800')

            # Create a notebook for tabbed viewing
            notebook = ttk.Notebook(root)
            notebook.pack(fill='both', expand=True, padx=10, pady=10)
            print('Notebook created')

            # Create frames for different types of data
            assertions_frame = ttk.Frame(notebook)
            details_frame = ttk.Frame(notebook)
            clauses_frame = ttk.Frame(notebook)
            results_frame = ttk.Frame(notebook)
            coverage_frame = ttk.Frame(notebook)
            metrics_frame = ttk.Frame(notebook)  # New metrics frame
            print('Frames created')

            notebook.add(assertions_frame, text='Assertions')
            notebook.add(details_frame, text='Property Details')
            notebook.add(clauses_frame, text='Clauses')
            notebook.add(results_frame, text='Results')
            if MATPLOTLIB_AVAILABLE:
                notebook.add(coverage_frame, text='Formal Coverage Progression')
            notebook.add(metrics_frame, text='Execution Metrics')  # Add metrics tab
            print('Tabs added to notebook')

            # Assertions List (Left side)
            assertions_list_frame = ttk.Frame(assertions_frame)
            assertions_list_frame.pack(
                side=tk.LEFT, fill='both', expand=True, padx=5, pady=5)
            print('Assertions list frame created')

            assertions_label = ttk.Label(
                assertions_list_frame, text='Processed Assertions:')
            assertions_label.pack(anchor='w')

            # Create a canvas and scrollbar for the assertions list
            canvas = tk.Canvas(assertions_list_frame)
            scrollbar = ttk.Scrollbar(
                assertions_list_frame, orient='vertical', command=canvas.yview)
            assertions_listbox = tk.Listbox(
                canvas, width=40, height=30, selectmode=tk.SINGLE)
            print('Canvas and scrollbar created')

            # Configure the canvas
            canvas.configure(yscrollcommand=scrollbar.set)

            # Pack the scrollbar and canvas
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            # Create a frame inside the canvas for the listbox
            listbox_frame = tk.Frame(canvas)
            canvas.create_window((0, 0), window=listbox_frame, anchor=tk.NW)
            assertions_listbox.pack(
                in_=listbox_frame, fill=tk.BOTH, expand=True)
            print('Listbox frame configured')

            # Configure the canvas scrolling region
            def configure_scroll_region(event):
                canvas.configure(scrollregion=canvas.bbox('all'))
            listbox_frame.bind('<Configure>', configure_scroll_region)

            # Assertion details (Right side)
            assertion_details_frame = ttk.Frame(assertions_frame)
            assertion_details_frame.pack(
                side=tk.RIGHT, fill='both', expand=True, padx=5, pady=5)

            details_label = ttk.Label(
                assertion_details_frame, text='Assertion Details:')
            details_label.pack(anchor='w')

            details_text = ScrolledText(
                assertion_details_frame, width=60, height=30)
            details_text.pack(fill='both', expand=True)
            details_text.config(state='disabled')
            print('Details frame configured')

            # Property Details Tab Content
            property_text = ScrolledText(details_frame, width=90, height=35)
            property_text.pack(fill='both', expand=True, padx=5, pady=5)
            property_text.config(state='disabled')

            # Clauses Tab Content
            clauses_text = ScrolledText(clauses_frame, width=90, height=35)
            clauses_text.pack(fill='both', expand=True, padx=5, pady=5)
            clauses_text.config(state='disabled')

            # Results Tab Content
            results_text = ScrolledText(results_frame, width=90, height=35)
            results_text.pack(fill='both', expand=True, padx=5, pady=5)
            results_text.config(state='disabled')
            print('Text widgets created')

            # Coverage Tab Content (only if matplotlib is available)
            if MATPLOTLIB_AVAILABLE:
                # Create matplotlib figure for coverage plot
                plt.style.use('seaborn')  # Use a modern style
                fig, ax = plt.subplots(figsize=(10, 6))

                # Sample data for coverage progression (replace with actual data when available)
                batches = [1, 2, 3, 4, 5]
                coverage_values = [60, 65, 75, 82, 85]
                assertion_counts = [10, 15, 20, 25, 30]

                # Calculate coverage improvements
                coverage_improvements = [
                    0] + [coverage_values[i] - coverage_values[i-1] for i in range(1, len(coverage_values))]
                assertion_increases = [
                    0] + [assertion_counts[i] - assertion_counts[i-1] for i in range(1, len(assertion_counts))]

                # Plot coverage progression
                ax.plot(batches, coverage_values, 'b-o',
                        label='Coverage %', linewidth=2, markersize=8)
                ax.set_xlabel('Batch ID', fontsize=10)
                ax.set_ylabel('Coverage %', fontsize=10)
                ax.set_title('Formal Coverage Progression',
                             fontsize=12, pad=20)
                ax.grid(True, linestyle='--', alpha=0.7)
                ax.legend(fontsize=10)

                # Add coverage improvement annotations
                for i in range(1, len(batches)):
                    ax.annotate(f'+{coverage_improvements[i]}%\n+{assertion_increases[i]} assertions',
                                xy=(batches[i], coverage_values[i]),
                                xytext=(10, 10), textcoords='offset points',
                                bbox=dict(boxstyle='round,pad=0.5',
                                          fc='yellow', alpha=0.5),
                                arrowprops=dict(arrowstyle='->', connectionstyle='arc3,rad=0'))

                # Adjust layout to prevent label cutoff
                plt.tight_layout()

                # Embed the plot in the coverage tab
                canvas = FigureCanvasTkAgg(fig, master=coverage_frame)
                canvas.draw()
                canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
                print('Coverage plot created')

            # Metrics Tab Content
            metrics_text = ScrolledText(metrics_frame, width=90, height=35)
            metrics_text.pack(fill='both', expand=True, padx=5, pady=5)
            metrics_text.config(state='disabled')

            def update_metrics_tab():
                metrics_text.config(state='normal')
                metrics_text.delete(1.0, tk.END)

                metrics_text.insert(tk.END, 'Execution Metrics:\n\n')
                metrics_text.insert(tk.END, f'Initial number of assertions: {self.metrics["initial_assertions"]}\n')
                metrics_text.insert(tk.END, f'Final number of assertions: {self.metrics["final_assertions"]}\n')
                metrics_text.insert(tk.END, f'Automatically syntactically corrected assertions: {self.metrics["syntax_corrected"]}\n')
                metrics_text.insert(tk.END, f'Automatically semantically corrected assertions: {self.metrics["semantic_corrected"]}\n')
                metrics_text.insert(tk.END, f'Number of assertions that were not semantically corrected automatically: {self.metrics["semantic_failed"]}\n')
                metrics_text.insert(tk.END, f'API requests for Syntax correction: {self.metrics["syntax_api_requests"]}\n')
                metrics_text.insert(tk.END, f'API requests for Semantic correction: {self.metrics["semantic_api_requests"]}\n')
                metrics_text.insert(tk.END, f'Total number of input tokens used during execution: {self.metrics["total_input_tokens"]}\n')
                metrics_text.insert(tk.END, f'Total number of output tokens used during execution: {self.metrics["total_output_tokens"]}\n')

                metrics_text.config(state='disabled')

            # Update metrics tab when showing assertion details
            def show_assertion_details(idx):
                # ... existing show_assertion_details code ...

                # Update metrics tab
                update_metrics_tab()

            # Initial metrics update
            update_metrics_tab()

            # Statistics Frame (Bottom)
            stats_frame = ttk.LabelFrame(root, text='Statistics')
            stats_frame.pack(fill='x', padx=10, pady=5, ipady=5)

            stats_text = f'''
            Module: {self.rtl_module}
            Total Assertions: {len(self.assertions)}
            SVA Strings: {len(self.parser.sva_string)}
            Delay Properties: {len(self.parser.delay_d)}
            Postconditions: {len(self.parser.postcondition_d)}
            Clauses: {len(self.parser.clauses_d)}
            Results: {len(self.parser.prop_result)}
            '''
            stats_label = ttk.Label(
                stats_frame, text=stats_text, justify=tk.LEFT)
            stats_label.pack(padx=10, pady=5)
            print('Statistics frame created')

            # Populate the assertions list with color coding
            print('Starting to populate assertions list...')
            # Create a list to store labels in order
            assertion_labels = []

            for idx, assertion in enumerate(self.assertions):
                # Extract assertion name for display
                match = re.search(r'(\w+):\s*assert', assertion)
                name = match.group(1) if match else f'Assertion {idx}'

                # Create a colored label for each assertion
                label = tk.Label(listbox_frame, text=name, anchor='w')

                # Color code based on verification result
                if name in self.parser.prop_result:
                    result = self.parser.prop_result[name]
                    if result == 'cex':
                        label.config(fg='red')
                    elif result == 'proven':
                        label.config(fg='green')
                    else:
                        label.config(fg='black')

                # Store the label and its index
                assertion_labels.append((label, idx))

            # Pack labels in order from top to bottom
            for label, idx in assertion_labels:
                label.pack(fill=tk.X, padx=2, pady=1)
                label.bind('<Button-1>', lambda e,
                           idx=idx: show_assertion_details(idx))

            print(f'Added {len(self.assertions)} assertions to the list')

            # Function to display assertion details when selected
            def show_assertion_details(idx):
                # Get full assertion text
                assertion_text = self.assertions[idx]

                # Enable text widget for editing
                details_text.config(state='normal')
                details_text.delete(1.0, tk.END)

                # Add the details
                match = re.search(r'(\w+):\s*assert', assertion_text)
                assertion_name = match.group(
                    1) if match else f'Assertion {idx}'
                details_text.insert(tk.END, f'Name: {assertion_name}\n\n')
                details_text.insert(
                    tk.END, f'Full Assertion:\n{assertion_text}\n\n')

                # Normalize for lookup
                assertion_normalized = self._parser_input(assertion_text)

                # Add parser details if available
                if assertion_name in self.parser.sva_string:
                    tokens = self.parser.tokenize(assertion_normalized)
                    details_text.insert(tk.END, f'Tokens:\n{tokens}\n\n')

                if assertion_name in self.parser.delay_d:
                    delay = self.parser.delay_d[assertion_name]
                    details_text.insert(tk.END, f'Delay Type: {delay}\n\n')

                if assertion_name in self.parser.postcondition_d:
                    post = self.parser.postcondition_d[assertion_name]
                    details_text.insert(tk.END, f'Postcondition:\n{post}\n\n')

                # Disable text widget after editing
                details_text.config(state='disabled')

                # Update other tabs
                update_property_details(assertion_name)
                update_clauses_tab(assertion_name)
                update_results_tab(assertion_name)

            def update_property_details(assertion_name):
                property_text.config(state='normal')
                property_text.delete(1.0, tk.END)

                if assertion_name in self.parser.sva_string:
                    property_text.insert(
                        tk.END, f'SVA String for {assertion_name}:\n')
                    property_text.insert(
                        tk.END, f'{self.parser.sva_string[assertion_name]}\n\n')

                    # Add postfix information if available
                    if hasattr(self.parser, 'postfix_d') and assertion_name in self.parser.postfix_d:
                        property_text.insert(tk.END, f'Postfix Notation:\n')
                        property_text.insert(
                            tk.END, f'{self.parser.postfix_d[assertion_name]}\n\n')

                    # Add evaluation tree if available
                    if hasattr(self.parser, 'eval_d') and assertion_name in self.parser.eval_d:
                        property_text.insert(tk.END, f'Evaluation Tree:\n')
                        property_text.insert(
                            tk.END, f'{self.parser.eval_d[assertion_name]}\n\n')
                else:
                    property_text.insert(
                        tk.END, 'No detailed property information available.')

                property_text.config(state='disabled')

            def update_clauses_tab(assertion_name):
                clauses_text.config(state='normal')
                clauses_text.delete(1.0, tk.END)

                if assertion_name in self.parser.clauses_d:
                    clauses = self.parser.clauses_d[assertion_name]
                    clauses_text.insert(
                        tk.END, f'Clauses for {assertion_name}:\n\n')

                    for i, clause in enumerate(clauses):
                        clauses_text.insert(tk.END, f'Clause {i+1}:\n')
                        for term in clause:
                            clauses_text.insert(tk.END, f'  {term}\n')
                        clauses_text.insert(tk.END, '\n')
                else:
                    clauses_text.insert(
                        tk.END, 'No clauses information available.')

                clauses_text.config(state='disabled')

            def update_results_tab(assertion_name):
                results_text.config(state='normal')
                results_text.delete(1.0, tk.END)

                if assertion_name in self.parser.prop_result:
                    result = self.parser.prop_result[assertion_name]
                    results_text.insert(
                        tk.END, f'Verification Result for {assertion_name}:\n\n')
                    results_text.insert(tk.END, f'Status: {result}\n\n')

                    if result == 'cex':
                        results_text.insert(
                            tk.END, 'The property has a counterexample (failed).\n')
                    elif result == 'proven':
                        results_text.insert(
                            tk.END, 'The property has been formally proven (passed).\n')
                    else:
                        results_text.insert(
                            tk.END, 'Property verification is undefined or incomplete.\n')
                else:
                    results_text.insert(
                        tk.END, 'No verification results available.')

                results_text.config(state='disabled')

            print('GUI setup completed, starting main loop...')
            # Main loop
            root.mainloop()
            print('GUI closed')

        except Exception as e:
            print(f'Error during GUI creation: {e}')
            import traceback
            traceback.print_exc()
            return

    def _extract_assertions(self, response):
        return _extract_assertions_from_response(
            response, module_type=getattr(self, 'module_type', 'sequential'))

    # Updates assertion set - destructive
    # WiP (renaming logic for duplicated assertion names, maybe not necessary)
    def _update_assertions(self, response):
        """
        Update assertions from LLM response, handling both single-line and multiline assertions.
        Preserves comments and headers above each assertion.

        Args:
            response: String containing the LLM's response with assertions
        """
        if self.verbosity >= 2:
            print("\nProcessing LLM response...")
            print("Raw response:")
            print(response)

        self.assertions = self._extract_assertions(response)

        if self.verbosity >= 2:
            print(f'\nFound {len(self.assertions)} assertions:')
            for i, a in enumerate(self.assertions):
                print(f"\nAssertion {i+1}:")
                print(a)

    # Saves prompt, response and checker file for the iteration

    def _save_iteration_info(self,  prompt, response):
        it_dir = 'syntax_it'+str(self.syntax_iterations)+'/'
        try:
            os.makedirs(self.assertions_dir+it_dir)
            self._log('debug', f'Directory \'{it_dir}\' created successfully')
        except FileExistsError:
            self._log('debug', f'Directory \'{it_dir}\' already exists')

        # new_file_name = self.assertions_file.replace(extension, str(self.syntax_iterations)+extension)
        copy_name = self.assertions_dir + it_dir + self.assertions_file
        current = self.assertions_dir + self.assertions_file
        p = subprocess.run(['cp', current, copy_name], check=True)

        with open(self.assertions_dir + it_dir + 'prompt_it', 'w') as f:
            f.write(prompt)

        with open(self.assertions_dir + it_dir + 'response_it', 'w') as f:
            f.write(response)

        self._log('medium', 'Saved iteration information')

    def _find_assertion_by_name(self, assertion_name):
        """Return the active assertion block with the given label, if present."""
        if not assertion_name:
            return None
        for assertion in self.assertions:
            if get_assertion_name(assertion) == assertion_name:
                return assertion
        return None

    def _replace_assertion(self, assertion_name, new_assertion):
        """Replace one assertion in the active list by label."""
        for index, assertion in enumerate(self.assertions):
            if get_assertion_name(assertion) == assertion_name:
                self.assertions[index] = new_assertion
                return True
        return False

    def _comment_out_assertion(self, assertion_name):
        """Comment out an assertion so the property file can still compile."""
        for index, assertion in enumerate(self.assertions):
            if get_assertion_name(assertion) != assertion_name:
                continue
            commented_lines = []
            for line in assertion.splitlines():
                commented_lines.append('// ' + line if line.strip() else line)
            commented = (
                f'// SVApshot: commented out due to unfixable syntax error in {assertion_name}\n'
                + '\n'.join(commented_lines)
            )
            self.commented_assertions.append(commented)
            del self.assertions[index]
            self._log('medium', f'Commented out assertion: {assertion_name}')
            return True
        return False

    def _identify_failing_assertion_from_error(self, error_message):
        """Identify the assertion responsible for a formal-tool syntax error."""
        if not error_message:
            return None, None

        seen_names = set()
        for match in re.finditer(r'(\w+)\s*:\s*assert\s+property', error_message, re.IGNORECASE):
            assertion_name = match.group(1)
            if assertion_name in seen_names:
                continue
            seen_names.add(assertion_name)
            assertion = self._find_assertion_by_name(assertion_name)
            if assertion:
                return assertion_name, assertion

        error_line = self._extract_error_line(error_message)
        if error_line:
            assertion = self._find_assertion_at_line(error_line)
            if assertion:
                return get_assertion_name(assertion), assertion

        return None, None

    def _run_svalint_check(self):
        """Run SVALint on the current property file and store violations."""
        prop_path = Path(self.assertions_dir) / self.assertions_file
        self._log('medium', f'Running SVALint on {prop_path}...')
        result = run_svalint(prop_path)
        self.current_lint_result = result
        self.current_error = format_violations_for_llm(result)
        self._log('medium', format_violation_summary(result))
        if result.violations:
            for violation in result.violations:
                self._log('medium', f'  [{violation.rule_id}] {violation.message[:120]}')
        # Zero parsed lint errors is not enough: timeout, missing executable, or
        # a tool crash also has zero errors and must keep the parser gate closed.
        return result.passed

    # Tries to correct assertion syntax
    # # WiP (printing)
    def _correct_assertions(self, target_assertion=None, target_name=None):
        self._log('medium', 'Requesting LLM to correct assertion syntax...')

        if not self.assertions:
            self._log('medium', 'No assertions to correct')
            return False

        module_name = self._extract_module_name(self.rtl_module)
        rules_text = rules.replace('MODULE', module_name)
        lint_feedback = self.current_error or 'No SVALint output available.'
        tool_display = 'VC Formal' if self.formal_tool == 'vcformal' else 'JasperGold'

        if target_assertion and target_name:
            assertions_to_fix = [target_assertion]
            prompt = (
                f'{rules_text}\n'
                f'Example output format:\n{STRUCTURED_OUTPUT_EXAMPLE}\n'
                f'{self._instantiation_context_block()}'
                f'The RTL module is {module_name}.\n'
                f'Fix ONLY the syntax error in assertion "{target_name}" reported by {tool_display}.\n'
                f'{lint_feedback}\n'
                f'Broken assertion:\n{target_assertion}\n'
                'RTL:\n'
                f'{self.rtl}\n'
                'Use unqualified checker port and parameter names only. Do NOT write '
                f'{module_name}.signal — declare DUT internals as checker inputs instead.\n'
                'Return ONLY ONE corrected assertion block in the structured --- id/property/failure --- format.\n'
                f'Keep the same assertion id: {target_name}.\n'
                'Do NOT rewrite or return any other assertions.'
            )
        else:
            target_name = get_assertion_name(self.assertions[0])
            if not target_name:
                self._log('medium', 'Could not extract assertion name')
                return False

            assertions_to_fix = self.assertions
            pretty_assertions = '\n\n'.join(assertions_to_fix)
            prompt = (
                f'{rules_text}\n'
                f'Example output format:\n{STRUCTURED_OUTPUT_EXAMPLE}\n'
                f'{self._instantiation_context_block()}'
                f'The RTL module is {module_name}.\n'
                'Correct the assertions below to fix ALL reported SVALint violations.\n'
                f'{lint_feedback}\n'
                'Current assertions:\n'
                f'{pretty_assertions}\n'
                'RTL:\n'
                f'{self.rtl}\n'
                'Use unqualified checker port and parameter names only. Do NOT write '
                f'{module_name}.signal — declare DUT internals as checker inputs instead.\n'
                'Return ONLY corrected assertion blocks in the structured --- id/property/failure --- format.\n'
                'TRY TO NOT REMOVE ANY ASSERTIONS, ONLY CORRECT THE VIOLATIONS.'
            )

        self._log('debug', '\n\nLLM prompt:')
        self._log('debug', prompt)

        response = self._get_llm_completion([
            {'role': 'system', 'content': 'You are a coding assistant. Your responses should ONLY provide syntactically correct code'},
            {'role': 'user', 'content': prompt}
        ])

        self._log('debug', 'LLM response:')
        self._log('debug', response)

        # Store the prompt-response pair
        self._store_llm_interaction('syntax', self.syntax_iterations, prompt, response)

        corrected_assertions = self._extract_assertions(response)

        if not corrected_assertions:
            self._log('medium', f'No valid assertions found in response for {target_name}')
            return False

        self._log('medium', f'\nFound {len(corrected_assertions)} assertions in response:')
        for i, assertion in enumerate(corrected_assertions):
            self._log('medium', f'Assertion {i+1}: {assertion}')

        if target_assertion and target_name:
            corrected = None
            for assertion in corrected_assertions:
                name = get_assertion_name(assertion)
                if name == target_name or name == _normalize_assertion_name(target_name):
                    corrected = assertion
                    break
            if corrected is None:
                corrected = corrected_assertions[0]
                corrected = rename_assertion(corrected, target_name)
            replaced = self._replace_assertion(target_name, corrected)
            if not replaced:
                self._log('medium', f'Could not replace assertion {target_name} in active list')
                return False
        else:
            self.assertions = corrected_assertions

        self._write_assertions()

        with open(self.assertions_dir + self.assertions_file, 'r') as f:
            file_contents = f.read()
            self._log('high', '\nProperty file contents after update:')
            self._log('high', file_contents)

        self._log('high', '\n\nImproved code:')
        self._log('high', response)
        return True

    def _correct_syntax(self):
        self._log('medium', 'Correcting assertion syntax with SVALint...')
        self.llm_stage = 'syntax_repair'

        max_lint_llm_iterations = 5
        max_fpv_llm_attempts_per_assertion = 3
        progressive_mode = False

        while True:
            self._log('medium', f'\nSVALint iteration {self.syntax_iterations + 1}')
            self._write_assertions()
            lint_clean = self._run_svalint_check()

            if lint_clean:
                self._log('medium', 'Assertions are SVALint-clean')
                break

            if getattr(self.current_lint_result, 'execution_error', ''):
                # Infrastructure feedback cannot be corrected by rewriting an
                # assertion. Preserve the set and let the independent formal
                # compile check run; do not spend an LLM iteration on a timeout
                # or broken SVALint installation.
                self._log(
                    'medium',
                    'SVALint did not complete; skipping LLM lint repair: '
                    f'{self.current_lint_result.execution_error}',
                )
                break

            if self.syntax_iterations >= max_lint_llm_iterations and not progressive_mode:
                self._log('medium', f'Reached maximum SVALint LLM iterations ({max_lint_llm_iterations}).')
                progressive_mode = True

            if progressive_mode:
                removed_in_progressive_mode = self._progressive_assertion_removal()
                if not removed_in_progressive_mode:
                    self._log('medium', 'Progressive removal failed. Stopping syntax correction.')
                    break
                continue

            if not self.assertions:
                self._log('medium', 'No assertions remaining after discarding problematic ones')
                break

            self._correct_assertions()
            self.syntax_iterations += 1
            self._update_metrics('syntax_api_requests')

        tool_display = 'VC Formal' if self.formal_tool == 'vcformal' else 'JasperGold'
        self._log('medium', f'Running final {tool_display} compile check...')

        while self.assertions:
            self._write_assertions()
            fpv_clean = self._get_syntax_errors()
            if fpv_clean:
                self._log('medium', f'{tool_display} compile check passed')
                break

            target_name, target_assertion = self._identify_failing_assertion_from_error(self.current_error)
            if not target_name or not target_assertion:
                self._log('medium', 'FPV compile failed but no failing assertion could be identified')
                break

            self._log(
                'medium',
                f'FPV syntax error in assertion "{target_name}"; attempting targeted LLM correction',
            )

            fixed = False
            for attempt in range(max_fpv_llm_attempts_per_assertion):
                previous_assertion = self._find_assertion_by_name(target_name)
                if not previous_assertion:
                    break

                self._log(
                    'medium',
                    f'Targeted FPV fix attempt {attempt + 1}/{max_fpv_llm_attempts_per_assertion} '
                    f'for {target_name}',
                )
                corrected = self._correct_assertions(
                    target_assertion=previous_assertion,
                    target_name=target_name,
                )
                self.syntax_iterations += 1
                self._update_metrics('syntax_api_requests')

                if not corrected:
                    continue

                self._write_assertions()
                if self._get_syntax_errors():
                    fixed = True
                    self._log('medium', f'Assertion "{target_name}" fixed after FPV-guided correction')
                    break

                updated_assertion = self._find_assertion_by_name(target_name)
                if updated_assertion and self._assertions_match(updated_assertion, previous_assertion):
                    self._log('medium', f'LLM did not change assertion "{target_name}" on attempt {attempt + 1}')
                    break

            if fixed:
                continue

            if not self._comment_out_assertion(target_name):
                self._log('medium', f'Failed to comment out assertion "{target_name}"; stopping FPV correction')
                break

            self._log('medium', f'Rerunning {tool_display} after commenting out "{target_name}"')

        self._quarantine_defective_assertions()
        self._update_metrics('syntax_corrected', self.syntax_iterations)
        self._display_error_tracking_stats()

    def _is_assertion_parser_defective(self, assertion):
        """Return True when the SVA parser cannot build usable CNF for an assertion."""
        assertion_name = get_assertion_name(assertion)
        assertion_clean = assertion.replace('\n', '').strip()
        assertion_normalized = self._parser_input(assertion_clean)

        span = assertion_property_span(assertion_clean)
        if span:
            property_body = assertion_clean[span[0]:span[1]]
        else:
            prop_body_match = re.search(
                r'assert\s+property\s*\((.*)\)\s*(?:else\b|$)',
                assertion_clean,
                re.IGNORECASE | re.DOTALL,
            )
            property_body = prop_body_match.group(1) if prop_body_match else ''
        if re.search(r'\)\s*\[', property_body):
            return True, assertion_name, 'part-select on parenthesized expression'

        try:
            parsed_name, _delay, clauses, postcondition = self.parser.parse_sva(
                assertion_normalized,
            )
        except Exception as exc:
            self._log('debug', f'Parser exception for {assertion_name}: {exc}')
            return True, assertion_name, 'parser exception'

        prop_name = parsed_name or assertion_name
        if clauses is None:
            return True, prop_name, 'unprocessable property'
        if _cnf_imperfect(clauses, postcondition):
            return True, prop_name, 'incomplete CNF extraction'
        return False, prop_name, ''

    def _quarantine_defective_assertions(self, max_llm_attempts=2):
        """Comment out assertions that compile but cannot be processed for proofs."""
        if not self.assertions:
            return

        self._log('medium', '\nValidating assertions with SVA parser before proof flow...')
        index = 0
        while index < len(self.assertions):
            assertion = self.assertions[index]
            defective, assertion_name, reason = self._is_assertion_parser_defective(assertion)
            if not defective:
                index += 1
                continue

            if not assertion_name:
                assertion_name = get_assertion_name(assertion) or f'assertion_{index + 1}'

            self._log(
                'medium',
                f'Parser defect in "{assertion_name}" ({reason}); attempting targeted correction',
            )

            fixed = False
            for attempt in range(max_llm_attempts):
                current_assertion = self._find_assertion_by_name(assertion_name)
                if not current_assertion:
                    break

                self.current_error = (
                    f'The SVA parser could not process assertion "{assertion_name}" ({reason}).\n'
                    'Common causes:\n'
                    '- part-select on a parenthesized expression, e.g. "(a + b)[127:0]"\n'
                    '- unsupported expression shapes inside assert property\n'
                    'Rewrite the property expression using valid SVA syntax while preserving intent.\n'
                    f'Broken assertion:\n{current_assertion}'
                )
                self._correct_assertions(
                    target_assertion=current_assertion,
                    target_name=assertion_name,
                )
                self.syntax_iterations += 1
                self._update_metrics('syntax_api_requests')
                self._write_assertions()

                updated_assertion = self._find_assertion_by_name(assertion_name)
                if not updated_assertion:
                    break

                still_defective, _, _ = self._is_assertion_parser_defective(updated_assertion)
                if not still_defective:
                    fixed = True
                    self._log('medium', f'Assertion "{assertion_name}" corrected after parser validation')
                    break

            if fixed:
                index += 1
                continue

            if self._comment_out_assertion(assertion_name):
                self._log(
                    'medium',
                    f'Quarantined parser-defective assertion "{assertion_name}" ({reason})',
                )
                self._write_assertions()
                continue

            index += 1

    def _remove_syntax_error_near_assertions(self):
        """
        Scan the formal tool log for syntax error messages and remove the corresponding assertions.
        This is an optimization to avoid LLM calls for obviously malformed assertions.

        For JasperGold: looks for single-line "syntax error near … prop.sv" messages.
        For VC Formal:  scans multi-line VCS error blocks that reference prop.sv and
                        extracts line numbers from those blocks.

        Returns:
            bool: True if any assertions were removed, False otherwise
        """
        try:
            tool_label = 'VC Formal' if self.formal_tool == 'vcformal' else 'JasperGold'
            self._log('medium', f'\nScanning {tool_label} log for syntax error messages...')

            # Get the log file path
            file_path = self._get_formal_log_path()

            if not os.path.isfile(file_path):
                self._log('medium', 'Log file not found for syntax error scan')
                return False

            # Read the log file
            with open(file_path, 'r') as f:
                log_content = f.read()

            syntax_error_lines = []  # Will hold representative lines with file+line info

            if self.formal_tool == 'jaspergold':
                # JasperGold puts file reference and "near" text on a single line
                for line in log_content.splitlines():
                    if 'syntax error near' in line.lower() and 'prop.sv' in line:
                        syntax_error_lines.append(line)
                        self._log('medium', f'Found syntax error near: {line}')

            elif self.formal_tool == 'vcformal':
                # VCS error blocks span multiple lines.  There are two formats observed
                # in actual runs:
                #
                # Format 1 – syntax errors (analyze), file and number on SEPARATE lines:
                #   Error-[SE] Syntax error
                #     Following verilog source has syntax error :
                #     "path/to/prop.sv",          ← prop.sv line (NO number here)
                #     48: token is ';', column 124 ← number on NEXT line
                #
                # Format 2 – semantic errors (elaborate), number on SAME line:
                #   Error-[IND] Identifier not declared
                #   path/to/prop.sv, 58           ← prop.sv + number on same line
                #
                # To ensure _extract_error_line() can always find the number, we capture
                # the prop.sv line AND the following 2 lines as the snippet.
                lines = log_content.splitlines()
                for i, line in enumerate(lines):
                    if re.match(r'^Error', line, re.IGNORECASE):
                        context_end = min(len(lines), i + 11)
                        block_lines = lines[i:context_end]
                        block = '\n'.join(block_lines)
                        if 'prop.sv' in block:
                            for j, bl in enumerate(block_lines):
                                if 'prop.sv' in bl:
                                    # Capture this line plus the next 2 so that
                                    # format-1 line numbers (on the following line)
                                    # are included in the snippet passed to
                                    # _extract_error_line().
                                    snippet_end = min(len(block_lines), j + 3)
                                    snippet = '\n'.join(block_lines[j:snippet_end])
                                    syntax_error_lines.append(snippet)
                                    self._log('medium',
                                        f'Found VC Formal syntax error: {bl.strip()}')
                                    break

            if not syntax_error_lines:
                self._log('medium', 'No syntax error messages referencing prop.sv found in log')
                return False

            # Extract unique line numbers to avoid processing the same assertion multiple times
            unique_line_numbers = set()
            for error_line in syntax_error_lines:
                line_number = self._extract_error_line(error_line)
                if line_number:
                    unique_line_numbers.add(line_number)

            self._log('medium', f'Found syntax errors at {len(unique_line_numbers)} unique line numbers: {sorted(unique_line_numbers)}')

            # Find problematic assertions for each unique line number
            assertions_to_remove = []
            for line_number in sorted(unique_line_numbers):
                self._log('medium', f'Processing syntax error near at line: {line_number}')

                # Find the assertion at this line
                problematic_assertion = self._find_assertion_at_line(line_number)
                if problematic_assertion:
                    # Extract assertion name for logging
                    assertion_name_match = re.search(r'(\w+)\s*:\s*assert', problematic_assertion)
                    assertion_name = assertion_name_match.group(1) if assertion_name_match else f"line_{line_number}"

                    self._log('medium', f'Found problematic assertion: {assertion_name}')
                    assertions_to_remove.append((assertion_name, problematic_assertion))
                else:
                    self._log('medium', f'Could not find assertion at line {line_number}')

            if not assertions_to_remove:
                self._log('medium', 'No assertions found for "syntax error near" messages')
                self._log('debug', 'This could indicate line number extraction failure or assertion matching issues')
                return False

            # Remove the problematic assertions
            original_count = len(self.assertions)
            removed_count = 0

            for assertion_name, assertion_to_remove in assertions_to_remove:
                # Remove the assertion from the list
                original_assertion_count = len(self.assertions)
                self.assertions = [a for a in self.assertions if not self._assertions_match(a, assertion_to_remove)]

                if len(self.assertions) < original_assertion_count:
                    removed_count += 1
                    self._log('medium', f'✓ Removed assertion with syntax error near: {assertion_name}')
                else:
                    self._log('medium', f'⚠️  Could not remove assertion: {assertion_name}')

            if removed_count > 0:
                self._log('medium', f'\n=== SYNTAX ERROR AUTO-REMOVAL OPTIMIZATION ===')
                self._log('medium', f'Automatically removed {removed_count} assertions with syntax errors')
                self._log('medium', f'Assertions before: {original_count}, after: {len(self.assertions)}')
                self._log('medium', f'Will rerun {tool_label} without LLM prompting...')
                self._log('medium', '=' * 50)

                # Write updated assertions to file
                self._write_assertions()
                return True
            else:
                # This is the problematic case - we found syntax errors but couldn't remove assertions
                self._log('medium', f'\n⚠️  FAILED TO REMOVE SYNTAX ERROR NEAR ASSERTIONS')
                self._log('medium', f'Found {len(assertions_to_remove)} assertions with syntax errors but removed 0')
                self._log('medium', f'This may indicate assertion matching issues or duplicate assertions')
                self._log('medium', 'Incrementing iteration counter to prevent infinite loop')
                self._log('medium', '=' * 50)

                # CRITICAL FIX: Increment iteration counter to prevent infinite loops
                # when we find syntax errors but can't remove the problematic assertions
                self.syntax_iterations += 1
                return False

        except Exception as e:
            self._log('medium', f'Error in _remove_syntax_error_near_assertions: {str(e)}')
            return False

    def _progressive_assertion_removal(self):
        """
        Progressive assertion removal mode: Remove assertions one by one until syntax is correct.
        This is used when LLM-based correction has failed after too many iterations.

        Returns:
            bool: True if an assertion was removed, False otherwise
        """
        try:
            self._log('medium', '\n--- Progressive Assertion Removal Mode ---')
            self._log('medium', f'Current assertions count: {len(self.assertions)}')
            self._log('medium', f'Current error: {self.current_error[:100]}...' if self.current_error else 'No current error available')

            if not self.assertions:
                self._log('medium', 'No assertions left to remove')
                return False

            # Extract line number from current error
            error_line = self._extract_error_line(self.current_error)
            if not error_line:
                self._log('medium', 'Could not extract error line from current error')
                # Fallback: remove the first assertion
                if self.assertions:
                    removed_assertion = self.assertions[0]
                    self.assertions = self.assertions[1:]
                    self._log('medium', f'Fallback: Removed first assertion: {removed_assertion[:50]}...')
                    self._write_assertions()
                    self._log('medium', f'Assertions remaining: {len(self.assertions)}')
                    return True
                return False

            self._log('medium', f'Error detected at line: {error_line}')

            # Find the assertion at the error line
            problematic_assertion = self._find_assertion_at_line(error_line)
            if not problematic_assertion:
                self._log('medium', f'Could not find assertion at line {error_line}')
                # Fallback: remove the first assertion
                if self.assertions:
                    removed_assertion = self.assertions[0]
                    self.assertions = self.assertions[1:]
                    self._log('medium', f'Fallback: Removed first assertion: {removed_assertion[:50]}...')
                    self._write_assertions()
                    self._log('medium', f'Assertions remaining: {len(self.assertions)}')
                    return True
                return False

            # Extract assertion name for logging
            assertion_name_match = re.match(r'\s*(\w+)\s*:\s*assert', problematic_assertion)
            assertion_name = assertion_name_match.group(1) if assertion_name_match else "unnamed"

            self._log('medium', f'Target assertion: {assertion_name}')
            self._log('medium', f'Assertion content: {problematic_assertion[:100]}...')

            # Remove the assertion
            tool_display = 'VC Formal' if self.formal_tool == 'vcformal' else 'JasperGold'
            original_count = len(self.assertions)
            self.assertions = [a for a in self.assertions if not self._assertions_match(a, problematic_assertion)]

            if len(self.assertions) < original_count:
                self._log('medium', f'✓ Successfully removed assertion: {assertion_name}')
                self._write_assertions()
                self._log('medium', f'Assertions remaining: {len(self.assertions)}')
                self._log('medium', f'Will rerun {tool_display} to check if syntax is now correct...')
                return True
            else:
                self._log('medium', 'Failed to remove assertion (no match found)')
                # Fallback: remove the first assertion
                if self.assertions:
                    removed_assertion = self.assertions[0]
                    self.assertions = self.assertions[1:]
                    self._log('medium', f'Fallback: Removed first assertion: {removed_assertion[:50]}...')
                    self._write_assertions()
                    self._log('medium', f'Assertions remaining: {len(self.assertions)}')
                    return True
                return False

        except Exception as e:
            self._log('medium', f'Error in progressive assertion removal: {str(e)}')
            # Fallback: remove the first assertion if any exist
            if self.assertions:
                removed_assertion = self.assertions[0]
                self.assertions = self.assertions[1:]
                self._log('medium', f'Exception fallback: Removed first assertion: {removed_assertion[:50]}...')
                self._write_assertions()
                self._log('medium', f'Assertions remaining: {len(self.assertions)}')
                return True
            return False

    def _extract_error_line(self, error_message):
        """Extract the line number from a formal tool error message.

        Supports JasperGold and both VCS error formats observed in actual VC Formal runs:

        VCS format 1 – syntax errors from 'analyze' (file and number on SEPARATE lines):
            Error-[SE] Syntax error
              Following verilog source has syntax error :
              "path/to/prop.sv",
              48: token is ';', column 124
                                 ^ number is on the line AFTER the quoted path

        VCS format 2 – semantic/elaboration errors from 'elaborate' (same line, unquoted):
            Error-[IND] Identifier not declared
            path/to/prop.sv, 58
                             ^ number is on the SAME line, comma-separated, unquoted path
        """
        try:
            # JasperGold: prop.sv(123):
            jg_match = re.search(r'\.sv\((\d+)\):', error_message)
            if jg_match:
                return int(jg_match.group(1))

            # VCS format 2: unquoted path with comma-separated line number on same line
            # Example: /path/to/prop.sv, 58
            vcf2_match = re.search(r'prop\.sv,\s*(\d+)', error_message)
            if vcf2_match:
                return int(vcf2_match.group(1))

            # VCS format 1: quoted path ending with comma, line number on the NEXT line
            # Example (may span lines): "path/to/prop.sv",\n  48: token is...
            vcf1_match = re.search(
                r'"[^"]*prop\.sv",\s*\n\s*(\d+)\s*:', error_message, re.MULTILINE
            )
            if vcf1_match:
                return int(vcf1_match.group(1))

            # VCS format 1b: quoted path with number on the same line (older VCS style)
            # Example: "path/to/prop.sv", 48
            vcf1b_match = re.search(r'"[^"]*prop\.sv",?\s*(\d+)', error_message)
            if vcf1b_match:
                return int(vcf1b_match.group(1))

            # Generic fallback patterns
            fallback_match = re.search(r'(?:prop\.sv:(\d+):|line\s+(\d+)[^0-9])', error_message)
            if fallback_match:
                return next(int(g) for g in fallback_match.groups() if g is not None)

            return None
        except Exception as e:
            self._log('medium', f'Error extracting line number: {str(e)}')
            return None

    def _find_assertion_at_line(self, line_number):
        """Find the assertion that corresponds to a given line number in the property file."""
        try:
            # Read the property file
            with open(self.assertions_dir + self.assertions_file, 'r') as f:
                lines = f.readlines()

            if line_number > len(lines) or line_number < 1:
                return None

            # Find the assertion that contains this line
            # Start from the error line and work backwards to find the assertion start
            assertion_start = line_number - 1  # Convert to 0-based index

            # Look backwards for the start of an assertion
            while assertion_start >= 0:
                line = lines[assertion_start].strip()
                # Check if this line starts an assertion
                if re.match(r'\s*\w+\s*:\s*assert\s+property', lines[assertion_start]):
                    break
                assertion_start -= 1

            if assertion_start < 0:
                # Couldn't find assertion start, try a different approach
                # Look for the nearest assertion before the error line
                for i in range(line_number - 1, -1, -1):
                    if re.match(r'\s*\w+\s*:\s*assert\s+property', lines[i]):
                        assertion_start = i
                        break
                else:
                    return None

            # Find the end of the assertion (look for semicolon)
            assertion_end = assertion_start
            brace_count = 0
            paren_count = 0
            in_assertion = False

            while assertion_end < len(lines):
                line = lines[assertion_end]

                # Count parentheses and braces to handle nested structures
                for char in line:
                    if char == '(':
                        paren_count += 1
                        in_assertion = True
                    elif char == ')':
                        paren_count -= 1
                    elif char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1

                # Check if we've reached the end of the assertion
                if in_assertion and line.strip().endswith(';') and paren_count == 0 and brace_count == 0:
                    break

                assertion_end += 1

            if assertion_end >= len(lines):
                assertion_end = len(lines) - 1

            # Extract the complete assertion
            assertion_lines = lines[assertion_start:assertion_end + 1]
            assertion = ''.join(assertion_lines).strip()

            # Validate that we found a complete assertion
            if not assertion or not re.search(r'assert\s+property', assertion):
                return None

            return assertion

        except Exception as e:
            self._log('medium', f'Error finding assertion at line {line_number}: {str(e)}')
            return None

    def _track_syntax_errors(self):
        """Track syntax errors and discard assertions that fail repeatedly.

        Improved error tracking that:
        1. Tracks errors by assertion content and error pattern
        2. Implements blacklisting for known difficult error patterns
        3. Removes assertions after 3 consecutive failures
        4. Immediately removes assertions that match blacklisted error patterns

        Returns:
            bool: True if an assertion was removed, False otherwise
        """
        try:
            # Create a directory for error tracking if it doesn't exist
            error_dir = os.path.join(self.assertions_dir, 'error_tracking')
            os.makedirs(error_dir, exist_ok=True)

            # Files to store error tracking data
            error_counts_file = os.path.join(error_dir, 'assertion_error_counts.json')
            blacklist_file = os.path.join(error_dir, 'blacklisted_errors.json')
            consecutive_failures_file = os.path.join(error_dir, 'consecutive_failures.json')

            # Load existing data or initialize new
            if os.path.exists(error_counts_file):
                with open(error_counts_file, 'r') as f:
                    error_counts = json.load(f)
            else:
                error_counts = {}

            if os.path.exists(blacklist_file):
                with open(blacklist_file, 'r') as f:
                    blacklisted_errors = json.load(f)
            else:
                blacklisted_errors = []

            if os.path.exists(consecutive_failures_file):
                with open(consecutive_failures_file, 'r') as f:
                    consecutive_failures = json.load(f)
            else:
                consecutive_failures = {}

            # Extract line number and find the problematic assertion
            error_line = self._extract_error_line(self.current_error)
            if not error_line:
                return False

            # Find the assertion at the error line
            problematic_assertion = self._find_assertion_at_line(error_line)
            if not problematic_assertion:
                self._log('medium', f'Could not find assertion at line {error_line}')
                return False

            # Extract assertion name for tracking
            assertion_name_match = re.match(r'\s*(\w+)\s*:\s*assert', problematic_assertion)
            assertion_name = assertion_name_match.group(1) if assertion_name_match else f"unnamed_line_{error_line}"

            # Create error pattern for blacklisting (simplified error message)
            error_pattern = self._extract_error_pattern(self.current_error)

            self._log('medium', f'Tracking error for assertion: {assertion_name}')
            self._log('medium', f'Error pattern: {error_pattern}')

            # Check if this error pattern is blacklisted
            if error_pattern in blacklisted_errors:
                self._log('medium', f'\nRemoving assertion {assertion_name} - matches blacklisted error pattern:')
                self._log('medium', f'Error: {self.current_error}')
                self._log('medium', f'Assertion: {problematic_assertion}')

                # Remove the assertion immediately
                self.assertions = [a for a in self.assertions if not self._assertions_match(a, problematic_assertion)]
                self._write_assertions()
                return True

            # Track consecutive failures for this assertion
            if assertion_name not in consecutive_failures:
                consecutive_failures[assertion_name] = {
                    'count': 0,
                    'last_error_pattern': None,
                    'assertion_content': problematic_assertion.strip()
                }

            # Check if this is the same error pattern as the last failure
            if consecutive_failures[assertion_name]['last_error_pattern'] == error_pattern:
                consecutive_failures[assertion_name]['count'] += 1
            else:
                # Different error pattern, reset count
                consecutive_failures[assertion_name]['count'] = 1
                consecutive_failures[assertion_name]['last_error_pattern'] = error_pattern

            # Update total error count for this assertion
            if assertion_name not in error_counts:
                error_counts[assertion_name] = {
                    'total_failures': 0,
                    'error_patterns': {}
                }

            error_counts[assertion_name]['total_failures'] += 1
            if error_pattern not in error_counts[assertion_name]['error_patterns']:
                error_counts[assertion_name]['error_patterns'][error_pattern] = 0
            error_counts[assertion_name]['error_patterns'][error_pattern] += 1

            # Save updated data
            with open(error_counts_file, 'w') as f:
                json.dump(error_counts, f, indent=2)
            with open(consecutive_failures_file, 'w') as f:
                json.dump(consecutive_failures, f, indent=2)

            # Check if we should remove this assertion
            consecutive_count = consecutive_failures[assertion_name]['count']
            total_failures = error_counts[assertion_name]['total_failures']

            # Remove assertion if it has failed 3 times consecutively with the same error
            if consecutive_count >= 3:
                self._log('medium', f'\nRemoving assertion {assertion_name} after {consecutive_count} consecutive failures:')
                self._log('medium', f'Error: {self.current_error}')
                self._log('medium', f'Assertion: {problematic_assertion}')

                # Add this error pattern to blacklist
                if error_pattern not in blacklisted_errors:
                    blacklisted_errors.append(error_pattern)
                    with open(blacklist_file, 'w') as f:
                        json.dump(blacklisted_errors, f, indent=2)
                    self._log('medium', f'Added error pattern to blacklist: {error_pattern}')

                # Remove the assertion
                self.assertions = [a for a in self.assertions if not self._assertions_match(a, problematic_assertion)]
                self._write_assertions()

                # Clean up tracking data for this assertion
                if assertion_name in consecutive_failures:
                    del consecutive_failures[assertion_name]
                if assertion_name in error_counts:
                    del error_counts[assertion_name]

                # Save cleaned up data
                with open(error_counts_file, 'w') as f:
                    json.dump(error_counts, f, indent=2)
                with open(consecutive_failures_file, 'w') as f:
                    json.dump(consecutive_failures, f, indent=2)

                return True

            self._log('medium', f'Assertion {assertion_name}: {consecutive_count} consecutive failures, {total_failures} total failures')
            return False

        except Exception as e:
            self._log('medium', f'Error tracking syntax errors: {str(e)}')
            return False

    def _extract_error_pattern(self, error_message):
        """Extract a simplified error pattern for blacklisting purposes."""
        try:
            # Remove line numbers and file references to create a generic pattern
            pattern = re.sub(r'prop\.sv:\d+:', '', error_message)
            pattern = re.sub(r'line \d+:', '', pattern)
            pattern = re.sub(r'\d+', 'N', pattern)  # Replace numbers with N

            # Extract the core error message (first line usually contains the main error)
            lines = pattern.split('\n')
            if lines:
                core_error = lines[0].strip()
                # Keep only the essential error description
                if 'Error:' in core_error:
                    core_error = core_error.split('Error:')[-1].strip()
                return core_error[:100]  # Limit length

            return pattern[:100]  # Fallback
        except Exception:
            return error_message[:100]  # Fallback to original message

    def _assertions_match(self, assertion1, assertion2):
        """Check if two assertions are the same (ignoring whitespace differences)."""
        try:
            # Normalize whitespace and compare
            norm1 = ' '.join(assertion1.split())
            norm2 = ' '.join(assertion2.split())
            return norm1 == norm2
        except Exception:
            return assertion1.strip() == assertion2.strip()

    def _get_highest_file_index(self):
        # Regex to match 'syntax_it<number>'
        pattern = re.compile(r'^syntax_it(\d+)$')

        if self.verbosity >= 2:
            self._log('medium', self.assertions_file)
            self._log('medium', 'Files in SVA directory:')

        max_index = -1  # Default to -1 if no directories are found
        directory = 'ft_'+self._extract_module_name(self.rtl_module)+'/sva/'

        for item in os.listdir(directory):
            match = pattern.match(item)
            # Ensure it's a directory
            if match and os.path.isdir(os.path.join(directory, item)):
                index = int(match.group(1))  # Extract the number
                max_index = max(max_index, index)

        return max_index

    # Run the formal tool to collect coverage metrics
    def _get_coverage(self):
        try:
            directory = self._get_formal_project_dir()
            file_path = self._get_formal_log_path()

            # Remove files from previous run
            p = subprocess.run(['rm', '-rf', directory], check=True)

            # Call the formal tool
            formal_start_time = time.time()
            p = self._run_formal_tool()
            formal_end_time = time.time()
            formal_call_time = formal_end_time - formal_start_time
            self.metrics['total_formal_time'] += formal_call_time
            self._log('debug', f"Formal tool call took {formal_call_time:.2f} seconds")

            self._wait_for_formal_log(file_path)

            # Example usage: Read log content from a file
            with open(file_path, 'r') as file:
                log_content = file.read()

            # print(log_content)
            # Regular expression to match the Checker Coverage
            # clearly easier to find the string index where 'Checker Coverage' string starts, add N(characters/line) *
            # lines_between_checker_coverage_and_percentage - 1 (number sits one position to the left compared to the string)
            # and just grab the characters that contain the percentage, TRY

            checker_coverage_pattern = r'\|\s*[\w\-_]+\s*\|\s*[\d.]+%\s*\(\d+/\d+\)\s*\|\s*[\d.]+%\s*\(\d+/\d+\)\s*\|\s*([\d.]+%)'

            # Find all matches for Checker Coverage
            matches = re.findall(checker_coverage_pattern, log_content)

            # Extract Checker Coverage
            checker_coverage_values = matches[0]

            self._log('medium', f'Current Checker Coverage: {checker_coverage_values}')
            return float(checker_coverage_values.replace('%', ''))

        except subprocess.CalledProcessError as e:
            self._log('medium', f'Program execution failed: {e}')
            return e

    # Ask LLM to add assertions
    def _add_assertions(self):
        self._log('medium', '\n\n\n\nRequesting LLM for new assertions...')

        pretty = ''
        for a in self.initial_assertions:
            pretty += a
            pretty += '\n'

        module_name = self._extract_module_name(self.rtl_module)
        rules_text = rules.replace('MODULE', module_name)
        prompt = (
            f'{rules_text}'
            f'Example output format:\n{STRUCTURED_OUTPUT_EXAMPLE}\n'
            f'{self._instantiation_context_block()}'
            f'We want to increase the assertion set for this RTL module called {module_name}\n'
            'Here are the current correct assertions:\n'
            f'{pretty}\n'
            'Here is the RTL:\n'
            f'{self.rtl}\n'
            'Use unqualified checker port and parameter names only. Do NOT write '
            f'{module_name}.signal — declare DUT internals as checker inputs instead.\n'
            'Return ONLY NEW assertions using the structured --- id/property/failure --- format.\n'
            'Do not repeat assertions already in the current set.'
        )
        cap = assertion_cap()
        if cap is not None:
            remaining = cap - len(self.initial_assertions)
            if remaining <= 0:
                self._log(
                    'medium',
                    f'⚠️  Assertion cap reached ({cap}). Not requesting more.')
                return
            prompt += (
                f'\nThe set is capped at {cap} assertions; '
                f'{len(self.initial_assertions)} are already kept. '
                f'Generate at most {remaining} NEW assertions.\n'
            )

        if self.verbosity >= 3:
            print('\n\nLLM prompt:')
            print(prompt)

        response = self._get_llm_completion([
            {'role': 'system', 'content': 'You are a coding assistant. Your responses should ONLY provide syntactically correct code'},
            {'role': 'user', 'content': prompt}
        ])

        if self.verbosity >= 3:
            print('\n\nNew Assertions Response:')
            print(response)

        # Store the prompt-response pair
        self._store_llm_interaction('extension', self.syntax_iterations, prompt, response)

        new_assertions = self._extract_assertions(response)

        if not new_assertions:
            self._log('medium', 'No valid assertions found in response')
            return

        self._log('medium', f'\nFound {len(new_assertions)} assertions in response:')
        for i, assertion in enumerate(new_assertions):
            self._log('medium', f'Assertion {i+1}: {assertion}')

        # Update the assertions list with all new assertions
        self.assertions = new_assertions

        # Write new assertions to property file
        self._write_assertions()

    def _distinct_properties(self, assertions):
        """The assertions with restatements of one property removed.

        The database holds a property once however many ways it is written, so
        a set carrying two labels for one property spends two of the cap's
        slots and one proof each per run for a single property's worth of
        coverage.  Text that is not a property the parser can identify is kept:
        deciding what to do with it is not this method's job.
        """
        distinct = []
        seen = set()
        for assertion in assertions:
            signature = self.parser.property_signature(
                self._parser_input(assertion))
            if signature is not None and signature in seen:
                self._log(
                    'medium',
                    f'⚠️  Dropping {get_assertion_name(assertion)}: it restates '
                    'a property already in the set')
                continue
            if signature is not None:
                seen.add(signature)
            distinct.append(assertion)
        return distinct

    def _extend_property_set(self):
        '''
        Extended property set generation with improved duplicate checking workflow:
        1. Get new properties from LLM
        2. Pre-syntax duplicate check (filter only, no storage)
        3. Syntax correction on unique properties
        4. Final processing and storage
        '''
        self.llm_stage = 'set_extension'

        self.initial_syntax_iterations += self._get_highest_file_index()
        self.syntax_iterations = (self.initial_syntax_iterations + 1)
        if self.verbosity >= 2:
            self._log('medium', 'Syntax iterations before extension: ' +
                  str(self.syntax_iterations))
        it = 0

        # Start from one assertion per distinct property, under the global cap
        distinct = self._distinct_properties(self.assertions)
        cap = assertion_cap()
        self.initial_assertions = distinct if cap is None else distinct[:cap]
        if cap is not None and len(distinct) > cap:
            self._log('medium',
                f'⚠️  Assertion cap: keeping {cap} of {len(distinct)} initial assertions')

        while True:
            if cap is not None and len(self.initial_assertions) >= cap:
                self._log('medium',
                    f'⚠️  Assertion cap reached ({cap}). Stopping extension.')
                break
            self._log('medium', '\n\nInitial correct assertions for extension iteration ' + str(it) + ':')
            for a in self.initial_assertions:
                self._log('medium', a)

            # Step 1: Get new assertions from LLM
            self._add_assertions()
            self._update_metrics('extension_iterations')

            # Check if LLM returned the same assertions as initial set
            if set(self.assertions) == set(self.initial_assertions):
                self._log('medium', '\n⚠️  WARNING: LLM returned the same assertions as the initial set!')
                self._log('medium', 'This suggests the LLM is not generating new properties.')
                self._log('medium', 'Terminating extension to avoid infinite loop.')
                break

            # Step 2: Pre-syntax duplicate check (NO STORAGE)
            # This filters out duplicates before we spend resources on syntax correction
            unique_assertions, pre_syntax_duplicates = self._check_pre_syntax_duplicates(self.assertions)

            # Display pre-syntax duplicate results
            if len(pre_syntax_duplicates) > 0:
                self._log('medium', '\n=== FILTERED DUPLICATE ASSERTIONS (Pre-Syntax Check) ===')
                for i, assertion in enumerate(pre_syntax_duplicates):
                    self._log('medium', f'Duplicate {i+1}: {assertion.strip()}')
                self._log('medium', '=' * 70)

            if len(unique_assertions) > 0:
                slots_left = (
                    None if cap is None
                    else cap - len(self.initial_assertions))
                if slots_left is not None and slots_left <= 0:
                    self._log(
                        'medium',
                        f'⚠️  Assertion cap reached ({cap}). Stopping extension.')
                    break
                if slots_left is not None and len(unique_assertions) > slots_left:
                    self._log(
                        'medium',
                        f'⚠️  Assertion cap: keeping {slots_left} of '
                        f'{len(unique_assertions)} unique assertions '
                        'before syntax correction')
                    unique_assertions = unique_assertions[:slots_left]

                self._log('medium', '\n=== UNIQUE ASSERTIONS FOR SYNTAX CORRECTION ===')
                for i, assertion in enumerate(unique_assertions):
                    self._log('medium', f'Unique Assertion {i+1}: {assertion.strip()}')
                self._log('medium', '=' * 70)

                # Update self.assertions with only the unique assertions for syntax correction
                self.assertions = unique_assertions

                # Step 3: Promote DUT internals to checker ports + bind wiring
                self._log('medium', '\nPreprocessing: Promoting referenced DUT internals to checker ports...')
                corrected_assertions, fix_count, fixed_signals = self._promote_internal_checker_ports(self.assertions)

                if fix_count > 0:
                    self._log('medium', f'Promoted {fix_count} internal signal(s) to checker ports: {sorted(fixed_signals)}')
                    self._log('medium', '\n=== INTERNAL PORT BINDS ===')
                    for i, signal in enumerate(sorted(fixed_signals)):
                        self._log('medium', f'Port {i+1}: {signal}')
                    self._log('medium', '=' * 70)

                    # Update the assertions with the corrected versions
                    self.assertions = corrected_assertions
                    self._update_metrics(
                        'internal_port_binds',
                        self.metrics.get('internal_port_binds', 0) + fix_count,
                        increment=False,
                    )
                else:
                    self._log('medium', 'No internal port promotions needed')

                # CRITICAL FIX: Write the unique assertions to the property file before syntax correction
                # This ensures that JasperGold only processes the unique assertions, not duplicates
                self._log('medium', '\nWriting unique assertions to property file before syntax correction...')
                self._log('medium', f'Writing {len(self.assertions)} unique assertions to property file')
                self._write_assertions()

                # Step 4: Syntax correction
                self._correct_syntax()

                self._log('medium', '\n\nBack to extension loop...')

                # Step 5: Final processing and storage, up to what the cap
                # leaves room for, so nothing is stored that cannot be kept
                slots_left = (
                    None if cap is None
                    else cap - len(self.initial_assertions))
                new, new_list, post_syntax_duplicates = self._process_batch(
                    limit=slots_left)

                # Display post-syntax results
                if len(post_syntax_duplicates) > 0:
                    self._log('medium', '\n=== FILTERED DUPLICATE ASSERTIONS (Post-Syntax Check) ===')
                    for i, assertion in enumerate(post_syntax_duplicates):
                        self._log('medium', f'Duplicate {i+1}: {assertion.strip()}')
                    self._log('medium', '=' * 70)

                if new > 0:
                    self._log('medium', '\n=== NEW ASSERTIONS ADDED (After Syntax Correction) ===')
                    for i, assertion in enumerate(new_list):
                        self._log('medium', f'Added Assertion {i+1}: {assertion.strip()}')
                    self._log('medium', '=' * 70)

                    self._log('medium', f'Found {new} syntactically correct new properties')
                    self.initial_assertions.extend(new_list)
                else:
                    self._log('medium', 'No new properties found after syntax correction')

                # Diagnostic information
                self._log('medium', f'\nDuplicate filtering summary:')
                self._log('medium', f'  Pre-syntax duplicates filtered: {len(pre_syntax_duplicates)}')
                self._log('medium', f'  Post-syntax duplicates filtered: {len(post_syntax_duplicates)}')
                self._log('medium', f'  Properties successfully added: {new}')

            else:
                # No unique assertions found after pre-syntax duplicate check
                self._log('medium', '\nNo unique assertions found after pre-syntax duplicate check. Skipping syntax correction.')
                new = 0

            self._log('medium', f'{new} NEW PROPERTIES added to the set')

            if new == 0:
                break

            if cap is not None and len(self.initial_assertions) >= cap:
                self._log(
                    'medium',
                    f'⚠️  Assertion cap reached ({cap}). Stopping extension.')
                break

            self._log('medium', '--------------------------------------- FINISHED EXTENSION LOOP ITERATION ---------------------------------------')
            it += 1

        # Update self.assertions with all accumulated assertions before exiting
        self._log('medium', '\nUpdating property file with complete set of assertions...')
        self._log('medium', f'Before update: self.assertions has {len(self.assertions)} assertions')
        self._log('medium', f'Before update: self.initial_assertions has {len(self.initial_assertions)} assertions')
        self.assertions = self.initial_assertions
        self._write_assertions()
        self._log('medium', f'Property file now contains {len(self.assertions)} total assertions')
        self._log('medium', f'After update: self.assertions has {len(self.assertions)} assertions')

    def _increase_coverage(self):
        self._log('medium', 'Improving assertion coverage...')

        self.initial_syntax_iterations += self._get_highest_file_index()
        self.syntax_iterations = (self.initial_syntax_iterations + 1)
        print(self.syntax_iterations)
        it = 0

        while it < 1:
            # Get current coverage
            self.coverage.append(self._get_coverage())

            print(self.coverage[0])

            self.initial_assertions = self._retrieve_assertions(
                self.assertions)

            print('Initial correct assertions for coverage iteration ' + str(it) + ':')
            print(self.initial_assertions)

            # Generate and store new assertions
            self._correct_syntax(1)

            print('back to coverage loop...')

            new_assertions = '\n'.join(
                line for line in self.assertions.splitlines() if not ('endmodule' in line))
            new_assertions += self.initial_assertions
            new_assertions += '\nendmodule\n'
            self.assertions = new_assertions
            self._write_assertions()

            print(new_assertions)

            # Generate and store new assertions
            self.remove_duplicates()

            # copy hl_mul_unit.sv/.v into e.g. hl_mul_unit_correct and then remove
            # correct ones from hl_mul_unit and add new ones (idem to syntax process)
            # once the new ones are in hl_mul_unit a syntax correction process can start using
            # the already implemented functionality
            # will need parametrization in syntax correction functions to distinguish the initial files of syntax correction
            # and the files of the coverage process, or maybe just have one terminology ? e.g hl_mul_unit_gen_i ?
            # makes sensse sematically since copies are only kept when assertions were incorrect so we do not really
            # care if we are correcting initial batch syntax or newer assertion syntax

            # Correct new assertions if necessary (~~~ new_assertions ?)

            # Group previous assertions with newer (maybe corrected) ones (~~~assertions += new_assertions ? stable state)

            # Implement heuristic to decide when to finish improvements (detect we have reached a plateau, or just stop when a
            # generation worsens coverage)
            if len(self.coverage) > 5:
                break

            it += 1

    # -------------------------------------------------------------------------
    # Environment assumption generation and screening
    # -------------------------------------------------------------------------

    def _run_formal_and_qualify(self):
        """Run the formal tool from scratch and return the qualified result.

        The project directory is removed first so a stale log can never be read
        as the result of the current configuration — which would silently
        invalidate every assumption screen.

        Returns:
            proof_status.FormalRunResult
        """
        directory = self._get_formal_project_dir()
        subprocess.run(['rm', '-rf', directory], check=False)

        start = time.time()
        self._run_formal_tool()
        self.metrics['total_formal_time'] += time.time() - start

        log_path = self._get_formal_log_path()
        while not os.path.isfile(log_path):
            time.sleep(3)
            self._log('medium', 'Waiting for formal tool log to be generated...')

        return self._qualify_formal_run()

    def _write_assumption_artifact(self):
        """Write the accepted assumptions as a standalone, named artifact.

        The snapshot must state the environment it was proved under, so the
        constraints are kept as their own file next to the property file rather
        than buried inside it.

        Returns:
            str or None: Path of the artifact, or None when none were accepted.
        """
        if not self.active_assumptions:
            return None

        module_name = self._extract_module_name(self.rtl_module)
        path = os.path.join(
            self.assertions_dir, f'{module_name}{assumption_gen.ASSUME_FILE_SUFFIX}')
        try:
            with open(path, 'w') as handle:
                handle.write(assumption_gen.render_assumption_file(
                    module_name, self.active_assumptions))
            self._log('medium', f'Wrote assumption artifact {path}')
            return path
        except OSError as error:
            self._log('medium', f'Could not write assumption artifact: {error}')
            return None

    def _propose_assumptions(self, baseline, failing):
        """Ask the LLM for environment constraints that explain the failures.

        Args:
            baseline (FormalRunResult): Result without the candidate assumptions.
            failing (list): ``(name, assertion_text)`` pairs to explain.

        Returns:
            list[assumption_gen.AssumptionCandidate]
        """
        self.llm_stage = 'assumption_generation'
        module_name = self._extract_module_name(self.rtl_module)

        tables = {}
        previous_result = self.formal_result
        self.formal_result = baseline
        try:
            if self._include_cex_in_prompt():
                for name, text in failing:
                    if name not in baseline.records:
                        continue
                    table = self._load_repair_cycle_table(
                        name, text, baseline.records[name])
                    if table is not None:
                        tables[name] = table
        finally:
            self.formal_result = previous_result
        if self._include_cex_in_prompt():
            counterexample_text = cex_context.format_assumption_cex(
                baseline.records,
                [name for name, _ in failing],
                tables,
            )
        else:
            counterexample_text = (
                'CEX traces omitted by the no_cex_in_prompt ablation.')

        prompt = assumption_gen.build_assumption_prompt(
            module_name=module_name,
            rtl_text=self.rtl,
            failing_properties=failing,
            counterexample_text=counterexample_text,
            existing_assumptions=self.active_assumptions,
        )

        system_msg = (
            'You are an expert in SystemVerilog Assertions and formal '
            'verification environments. You propose the weakest environment '
            'assumption that removes an illegal input scenario. When the '
            'counterexample is legal input behaviour you must still answer, '
            'using a decision: none block. Never reply with an empty message.'
        )
        messages = [
            {'role': 'system', 'content': system_msg},
            {'role': 'user', 'content': prompt},
        ]
        response = ''
        candidates = []
        for attempt in range(1, ASSUMPTION_PROPOSAL_ATTEMPTS + 1):
            response = self._get_llm_completion(messages) or ''
            self._store_llm_interaction('assumption', attempt, prompt, response)
            candidates = assumption_gen.parse_assumption_response(response)
            if candidates:
                break
            if assumption_gen.assumption_decision_is_none(response):
                self._log(
                    'medium',
                    'LLM explicitly proposed no environment assumptions')
                break
            self._log(
                'medium',
                f'Assumption generation reply was empty or unusable '
                f'(attempt {attempt}/{ASSUMPTION_PROPOSAL_ATTEMPTS})')
            if attempt >= ASSUMPTION_PROPOSAL_ATTEMPTS:
                break
            messages = [
                {'role': 'system', 'content': system_msg},
                {'role': 'user', 'content': prompt},
                {'role': 'assistant', 'content': response or '(empty)'},
                {'role': 'user',
                 'content': assumption_gen.ASSUMPTION_RETRY_INSTRUCTION},
            ]

        self._log('medium',
            f'LLM proposed {len(candidates)} assumption candidate(s)')
        return candidates

    def _screen_assumption(self, candidate, baseline):
        """Apply every screen to one candidate assumption.

        The candidate is temporarily put in force, the design re-proved, and the
        result compared with ``baseline``.  It is kept only if it unblocks a
        targeted property without making established behaviour vacuous or
        masking defects.

        Args:
            candidate (AssumptionCandidate): Proposed constraint.
            baseline (FormalRunResult): Result before the constraint.

        Returns:
            tuple: ``(ScreeningResult, FormalRunResult or None)``
        """
        screening = assumption_gen.ScreeningResult()
        previous = list(self.active_assumptions)
        # The candidate has to be in force for the screens to mean anything. It
        # stays in force if it is accepted, so the caller must not add it again.
        self.active_assumptions = previous + [candidate]
        self._write_assertions()

        try:
            if not self._run_svalint_check():
                screening.lint_clean = assumption_gen.ScreenVerdict.FAIL
                screening.detail = 'SVALint rejected the assumption'
                return screening, None
            screening.lint_clean = assumption_gen.ScreenVerdict.PASS

            candidate_result = self._run_formal_and_qualify()

            verdict, detail = assumption_gen.screen_consistency(candidate_result)
            screening.consistency = verdict
            screening.detail = detail
            if verdict is not assumption_gen.ScreenVerdict.PASS:
                return screening, candidate_result

            verdict, newly_vacuous = assumption_gen.screen_reachability_preservation(
                baseline, candidate_result)
            screening.reachability_preserved = verdict
            screening.newly_vacuous = newly_vacuous
            if verdict is assumption_gen.ScreenVerdict.FAIL:
                return screening, candidate_result

            verdict, unblocked = assumption_gen.screen_unblocks_targets(
                candidate, baseline, candidate_result)
            screening.unblocks_target = verdict
            screening.unblocked = unblocked

            if self.assumption_mutation_screen:
                # The screen needs a mutant population and two full evaluations,
                # which would multiply the cost of every candidate. It is run
                # once on the finished snapshot instead:
                #     python3 evaluate.py assumptions --rtl <rtl>
                screening.mutation_sensitivity_preserved = \
                    assumption_gen.ScreenVerdict.SKIPPED
                screening.detail += (
                    ' | mutation-sensitivity screening deferred to '
                    '`evaluate.py assumptions`, which measures this snapshot '
                    'with and without the assumption block against one mutant '
                    'population')

            return screening, candidate_result

        finally:
            if not screening.accepted:
                self.active_assumptions = previous
                self._write_assertions()

    def _generate_and_screen_assumptions(self, baseline):
        """Diagnose failing properties as under-constrained environments.

        A counterexample on a plausible property often means the input space is
        underconstrained rather than that the property is wrong.  This stage
        proposes named constraints for those properties and keeps only the ones
        that survive screening, so the snapshot never gains a constraint that
        hides defects.

        Args:
            baseline (FormalRunResult): Qualified result without new assumptions.

        Returns:
            list[str]: Names of properties reclassified as
            ``failing_missing_assumption``.
        """
        if not self.enable_assumption_generation:
            return []

        failing_names = baseline.names_with(Qualification.FAILING_PROPERTY_MISMATCH)
        if not failing_names:
            self._log('medium',
                'No property-mismatch failures; skipping assumption generation.')
            return []

        text_by_name = {}
        for assertion in self.assertions:
            name = coi_analysis.assertion_name(assertion)
            if name:
                text_by_name[name] = assertion
        failing = [(name, text_by_name.get(name, '')) for name in failing_names]

        self._print_log_separator('ENVIRONMENT ASSUMPTION GENERATION')
        self._log('medium',
            f'{len(failing)} property/properties fail against the RTL. Checking '
            f'whether the environment is underconstrained before repairing them.')

        try:
            candidates = self._propose_assumptions(baseline, failing)
        except Exception as error:
            self._log('medium', f'Assumption generation failed: {error}')
            return []

        saved_assertions = list(self.assertions)
        reclassified = []

        for candidate in candidates:
            self._log('medium',
                f'\nScreening {candidate.name}: {candidate.expression}')
            screening, _ = self._screen_assumption(candidate, baseline)
            self.assumption_candidates.append(candidate)
            self.assumption_screening[candidate.name] = screening

            if screening.accepted:
                # Already left in force by _screen_assumption.
                self._log('medium',
                    f'  accepted — unblocks {", ".join(screening.unblocked)}')
                for name in screening.unblocked:
                    record = baseline.records.get(name)
                    if record is not None:
                        record.missing_assumptions = sorted(
                            set(record.missing_assumptions) | {candidate.name})
                        reclassified.append(name)
            else:
                self._log('medium', f'  rejected — {screening.rejection_reason()}')

        self.assertions = saved_assertions
        self._write_assertions()

        self._log('medium', '\n' + assumption_gen.format_screening_report(
            self.assumption_candidates, self.assumption_screening))
        self._dump_json(
            [
                {**c.to_dict(),
                 'screening': self.assumption_screening[c.name].to_dict()}
                for c in self.assumption_candidates
            ],
            'assumptions.json')
        self._write_assumption_artifact()

        return sorted(set(reclassified))

    # -------------------------------------------------------------------------
    # Snapshot reporting: qualification, diversity, cost, provenance
    # -------------------------------------------------------------------------

    def _elaboration_settings(self):
        """Recover the settings the formal tool actually elaborated under.

        Read back from the generated Tcl rather than from the agent's own
        configuration, so the manifest records what the tool was told, not what
        the tool was meant to be told.

        Returns:
            provenance.ElaborationSettings
        """
        module_name = self._extract_module_name(self.rtl_module)
        tcl_name = 'FPV_vcf.tcl' if self.formal_tool == 'vcformal' else 'FPV.tcl'
        tcl_path = os.path.join(f'ft_{module_name}', tcl_name)

        settings = provenance.ElaborationSettings(
            top=module_name,
            formal_tool=self.formal_tool,
            module_type=self.module_type,
            tcl_path=tcl_path,
        )
        if self.formal_result is not None:
            settings.formal_tool_version = self.formal_result.summary.tool_version

        if os.path.isfile(tcl_path):
            with open(tcl_path, 'r', errors='replace') as handle:
                tcl_text = handle.read()
            settings.tcl_sha256 = provenance.sha256_text(tcl_text)

            clock = re.search(r'^\s*create_clock\s+(\S+)', tcl_text, re.MULTILINE)
            reset = re.search(
                r'^\s*create_reset\s+(\S+)\s+-sense\s+(\S+)', tcl_text, re.MULTILINE)
            coverage = re.search(r'^\s*elaborate\s+-cov\s+(\S+)', tcl_text, re.MULTILINE)
            if clock:
                settings.clock = clock.group(1)
            if reset:
                settings.reset, settings.reset_sense = reset.group(1), reset.group(2)
            if coverage:
                settings.coverage_mode = coverage.group(1)
            settings.vacuity_checking = 'fml_vacuity_on true' in tcl_text

        file_list = os.path.join(f'ft_{module_name}', 'files_vcf.vc')
        if os.path.isfile(file_list):
            settings.file_list_sha256 = provenance.sha256_file(file_list)

        return settings

    def _generation_settings(self):
        """Record the model, decoding parameters, and prompt material used."""
        return provenance.GenerationSettings(
            model=self.llm,
            temperature=self.model_config.get('temperature'),
            top_p=0.7 if self.model_config.get('supports_top_p') else None,
            max_completion_tokens=self.model_config.get('max_completion_tokens'),
            prompting_strategy=self.prompting_strategy,
            rules_sha256=provenance.sha256_text(rules),
            template_version=provenance.sha256_text(STRUCTURED_OUTPUT_EXAMPLE)[:12],
            svalint_root=str(resolve_svalint_root()),
            svapshot_commit=provenance._git(['rev-parse', 'HEAD'], '.') or '',
        )

    def _analyse_cones(self):
        """Compute cone-of-influence diversity for the current property set.

        Returns:
            tuple: ``(DiversityMetrics, {name: PropertyCone})``, or
            ``(None, {})`` when the RTL could not be parsed.
        """
        try:
            module_name = self._extract_module_name(self.rtl_module)
            graph = coi_analysis.build_signal_graph(
                self.rtl, module_name, coi_analysis.language_of(self.rtl_module))
            cones = coi_analysis.compute_property_cones(self.assertions, graph)
            return coi_analysis.compute_diversity(cones, graph), {c.name: c for c in cones}
        except Exception as error:
            self._log('medium', f'Cone-of-influence analysis failed: {error}')
            return None, {}

    def _build_snapshot_manifest(self, diversity_metrics, cones, snapshot_metrics):
        """Assemble the provenance manifest for this run."""
        module_name = self._extract_module_name(self.rtl_module)
        manifest = provenance.SnapshotManifest(
            design=provenance.DesignRevision.capture(
                module=module_name, rtl_paths=[self.rtl_source]),
            elaboration=self._elaboration_settings(),
            generation=self._generation_settings(),
            contract_property_file=os.path.join(self.assertions_dir, self.assertions_file),
        )
        manifest.assumptions = assumption_gen.to_records(
            self.assumption_candidates, self.assumption_screening)
        if self.active_assumptions:
            manifest.assumption_file = os.path.join(
                self.assertions_dir,
                f'{module_name}{assumption_gen.ASSUME_FILE_SUFFIX}')

        attempts_by_property = {
            outcome.property_name: outcome.attempts
            for outcome in self.stopping_policy.outcomes
        }
        assertion_text_by_name = {}
        for assertion in self.assertions:
            name = coi_analysis.assertion_name(assertion)
            if name:
                assertion_text_by_name[name] = assertion

        records = self.formal_result.records if self.formal_result else {}
        for name, record in sorted(records.items()):
            cone = cones.get(name)
            manifest.properties.append(provenance.PropertyEntry(
                name=name,
                expression=coi_analysis.property_expression(
                    assertion_text_by_name.get(name, '')).strip(),
                assertion_text=assertion_text_by_name.get(name, ''),
                qualification=record.qualification.value,
                proof_status=record.proof_status.value,
                vacuity_status=record.vacuity_status.value,
                engine=record.engine,
                proof_time=record.proof_time,
                cone_size=cone.size if cone else record.coi_size,
                cone=sorted(cone.cone) if cone else [],
                referenced_signals=sorted(cone.referenced_signals) if cone else [],
                assumption_dependence=list(record.assumption_dependence),
                repair_attempts=attempts_by_property.get(name, 0),
                note=record.note,
            ))

        manifest.metrics = snapshot_metrics.to_dict()
        manifest.cost = self.cost_ledger.to_dict()
        manifest.diversity = diversity_metrics.to_dict() if diversity_metrics else {}
        manifest.finalise_id()
        return manifest

    def _finalize_reports(self):
        """Emit the qualification, diversity, cost, and provenance artifacts.

        Called once at the end of a run.  Failures here are logged and
        swallowed: reporting must never destroy a completed snapshot.
        """
        try:
            self._print_log_separator('SNAPSHOT QUALIFICATION AND METRICS')

            if self.formal_result is None:
                self._log('medium',
                    'No formal result available; skipping snapshot reports.')
                return

            self._log('medium',
                proof_status.format_qualification_table(self.formal_result))

            counts = self.formal_result.counts_by_qualification()
            for key, value in counts.items():
                self._update_metrics(key, value, increment=False)
            self._update_metrics(
                'proven_assumption_dependent',
                self.formal_result.assumption_dependence_audit()[
                    'proven_assumption_dependent'],
                increment=False)

            diversity_metrics, cones = self._analyse_cones()
            if diversity_metrics is not None:
                self._log('medium', '\n' + coi_analysis.format_diversity_summary(
                    diversity_metrics))

            self._log('medium', '\n' + self.cost_ledger.format_summary())

            timing = {
                'total_seconds': self.timing['total_end'] - self.timing['total_start']
                if self.timing['total_end'] else 0.0,
                'formal_seconds': self.metrics['total_formal_time'],
                'preprocessing_seconds': (
                    self.timing['preprocessing_end'] - self.timing['preprocessing_start']),
                'syntax_seconds': (
                    self.timing['syntax_correction_end']
                    - self.timing['syntax_correction_start']),
                'extension_seconds': (
                    self.timing['set_extension_end'] - self.timing['set_extension_start']),
                'semantic_seconds': (
                    self.timing['semantic_correction_end']
                    - self.timing['semantic_correction_start']),
                'formal_runs': self.metrics['formal_runs'],
            }

            snapshot_metrics = metrics_from_run(
                module=self._extract_module_name(self.rtl_module),
                formal_result=self.formal_result,
                diversity_metrics=diversity_metrics,
                cost_ledger=self.cost_ledger,
                timing=timing,
                model=self.llm,
                formal_tool=self.formal_tool,
                stopping_policy=self.stopping_policy,
            )
            self.snapshot_metrics = snapshot_metrics
            self._log('medium', '\n' + snapshot_metrics.format_report())

            self._dump_json(self.formal_result.to_dict(), 'qualification.json')
            self._dump_json(self.cost_ledger.to_dict(), 'llm_cost.json')
            if diversity_metrics is not None:
                self._dump_json({
                    'metrics': diversity_metrics.to_dict(),
                    'cones': {name: cone.to_dict() for name, cone in cones.items()},
                }, 'coi_diversity.json')
            self._dump_json(snapshot_metrics.to_dict(), 'snapshot_metrics.json')

            manifest = self._build_snapshot_manifest(
                diversity_metrics, cones, snapshot_metrics)
            manifest_path = os.path.join(self.assertions_dir, 'snapshot_manifest.json')
            manifest.save(manifest_path)
            snapshot_path = os.path.join(self.assertions_dir, 'snapshot.json')
            write_snapshot(
                build_snapshot(
                    manifest,
                    self.formal_result,
                    snapshot_metrics,
                    rtl_source=self.rtl_source,
                    rtl_module=self.rtl_module,
                ),
                snapshot_path,
            )
            self._log('medium', '\n' + manifest.format_summary())
            self._log('medium', f'\nSnapshot manifest written to {manifest_path}')
            self._log('medium', f'Snapshot UI data written to {snapshot_path}')

        except Exception as error:
            self._log('medium', f'Failed to build snapshot reports: {error}')
            import traceback
            self._log('debug', traceback.format_exc())

    def _report(self):
        print('\n=============================== FINAL REPORT ===============================\n')

        if self.run_option == 'SYNTAX' or self.run_option == 'FULL':
            print('Took ' + str(self.syntax_iterations) +
                  ' iterations to fix syntax\n')

            print('Generated the following files while correcting syntax in dir ' +
                  self.assertions_dir + ':')
            for i in range(0, self.syntax_iterations):
                # Generate iteration filename with proper extension
                base_name = os.path.splitext(self.assertions_file)[0]
                extension = os.path.splitext(self.assertions_file)[1]
                print(f'{base_name}{i}{extension}')

            print('\nTargetted the following errors:')

            # has to be coverage_its - syntax_its or have 'initial syntax its' saved because if not we will try to output more errors than we actually found in a coverage-only execution
            for i in range(0, self.syntax_iterations):
                print('Iteration ' + str(i) + ': ' + self.errors[i])
            print('\n--------------------------------------------------------------\n')

        if self.run_option == 'COVERAGE' or self.run_option == 'FULL':
            metrics = getattr(self, 'snapshot_metrics', None)
            if metrics is not None:
                qual = metrics.qualification
                print(f'Property file contains {self.assertion_count} assertions, '
                      f'of which {qual.proved_non_vacuous} are proved non-vacuously '
                      f'({qual.snapshot_yield:.1%} snapshot yield)')
                print('Full metrics, including both checker-coverage definitions, '
                      'are in snapshot_metrics.json')
            else:
                print(f'Property file contains {self.assertion_count} assertions')

            # The scraped per-checker figure from the tool log is kept for
            # continuity with earlier runs, but it is not the metric of record:
            # it says nothing about vacuity, so it is printed as a trend only.
            if self.coverage:
                print(f'\nLegacy tool checker coverage over '
                      f'{len(self.coverage)} extension iterations: '
                      f'{self.coverage[0]} -> {self.coverage[-1]} '
                      f'(unqualified; see snapshot_metrics.json for the '
                      f'vacuity-aware figures)')

    # Print attributes of all the properties in the set
    def _print_property_db(self):
        # Check if parser dictionaries are empty but prop_result has data
        parser_dicts_empty = (len(self.parser.sva_string) == 0 and
                             len(self.parser.delay_d) == 0 and
                             len(self.parser.postcondition_d) == 0 and
                             len(self.parser.clauses_d) == 0)

        prop_result_has_data = len(self.parser.prop_result) > 0

        if parser_dicts_empty and prop_result_has_data:
            self._log('medium', '\n⚠️  WARNING: Parser dictionaries are empty but prop_result has data!')
            self._log('medium', 'This suggests that properties were processed by the formal tool but not stored in parser dictionaries.')
            self._log('medium', 'This can happen when:')
            self._log('medium', '  1. All properties were detected as duplicates during processing')
            self._log('medium', '  2. Properties failed to parse correctly')
            self._log('medium', '  3. There was an issue with the parser processing workflow')
            self._log('medium', '\nDiagnostic Information:')
            self._log('medium', f'  Current assertions count: {len(self.assertions) if hasattr(self, "assertions") else "N/A"}')
            self._log('medium', f'  Assertions list count: {len(self.assertions_list) if hasattr(self, "assertions_list") else "N/A"}')
            self._log('medium', f'  Parser unprocessable properties: {len(self.parser.unprocessable_props_dict)}')
            self._log('medium', '=' * 80)

        self._log('medium', '\n\nName - SVA String Pairs')
        if len(self.parser.sva_string) == 0:
            self._log('medium', '(No SVA strings stored - see diagnostic information above)')
        else:
            for k, v in self.parser.sva_string.items():
                self._log('medium', 'Name: ' + str(k) + ' || Clauses: ' + str(v))

        self._log('medium', '\n\nName - Delay Pairs')
        if len(self.parser.delay_d) == 0:
            self._log('medium', '(No delays stored - see diagnostic information above)')
        else:
            for k, v in self.parser.delay_d.items():
                self._log('medium', 'Name: ' + str(k) + ' || Delay: ' + str(v))

        self._log('medium', '\n\nName - Postcondition Pairs')
        if len(self.parser.postcondition_d) == 0:
            self._log('medium', '(No postconditions stored - see diagnostic information above)')
        else:
            for k, v in self.parser.postcondition_d.items():
                self._log('medium', 'Name: ' + str(k) + ' || Clauses: ' + str(v))

        self._log('medium', '\n\nName - Clause Pairs')
        if len(self.parser.clauses_d) == 0:
            self._log('medium', '(No clauses stored - see diagnostic information above)')
        else:
            for k, v in self.parser.clauses_d.items():
                post = self.parser.postcondition_d.get(k)
                mark = '* ' if _cnf_imperfect(v, post) else ''
                self._log('medium', 'Name: ' + mark + str(k) + '\n' + _format_cnf_clauses(v) + '\n')

        self._log('medium', '\n\nName - Proof Result Pairs')
        if len(self.parser.prop_result) == 0:
            self._log('medium', '(No proof results available)')
        else:
            for k, v in self.parser.prop_result.items():
                self._log('medium', 'Name: ' + str(k) + ' || Proof Result: ' + str(v))

    # Promote referenced DUT internals to checker ports and bind connections
    def _promote_internal_checker_ports(self, assertions, hierarchical_overrides=None):
        """Rewrite internals for density-safe checker ports + bind wiring.

        Strips ``MODULE.`` / ``MODULE.MODULE.`` prefixes, keeps unqualified
        names in property expressions, declares referenced DUT internals as
        checker inputs, and updates ``*_bind.svh`` (``.*`` or explicit
        hierarchical maps). Parameters are unqualified in place.
        """
        self._log('debug', '\nStarting promote_internal_checker_ports...')
        module_name = self._extract_module_name(self.rtl_module)
        graph = coi_analysis.build_signal_graph(
            self.rtl, module_name, coi_analysis.language_of(self.rtl_module))

        prop_text = getattr(self, 'checker_code', '') or ''
        bind_path = os.path.join(
            self.assertions_dir, f'{module_name}_bind.svh')
        bind_text = ''
        if os.path.isfile(bind_path):
            with open(bind_path, encoding='utf-8', errors='replace') as handle:
                bind_text = handle.read()

        overrides = dict(getattr(self, '_hierarchical_bind_overrides', {}) or {})
        if hierarchical_overrides:
            overrides.update(hierarchical_overrides)

        result, new_prop, new_bind = bind_internals.apply_port_promotion(
            assertions,
            module_name=module_name,
            graph=graph,
            rtl_text=self.rtl,
            prop_text=prop_text,
            bind_text=bind_text,
            hierarchical_overrides=overrides,
        )

        if result.needed_ports and new_prop != prop_text:
            self.checker_code = new_prop
            if getattr(self, 'module_type', 'sequential') == 'combinational':
                self.checker_code = rtl_clocking.ensure_combinational_clocking(
                    self.checker_code)
            self._log(
                'debug',
                f'Updated checker port list with {len(result.needed_ports)} internal(s)',
            )

        if result.needed_ports and bind_text and new_bind != bind_text:
            with open(bind_path, 'w', encoding='utf-8') as handle:
                handle.write(new_bind)
            self._log('debug', f'Updated bind file: {bind_path}')

        if result.unresolved_hierarchical:
            self._pending_density_probe_signals = set(
                getattr(self, '_pending_density_probe_signals', set())
            ) | set(result.unresolved_hierarchical)
        elif result.needed_ports:
            # Clear stale probe requests when static rewrite resolved ports.
            hierarchical = {
                name for name, binding in result.needed_ports.items()
                if binding.hierarchical_path
            }
            pending = set(getattr(self, '_pending_density_probe_signals', set()))
            self._pending_density_probe_signals = pending - hierarchical

        for name, binding in result.needed_ports.items():
            if binding.hierarchical_path:
                overrides[name] = binding.hierarchical_path
        self._hierarchical_bind_overrides = overrides

        fix_count = len(result.needed_ports)
        self._log(
            'debug',
            f'Promoted {fix_count} port(s); stripped '
            f'{result.rewrite_count} MODULE. occurrence(s) across '
            f'{len(result.rewritten_names)} name(s)',
        )
        return result.assertions, fix_count, set(result.needed_ports)

    # Backward-compatible alias used by older tests / call sites.
    def _fix_missing_module_prefixes(self, assertions):
        return self._promote_internal_checker_ports(assertions)

    def _maybe_run_density_bind_probe(self):
        """Optional elaborate+density probe for remaining hierarchical binds.

        Runs only for VC Formal when the static rewrite left unresolved
        hierarchical refs (or when coverage is explicitly enabled) and never
        performs a full ``check_fv`` cycle.
        """
        if self.formal_tool != 'vcformal':
            return False
        pending = set(getattr(self, '_pending_density_probe_signals', set()) or ())
        coverage_on = os.environ.get('SVAPSHOT_COVERAGE', '0') not in ('0', 'false', 'False')
        if not pending and not coverage_on:
            self._log('debug', 'Density bind probe skipped (nothing unresolved)')
            return False

        module_name = self._extract_module_name(self.rtl_module)
        ft_dir = f'ft_{module_name}'
        probe_tcl = os.path.join(ft_dir, 'FPV_density_probe.tcl')
        files_vcf = os.path.join(ft_dir, 'files_vcf.vc')
        if not os.path.isfile(files_vcf):
            self._log('medium', 'Density bind probe skipped (files_vcf.vc missing)')
            return False

        clk_sig = rst_sig = None
        rst_active_low = True
        jg_tcl = os.path.join(ft_dir, 'FPV.tcl')
        if os.path.isfile(jg_tcl):
            with open(jg_tcl, encoding='utf-8', errors='replace') as handle:
                for raw in handle:
                    line = raw.strip()
                    match = re.match(r'^\s*clock\s+(?!-none)(\w+)', line)
                    if match:
                        clk_sig = match.group(1)
                    match = re.match(
                        r'^\s*reset\s+-expression\s+\((!?)(\w+)\)', line)
                    if match:
                        rst_active_low = bool(match.group(1))
                        rst_sig = match.group(2)

        tcl_text = bind_internals.generate_density_probe_tcl(
            module_name,
            module_type=self.module_type,
            clk_sig=clk_sig,
            rst_sig=rst_sig,
            rst_active_low=rst_active_low,
        )
        with open(probe_tcl, 'w', encoding='utf-8') as handle:
            handle.write(tcl_text)

        self._log('medium', f'Running density-only VC Formal probe via {probe_tcl}...')
        log_path = os.path.join(f'vcf_projs/{module_name}', 'density_probe.log')
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        try:
            with open(log_path, 'w', encoding='utf-8') as log_handle:
                subprocess.run(
                    ['vcf', '-f', probe_tcl, '-batch'],
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                    **self.DETACHED_FROM_TERMINAL,
                    timeout=self._formal_run_timeout_s(),
                    check=False,
                )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._log('medium', f'Density bind probe not run: {exc}')
            return False

        report_path = os.path.join(
            f'vcf_projs/{module_name}', 'reports', 'density_probe.rpt')
        blob = ''
        for path in (log_path, report_path):
            if os.path.isfile(path):
                with open(path, encoding='utf-8', errors='replace') as handle:
                    blob += handle.read() + '\n'
        paths = bind_internals.parse_rfc_obj_not_found(blob)
        if not paths:
            self._log('medium', 'Density bind probe found no RFC_OBJ_NOT_FOUND paths')
            self._pending_density_probe_signals = set()
            return False

        overrides = bind_internals.hierarchical_overrides_from_rfc(paths)
        self._log('medium', f'Density probe hierarchical overrides: {overrides}')
        self._hierarchical_bind_overrides = {
            **getattr(self, '_hierarchical_bind_overrides', {}),
            **overrides,
        }
        corrected, fix_count, fixed = self._promote_internal_checker_ports(
            self.assertions, hierarchical_overrides=overrides)
        if fix_count:
            self.assertions = corrected
            self._update_metrics(
                'internal_port_binds',
                self.metrics.get('internal_port_binds', 0) + fix_count,
                increment=False,
            )
            self._write_assertions()
            self._log(
                'medium',
                f'Density probe rebound {fix_count} port(s): {sorted(fixed)}',
            )
        self._pending_density_probe_signals = set()
        return True


    # Main function to run the agent
    # Syntax correction, set extension, and GUI display
    def _run_semantic_pipeline(self):
        """Semantic correction (or validation) plus reports. Shared by resume."""
        self._install_timeout_finalizer()
        run_status_path = os.path.join(self.assertions_dir, 'run_status.json')

        # Semantic correction stage (only in golden mode)
        if self.execution == 'golden':
            self.timing['semantic_correction_start'] = time.time()

            # Collect verification results before semantic correction
            self._collect_verification_results('before_semantic')

            # Add call to fix unproven assertions
            self._print_log_separator('STARTING UNPROVEN ASSERTION FIXING')
            self._fix_unproven_assertions()
            self._print_log_separator('FINISHED UNPROVEN ASSERTION FIXING')

            # Collect verification results after semantic correction
            self._collect_verification_results('after_semantic')

            # Track final assertion count
            final_assertion_count = len([a for a in self.assertions if re.search(r'(\w+)\s*:\s*assert', a)])
            self._update_metrics('final_assertions', final_assertion_count, increment=False)
            self._log('debug', f'Final assertion count: {final_assertion_count}')

            self.timing['semantic_correction_end'] = time.time()

            # Store property file after semantic correction
            self._store_property_version('after_semantic_correction')

            # Create proven-only property file for golden execution
            self._create_proven_only_property_file()

        else:
            # Enhanced validation mode with specification-based semantic correction
            self._log('medium', 'Starting enhanced validation mode with specification-based semantic correction')
            self.timing['semantic_correction_start'] = time.time()

            # Collect verification results before semantic correction
            self._collect_verification_results('before_semantic')

            # Add call to fix unproven assertions using specification-based approach
            self._print_log_separator('STARTING VALIDATION MODE SEMANTIC CORRECTION')
            self._fix_unproven_assertions_validation_mode()
            self._print_log_separator('FINISHED VALIDATION MODE SEMANTIC CORRECTION')

            # Collect verification results after semantic correction
            self._collect_verification_results('after_semantic')

            # Track final assertion count for validation mode
            final_assertion_count = len([a for a in self.assertions if re.search(r'(\w+)\s*:\s*assert', a)])
            self._update_metrics('final_assertions', final_assertion_count, increment=False)
            self._log('debug', f'Final assertion count (validation mode): {final_assertion_count}')

            self.timing['semantic_correction_end'] = time.time()

            # Store property file after validation semantic correction
            self._store_property_version('after_validation_semantic_correction')

        # End total timing
        self.timing['total_end'] = time.time()

        # Display timing results
        self._display_timing_results()

        # Qualification, diversity, cost, and provenance artifacts
        self._finalize_reports()
        write_run_status(
            run_status_path,
            status='complete',
            phase='reports',
            module=self._extract_module_name(self.rtl_module),
            run_id=getattr(self, 'snapshot_metrics', None).module
            if getattr(self, 'snapshot_metrics', None) else '',
        )

        # GUI stage
        self.timing['gui_start'] = time.time()

        # Create a GUI to display processed assertions
        #self._log('high', None, self._display_assertions_gui())

        self.timing['gui_end'] = time.time()

        # Finalize log file
        self._finalize_log()

    def _resume_extension_from_existing_set(self):
        """Rebuild parser state from after_syntax and run set extension."""
        self.timing['total_start'] = time.time()
        run_status_path = os.path.join(self.assertions_dir, 'run_status.json')
        write_run_status(
            run_status_path,
            status='running',
            phase='extension_resume',
            module=self._extract_module_name(self.rtl_module),
        )
        self._collect_rtl_metrics()
        loaded = len([a for a in self.assertions if re.search(r'(\w+)\s*:\s*assert', a)])
        self._update_metrics('initial_assertions', loaded, increment=False)
        self._update_metrics('assertions_after_syntax_correction', loaded, increment=False)
        self._log(
            'medium',
            f'Resuming set extension from {loaded} after-syntax assertions')
        self._process_batch(limit=assertion_cap())
        if not self.assertions_list:
            self.assertions_list = self.assertions.copy()
        self._store_property_version('after_syntax')
        self.parser.prop_result = self._get_prop_result()
        self._run_extension_phase(loaded, run_status_path)

    def _run_extension_phase(self, syntax_corrected_count, run_status_path):
        """Set extension, post-extension FPV, then stop, score, or repair."""
        self.timing['set_extension_start'] = time.time()

        self._print_log_separator('STARTING SET EXTENSION')
        self._extend_property_set()

        try:
            self._maybe_run_density_bind_probe()
        except Exception as exc:  # pragma: no cover - defensive
            self._log('medium', f'Density bind probe raised: {exc}')

        self._log('medium', '\nRunning formal tool after set extension to get fresh verification results for complete assertion set...')
        try:
            directory = self._get_formal_project_dir()
            file_path = self._get_formal_log_path()
            subprocess.run(['rm', '-rf', directory], check=True)
            formal_start_time = time.time()
            self._run_formal_tool()
            self.metrics['total_formal_time'] += time.time() - formal_start_time
            while not os.path.isfile(file_path):
                time.sleep(3)
                self._log('medium', 'Waiting for formal tool log to be generated after set extension...')
        except subprocess.CalledProcessError as e:
            self._log('medium', f'Error running formal tool after set extension: {e}')

        self.parser.prop_result = self._get_prop_result()
        self._log('medium', None, self._print_property_db())
        self._print_log_separator('FINISHED SET EXTENSION')

        expansion_count = len([a for a in self.assertions if re.search(r'(\w+)\s*:\s*assert', a)])
        added_during_expansion = expansion_count - syntax_corrected_count
        self._update_metrics('assertions_after_property_expansion', expansion_count, increment=False)
        self._update_metrics('assertions_added_during_expansion', added_during_expansion, increment=False)
        self.timing['set_extension_end'] = time.time()
        self._store_property_version('after_extension')

        if self.stop_after_extension:
            self._log('medium', 'Stopping after set extension (--stop-after-extension)')
            write_run_status(
                run_status_path,
                status='complete',
                phase='extension_complete',
                module=self._extract_module_name(self.rtl_module),
            )
            self.timing['total_end'] = time.time()
            self._finalize_log()
            return

        self._run_semantic_pipeline()

    def _resume_semantic_from_existing_set(self):
        """Rebuild parser state from the current property file and repair."""
        self.timing['total_start'] = time.time()
        run_status_path = os.path.join(self.assertions_dir, 'run_status.json')
        write_run_status(
            run_status_path,
            status='running',
            phase='semantic_resume',
            module=self._extract_module_name(self.rtl_module),
        )
        self._collect_rtl_metrics()
        loaded = len([a for a in self.assertions if re.search(r'(\w+)\s*:\s*assert', a)])
        self._update_metrics('initial_assertions', loaded, increment=False)
        self._update_metrics('assertions_after_property_expansion', loaded, increment=False)
        self._log(
            'medium',
            f'Resuming semantic correction from {loaded} existing assertions')
        self._process_batch(limit=assertion_cap())
        if not self.assertions_list:
            self.assertions_list = self.assertions.copy()
        self.timing['set_extension_end'] = time.time()
        self._store_property_version('after_extension')
        self._run_semantic_pipeline()

    def run_agent(self):
        self._install_timeout_finalizer()
        if self.resume_from_semantic:
            self._resume_semantic_from_existing_set()
            return
        if self.resume_from_extension:
            self._resume_extension_from_existing_set()
            return

        # Start total timing
        self.timing['total_start'] = time.time()
        run_status_path = os.path.join(self.assertions_dir, 'run_status.json')
        write_run_status(
            run_status_path,
            status='running',
            phase='agent',
            module=self._extract_module_name(self.rtl_module),
        )

        # Collect RTL metrics at the beginning
        self._collect_rtl_metrics()

        # Preprocessing stage
        self.timing['preprocessing_start'] = time.time()

        # Track initial assertion count
        initial_assertion_count = len([a for a in self.assertions if re.search(r'(\w+)\s*:\s*assert', a)])

        self._update_metrics('initial_assertions', initial_assertion_count, increment=False)
        self._log('debug', f'Initial assertion count: {initial_assertion_count}')

        self._print_log_separator('STARTING INITIAL PROPERTY SET SYNTAX CORRECTION')
        self._log('medium','\nPreprocessing: Promoting referenced DUT internals to checker ports before syntax correction...')

        corrected_assertions, fix_count, fixed_signals = self._promote_internal_checker_ports(self.assertions)

        if fix_count > 0:
            self._log('debug', f'Promoted {fix_count} internal signal(s) to checker ports: {sorted(fixed_signals)}')
            self._log('debug','\n=== INTERNAL PORT BINDS ===')
            for i, signal in enumerate(sorted(fixed_signals)):
                self._log('debug', f'Port {i+1}: {signal}')
                self._log('debug','=' * 70)

            self.assertions = corrected_assertions
            self._update_metrics('internal_port_binds', fix_count, increment=False)
        else:
            self._log('debug', 'No internal port promotions needed in initial assertions')

        # Second automatic correction: Fix duplicate assertion names
        self._log('medium','\nPreprocessing: Checking for duplicate assertion names and fixing collisions...')

        name_corrected_assertions, name_fix_count, renamed_assertions = self._fix_duplicate_assertion_names(self.assertions)

        if name_fix_count > 0:
            self._log('debug', f'Fixed {name_fix_count} duplicate assertion names')
            self._log('debug','\n=== FIXED DUPLICATE NAMES ===')
            for old_key, new_name in renamed_assertions.items():
                self._log('debug', f'{old_key} -> {new_name}')
            self._log('debug','=' * 70)

            self.assertions = name_corrected_assertions
            # Update duplicate name fixes metric
            self._update_metrics('duplicate_name_fixes', name_fix_count, increment=False)
        else:
            self._log('debug', 'No duplicate assertion names found in initial assertions')

        # Print assertions after automatic corrections
        self._log('medium', '\nAssertions after automatic corrections:\n')
        for a in self.assertions:
            self._log('medium', a)
        self._log('medium', '\n')

        self._write_assertions()

        # Store initial property file version
        self._store_property_version('initial')

        # Initial syntax correction
        self.timing['preprocessing_end'] = time.time()
        self.timing['syntax_correction_start'] = time.time()

        self._correct_syntax()

        # Track assertion count after syntax correction
        syntax_corrected_count = len([a for a in self.assertions if re.search(r'(\w+)\s*:\s*assert', a)])
        self._update_metrics('assertions_after_syntax_correction', syntax_corrected_count, increment=False)
        self._log('debug', f'Assertions after syntax correction: {syntax_corrected_count}')

        self.timing['syntax_correction_end'] = time.time()

        # Store property file after syntax correction
        self._store_property_version('after_syntax')

        self._log('medium', '\n\n\nSYNTACTICALLY CORRECT ASSERTIONS:\n')

        for a in self.assertions:
            self._log('medium', a)
        self._log('medium', '\n')

        # Process the initial batch to establish base set for duplicate checking
        self._log('medium', '\nProcessing initial batch to establish base set...')

        # Debug: Check parser state before initial processing
        self._log('debug', f'Parser state before initial processing:')
        self._log('debug', f'  SVA String dictionary: {len(self.parser.sva_string)}')
        self._log('debug', f'  Delay dictionary: {len(self.parser.delay_d)}')
        self._log('debug', f'  Postcondition dictionary: {len(self.parser.postcondition_d)}')
        self._log('debug', f'  Clauses dictionary: {len(self.parser.clauses_d)}')
        self._log('debug', f'  Results dictionary: {len(self.parser.prop_result)}')
        self._log('debug', f'  Unprocessable properties: {len(self.parser.unprocessable_props_dict)}')

        # The cap that extension applies to this set is applied here too: a
        # property stored now but trimmed from the file later would keep
        # answering "already got this one" from outside the property file.
        new, new_list, duplicates = self._process_batch(limit=assertion_cap())

        # Debug: Check parser state after initial processing
        self._log('debug', f'Parser state after initial processing:')
        self._log('debug', f'  SVA String dictionary: {len(self.parser.sva_string)}')
        self._log('debug', f'  Delay dictionary: {len(self.parser.delay_d)}')
        self._log('debug', f'  Postcondition dictionary: {len(self.parser.postcondition_d)}')
        self._log('debug', f'  Clauses dictionary: {len(self.parser.clauses_d)}')
        self._log('debug', f'  Results dictionary: {len(self.parser.prop_result)}')
        self._log('debug', f'  Unprocessable properties: {len(self.parser.unprocessable_props_dict)}')

        self._log('medium', f'Found {new} unique assertions in initial set')
        if duplicates:
            self._log('medium', f'Found {len(duplicates)} duplicate assertions in initial set')
            for i, dup in enumerate(duplicates):
                self._log('medium', f'Duplicate {i+1}: {dup.strip()}')

        # Check if we have a problem where assertions exist but parser dictionaries are empty
        if (len(self.assertions) > 0 and
            len(self.parser.sva_string) == 0 and
            len(self.parser.clauses_d) == 0 and
            new == 0):

            self._log('medium', '\n⚠️  WARNING: Initial assertions exist but parser dictionaries are empty!')
            self._log('medium', 'This suggests all properties were detected as duplicates or failed to parse.')
            self._log('medium', 'Attempting to force-process the initial assertions...')

            # Temporarily enable debug mode in parser to see what's happening
            original_debug = self.parser.debug_mode
            self.parser.debug_mode = True

            # Try to process each assertion individually to see what's happening
            for i, assertion in enumerate(self.assertions):
                assertion_clean = assertion.replace('\n', '').strip()
                assertion_normalized = self._parser_input(assertion_clean)
                self._log('medium', f'\nForce-processing assertion {i+1}: {assertion_clean}')

                result = self.parser.process_property(assertion_normalized)
                self._log('medium', f'  Result: {"SUCCESS" if result else "FAILED/DUPLICATE"}')

            # Restore original debug mode
            self.parser.debug_mode = original_debug

            # Check parser state after force processing
            self._log('medium', f'\nParser state after force processing:')
            self._log('medium', f'  SVA String dictionary: {len(self.parser.sva_string)}')
            self._log('medium', f'  Delay dictionary: {len(self.parser.delay_d)}')
            self._log('medium', f'  Postcondition dictionary: {len(self.parser.postcondition_d)}')
            self._log('medium', f'  Clauses dictionary: {len(self.parser.clauses_d)}')
            self._log('medium', f'  Unprocessable properties: {len(self.parser.unprocessable_props_dict)}')

        # Get property results after syntax correction to ensure initial assertions have proof results
        self.parser.prop_result = self._get_prop_result()
        self._log('medium', None, self._print_property_db())

        self._run_extension_phase(syntax_corrected_count, run_status_path)

    def write_to_spreadsheet(self, column_name, row_name, value):
        """
        Write a value to an existing cell in the pre-formatted spreadsheet.

        Args:
            column_name (str): The name of the column to write to (must exist in spreadsheet)
            row_name (str): The name of the row to write to (must exist in spreadsheet)
            value: The value to write (can be any type that can be converted to string)

        Raises:
            ValueError: If the specified column or row doesn't exist in the spreadsheet
        """
        # try:
        #     # Load the existing workbook
        #     wb = load_workbook(self.spreadsheet_path)
        #     sheet = wb.active

        #     # Find column index
        #     col_idx = None
        #     for idx, cell in enumerate(sheet[1], 1):
        #         if cell.value == column_name:
        #             col_idx = idx
        #             break

        #     if col_idx is None:
        #         raise ValueError(f"Column '{column_name}' not found in spreadsheet")

        #     # Find row index
        #     row_idx = None
        #     for idx, cell in enumerate(sheet['A'], 1):
        #         if cell.value == row_name:
        #             row_idx = idx
        #             break

        #     if row_idx is None:
        #         raise ValueError(f"Row '{row_name}' not found in spreadsheet")

        #     # Write the value
        #     sheet.cell(row=row_idx, column=col_idx, value=value)

        #     # Save the workbook
        #     wb.save(self.spreadsheet_path)

        #     if self.verbosity >= 2:
        #         print(f"Successfully wrote value '{value}' to column '{column_name}', row '{row_name}'")

        # except Exception as e:
        #     print(f"Error writing to spreadsheet: {str(e)}")
        #     raise
        pass

    def _install_timeout_finalizer(self):
        """On SIGTERM/SIGINT write the snapshot as if the allotted time ended."""
        if getattr(self, '_timeout_finalizer_installed', False):
            return
        self._timeout_finalizer_installed = True
        self._timeout_finalized = False

        def handler(signum, _frame):
            try:
                self._finalize_timeout_as_success()
            finally:
                raise SystemExit(0)

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _persist_semantic_progress(self):
        try:
            from publication_eval.timeout_snapshot import persist_semantic_progress
        except ImportError:
            sys.path.insert(0, str(REPO_ROOT))
            from publication_eval.timeout_snapshot import persist_semantic_progress
        persist_semantic_progress(
            self.assertions_dir,
            header=self.checker_code,
            assertions=list(
                getattr(self, '_semantic_working_set', None)
                or getattr(self, 'assertions_list', None)
                or self.assertions
            ),
            prop_results=getattr(self, '_semantic_prop_results', None) or {},
        )

    def _finalize_timeout_as_success(self):
        """Restore the last full assertion set and write the three property files."""
        if getattr(self, '_timeout_finalized', False):
            return
        self._timeout_finalized = True
        assertions = list(
            getattr(self, '_semantic_working_set', None)
            or getattr(self, 'assertions_list', None)
            or self.assertions
            or []
        )
        if not assertions:
            self._log('medium', 'Timeout finalizer: no assertions to restore')
            return
        self.assertions = assertions
        self._write_assertions()
        self._store_property_version('after_semantic_correction')
        results = getattr(self, '_semantic_prop_results', None) or {}
        self._create_proven_only_property_file(prop_results=results)
        try:
            self._persist_semantic_progress()
        except Exception as exc:
            self._log('medium', f'Timeout progress not written: {exc}')
        baseline = getattr(self, '_semantic_formal_result', None)
        if baseline is not None:
            self.formal_result = baseline
        try:
            self._finalize_reports()
        except Exception as exc:
            self._log('medium', f'Timeout snapshot reports not written: {exc}')
        try:
            from publication_eval.timeout_snapshot import (
                compute_structural_diversity,
                write_timeout_metrics,
            )
            diversity = None
            try:
                diversity = compute_structural_diversity(
                    self.rtl,
                    self.rtl_module,
                    self._extract_module_name(self.rtl_module),
                    assertions,
                    {name for name, value in results.items() if value == 'proven'},
                )
            except Exception:
                diversity = None
            write_timeout_metrics(
                self.assertions_dir,
                self._extract_module_name(self.rtl_module),
                results,
                total_properties=len(assertions),
                graceful_timeout=True,
                diversity=diversity,
            )
        except Exception as exc:
            self._log('medium', f'Timeout metrics not written: {exc}')
        self._log(
            'medium',
            f'Timeout treated as completed snapshot: {len(assertions)} assertions, '
            f'{sum(1 for value in results.values() if value == "proven")} proven',
        )

    def _fix_unproven_assertions(self):
        """
        Attempts to fix all unproven assertions under an adaptive attempt budget.
        Each assertion is processed separately by creating a temporary property
        file.  The number of attempts per assertion is decided by
        :class:`adaptive_stop.StoppingPolicy` rather than a fixed limit: repair
        stops as soon as the estimated chance of the next attempt succeeding no
        longer justifies its cost, or when the model repeats itself.
        """
        self._log('medium', '\nStarting to fix unproven assertions...')
        self.llm_stage = 'semantic_repair'

        # Store property file before semantic correction
        self._store_property_version('after_extension')

        # Ensure assertions_list is populated
        if not self.assertions_list:
            self._log('medium', 'Populating assertions_list from current assertions...')
            self.assertions_list = self.assertions.copy()
            self._log('medium', f'Found {len(self.assertions_list)} assertions')

        # Run formal tool to get fresh results
        self._log('medium', 'Running formal tool to get fresh property results...')

        try:
            directory = self._get_formal_project_dir()
            file_path = self._get_formal_log_path()

            self._log('debug', directory)

            # Remove files from previous run
            p = subprocess.run(['rm', '-rf', directory], check=True)

            # Call the formal tool
            formal_start_time = time.time()
            p = self._run_formal_tool()
            formal_end_time = time.time()
            formal_call_time = formal_end_time - formal_start_time
            self.metrics['total_formal_time'] += formal_call_time
            self._log('debug', f"Formal tool call took {formal_call_time:.2f} seconds")

            while not os.path.isfile(file_path):
                time.sleep(3)
                self._log('medium', 'Waiting for formal tool log to be generated...')

            # Get current property results
            prop_results = self._get_prop_result()

            # Before repairing a property, establish whether the design or the
            # environment is at fault. A counterexample that disappears under a
            # screened assumption is a missing constraint, not a wrong property,
            # and rewriting the property would bake the under-constraint into
            # the snapshot.
            baseline_result = self.formal_result
            if baseline_result is not None:
                reclassified = self._generate_and_screen_assumptions(baseline_result)
                if reclassified:
                    self._log('medium',
                        f'Reclassified as failing_missing_assumption: '
                        f'{", ".join(reclassified)}')
                if self.active_assumptions:
                    self._log('medium',
                        f'{len(self.active_assumptions)} assumption(s) now in force; '
                        f're-running the formal tool before semantic repair.')
                    self._run_formal_and_qualify()
                    prop_results = self.formal_result.legacy_results()

            # Find all unproven assertions
            unproven_assertions = []
            proven_assertions = []  # Store proven assertions separately

            for assertion in self.assertions_list:
                # Extract assertion name
                match = re.search(r'(\w+):\s*assert', assertion)
                if not match:
                    continue

                assertion_name = match.group(1)

                # Check if this assertion has a counterexample
                if assertion_name in prop_results and prop_results[assertion_name] == 'cex':
                    unproven_assertions.append((assertion_name, assertion))
                else:
                    proven_assertions.append(assertion)  # Keep track of proven assertions

            self._semantic_prop_results = dict(prop_results) if isinstance(prop_results, dict) else {}
            self._semantic_formal_result = self.formal_result
            self._semantic_working_set = list(self.assertions_list)
            self._persist_semantic_progress()

            if not unproven_assertions:
                self._log('medium', 'No unproven assertions found to fix')
                return

            self._log('medium', f'\nFound {len(unproven_assertions)} unproven assertions to fix')
            self._log('medium', f'Found {len(proven_assertions)} proven assertions')

            # Store successful fixes
            fixed_assertions = []

            # Dictionary to store all correction attempts for each assertion
            correction_attempts = {}

            _sem_corr_sep = '=' * 70
            _sem_corr_n = len(unproven_assertions)

            # Process each unproven assertion
            for prop_idx, (target_name, target_assertion) in enumerate(unproven_assertions):
                if prop_idx > 0:
                    self._log('medium', '\n' + _sem_corr_sep)
                    self._log('medium', ' NEXT PROPERTY ')
                    self._log('medium', _sem_corr_sep + '\n')
                self._log('medium', f'\nProcessing unproven assertion: {target_name} ({prop_idx + 1}/{_sem_corr_n})')
                self._log('medium', f'Original assertion: {target_assertion}')

                # Initialize attempt counter and store attempts for this assertion
                attempts = 0
                fixed = False
                assertion_attempts = []
                property_start = time.time()
                cost_before = self.cost_ledger.total_cost
                stop_reason = StopReason.MAX_ATTEMPTS
                previous_cex = None

                while not fixed:
                    latest_cex = self._counterexample_signature(target_name)
                    proceed, reason = self.stopping_policy.should_attempt(
                        attempt_index=attempts + 1,
                        previous_attempts=assertion_attempts,
                        latest_attempt=assertion_attempts[-1] if assertion_attempts else None,
                        previous_counterexample=previous_cex,
                        latest_counterexample=latest_cex,
                    )
                    if not proceed:
                        stop_reason = reason
                        self._log('medium',
                            f'\nStopping repair of {target_name} after {attempts} attempt(s): '
                            f'{reason.value}')
                        break

                    prompt_previous_cex = previous_cex
                    previous_cex = latest_cex
                    attempts += 1
                    self._log('medium',
                        f'\nAttempt {attempts} '
                        f'(max {self.stopping_policy.config.max_attempts_per_property}, '
                        f'{reason})')

                    # Create temporary property file with just this assertion
                    temp_assertions = [target_assertion]
                    self.assertions = temp_assertions
                    self._write_assertions()

                    # Build prompt with previous attempts if any
                    previous_attempts = ""
                    if assertion_attempts:
                        previous_attempts = "\nPrevious attempts that didn't work:\n"
                        for i, attempt in enumerate(assertion_attempts, 1):
                            previous_attempts += f"Attempt {i}: {attempt}\n"

                    # Try to fix the assertion
                    module_name = self._extract_module_name(self.rtl_module)
                    rules_text = rules.replace('MODULE', module_name)
                    tool_display = 'VC Formal' if self.formal_tool == 'vcformal' else 'JasperGold'
                    repair_context = self._semantic_repair_context_block(
                        target_name, target_assertion,
                        proven_assertions=proven_assertions,
                        previous_signature=prompt_previous_cex,
                    )
                    focused_rtl = self._focused_rtl_for_repair(target_assertion)
                    if self._include_cex_in_prompt():
                        rtl_hint = (
                            '- Prefer formal-core / CEX signals over unrelated FSMs\n'
                            'RTL (signals in the formal core / CEX first):\n'
                        )
                    else:
                        rtl_hint = (
                            '- Prefer formal-core signals over unrelated FSMs\n'
                            'RTL (formal-core signals first; no CEX traces):\n'
                        )
                    prompt = (
                        f'{rules_text}'
                        f'Example output format:\n{STRUCTURED_OUTPUT_EXAMPLE}\n'
                        f'{self._instantiation_context_block()}'
                        f'{repair_context}'
                        f'The following assertion has not been proven by {tool_display}:\n'
                        f'{target_assertion}\n'
                        'Analyze why it might be failing and provide ONE corrected assertion.\n'
                        f'{previous_attempts}'
                        'IMPORTANT:\n'
                        '- Return ONLY ONE assertion block in structured --- id/property/failure --- format\n'
                        '- Keep the same assertion id\n'
                        '- Use unqualified checker port and parameter names only\n'
                        f'- Do NOT write {module_name}.signal; declare DUT internals as checker inputs\n'
                        f'{rtl_hint}'
                        f'{focused_rtl}\n'
                    )

                    self._log('high', '\n\nPrompt for LLM:')
                    self._log('high', prompt)

                    response = self._get_llm_completion([
                        {'role': 'system', 'content': 'You are an expert in SystemVerilog Assertions. Your responses should include detailed analysis and explanation before providing the corrected assertion. Always consider pipeline behavior and timing in your analysis.'},
                        {'role': 'user', 'content': prompt}
                    ])

                    # Track semantic API request
                    self._update_metrics('semantic_api_requests')

                    self._log('medium', '\n\nFull LLM Response:')
                    self._log('medium', response)

                    # Store the prompt-response pair
                    self._store_llm_interaction('correction', attempts, prompt, response, target_name)

                    corrected_assertions = self._extract_assertions(response)

                    if not corrected_assertions:
                        self._log('medium', f'No valid assertions found in response for {target_name}')
                        self.stopping_policy.record_attempt(attempts, succeeded=False)
                        continue

                    self._log('medium', f'\nFound {len(corrected_assertions)} assertions in response:')
                    for i, assertion in enumerate(corrected_assertions):
                        self._log('medium', f'Assertion {i+1}: {assertion}')

                    # Always use the last assertion from the response
                    selected_assertion = corrected_assertions[-1]
                    self._log('medium', f'\nSelected last assertion from response: {selected_assertion}')

                    # Store this attempt
                    assertion_attempts.append(selected_assertion)

                    # Update the assertions list with the corrected version
                    self.assertions = [selected_assertion]

                    # Write to property file and verify
                    self._write_assertions()

                    # Run formal tool again to check if the assertion is now proven
                    formal_start_time = time.time()
                    p = self._run_formal_tool()
                    formal_end_time = time.time()
                    formal_call_time = formal_end_time - formal_start_time
                    self.metrics['total_formal_time'] += formal_call_time
                    self._log('debug', f"Formal tool call took {formal_call_time:.2f} seconds")

                    # Wait for log file to be generated
                    while not os.path.isfile(file_path):
                        time.sleep(3)
                        self._log('medium', 'Waiting for formal tool log to be generated...')

                    # Check if the assertion is now proven. A vacuous proof maps
                    # to 'cex', so it does not count as a successful repair.
                    prop_results = self._get_prop_result()
                    succeeded = prop_results.get(target_name) == 'proven'
                    self.stopping_policy.record_attempt(attempts, succeeded=succeeded)

                    if succeeded:
                        self._log('medium', '\n' + _sem_corr_sep)
                        self._log('medium',
                            f'  ✅  CORRECTION SUCCEEDED — {target_name} is PROVEN after {attempts} attempt(s)')
                        self._log('medium', _sem_corr_sep)
                        fixed_assertions.append(selected_assertion)
                        fixed = True
                        stop_reason = StopReason.REPAIRED
                        break

                if not fixed:
                    self._log('medium', '\n' + '-' * 70)
                    self._log('medium',
                        f'  CORRECTION ABANDONED — {target_name} still not proven after '
                        f'{attempts} attempt(s) [{stop_reason.value}]')
                    self._log('medium', '-' * 70)
                    fixed_assertions.append(target_assertion)  # Keep original if not fixed

                self.stopping_policy.finish_property(RepairOutcome(
                    property_name=target_name,
                    attempts=attempts,
                    repaired=fixed,
                    stop_reason=stop_reason,
                    seconds=time.time() - property_start,
                    cost=self.cost_ledger.total_cost - cost_before,
                    attempt_texts=list(assertion_attempts),
                ))

                # Store all attempts for this assertion
                correction_attempts[target_name] = assertion_attempts
                remaining = [
                    assertion for _name, assertion in unproven_assertions[prop_idx + 1:]
                ]
                self._semantic_working_set = (
                    list(proven_assertions) + list(fixed_assertions) + remaining
                )
                if fixed:
                    self._semantic_prop_results[target_name] = 'proven'
                self._persist_semantic_progress()

            # Update assertions_list with fixes applied
            self.assertions_list = proven_assertions + fixed_assertions
            self.assertions = proven_assertions + fixed_assertions
            self._write_assertions()

            # Run formal tool with all final assertions to get complete results
            self._log('medium', '\nRunning formal tool with all final assertions to collect complete results...')
            try:
                # Remove files from previous run
                p = subprocess.run(['rm', '-rf', directory], check=True)

                # Call the formal tool for all final assertions
                formal_start_time = time.time()
                p = self._run_formal_tool()
                formal_end_time = time.time()
                formal_call_time = formal_end_time - formal_start_time
                self.metrics['total_formal_time'] += formal_call_time
                self._log('debug', f"Formal tool call took {formal_call_time:.2f} seconds")

                # Wait for log file to be generated
                while not os.path.isfile(file_path):
                    time.sleep(3)
                    self._log('medium', 'Waiting for formal tool log with all final assertions to be generated...')

                # Get final comprehensive property results
                final_prop_results = self._get_prop_result()
                self._log('medium', f'Final comprehensive results collected for {len(final_prop_results)} assertions')

            except subprocess.CalledProcessError as e:
                self._log('medium', f'Error running final formal tool verification: {e}')
                # Fallback to existing results if final run fails
                final_prop_results = self._get_prop_result()

            # Print summary of all correction attempts
            self._log('medium', '\n=== CORRECTION ATTEMPTS SUMMARY ===')
            for assertion_name, attempts in correction_attempts.items():
                self._log('medium', f'\nAssertion: {assertion_name}')
                self._log('medium', 'Original assertion:')
                self._log('medium', next(a[1] for a in unproven_assertions if a[0] == assertion_name))
                self._log('medium', '\nCorrection attempts:')
                for i, attempt in enumerate(attempts, 1):
                    self._log('medium', f'Attempt {i}: {attempt}')
                self._log('medium', '-' * 70)

            # Count successful and failed semantic corrections
            successful_corrections = 0
            failed_corrections = 0

            # Check final results to count successes and failures
            for assertion_name, _ in unproven_assertions:
                if assertion_name in final_prop_results and final_prop_results[assertion_name] == 'proven':
                    successful_corrections += 1
                else:
                    failed_corrections += 1

            # Update metrics
            self._update_metrics('semantic_corrected', successful_corrections, increment=False)
            self._update_metrics('semantic_failed', failed_corrections, increment=False)

            self._log('medium', f'\n=== SEMANTIC CORRECTION RESULTS ===')
            self._log('medium', f'Successfully corrected assertions: {successful_corrections}')
            self._log('medium', f'Failed to correct assertions: {failed_corrections}')
            self._log('medium', f'Total unproven assertions processed: {len(unproven_assertions)}')
            self._log('medium', '\n' + self.stopping_policy.format_summary())
            self._dump_json(
                self.stopping_policy.to_dict(), 'adaptive_stopping.json')

            # Verify final state
            with open(self.assertions_dir + self.assertions_file, 'r') as f:
                file_contents = f.read()
                self._log('medium', '\nFinal property file contents:')
                self._log('medium', file_contents)

        except subprocess.CalledProcessError as e:
            self._log('medium', f'Program execution failed: {e}')
            return

        # Store property file after semantic correction is completed
        self._store_property_version('after_semantic_correction')

        # Create proven-only property file
        self._create_proven_only_property_file()

    def _create_proven_only_property_file(self, prop_results=None):
        """
        Create a property file with only proven assertions enabled.
        Failing assertions are commented out.
        """
        try:
            self._log('medium', '\nCreating proven-only property file...')

            # Prefer the last full-set results. A mid-repair formal run only
            # saw the single assertion under test.
            if prop_results is None:
                prop_results = getattr(self, '_semantic_prop_results', None)
            if not prop_results:
                prop_results = self._get_prop_result()

            # Generate the proven-only property file
            base_name = os.path.splitext(self.assertions_file)[0]
            proven_only_file = os.path.join(self.assertions_dir, f'{base_name}_only_proven.sv')

            with open(proven_only_file, 'w') as f:
                # Write the header
                f.write('// Property file with only proven assertions enabled\n')
                f.write('// Failing assertions are commented out\n')
                f.write('// To enable failing assertions, uncomment them manually\n\n')

                # Write the checker code (module header)
                f.write(self.checker_code)

                # Process each assertion
                for assertion in self.assertions:
                    if assertion[0] != '\n':
                        f.write('\n')

                    # Extract assertion name
                    assertion_name_match = re.search(r'(\w+)\s*:\s*assert', assertion)
                    if assertion_name_match:
                        assertion_name = assertion_name_match.group(1)

                        # Check if this assertion is proven
                        if assertion_name in prop_results and prop_results[assertion_name] == 'proven':
                            # Proven assertion - write as is
                            f.write(assertion)
                            self._log('debug', f'Added proven assertion: {assertion_name}')
                        else:
                            # Unproven/failing assertion - comment it out
                            # Split assertion into lines and comment each line
                            assertion_lines = assertion.split('\n')
                            for line in assertion_lines:
                                if line.strip():  # Only comment non-empty lines
                                    f.write('// ' + line + '\n')
                                else:
                                    f.write(line + '\n')
                            self._log('debug', f'Commented out failing assertion: {assertion_name}')
                    else:
                        # Could not extract name, write as is (might be a comment or other content)
                        f.write(assertion)

                    if not assertion.endswith('\n'):
                        f.write('\n')

                f.write('\nendmodule\n')

            self._log('medium', f'Created proven-only property file: {proven_only_file}')

            # Count proven vs failing assertions
            proven_count = sum(1 for assertion in self.assertions
                             if re.search(r'(\w+)\s*:\s*assert', assertion) and
                             re.search(r'(\w+)\s*:\s*assert', assertion).group(1) in prop_results and
                             prop_results[re.search(r'(\w+)\s*:\s*assert', assertion).group(1)] == 'proven')

            total_count = len([a for a in self.assertions if re.search(r'(\w+)\s*:\s*assert', a)])
            failing_count = total_count - proven_count

            self._log('medium', f'Proven assertions: {proven_count}')
            self._log('medium', f'Failing assertions: {failing_count} (commented out)')
            self._log('medium', f'Total assertions: {total_count}')

        except Exception as e:
            self._log('medium', f'Error creating proven-only property file: {str(e)}')
            import traceback
            traceback.print_exc()

    def _store_property_version(self, stage):
        """Store a copy of the current property file at a specific stage."""
        try:
            # Create versions directory if it doesn't exist
            versions_dir = os.path.join(self.assertions_dir, 'versions')
            os.makedirs(versions_dir, exist_ok=True)

            # Generate filename for this version
            base_name = os.path.splitext(self.assertions_file)[0]
            version_file = os.path.join(versions_dir, f'{base_name}_{stage}.sv')

            # Write current assertions to the version file
            with open(version_file, 'w') as f:
                f.write(self.checker_code)
                for assertion in self.assertions:
                    if assertion[0] != '\n':
                        f.write('\n')
                    f.write(assertion)
                    if not assertion.endswith('\n'):
                        f.write('\n')
                f.write('\nendmodule\n')

            # Store the version in our tracking dictionary
            self.property_versions[stage] = version_file
            self._log('medium', f'Stored property file version for {stage} stage at {version_file}')

        except Exception as e:
            self._log('medium', f'Error storing property version for {stage}: {str(e)}')

    def _update_metrics(self, metric_name, value=None, increment=True):
        """Update a specific metric, either by incrementing or setting a value."""
        if metric_name in self.metrics:
            if increment and value is None:
                self.metrics[metric_name] += 1
            else:
                self.metrics[metric_name] = value
            self._log('debug', f'Updated metric {metric_name} to {self.metrics[metric_name]}')

    def _collect_rtl_metrics(self):
        """Collect RTL complexity metrics for analysis."""
        try:
            self._log('debug', 'Starting RTL metrics collection...')

            # Load RTL content
            rtl_lines = self.rtl.split('\n')
            # Initialize metrics
            unique_submodules = set()
            total_submodule_instances = 0
            rtl_line_count = len([line for line in rtl_lines if line.strip() and not line.strip().startswith('//')])
            is_sequential = False
            is_combinational = True

            # Analyze RTL content
            for line in rtl_lines:
                line_clean = line.strip()

                # Skip comments and empty lines
                if not line_clean or line_clean.startswith('//'):
                    continue

                # Check for sequential elements (flip-flops, latches)
                if any(keyword in line_clean for keyword in ['always_ff', 'always @(posedge', 'always @(negedge', 'always @(edge']):
                    is_sequential = True
                    is_combinational = False

                # Check for combinational always blocks
                if 'always_comb' in line_clean or 'always @(*)' in line_clean:
                    # Still combinational, but has always blocks
                    pass

                # Look for module instantiations
                # Pattern: module_name instance_name (
                module_inst_match = re.match(r'\s*(\w+)\s+(\w+)\s*\(', line_clean)
                if module_inst_match and not line_clean.startswith('module'):
                    module_name = module_inst_match.group(1)
                    instance_name = module_inst_match.group(2)

                    # Filter out SystemVerilog keywords and built-in types
                    sv_keywords = {'logic', 'wire', 'reg', 'input', 'output', 'inout', 'parameter', 'localparam',
                                 'assign', 'always', 'initial', 'if', 'else', 'case', 'for', 'while', 'generate',
                                 'genvar', 'typedef', 'struct', 'union', 'enum', 'interface', 'modport'}

                    if module_name not in sv_keywords and not module_name.startswith('$'):
                        unique_submodules.add(module_name)
                        total_submodule_instances += 1
                        self._log('debug', f'Found submodule instance: {module_name} {instance_name}')

            # Determine module type
            if is_sequential:
                module_type = 'Sequential'
            elif is_combinational:
                module_type = 'Combinational'
            else:
                module_type = 'Mixed/Unknown'

            # Update metrics
            self._update_metrics('unique_submodules', len(unique_submodules), increment=False)
            self._update_metrics('total_submodule_instances', total_submodule_instances, increment=False)
            self._update_metrics('rtl_line_count', rtl_line_count, increment=False)
            self._update_metrics('module_type', module_type, increment=False)

            self._log('debug', f'RTL metrics collected:')
            self._log('debug', f'  Unique submodules: {len(unique_submodules)}')
            self._log('debug', f'  Total submodule instances: {total_submodule_instances}')
            self._log('debug', f'  RTL lines: {rtl_line_count}')
            self._log('debug', f'  Module type: {module_type}')

            if unique_submodules:
                self._log('debug', f'  Submodules found: {", ".join(sorted(unique_submodules))}')

        except Exception as e:
            self._log('debug', f'Error collecting RTL metrics: {str(e)}')
            # Set default values on error
            self._update_metrics('unique_submodules', 0, increment=False)
            self._update_metrics('total_submodule_instances', 0, increment=False)
            self._update_metrics('rtl_line_count', 0, increment=False)
            self._update_metrics('module_type', 'Unknown', increment=False)

    def _collect_verification_results(self, stage):
        """Collect verification results at a specific stage."""
        try:
            self._log('debug', f'Collecting verification results for stage: {stage}')

            # Get current property results
            prop_results = self._get_prop_result()

            # Count passing and failing assertions
            passing_count = 0
            failing_count = 0
            undefined_count = 0

            # Track different result types for better debugging
            result_types = {}

            for assertion_name, result in prop_results.items():
                if result == 'proven':
                    passing_count += 1
                elif result == 'cex':
                    failing_count += 1
                else:
                    undefined_count += 1

                # Track result type distribution
                if result not in result_types:
                    result_types[result] = 0
                result_types[result] += 1

            # Update metrics based on stage
            if stage == 'before_semantic':
                self._update_metrics('passing_assertions_before_semantic', passing_count, increment=False)
                self._update_metrics('failing_assertions_before_semantic', failing_count, increment=False)
                self._update_metrics('undefined_assertions_before_semantic', undefined_count, increment=False)
            elif stage == 'after_semantic':
                self._update_metrics('passing_assertions_after_semantic', passing_count, increment=False)
                self._update_metrics('failing_assertions_after_semantic', failing_count, increment=False)
                self._update_metrics('undefined_assertions_after_semantic', undefined_count, increment=False)

            self._log('debug', f'Verification results for {stage}:')
            self._log('debug', f'  Passing assertions: {passing_count}')
            self._log('debug', f'  Failing assertions: {failing_count}')
            self._log('debug', f'  Undefined assertions: {undefined_count}')
            self._log('debug', f'  Total assertions processed: {len(prop_results)}')
            self._log('debug', f'  Current assertions count: {len(self.assertions) if hasattr(self, "assertions") else "N/A"}')

            # Log result type distribution for debugging
            if result_types:
                self._log('debug', f'  Result type distribution:')
                for result_type, count in sorted(result_types.items()):
                    self._log('debug', f'    {result_type}: {count}')

            # Warn if there's a mismatch between assertion count and results
            current_assertion_count = len(self.assertions) if hasattr(self, 'assertions') else 0
            if len(prop_results) != current_assertion_count and current_assertion_count > 0:
                self._log('medium', f'⚠️  WARNING: Mismatch between assertion count ({current_assertion_count}) and verification results ({len(prop_results)}) for stage {stage}')
                tool_display = 'VC Formal' if self.formal_tool == 'vcformal' else 'JasperGold'
                self._log('medium', f'   This may indicate that some assertions failed to run or were not processed by {tool_display}')
                self._log('medium', f'   If this occurs after set extension, ensure {tool_display} was run with the complete assertion set')

        except Exception as e:
            self._log('debug', f'Error collecting verification results for {stage}: {str(e)}')

    def _store_llm_interaction(self, stage, iteration, prompt, response, assertion_name=None):
        """Store a prompt-response pair from LLM interaction.

        Args:
            stage (str): The stage of execution ('syntax', 'extension', 'correction')
            iteration (int): The iteration number
            prompt (str): The prompt sent to the LLM
            response (str): The response received from the LLM
        """
        try:
            # Create interactions directory if it doesn't exist
            interactions_dir = os.path.join(self.assertions_dir, 'interactions')
            os.makedirs(interactions_dir, exist_ok=True)

            # Generate filenames for this interaction
            base_name = os.path.splitext(self.assertions_file)[0]
            # Include assertion name when provided to avoid overwriting across different assertions
            if assertion_name:
                safe_name = re.sub(r'[^A-Za-z0-9_\-]+', '_', assertion_name)
                prompt_file = os.path.join(interactions_dir, f'{base_name}_{stage}{iteration}_{safe_name}_prompt.txt')
                response_file = os.path.join(interactions_dir, f'{base_name}_{stage}{iteration}_{safe_name}_response.txt')
            else:
                prompt_file = os.path.join(interactions_dir, f'{base_name}_{stage}{iteration}_prompt.txt')
                response_file = os.path.join(interactions_dir, f'{base_name}_{stage}{iteration}_response.txt')

            # Write prompt and response to their respective files
            with open(prompt_file, 'w') as f:
                f.write(prompt)
            with open(response_file, 'w') as f:
                f.write(response)

            self._log('medium', f'Stored LLM interaction for {stage} stage iteration {iteration}')

        except Exception as e:
            self._log('medium', f'Error storing LLM interaction for {stage} stage iteration {iteration}: {str(e)}')

    def _fix_duplicate_assertion_names(self, assertions):
        """
        Check for duplicate assertion names in the initial assertion set and modify them
        with numerals to avoid name collisions while keeping ALL assertions.

        Args:
            assertions: List of assertion strings to check

        Returns:
            tuple: (corrected_assertions, fix_count, renamed_assertions_dict)
        """
        self._log('debug', '\n=== CHECKING FOR DUPLICATE ASSERTION NAMES ===')
        corrected, fix_count, renamed = _deduplicate_assertion_names(assertions)
        self._log('debug', f'  Total assertions processed: {len(assertions)}')
        self._log('debug', f'  Duplicate names fixed: {fix_count}')
        for old_key, new_name in renamed.items():
            self._log('debug', f'  {old_key} -> {new_name}')
        return corrected, fix_count, renamed

    def _cleanup_error_tracking(self):
        """Clean up error tracking data to prevent accumulation of stale data."""
        try:
            error_dir = os.path.join(self.assertions_dir, 'error_tracking')
            if os.path.exists(error_dir):
                # Remove all error tracking files to start fresh
                import shutil
                shutil.rmtree(error_dir)
                self._log('medium', 'Cleaned up previous error tracking data')
        except Exception as e:
            self._log('medium', f'Error cleaning up error tracking data: {str(e)}')

    def _display_error_tracking_stats(self):
        """Display current error tracking statistics for debugging."""
        try:
            error_dir = os.path.join(self.assertions_dir, 'error_tracking')
            if not os.path.exists(error_dir):
                self._log('medium', 'No error tracking data available')
                return

            # Load and display error counts
            error_counts_file = os.path.join(error_dir, 'assertion_error_counts.json')
            if os.path.exists(error_counts_file):
                with open(error_counts_file, 'r') as f:
                    error_counts = json.load(f)

                if error_counts:
                    self._log('medium', '\n=== ERROR TRACKING STATISTICS ===')
                    for assertion_name, data in error_counts.items():
                        self._log('medium', f'Assertion: {assertion_name}')
                        self._log('medium', f'  Total failures: {data["total_failures"]}')
                        self._log('medium', f'  Error patterns: {len(data["error_patterns"])}')
                        for pattern, count in data["error_patterns"].items():
                            self._log('medium', f'    "{pattern}": {count} times')

            # Load and display consecutive failures
            consecutive_failures_file = os.path.join(error_dir, 'consecutive_failures.json')
            if os.path.exists(consecutive_failures_file):
                with open(consecutive_failures_file, 'r') as f:
                    consecutive_failures = json.load(f)

                if consecutive_failures:
                    self._log('medium', '\n=== CONSECUTIVE FAILURES ===')
                    for assertion_name, data in consecutive_failures.items():
                        self._log('medium', f'Assertion: {assertion_name}')
                        self._log('medium', f'  Consecutive count: {data["count"]}')
                        self._log('medium', f'  Last error pattern: {data["last_error_pattern"]}')

            # Load and display blacklisted errors
            blacklist_file = os.path.join(error_dir, 'blacklisted_errors.json')
            if os.path.exists(blacklist_file):
                with open(blacklist_file, 'r') as f:
                    blacklisted_errors = json.load(f)

                if blacklisted_errors:
                    self._log('medium', '\n=== BLACKLISTED ERROR PATTERNS ===')
                    for i, pattern in enumerate(blacklisted_errors, 1):
                        self._log('medium', f'{i}. "{pattern}"')

            self._log('medium', '=' * 50)

        except Exception as e:
            self._log('medium', f'Error displaying error tracking stats: {str(e)}')

    def _display_timing_results(self):
        """Display comprehensive execution metrics and timing results."""
        total_time = self.timing['total_end'] - self.timing['total_start']
        preprocessing_time = self.timing['preprocessing_end'] - self.timing['preprocessing_start']
        syntax_time = self.timing['syntax_correction_end'] - self.timing['syntax_correction_start']
        extension_time = self.timing['set_extension_end'] - self.timing['set_extension_start']
        semantic_time = self.timing['semantic_correction_end'] - self.timing['semantic_correction_start']

        self._log('medium', '\n' + '=' * 80)
        self._log('medium', '🚀 AI-SVA AGENT EXECUTION REPORT')
        self._log('medium', '=' * 80)

        # RTL Analysis Metrics
        self._log('medium', '\n📊 RTL MODULE ANALYSIS')
        self._log('medium', '-' * 40)
        self._log('medium', f'Number of unique submodules:     {self.metrics.get("unique_submodules", 0)}')
        self._log('medium', f'Total submodule instances:       {self.metrics.get("total_submodule_instances", 0)}')
        self._log('medium', f'RTL module lines:                {self.metrics.get("rtl_line_count", 0)}')
        self._log('medium', f'Module type:                     {self.metrics.get("module_type", "Unknown")}')

        # Assertion Count Metrics
        self._log('medium', '\n📝 ASSERTION METRICS')
        self._log('medium', '-' * 40)
        self._log('medium', f'Initial assertions:              {self.metrics.get("initial_assertions", 0)}')
        self._log('medium', f'After syntax correction:         {self.metrics.get("assertions_after_syntax_correction", 0)}')
        self._log('medium', f'After property set expansion:    {self.metrics.get("assertions_after_property_expansion", 0)}')
        self._log('medium', f'Added during expansion:          {self.metrics.get("assertions_added_during_expansion", 0)}')
        self._log('medium', f'Final assertions:                {self.metrics.get("final_assertions", 0)}')

        # Automatic Fixes Applied
        self._log('medium', '\n🔧 AUTOMATIC SYNTACTICAL FIXES')
        self._log('medium', '-' * 40)
        self._log('medium', f'Internal port binds:             {self.metrics.get("internal_port_binds", 0)}')
        self._log('medium', f'Duplicate name fixes:            {self.metrics.get("duplicate_name_fixes", 0)}')
        self._log('medium', f'Total automatic fixes:           {self.metrics.get("internal_port_binds", 0) + self.metrics.get("duplicate_name_fixes", 0)}')

        # Verification Results (Before Semantic Correction)
        self._log('medium', '\n✅ VERIFICATION RESULTS (Before Semantic Correction)')
        self._log('medium', '-' * 40)
        before_passing = self.metrics.get("passing_assertions_before_semantic", 0)
        before_failing = self.metrics.get("failing_assertions_before_semantic", 0)
        before_undefined = self.metrics.get("undefined_assertions_before_semantic", 0)
        before_total = before_passing + before_failing + before_undefined

        self._log('medium', f'Passing assertions:              {before_passing}')
        self._log('medium', f'Failing assertions:              {before_failing}')
        self._log('medium', f'Undefined assertions:            {before_undefined}')
        self._log('medium', f'Total verified:                  {before_total}')

        # Show discrepancy if exists
        expected_total = self.metrics.get("assertions_after_property_expansion", 0)
        if before_total != expected_total and expected_total > 0:
            self._log('medium', f'⚠️  Expected total assertions:    {expected_total}')
            self._log('medium', f'⚠️  Verification gap:             {expected_total - before_total}')

        # Verification Results (After Semantic Correction)
        after_passing = self.metrics.get("passing_assertions_after_semantic", 0)
        after_failing = self.metrics.get("failing_assertions_after_semantic", 0)
        after_undefined = self.metrics.get("undefined_assertions_after_semantic", 0)
        after_total = after_passing + after_failing + after_undefined

        if self.execution == 'golden':
            self._log('medium', '\n✅ VERIFICATION RESULTS (After Golden Mode Semantic Correction)')
            self._log('medium', '-' * 40)
            self._log('medium', f'Passing assertions:              {after_passing}')
            self._log('medium', f'Failing assertions:              {after_failing}')
            self._log('medium', f'Undefined assertions:            {after_undefined}')
            self._log('medium', f'Total verified:                  {after_total}')

            # Show improvement from semantic correction
            if before_total > 0 and after_total > 0:
                passing_improvement = after_passing - before_passing
                failing_improvement = before_failing - after_failing  # Positive means fewer failures
                if passing_improvement != 0 or failing_improvement != 0:
                    self._log('medium', f'Improvement: +{passing_improvement} passing, -{before_failing - after_failing} failing')

            # Semantic Correction Results
            self._log('medium', '\n🎯 GOLDEN MODE SEMANTIC CORRECTION RESULTS')
            self._log('medium', '-' * 40)
            self._log('medium', f'Successfully corrected:          {self.metrics.get("semantic_corrected", 0)}')
            self._log('medium', f'Failed to correct:               {self.metrics.get("semantic_failed", 0)}')
            self._log('medium', f'Max attempts per assertion:      5')
            self._log('medium', f'Uses RTL for context:            Yes')

        elif self.execution == 'validation':
            self._log('medium', '\n✅ VERIFICATION RESULTS (After Validation Mode Semantic Correction)')
            self._log('medium', '-' * 40)
            self._log('medium', f'Passing assertions:              {after_passing}')
            self._log('medium', f'Failing assertions:              {after_failing}')
            self._log('medium', f'Undefined assertions:            {after_undefined}')
            self._log('medium', f'Total verified:                  {after_total}')

            # Show improvement from semantic correction
            if before_total > 0 and after_total > 0:
                passing_improvement = after_passing - before_passing
                failing_improvement = before_failing - after_failing  # Positive means fewer failures
                if passing_improvement != 0 or failing_improvement != 0:
                    self._log('medium', f'Improvement: +{passing_improvement} passing, -{before_failing - after_failing} failing')

            # Semantic Correction Results
            self._log('medium', '\n🎯 VALIDATION MODE SEMANTIC CORRECTION RESULTS')
            self._log('medium', '-' * 40)
            self._log('medium', f'Successfully corrected:          {self.metrics.get("semantic_corrected", 0)}')
            self._log('medium', f'Failed to correct:               {self.metrics.get("semantic_failed", 0)}')
            self._log('medium', f'Max attempts per assertion:      1')
            self._log('medium', f'Uses specification files:        Yes')
            self._log('medium', f'Uses RTL for context:            No')

        # Timing Information
        self._log('medium', '\n🕒 EXECUTION TIMING')
        self._log('medium', '-' * 40)
        self._log('medium', f'Total execution time:            {total_time:.2f}s')
        self._log('medium', f'Preprocessing:                   {preprocessing_time:.2f}s ({preprocessing_time/total_time*100:.1f}%)')
        self._log('medium', f'Syntax correction:               {syntax_time:.2f}s ({syntax_time/total_time*100:.1f}%)')
        self._log('medium', f'Property set extension:          {extension_time:.2f}s ({extension_time/total_time*100:.1f}%)')
        if self.execution == 'golden':
            self._log('medium', f'Semantic correction (Golden):    {semantic_time:.2f}s ({semantic_time/total_time*100:.1f}%)')
        elif self.execution == 'validation':
            self._log('medium', f'Semantic correction (Validation): {semantic_time:.2f}s ({semantic_time/total_time*100:.1f}%)')

        # Calculate core processing time (excluding GUI)
        core_time = preprocessing_time + syntax_time + extension_time + semantic_time
        self._log('medium', f'Core processing time:            {core_time:.2f}s ({core_time/total_time*100:.1f}%)')

        # Fine-grained timing metrics
        jasper_time = self.metrics.get('total_formal_time', 0.0)
        llm_time = self.metrics.get('total_llm_time', 0.0)
        self._log('medium', f'Formal tool time:                {jasper_time:.2f}s ({jasper_time/total_time*100:.1f}%)')
        self._log('medium', f'LLM time:                        {llm_time:.2f}s ({llm_time/total_time*100:.1f}%)')

        # Show breakdown of core time
        other_time = core_time - jasper_time - llm_time
        if other_time > 0:
            self._log('medium', f'Other processing time:           {other_time:.2f}s ({other_time/total_time*100:.1f}%)')

        # Token Usage Metrics
        self._log('medium', '\n🔢 LLM API USAGE STATISTICS')
        self._log('medium', '-' * 40)
        total_input_tokens = self.metrics.get('total_input_tokens', 0)
        total_output_tokens = self.metrics.get('total_output_tokens', 0)
        total_tokens = total_input_tokens + total_output_tokens
        llm_calls = self.metrics.get('llm_calls', 0)
        syntax_api_calls = self.metrics.get('syntax_api_requests', 0)
        semantic_api_calls = self.metrics.get('semantic_api_requests', 0)

        self._log('medium', f'Total input tokens:              {total_input_tokens:,}')
        self._log('medium', f'Total output tokens:             {total_output_tokens:,}')
        self._log('medium', f'Total tokens:                    {total_tokens:,}')
        self._log('medium', f'Total API calls:                 {llm_calls}')
        self._log('medium', f'Syntax correction API calls:     {syntax_api_calls}')
        self._log('medium', f'Semantic correction API calls:   {semantic_api_calls}')

        if llm_calls > 0:
            self._log('medium', f'Avg input tokens per call:       {total_input_tokens/llm_calls:.1f}')
            self._log('medium', f'Avg output tokens per call:      {total_output_tokens/llm_calls:.1f}')
            self._log('medium', f'Avg total tokens per call:       {total_tokens/llm_calls:.1f}')

        self._log('medium', '=' * 80)

        # Also log to the regular logging system for file records
        self._log('medium', '\n=== COMPREHENSIVE EXECUTION REPORT ===')
        self._log('medium', f'RTL Analysis: {self.metrics.get("unique_submodules", 0)} unique submodules, {self.metrics.get("total_submodule_instances", 0)} instances, {self.metrics.get("rtl_line_count", 0)} lines, {self.metrics.get("module_type", "Unknown")} type')
        self._log('medium', f'Assertion Counts: {self.metrics.get("initial_assertions", 0)} initial → {self.metrics.get("assertions_after_syntax_correction", 0)} after syntax → {self.metrics.get("assertions_after_property_expansion", 0)} after expansion → {self.metrics.get("final_assertions", 0)} final')
        self._log('medium', f'Automatic Fixes: {self.metrics.get("internal_port_binds", 0)} internal port binds, {self.metrics.get("duplicate_name_fixes", 0)} name fixes')
        self._log('medium', f'Verification (Before): {self.metrics.get("passing_assertions_before_semantic", 0)} passing, {self.metrics.get("failing_assertions_before_semantic", 0)} failing')
        if self.execution == 'golden':
            self._log('medium', f'Verification (After): {self.metrics.get("passing_assertions_after_semantic", 0)} passing, {self.metrics.get("failing_assertions_after_semantic", 0)} failing')
            self._log('medium', f'Golden Semantic Correction: {self.metrics.get("semantic_corrected", 0)} corrected, {self.metrics.get("semantic_failed", 0)} failed (RTL-based, max 5 attempts)')
        elif self.execution == 'validation':
            self._log('medium', f'Verification (After): {self.metrics.get("passing_assertions_after_semantic", 0)} passing, {self.metrics.get("failing_assertions_after_semantic", 0)} failing')
            self._log('medium', f'Validation Semantic Correction: {self.metrics.get("semantic_corrected", 0)} corrected, {self.metrics.get("semantic_failed", 0)} failed (spec-based, 1 attempt)')
        self._log('medium', f'Timing: {total_time:.2f}s total ({preprocessing_time:.2f}s preprocessing, {syntax_time:.2f}s syntax, {extension_time:.2f}s extension, {semantic_time:.2f}s semantic)')
        jasper_time = self.metrics.get('total_formal_time', 0.0)
        llm_time = self.metrics.get('total_llm_time', 0.0)
        self._log('medium', f'Fine-grained Timing: {jasper_time:.2f}s formal tool, {llm_time:.2f}s LLM')
        self._log('medium', f'API Usage: {total_tokens:,} tokens ({total_input_tokens:,} input, {total_output_tokens:,} output), {llm_calls} calls ({syntax_api_calls} syntax, {semantic_api_calls} semantic)')
        self._log('medium', '=' * 50)

        # Save comprehensive results to file
        self._save_timing_results(total_time, preprocessing_time, syntax_time, extension_time, semantic_time, core_time)

        # Display property versions created
        if self.property_versions:
            self._log('medium', '\n📁 PROPERTY FILE VERSIONS CREATED')
            self._log('medium', '-' * 40)
            for stage, file_path in self.property_versions.items():
                self._log('medium', f'{stage.replace("_", " ").title()}: {file_path}')
            self._log('medium', '=' * 80)

    def _save_timing_results(self, total_time, preprocessing_time, syntax_time, extension_time, semantic_time, core_time):
        """Save comprehensive timing and metrics results to a file for later analysis."""
        try:
            timing_file = os.path.join(self.assertions_dir, 'comprehensive_results.txt')
            with open(timing_file, 'w') as f:
                f.write('AI-SVA AGENT COMPREHENSIVE EXECUTION REPORT\n')
                f.write('=' * 80 + '\n')
                f.write(f'Execution timestamp: {time.strftime("%Y-%m-%d %H:%M:%S")}\n\n')

                # RTL Analysis
                f.write('RTL MODULE ANALYSIS\n')
                f.write('-' * 40 + '\n')
                f.write(f'Number of unique submodules:     {self.metrics.get("unique_submodules", 0)}\n')
                f.write(f'Total submodule instances:       {self.metrics.get("total_submodule_instances", 0)}\n')
                f.write(f'RTL module lines:                {self.metrics.get("rtl_line_count", 0)}\n')
                f.write(f'Module type:                     {self.metrics.get("module_type", "Unknown")}\n\n')

                # Assertion Metrics
                f.write('ASSERTION METRICS\n')
                f.write('-' * 40 + '\n')
                f.write(f'Initial assertions:              {self.metrics.get("initial_assertions", 0)}\n')
                f.write(f'After syntax correction:         {self.metrics.get("assertions_after_syntax_correction", 0)}\n')
                f.write(f'After property set expansion:    {self.metrics.get("assertions_after_property_expansion", 0)}\n')
                f.write(f'Added during expansion:          {self.metrics.get("assertions_added_during_expansion", 0)}\n')
                f.write(f'Final assertions:                {self.metrics.get("final_assertions", 0)}\n\n')

                # Automatic Fixes
                f.write('AUTOMATIC SYNTACTICAL FIXES\n')
                f.write('-' * 40 + '\n')
                f.write(f'Internal port binds:             {self.metrics.get("internal_port_binds", 0)}\n')
                f.write(f'Duplicate name fixes:            {self.metrics.get("duplicate_name_fixes", 0)}\n')
                f.write(f'Total automatic fixes:           {self.metrics.get("internal_port_binds", 0) + self.metrics.get("duplicate_name_fixes", 0)}\n\n')

                # Verification Results
                f.write('VERIFICATION RESULTS (Before Semantic Correction)\n')
                f.write('-' * 40 + '\n')
                f.write(f'Passing assertions:              {self.metrics.get("passing_assertions_before_semantic", 0)}\n')
                f.write(f'Failing assertions:              {self.metrics.get("failing_assertions_before_semantic", 0)}\n')
                f.write(f'Undefined assertions:            {self.metrics.get("undefined_assertions_before_semantic", 0)}\n\n')

                if self.execution == 'golden':
                    f.write('VERIFICATION RESULTS (After Semantic Correction)\n')
                    f.write('-' * 40 + '\n')
                    f.write(f'Passing assertions:              {self.metrics.get("passing_assertions_after_semantic", 0)}\n')
                    f.write(f'Failing assertions:              {self.metrics.get("failing_assertions_after_semantic", 0)}\n')
                    f.write(f'Undefined assertions:            {self.metrics.get("undefined_assertions_after_semantic", 0)}\n\n')

                    f.write('SEMANTIC CORRECTION RESULTS\n')
                    f.write('-' * 40 + '\n')
                    f.write(f'Successfully corrected:          {self.metrics.get("semantic_corrected", 0)}\n')
                    f.write(f'Failed to correct:               {self.metrics.get("semantic_failed", 0)}\n\n')

                # Timing Information
                f.write('EXECUTION TIMING\n')
                f.write('-' * 40 + '\n')
                f.write(f'Total execution time:            {total_time:.2f}s\n')
                f.write(f'Preprocessing:                   {preprocessing_time:.2f}s ({preprocessing_time/total_time*100:.1f}%)\n')
                f.write(f'Syntax correction:               {syntax_time:.2f}s ({syntax_time/total_time*100:.1f}%)\n')
                f.write(f'Property set extension:          {extension_time:.2f}s ({extension_time/total_time*100:.1f}%)\n')
                if self.execution == 'golden':
                    f.write(f'Semantic correction:             {semantic_time:.2f}s ({semantic_time/total_time*100:.1f}%)\n')
                f.write(f'Core processing time:            {core_time:.2f}s ({core_time/total_time*100:.1f}%)\n')

                # Fine-grained timing metrics
                jasper_time = self.metrics.get('total_formal_time', 0.0)
                llm_time = self.metrics.get('total_llm_time', 0.0)
                f.write(f'Formal tool time:                {jasper_time:.2f}s ({jasper_time/total_time*100:.1f}%)\n')
                f.write(f'LLM time:                        {llm_time:.2f}s ({llm_time/total_time*100:.1f}%)\n')

                # Show breakdown of core time
                other_time = core_time - jasper_time - llm_time
                if other_time > 0:
                    f.write(f'Other processing time:           {other_time:.2f}s ({other_time/total_time*100:.1f}%)\n')
                f.write('\n')

                # Token Usage
                total_input_tokens = self.metrics.get('total_input_tokens', 0)
                total_output_tokens = self.metrics.get('total_output_tokens', 0)
                total_tokens = total_input_tokens + total_output_tokens
                llm_calls = self.metrics.get('llm_calls', 0)
                syntax_api_calls = self.metrics.get('syntax_api_requests', 0)
                semantic_api_calls = self.metrics.get('semantic_api_requests', 0)

                f.write('LLM API USAGE STATISTICS\n')
                f.write('-' * 40 + '\n')
                f.write(f'Total input tokens:              {total_input_tokens:,}\n')
                f.write(f'Total output tokens:             {total_output_tokens:,}\n')
                f.write(f'Total tokens:                    {total_tokens:,}\n')
                f.write(f'Total API calls:                 {llm_calls}\n')
                f.write(f'Syntax correction API calls:     {syntax_api_calls}\n')
                f.write(f'Semantic correction API calls:   {semantic_api_calls}\n')

                if llm_calls > 0:
                    f.write(f'Avg input tokens per call:       {total_input_tokens/llm_calls:.1f}\n')
                    f.write(f'Avg output tokens per call:      {total_output_tokens/llm_calls:.1f}\n')
                    f.write(f'Avg total tokens per call:       {total_tokens/llm_calls:.1f}\n')

                f.write('\n' + '=' * 80 + '\n')

            self._log('medium', f'Comprehensive results saved to: {timing_file}')

        except Exception as e:
            self._log('medium', f'Error saving comprehensive results: {str(e)}')

    def _load_specification_files(self):
        """
        Load specification files (markdown and PNG diagrams) from specs/module_name directory.

        Returns:
            dict: Dictionary containing specification content with keys 'markdown' and 'diagrams'
        """
        try:
            module_name = self._extract_module_name(self.rtl_module)
            specs_dir = os.path.join('specs', module_name)

            self._log('medium', f'\nLoading specification files from: {specs_dir}')

            if not os.path.exists(specs_dir):
                self._log('medium', f'⚠️  Specifications directory not found: {specs_dir}')
                return {'markdown': '', 'diagrams': []}

            specs_content = {'markdown': '', 'diagrams': []}

            # Load all files in the specs directory
            for filename in os.listdir(specs_dir):
                file_path = os.path.join(specs_dir, filename)

                if filename.lower().endswith('.md'):
                    # Load markdown specification file
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            markdown_content = f.read()
                            specs_content['markdown'] += f"\n=== {filename} ===\n{markdown_content}\n"
                            self._log('medium', f'✅ Loaded markdown specification: {filename}')
                    except Exception as e:
                        self._log('medium', f'⚠️  Error reading markdown file {filename}: {str(e)}')

                elif filename.lower().endswith('.png'):
                    # Record PNG diagram files (we'll reference them in the prompt)
                    specs_content['diagrams'].append(filename)
                    self._log('medium', f'✅ Found diagram: {filename}')

            if not specs_content['markdown'] and not specs_content['diagrams']:
                self._log('medium', f'⚠️  No specification files found in {specs_dir}')
                return {'markdown': '', 'diagrams': []}

            # Summary of loaded specs
            self._log('medium', f'\n📋 Specification Summary:')
            if specs_content['markdown']:
                markdown_lines = len(specs_content['markdown'].split('\n'))
                self._log('medium', f'   Markdown content: {markdown_lines} lines')
            if specs_content['diagrams']:
                self._log('medium', f'   Diagrams found: {len(specs_content["diagrams"])} files')
                for diagram in specs_content['diagrams']:
                    self._log('medium', f'     - {diagram}')

            return specs_content

        except Exception as e:
            self._log('medium', f'Error loading specification files: {str(e)}')
            import traceback
            traceback.print_exc()
            return {'markdown': '', 'diagrams': []}

    def _fix_unproven_assertions_validation_mode(self):
        """
        Validation mode semantic correction: Fix unproven assertions with specification-based prompts.
        Limited to 1 attempt per assertion, using specification files instead of RTL.
        """
        self._log('medium', '\n🎯 Starting validation mode semantic correction...')
        self._log('medium', 'Using specification files instead of RTL for context')
        self.llm_stage = 'semantic_repair_validation'

        # Load specification files
        specs_content = self._load_specification_files()

        if not specs_content['markdown'] and not specs_content['diagrams']:
            self._log('medium', '⚠️  No specification files available - skipping validation semantic correction')
            return

        # Store property file before semantic correction
        self._store_property_version('before_validation_semantic')

        # Ensure assertions_list is populated
        if not self.assertions_list:
            self._log('medium', 'Populating assertions_list from current assertions...')
            self.assertions_list = self.assertions.copy()
            self._log('medium', f'Found {len(self.assertions_list)} assertions')

        # Run formal tool to get fresh results
        self._log('medium', 'Running formal tool to get fresh property results...')

        try:
            directory = self._get_formal_project_dir()
            file_path = self._get_formal_log_path()

            self._log('debug', directory)

            # Remove files from previous run
            p = subprocess.run(['rm', '-rf', directory], check=True)

            # Call the formal tool
            formal_start_time = time.time()
            p = self._run_formal_tool()
            formal_end_time = time.time()
            formal_call_time = formal_end_time - formal_start_time
            self.metrics['total_formal_time'] += formal_call_time
            self._log('debug', f"Formal tool call took {formal_call_time:.2f} seconds")

            while not os.path.isfile(file_path):
                time.sleep(3)
                self._log('medium', 'Waiting for formal tool log to be generated...')

            # Get current property results
            prop_results = self._get_prop_result()

            # Find all unproven assertions
            unproven_assertions = []
            proven_assertions = []  # Store proven assertions separately

            for assertion in self.assertions_list:
                # Extract assertion name
                match = re.search(r'(\w+):\s*assert', assertion)
                if not match:
                    continue

                assertion_name = match.group(1)

                # Check if this assertion has a counterexample
                if assertion_name in prop_results and prop_results[assertion_name] == 'cex':
                    unproven_assertions.append((assertion_name, assertion))
                else:
                    proven_assertions.append(assertion)  # Keep track of proven assertions

            if not unproven_assertions:
                self._log('medium', 'No unproven assertions found to fix')
                return

            self._log('medium', f'\n📊 Validation Mode Semantic Correction Summary:')
            self._log('medium', f'   Unproven assertions to fix: {len(unproven_assertions)}')
            self._log('medium', f'   Proven assertions: {len(proven_assertions)}')
            self._log('medium', f'   Attempts per assertion: 1 (validation mode limit)')

            # Store successful fixes
            fixed_assertions = []

            # Dictionary to store correction attempts for each assertion
            correction_attempts = {}

            _val_corr_sep = '=' * 70
            _val_corr_n = len(unproven_assertions)

            # Process each unproven assertion (validation mode: only 1 attempt each)
            for prop_idx, (target_name, target_assertion) in enumerate(unproven_assertions):
                if prop_idx > 0:
                    self._log('medium', '\n' + _val_corr_sep)
                    self._log('medium', ' NEXT PROPERTY ')
                    self._log('medium', _val_corr_sep + '\n')
                self._log('medium', f'\n🔧 Processing unproven assertion: {target_name} ({prop_idx + 1}/{_val_corr_n})')
                self._log('medium', f'Original assertion: {target_assertion}')
                self._log('medium', f'Validation mode: Single attempt correction')

                # Create temporary property file with just this assertion
                temp_assertions = [target_assertion]
                self.assertions = temp_assertions
                self._write_assertions()

                # Build specification-based prompt (no RTL, no previous attempts)
                module_name = self._extract_module_name(self.rtl_module)
                rules_text = rules.replace('MODULE', module_name)

                # Construct specification context
                spec_context = ""
                if specs_content['markdown']:
                    spec_context += f"\nModule Specification:\n{specs_content['markdown']}\n"

                if specs_content['diagrams']:
                    spec_context += f"\nAvailable Diagrams (reference these when relevant):\n"
                    for diagram in specs_content['diagrams']:
                        spec_context += f"   - {diagram}\n"

                tool_display = 'VC Formal' if self.formal_tool == 'vcformal' else 'JasperGold'
                repair_context = self._semantic_repair_context_block(
                    target_name, target_assertion,
                    proven_assertions=proven_assertions,
                )
                prompt = (
                    f'{rules_text}'
                    f'Example output format:\n{STRUCTURED_OUTPUT_EXAMPLE}\n'
                    f'{self._instantiation_context_block()}'
                    f'{repair_context}'
                    f'The following assertion has not been proven by {tool_display}:\n'
                    f'{target_assertion}\n\n'
                    'VALIDATION MODE CORRECTION:\n'
                    'You have ONE attempt to fix this assertion based on the module specification.\n'
                    f'{spec_context}\n'
                    'REQUIREMENTS:\n'
                    '- Return ONLY ONE assertion block in structured --- id/property/failure --- format\n'
                    '- Keep the same assertion id\n'
                    '- Base corrections on the specification, not implementation details\n'
                    '- Use unqualified checker port and parameter names only; do NOT write '
                    f'{module_name}.signal\n'
                    '- Prefer formal-core / CEX signals named above\n'
                    'NOTE: This is validation mode - you will not see the RTL implementation.\n'
                )

                self._log('high', '\n\nValidation Mode Prompt for LLM:')
                self._log('high', prompt)

                response = self._get_llm_completion([
                    {'role': 'system', 'content': 'You are an expert in SystemVerilog Assertions and specification-based verification. Your responses should focus on specification compliance rather than implementation details. Provide detailed analysis based on the module specification provided.'},
                    {'role': 'user', 'content': prompt}
                ])

                # Track semantic API request for validation mode
                self._update_metrics('semantic_api_requests')

                self._log('medium', '\n\nValidation Mode LLM Response:')
                self._log('medium', response)

                # Store the prompt-response pair with validation mode tag
                self._store_llm_interaction('validation_correction', 1, prompt, response, target_name)

                corrected_assertions = self._extract_assertions(response)

                if not corrected_assertions:
                    self._log('medium', f'❌ No valid assertions found in response for {target_name}')
                    fixed_assertions.append(target_assertion)  # Keep original if no correction found
                    correction_attempts[target_name] = [target_assertion]
                    continue

                self._log('medium', f'\n📝 Found {len(corrected_assertions)} assertions in response:')
                for i, assertion in enumerate(corrected_assertions):
                    self._log('medium', f'Assertion {i+1}: {assertion}')

                # Use the last assertion from the response (validation mode: single attempt)
                selected_assertion = corrected_assertions[-1]
                self._log('medium', f'\n✅ Selected assertion: {selected_assertion}')

                # Store this attempt
                correction_attempts[target_name] = [selected_assertion]

                # Update the assertions list with the corrected version
                self.assertions = [selected_assertion]

                # Write to property file and verify
                self._write_assertions()

                # Run formal tool again to check if the assertion is now proven
                self._log('medium', f'🔍 Verifying corrected assertion for {target_name}...')
                formal_start_time = time.time()
                p = self._run_formal_tool()
                formal_end_time = time.time()
                formal_call_time = formal_end_time - formal_start_time
                self.metrics['total_formal_time'] += formal_call_time
                self._log('debug', f"Formal tool verification took {formal_call_time:.2f} seconds")

                # Wait for log file to be generated
                while not os.path.isfile(file_path):
                    time.sleep(3)
                    self._log('medium', 'Waiting for formal tool log to be generated...')

                # Check if the assertion is now proven
                prop_results = self._get_prop_result()
                if target_name in prop_results and prop_results[target_name] == 'proven':
                    self._log('medium', '\n' + _val_corr_sep)
                    self._log('medium',
                        f'  ✅  CORRECTION SUCCEEDED — {target_name} is PROVEN (validation mode, 1 attempt)')
                    self._log('medium', _val_corr_sep)
                    self._log('medium', f'✅ Successfully fixed assertion {target_name} in validation mode')
                    fixed_assertions.append(selected_assertion)
                else:
                    self._log('medium', '\n' + '-' * 70)
                    self._log('medium',
                        f'  NOT PROVEN — {target_name} after validation correction (1 attempt)')
                    self._log('medium', '-' * 70)
                    self._log('medium', f'❌ Failed to fix assertion {target_name} in validation mode')
                    fixed_assertions.append(target_assertion)  # Keep original if not fixed

            # Update assertions_list with fixes applied
            self.assertions_list = proven_assertions + fixed_assertions
            self.assertions = proven_assertions + fixed_assertions
            self._write_assertions()

            # Run formal tool with all final assertions to get complete results
            self._log('medium', '\n🔍 Running final formal tool verification with all assertions...')
            try:
                # Remove files from previous run
                p = subprocess.run(['rm', '-rf', directory], check=True)

                # Call the formal tool for all final assertions
                formal_start_time = time.time()
                p = self._run_formal_tool()
                formal_end_time = time.time()
                formal_call_time = formal_end_time - formal_start_time
                self.metrics['total_formal_time'] += formal_call_time
                self._log('debug', f"Final formal tool call took {formal_call_time:.2f} seconds")

                # Wait for log file to be generated
                while not os.path.isfile(file_path):
                    time.sleep(3)
                    self._log('medium', 'Waiting for final formal tool log to be generated...')

                # Get final comprehensive property results
                final_prop_results = self._get_prop_result()
                self._log('medium', f'📊 Final validation results collected for {len(final_prop_results)} assertions')

            except subprocess.CalledProcessError as e:
                self._log('medium', f'❌ Error running final formal tool verification: {e}')
                # Fallback to existing results if final run fails
                final_prop_results = self._get_prop_result()

            # Count successful and failed semantic corrections for validation mode
            successful_corrections = 0
            failed_corrections = 0

            # Check final results to count successes and failures
            for assertion_name, _ in unproven_assertions:
                if assertion_name in final_prop_results and final_prop_results[assertion_name] == 'proven':
                    successful_corrections += 1
                else:
                    failed_corrections += 1

            # Update metrics for validation mode
            self._update_metrics('semantic_corrected', successful_corrections, increment=False)
            self._update_metrics('semantic_failed', failed_corrections, increment=False)

            self._log('medium', f'\n📋 VALIDATION MODE SEMANTIC CORRECTION RESULTS')
            self._log('medium', f'   Successfully corrected assertions: {successful_corrections}')
            self._log('medium', f'   Failed to correct assertions: {failed_corrections}')
            self._log('medium', f'   Total unproven assertions processed: {len(unproven_assertions)}')
            self._log('medium', f'   Success rate: {(successful_corrections/len(unproven_assertions)*100):.1f}%' if unproven_assertions else 'N/A')

            # Print summary of all correction attempts
            self._log('medium', '\n📝 VALIDATION MODE CORRECTION ATTEMPTS SUMMARY')
            for assertion_name, attempts in correction_attempts.items():
                self._log('medium', f'\nAssertion: {assertion_name}')
                self._log('medium', 'Original assertion:')
                self._log('medium', next(a[1] for a in unproven_assertions if a[0] == assertion_name))
                self._log('medium', '\nValidation mode correction:')
                self._log('medium', f'{attempts[0]}')
                self._log('medium', '-' * 70)

            # Verify final state
            with open(self.assertions_dir + self.assertions_file, 'r') as f:
                file_contents = f.read()
                self._log('high', '\nFinal property file contents after validation mode semantic correction:')
                self._log('high', file_contents)

        except subprocess.CalledProcessError as e:
            self._log('medium', f'❌ Program execution failed during validation semantic correction: {e}')
            return

        # Store property file after validation mode semantic correction
        self._store_property_version('after_validation_semantic_correction')

        # Create proven-only property file for validation mode
        self._create_proven_only_property_file()

def parse_arguments():
    parser = argparse.ArgumentParser(description='Run the CodingAgent for SystemVerilog Assertions')

    parser.add_argument('--rtl-source',
                      default='hl_mul_unit.sv',
                      help='Path to the RTL source file (supports both .sv and .v extensions)')

    parser.add_argument('--llm',
                      default='meta/llama-3.1-405b-instruct',
                      help='LLM model to use')

    parser.add_argument('--prompting-strategy',
                      default='zero-shot',
                      choices=['zero-shot', 'few-shot'],
                      help='Prompting strategy to use')

    parser.add_argument('--module-type',
                      default='combinational',
                      choices=['combinational', 'sequential'],
                      help='Type of module to process')

    parser.add_argument('--verbosity',
                      type=int,
                      default=2,
                      choices=[1, 2, 3, 4],
                      help='Verbosity level (1=low, 2=medium, 3=high, 4=debug)')

    parser.add_argument('--execution',
                      default='golden',
                      choices=['golden', 'validation'],
                      help='Execution mode (golden: full semantic correction with RTL context and up to 5 attempts per assertion; validation: specification-based semantic correction with 1 attempt per assertion using specs/module_name files)')

    parser.add_argument('--formal-tool',
                      default='jaspergold',
                      choices=['jaspergold', 'vcformal'],
                      help='Formal verification tool to use (jaspergold: uses run_jg_batch.sh; vcformal: uses run_vcf_batch.sh)')

    parser.add_argument('--stop-after-extension',
                      action='store_true',
                      help='Stop after scaffold, generate, and set extension')

    parser.add_argument('--resume-from-semantic',
                      action='store_true',
                      help='Skip generate/extension and start at semantic correction')

    parser.add_argument('--resume-from-extension',
                      action='store_true',
                      help='Reuse the existing after-syntax set and start at set extension')

    args = parser.parse_args()
    if args.resume_from_semantic and args.resume_from_extension:
        parser.error('--resume-from-semantic and --resume-from-extension cannot be combined')
    if args.stop_after_extension and args.resume_from_semantic:
        parser.error('--stop-after-extension and --resume-from-semantic cannot be combined')

    # Derive assertions file and directory from RTL source name
    rtl_name = os.path.splitext(os.path.basename(args.rtl_source))[0]
    args.assertions_file = f'{rtl_name}_prop.sv'
    args.assertions_dir = f'ft_{rtl_name}/sva/'

    return args

def main():
    # Parse command line arguments
    args = parse_arguments()

    # Find the model configuration
    model_config = resolve_model_config(args.llm)
    if not model_config:
        raise ValueError(f"Model {args.llm} not found in configuration")

    # Get appropriate API key based on model type
    if model_config["type"] == "nvidia":
        api_key = os.environ.get('NVIDIA_API_KEY')
        if not api_key:
            raise ValueError("NVIDIA_API_KEY environment variable not set")
    elif model_config["type"] in ("openai", "openai_responses"):
        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")
    elif model_config["type"] == "cursor_sdk":
        api_key = os.environ.get('CURSOR_API_KEY')
        if not api_key:
            raise ValueError("CURSOR_API_KEY environment variable not set")
    elif model_config["type"] in ("copilot_cli", "cursor_cli", "vertex"):
        api_key = None
    else:
        raise ValueError(f"Unsupported model type: {model_config['type']}")

    # Create and run the agent
    agent = CodingAgent(
        rtl_source=args.rtl_source,
        assertions_file=args.assertions_file,
        assertions_dir=args.assertions_dir,
        llm=args.llm,
        prompting_strategy=args.prompting_strategy,
        module_type=args.module_type,
        verbosity=args.verbosity,
        api_key=api_key,
        execution=args.execution,
        formal_tool=args.formal_tool,
        stop_after_extension=args.stop_after_extension,
        resume_from_semantic=args.resume_from_semantic,
        resume_from_extension=args.resume_from_extension,
    )

    agent.run_agent()

if __name__ == '__main__':
    main()