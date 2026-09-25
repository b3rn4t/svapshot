#!/usr/bin/env python3

import os
import sys
import time
import subprocess
import argparse
import tempfile
import re
import json

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(CORE_DIR)
REPO_ROOT = os.path.dirname(SRC_DIR)
ANALYSIS_DIR = os.path.join(SRC_DIR, 'analysis')
for _path in (REPO_ROOT, ANALYSIS_DIR, CORE_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cli_log
import coi
import rtl_clocking
try:
    from openai import OpenAI
except ImportError:  # unit tests and LLM-free jobs import this module without the package
    OpenAI = None
from rtl_clocking import looks_like_clock
from assertion_template import (
    TEMPLATE_RULES,
    STRUCTURED_OUTPUT_EXAMPLE,
    process_llm_output_to_sv,
    extract_assertions_from_response,
    get_assertion_name,
    rewrite_assertions_without_clock_events,
    _extract_assembled_assertions,
)
from instantiation_params import (
    build_contexts_for_parent,
    format_instantiation_prompt,
    format_vcs_pvalue_string,
    read_elaboration_overrides,
    write_elaboration_overrides,
)
from llm_cost import extract_usage

#: How long one blocking VC Formal check command may run, and how long any one
#: property inside it may take. VC Formal's own defaults are twelve hours and
#: unlimited, which on a large design means the flow can wait all day and
#: receive no result to qualify. Written into the generated TCL, where
#: SVAPSHOT_FML_MAX_TIME and SVAPSHOT_FML_PROPERTY_TIME override them.
VCF_COMMAND_TIME_BUDGET = '1H'
VCF_PROPERTY_TIME_BUDGET = '5M'


def _initial_assertions_path() -> str:
    """Return the run-scoped draft path when orchestration provides one."""
    return os.environ.get(
        'SVAPSHOT_INITIAL_ASSERTIONS_PATH',
        os.path.join(os.getcwd(), 'initial_assertions'),
    )


def _count_assertions(text: str) -> int:
    """Count assertions in structured or assembled SVA text."""
    if not text.strip():
        return 0
    if 'id:' in text and '---' in text:
        return len(extract_assertions_from_response(text))
    if 'assert property' in text.lower():
        return len(re.findall(r'^\s*\w+\s*:\s*assert\s+property', text, re.MULTILINE | re.IGNORECASE))
    return 0


def _prepare_initial_assertions(text: str, module_type: str = 'sequential') -> str:
    """Convert structured LLM output to assembled template-based SVA."""
    stripped = text.strip()
    if not stripped:
        return ''
    if re.search(r'^\s*\w+\s*:\s*assert\s+property', stripped, re.MULTILINE | re.IGNORECASE):
        prepared = stripped
    else:
        prepared = process_llm_output_to_sv(stripped, module_type=module_type)
    if module_type == 'combinational' and prepared:
        prepared = rewrite_assertions_without_clock_events(prepared)
    return prepared


def _cap_initial_assertions(text: str, module_type: str = 'sequential') -> str:
    """Trim the set that enters the checker. The LLM dump file stays intact.

    DATE cells export ``SVAPSHOT_MAX_ASSERTIONS`` from
    ``generation.max_assertions``. The cap used to apply only at set
    extension, so a large initial dump was syntax-checked in full and a
    license miss left that whole set in the snapshot.
    """
    from agent import assertion_cap

    cap = assertion_cap()
    if cap is None or not text.strip():
        return text
    blocks = _extract_assembled_assertions(text, module_type=module_type)
    if not blocks or len(blocks) <= cap:
        return text
    print(
        f'⚠️  Assertion cap: keeping {cap} of {len(blocks)} initial '
        'assertions for syntax correction')
    return '\n\n'.join(blocks[:cap]) + '\n'


def _print_startup_banner():
    """Print SVApshot startup banner."""
    try:
        from assets.svapshot_banner import print_startup_banner
        print_startup_banner()
    except ImportError:
        print("SVApshot")
        print()


def _argv_with_default_verbosity(argv=None):
    """Append ``low`` when the trailing positional is an execution type.

    ``module_type`` is already an optional positional. Making ``verbosity``
    optional too breaks ``… golden medium`` under argparse, so the default is
    injected here instead.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg == '--verbosity' or arg.startswith('--verbosity=') for arg in argv):
        return argv
    positionals = [arg for arg in argv if not arg.startswith('-')]
    if positionals and positionals[-1] in ('golden', 'validation'):
        argv.append('low')
    return argv


def parse_arguments(argv=None):
    """Parse command line arguments for the orchestrated execution."""
    parser = argparse.ArgumentParser(description='AI-SVA: Orchestrated execution of SVA generation and verification')

    parser.add_argument('rtl_module',
                       help='RTL source module (e.g., hl_mul_unit.sv)')

    parser.add_argument('llm_model',
                       help='LLM model to use (e.g., meta/llama-3.1-405b-instruct)')

    # Detected from the RTL, not asked for: a wrong answer here produces a
    # property file that cannot elaborate, and the caller has no way of knowing
    # that a submodule is clocked differently from its parent. Still accepted so
    # existing command lines keep working, and honoured only when the RTL cannot
    # be classified at all.
    parser.add_argument('module_type',
                       nargs='?',
                       choices=['auto', 'sequential', 'combinational'],
                       default='auto',
                       help='Deprecated: the module type is detected from the RTL. '
                            'An explicit value is used only if detection fails.')

    parser.add_argument('execution_type',
                       choices=['golden', 'validation'],
                       help='Type of execution (golden: full semantic correction with RTL context and up to 5 attempts; validation: specification-based semantic correction with 1 attempt using specs/module_name files)')

    # Verbosity stays required for argparse matching with optional module_type.
    # When omitted on the command line, ``_argv_with_default_verbosity`` injects
    # ``low`` so quiet STEP progress is the default.
    parser.add_argument('verbosity',
                       choices=['low', 'medium', 'high', 'debug'],
                       help='Verbosity level (default when omitted: low — STEP '
                            'progress only; medium/high/debug restore the detailed trail)')

    parser.add_argument('--sources', '-src',
                       nargs='+',
                       default=['sources'],
                       help='Source directories (default: sources)')

    parser.add_argument('--includes', '-i',
                       default='packages',
                       help='Include directory (default: packages)')

    parser.add_argument('--assertion-source', '-as',
                       choices=['rtl', 'file'],
                       default='rtl',
                       help='Source of initial assertions: "rtl" to prompt the LLM with RTL and SVA rules, or "file" to read an initial file (default: rtl)')

    parser.add_argument('--initial-file', '-if',
                       default='initial_assertions',
                       help='File containing initial assertions when using --assertion-source file (default: initial_assertions)')

    parser.add_argument('--disable-package-detection', '-dpd',
                       action='store_true',
                       help='Disable automatic package detection for manual_sub.vc files')

    parser.add_argument('--formal-tool', '-ft',
                       choices=['jaspergold', 'vcformal'],
                       default='jaspergold',
                       help='Formal verification tool to use (default: jaspergold)')

    parser.add_argument('--stop-after-extension',
                       action='store_true',
                       help='Stop after scaffold, generate, and set extension')

    parser.add_argument('--stop-after-scaffold',
                       action='store_true',
                       help='Build ft_<module>/ and FPV_vcf.tcl, then stop (one-shot)')

    parser.add_argument('--resume-from-semantic',
                       action='store_true',
                       help='Reuse the existing harness and resume at semantic correction')

    parser.add_argument('--resume-from-extension',
                       action='store_true',
                       help='Reuse the existing after-syntax set and resume at set extension')

    # The checker is SystemVerilog whatever the design is; this says only what
    # the .v files of a Verilog design are compiled as.
    parser.add_argument('--parent-rtl',
                       default='',
                       help='Parent RTL used only to resolve instantiation '
                            'parameters and FPV -pvalue for this leaf. The leaf '
                            'is still the formal top; the parent is not generated.')

    parser.add_argument('--verilog-std',
                       choices=['1995', '2001'],
                       default='2001',
                       help='Standard .v sources are compiled as (default: 2001). '
                            'Use 1995 for a design that uses a Verilog-2001 '
                            'keyword such as generate or localparam as an identifier.')

    normalized = _argv_with_default_verbosity(argv)
    args = parser.parse_args(normalized)
    if args.resume_from_semantic and args.resume_from_extension:
        parser.error('--resume-from-semantic and --resume-from-extension cannot be combined')
    if args.stop_after_extension and args.resume_from_semantic:
        parser.error('--stop-after-extension and --resume-from-semantic cannot be combined')
    if args.stop_after_scaffold and (
            args.stop_after_extension or args.resume_from_semantic
            or args.resume_from_extension):
        parser.error('--stop-after-scaffold cannot be combined with resume/extension flags')
    return args


def _ordered_input_ports(text, module_name='', language='sv'):
    """Input ports in declaration order, which is how a clock is picked when the
    module has no clocked block of its own to name one."""
    graph = coi.build_signal_graph(text, module_name, language)
    _, _, port_list, _ = coi.split_module_header(
        coi.strip_sv_noise(text), module_name)
    seen, ordered = set(), []
    for name in re.findall(r'[A-Za-z_]\w*', port_list):
        if name in graph.ports_in and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def detect_clocking(rtl_file):
    """Read a module's clock and reset out of the RTL, or None if there is no
    module in the file.

    Every stage that needs to know whether a module is sequential asks this, so
    that a submodule and its parent are judged the same way and neither depends
    on what the caller typed on the command line.
    """
    text = _read_text(rtl_file)
    if not text or not re.search(r'\bmodule\s+\w', rtl_clocking.strip_noise(text)):
        return None
    # The DUT is the module the file is named after, which matters for a Verilog
    # source: one file there may declare several modules.
    module_name = os.path.splitext(os.path.basename(rtl_file))[0]
    ports = _ordered_input_ports(text, module_name, coi.language_of(rtl_file))
    return rtl_clocking.analyze_clocking(text, ports)


def detect_module_type(rtl_file):
    """'sequential' or 'combinational' for a module, or None if there is none.

    The label decides whether the property file gets a clocking block, so it is
    derived from the RTL: first from the module's clocked blocks, and only then
    from its port names.
    """
    clocking = detect_clocking(rtl_file)
    if clocking is None:
        return None
    return 'sequential' if clocking.is_sequential else 'combinational'


def resolve_module_type(rtl_file, requested='auto'):
    """The module type to build a testbench with, and why.

    Detection wins over the request. A caller can only state one type per run,
    which is wrong as soon as the design has a combinational submodule under a
    sequential parent, and a mismatch is not reported by anything downstream: it
    surfaces as a property file that will not elaborate.
    """
    detected = detect_module_type(rtl_file)
    module = os.path.basename(rtl_file)

    if detected is None:
        fallback = 'combinational' if requested == 'auto' else requested
        print(f"⚠️  No module found in {module}; assuming {fallback}.")
        return fallback

    if requested not in ('auto', detected):
        clocking = detect_clocking(rtl_file)
        if clocking.evidence == rtl_clocking.FROM_CLOCKED_BLOCK:
            why = f"its clocked blocks run on {clocking.clock}"
        elif clocking.is_sequential:
            why = f"it has a clock port, {clocking.clock}"
        else:
            why = 'it has no clock'
        print(f"⚠️  {module} was requested as {requested} but {why}.")
        print(f"   Building a {detected} testbench; the flag is no longer needed.")

    return detected


def _is_within(path, directory):
    """True when ``path`` lies inside ``directory``.

    A plain string prefix is not enough: ``/proj/rtl_extra/core.sv`` starts with
    ``/proj/rtl`` without being inside it, and treating it as contained yields a
    DUT_ROOT that the module escapes with ``../``.
    """
    path = os.path.abspath(path)
    directory = os.path.abspath(directory)
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


def compute_dut_root_and_relative_path(rtl_module, sources):
    """Compute the correct DUT_ROOT and relative module path for scaffolding.

    Args:
        rtl_module (str): Full path to the RTL module
        sources (list): List of source directories

    Returns:
        tuple: (dut_root, relative_module_path)
    """
    rtl_module_abs = os.path.abspath(rtl_module)

    # Try to find which source directory contains this module
    for source_dir in sources:
        source_abs = os.path.abspath(source_dir)
        if _is_within(rtl_module_abs, source_abs):
            # Module is within this source directory
            relative_path = os.path.relpath(rtl_module_abs, source_abs)
            print(f"📍 Module path analysis:")
            print(f"   Full path: {rtl_module_abs}")
            print(f"   DUT_ROOT: {source_abs}")
            print(f"   Relative: {relative_path}")
            return source_abs, relative_path

    # If not found in any source directory, use the module's directory as DUT_ROOT
    module_dir = os.path.dirname(rtl_module_abs)
    module_file = os.path.basename(rtl_module_abs)
    print(f"📍 Module path analysis (fallback):")
    print(f"   Full path: {rtl_module_abs}")
    print(f"   DUT_ROOT: {module_dir}")
    print(f"   Relative: {module_file}")
    return module_dir, module_file

def run_scaffold(rtl_module, sources, includes, module_type):
    """Build the ``ft_<module>/`` harness via the native Python 3 scaffolder.

    Native Python 3 harness scaffold; does not shell out to an external tool.
    """
    from scaffold import ScaffoldError, scaffold_harness

    with cli_log.get().step("🔧 STEP 1: Scaffolding formal harness (ft_<module>/)") as step:
        # Compute the correct DUT_ROOT and relative path for this module
        dut_root, relative_module = compute_dut_root_and_relative_path(rtl_module, sources)

        # Idempotent when the caller already resolved it; the guard is here because
        # a clocking block on a module with no clock cannot elaborate, and this is
        # the last point before the scaffolder writes one.
        module_type = resolve_module_type(rtl_module, module_type)

        print(f"🔧 Original DUT_ROOT={os.environ.get('DUT_ROOT')}")
        original_dut_root = os.environ.get('DUT_ROOT')
        output_root = (
            os.environ.get('SVAPSHOT_ROOT')
            or os.getcwd()
        )

        try:
            os.environ['DUT_ROOT'] = dut_root
            print(f"🔧 Set DUT_ROOT={dut_root} for this module")
            print(
                f"Scaffolding: -f {relative_module}"
                + (f" -src {' '.join(sources)}" if sources else '')
                + (f" -i {includes}" if includes else '')
                + f" -mt {module_type}"
            )
            scaffold_harness(
                filename=relative_module,
                sources=sources or [],
                include=includes,
                module_type=module_type,
                dut_root=dut_root,
                output_root=output_root,
            )
            print("✅ Harness scaffold completed successfully")
            return True
        except ScaffoldError as exc:
            print(f"❌ Harness scaffold failed: {exc.message}")
            step.fail()
            return False
        except Exception as exc:
            print(f"❌ Harness scaffold failed: {exc}")
            step.fail()
            return False
        finally:
            if original_dut_root is not None:
                print(f"🔄 Restored DUT_ROOT={original_dut_root}")
            elif 'DUT_ROOT' in os.environ:
                del os.environ['DUT_ROOT']
                print("🔄 Cleared temporary DUT_ROOT")

def _model_config(llm_model):
    """The model table's entry for ``llm_model``, or None if it has none.

    Imported on call rather than at module level: ``agent.py`` is a heavy
    import, and a model this step does not recognise must still run the way it
    did before the table existed. Cursor ``composer-*`` / ``cursor:<id>``
    names are resolved here the same way Vertex ``gemini-*`` names are.
    """
    try:
        from agent import resolve_model_config
    except Exception:
        return None
    return resolve_model_config(llm_model)


def run_initial_assertion_generation(rtl_module, llm_model,
                                     instantiation_context='',
                                     module_type='sequential'):
    """Generate initial assertions directly from RTL and shared SVA rules.

    ``instantiation_context`` is the optional PARENT INSTANTIATION CONTEXT
    block for a direct submodule under a parent: without it the model only
    sees the leaf defaults and invents both polarities of config-gated
    properties, which then prove vacuously under the parent's overrides.
    """
    with cli_log.get().step("🧠 STEP 2: Generating initial assertions from RTL + SVA rules") as step:
        rtl_module_abs = os.path.abspath(rtl_module)
        print(f"📍 Processing RTL module: {rtl_module_abs}")

        try:
            with open(rtl_module_abs, 'r', encoding='utf-8', errors='replace') as handle:
                rtl_text = handle.read()
        except OSError as exc:
            print(f"❌ Could not read RTL module: {exc}")
            step.fail()
            return False

        if not rtl_text.strip():
            print("❌ RTL module is empty.")
            step.fail()
            return False

        if llm_model in ('copilot', 'cursor-cli'):
            print(f"❌ CLI model {llm_model} is not supported for initial generation.")
            step.fail()
            return False

        # Which endpoint a model is served by is a fact about the model, and the
        # table in agent.py is where the rest of the flow keeps it. Restating it
        # here is how this step came to send a reasoning model to
        # v1/chat/completions and fail the run with a 404 the moment the agent had
        # already been taught otherwise.
        config = _model_config(llm_model)
        is_vertex = (
            (bool(config) and config['type'] == 'vertex')
            or llm_model.startswith('gemini-')
        )
        is_cursor_sdk = bool(config) and config['type'] == 'cursor_sdk'
        is_responses_model = bool(config) and config['type'] == 'openai_responses'
        if config:
            is_nvidia = config['type'] == 'nvidia'
        else:
            is_nvidia = (
                llm_model.startswith(('nvdev/', 'meta/', 'deepseek-ai/', 'moonshotai/'))
                or os.environ.get('LLM_PROVIDER', '').lower() == 'nvidia'
            )
        if is_cursor_sdk:
            if not os.environ.get('CURSOR_API_KEY'):
                print("❌ CURSOR_API_KEY environment variable is not set.")
                step.fail()
                return False
        elif not is_vertex:
            api_key_name = 'NVIDIA_API_KEY' if is_nvidia else 'OPENAI_API_KEY'
            api_key = os.environ.get(api_key_name)
            if not api_key:
                print(f"❌ {api_key_name} environment variable is not set.")
                step.fail()
                return False
            client_args = {'api_key': api_key}
            if is_nvidia:
                client_args['base_url'] = 'https://integrate.api.nvidia.com/v1'

        module_name = os.path.splitext(os.path.basename(rtl_module_abs))[0]
        prompt = (
            TEMPLATE_RULES.replace('MODULE', module_name)
            + '\n'
            + STRUCTURED_OUTPUT_EXAMPLE
        )
        if instantiation_context and instantiation_context.strip():
            prompt += '\n\n' + instantiation_context.strip() + '\n'
            print("📎 Including parent instantiation parameter context in the prompt")
        prompt += '\n\nRTL MODULE:\n' + rtl_text
        specification_path = os.environ.get('SVAPSHOT_SPEC_PATH')
        if specification_path and os.path.isfile(specification_path):
            with open(specification_path, encoding='utf-8') as handle:
                prompt += '\n\nDESIGN SPECIFICATION:\n' + handle.read()

        try:
            request_started = time.time()
            if is_cursor_sdk:
                import cursor_llm
                content = cursor_llm.generate(
                    (config or {}).get('cursor_model') or llm_model,
                    prompt,
                )
                completion = None
            elif is_vertex:
                import vertex_llm
                content = vertex_llm.generate(
                    llm_model,
                    prompt,
                    max_output_tokens=int(
                        os.environ.get('SVAPSHOT_MAX_OUTPUT_TOKENS', '16384')),
                )
                completion = None
            elif OpenAI is None:
                raise ImportError(
                    'The openai package is required for LLM generation; '
                    'install build/requirements.txt')
            else:
                client = OpenAI(**client_args)
            if is_vertex or is_cursor_sdk:
                usage = None
            elif is_responses_model:
                # v1/responses takes one prompt rather than a message list, and
                # rejects the sampling parameters the chat endpoint expects.
                completion = client.responses.create(
                    model=llm_model,
                    reasoning={'effort': 'medium'},
                    input=prompt,
                    max_output_tokens=int(
                        os.environ.get('SVAPSHOT_MAX_OUTPUT_TOKENS', '16384')),
                )
                content = completion.output_text or ''
            else:
                completion_params = {
                    'model': llm_model,
                    'messages': [{'role': 'user', 'content': prompt}],
                }
                # OpenAI reasoning models (including o4-mini) use the newer parameter
                # name. They also use their default sampling configuration; sending the
                # legacy temperature/max_tokens pair causes a 400 before generation.
                if llm_model.startswith(('o1', 'o3', 'o4')):
                    completion_params['max_completion_tokens'] = 32768
                else:
                    completion_params['temperature'] = 0.2
                    completion_params['max_tokens'] = 32768
                completion = client.chat.completions.create(**completion_params)
                content = completion.choices[0].message.content or ''
            module_artifact_dir = os.path.join(
                os.getcwd(), f'ft_{module_name}', 'sva')
            os.makedirs(module_artifact_dir, exist_ok=True)
            with open(
                os.path.join(module_artifact_dir, 'initial_raw_response.txt'),
                'w',
                encoding='utf-8',
            ) as handle:
                handle.write(content)
            if not is_vertex and not is_cursor_sdk:
                usage = extract_usage(completion)
            usage_record = {
                'stage': 'initial_generation',
                'model': llm_model,
                'seconds': round(time.time() - request_started, 3),
                'exact': usage is not None,
                'input_tokens': (
                    usage['input_tokens'] if usage else len(prompt) // 4),
                'output_tokens': (
                    usage['output_tokens'] if usage else len(content) // 4),
            }
            usage_path = os.environ.get('PUBLICATION_LLM_USAGE_LOG')
            if usage_path:
                os.makedirs(os.path.dirname(os.path.abspath(usage_path)),
                            exist_ok=True)
                with open(usage_path, 'a', encoding='utf-8') as handle:
                    handle.write(json.dumps(usage_record, sort_keys=True) + '\n')
            fresh_assertions = _prepare_initial_assertions(
                content, module_type=module_type)

            if not fresh_assertions:
                print("❌ The model returned no parsable initial assertions.")
                step.fail()
                return False

            output_file = _initial_assertions_path()
            os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as handle:
                handle.write(fresh_assertions)
            print(f"📄 Initial assertions saved to: {output_file}")

            assertion_count = _count_assertions(fresh_assertions)
            print(f"📊 Generated {assertion_count} initial assertions")

            return True
        except Exception as exc:
            print(f"❌ Error during initial assertion generation: {exc}")
            step.fail()
            return False

def read_initial_file(initial_file, module_type='sequential'):
    """Read initial assertions from a provided file."""
    with cli_log.get().step("📄 STEP 2: Reading initial assertions from file") as step:
        if not os.path.exists(initial_file):
            print(f"❌ Initial file not found: {initial_file}")
            step.fail()
            return False

        try:
            with open(initial_file, 'r') as f:
                initial_assertions = f.read().strip()

            if not initial_assertions:
                print(f"❌ Initial file is empty: {initial_file}")
                step.fail()
                return False

            print(f"📖 Read assertions from: {initial_file}")

            initial_assertions = _prepare_initial_assertions(
                initial_assertions, module_type=module_type)
            if not initial_assertions:
                print(f"❌ No valid assertions found in: {initial_file}")
                step.fail()
                return False

            # Save the assertions to a standardized file for processing
            output_file = _initial_assertions_path()
            os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
            with open(output_file, 'w') as f:
                f.write(initial_assertions)

            print(f"📄 Assertions saved to: {output_file}")

            # Count assertions for reporting
            assertion_count = _count_assertions(initial_assertions)
            print(f"📊 Read {assertion_count} initial assertions")

            if assertion_count:
                blocks = [b for b in initial_assertions.split('\n\n') if b.strip()]
                print("\nFirst few assertions read:")
                for i, assertion in enumerate(blocks[:3], 1):
                    print(f"  {i}. {assertion[:100]}{'...' if len(assertion) > 100 else ''}")
                if assertion_count > 3:
                    print(f"  ... and {assertion_count - 3} more assertions")

            return True

        except Exception as e:
            print(f"❌ Error reading initial file: {str(e)}")
            step.fail()
            return False

def create_base_from_initial_assertions(rtl_module, module_type='sequential'):
    """Create base file from initial assertions and existing property file."""
    with cli_log.get().step("📄 STEP 3: Creating base file from initial assertions") as step:
        # Check if initial assertions file exists
        assertions_file = _initial_assertions_path()
        if not os.path.exists(assertions_file):
            print(f"❌ Initial assertions file not found: {assertions_file}")
            print("Make sure initial assertions step completed successfully.")
            step.fail()
            return False

        # Get module name and paths
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        ft_dir = f'ft_{module_name}'
        sva_dir = os.path.join(ft_dir, 'sva')
        prop_file_path = os.path.join(sva_dir, f'{module_name}_prop.sv')
        base_file_path = os.path.join(sva_dir, 'base')

        # Check if property file exists
        if not os.path.exists(prop_file_path):
            print(f"❌ Property file not found: {prop_file_path}")
            print("Make sure scaffold step completed successfully.")
            step.fail()
            return False

        try:
            # Read initial assertions
            with open(assertions_file, 'r') as f:
                raw_assertions = f.read().strip()

            initial_assertions = _prepare_initial_assertions(
                raw_assertions, module_type=module_type)
            if raw_assertions and not initial_assertions:
                preview = raw_assertions[:500]
                print("❌ Initial assertions file could not be parsed")
                print(f"   File: {assertions_file}")
                print(f"   Content preview:\n{preview}")
                step.fail()
                return False

            if initial_assertions:
                print(f"📖 Read initial assertions from: {assertions_file}")
                initial_assertions = _cap_initial_assertions(
                    initial_assertions, module_type=module_type)
            else:
                print(f"⚠️  No designer assertions in {assertions_file}; continuing with the harness skeleton only")
                initial_assertions = ''

            # Read property file
            with open(prop_file_path, 'r') as f:
                prop_content = f.read()

            # Find the designer marker and insert assertions after it
            designer_marker = '//====DESIGNER-ADDED-SVA====//\n'
            if designer_marker not in prop_content:
                print(f"❌ Designer marker not found in property file: {prop_file_path}")
                print("Expected marker: //====DESIGNER-ADDED-SVA====//")
                step.fail()
                return False

            # Split content at the marker and insert assertions
            parts = prop_content.split(designer_marker)
            if len(parts) != 2:
                print(f"❌ Multiple or no designer markers found in property file")
                step.fail()
                return False

            # Create base content with initial assertions
            header = parts[0]
            if module_type == 'combinational':
                header = rtl_clocking.ensure_combinational_clocking(header)
            base_content = header + designer_marker + initial_assertions + '\n' + parts[1]

            # Write base file
            os.makedirs(os.path.dirname(base_file_path), exist_ok=True)
            with open(base_file_path, 'w') as f:
                f.write(base_content)

            print(f"✅ Created base file at: {base_file_path}")

            # Count assertions for reporting
            assertion_count = _count_assertions(initial_assertions)
            print(f"📊 Added {assertion_count} initial assertions to base file")

            if assertion_count:
                blocks = [b for b in initial_assertions.split('\n\n') if b.strip()]
                print("\nFirst few assertions added:")
                for i, assertion in enumerate(blocks[:3], 1):
                    print(f"  {i}. {assertion}")
                if assertion_count > 3:
                    print(f"  ... and {assertion_count - 3} more assertions")

            return True

        except Exception as e:
            print(f"❌ Error creating base file: {str(e)}")
            step.fail()
            return False

def run_reset_tb(rtl_module):
    """Run reset_tb.sh to copy base file to property file."""
    with cli_log.get().step("🔄 STEP 4: Running reset_tb.sh to establish initial property set") as step:
        # Extract just the module name (filename without extension) from any path
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        cmd = [os.path.join(REPO_ROOT, 'scripts', 'reset_tb.sh'), module_name]

        print(f"Executing: {' '.join(cmd)}")
        print(f"📍 Module name extracted: {module_name}")

        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            print("✅ reset_tb.sh completed successfully")
            if result.stdout:
                print(result.stdout)
            return True
        except subprocess.CalledProcessError as e:
            print(f"❌ reset_tb.sh failed with return code {e.returncode}")
            print(f"STDOUT: {e.stdout}")
            print(f"STDERR: {e.stderr}")
            step.fail()
            return False

def run_agent(rtl_module, llm_model, module_type, execution_type, verbosity, formal_tool='jaspergold', stop_after_extension=False, resume_from_semantic=False, resume_from_extension=False):
    """Run agent.py with the specified parameters."""
    with cli_log.get().step("🤖 STEP 5: Running Agent for SVA correction and enhancement") as step:
        # Convert verbosity format
        verbosity_map = {'low': '1', 'medium': '2', 'high': '3', 'debug': '4'}
        verbosity_level = verbosity_map.get(verbosity, '2')

        cmd = [
            sys.executable, os.path.join(CORE_DIR, 'agent.py'),
            '--rtl-source', rtl_module,
            '--llm', llm_model,
            '--module-type', module_type,
            '--verbosity', verbosity_level,
            '--execution', execution_type,
            '--formal-tool', formal_tool
        ]
        if stop_after_extension:
            cmd.append('--stop-after-extension')
        if resume_from_semantic:
            cmd.append('--resume-from-semantic')
        if resume_from_extension:
            cmd.append('--resume-from-extension')

        print(f"Executing: {' '.join(cmd)}")

        try:
            # Run agent interactively (don't capture output so user can see progress)
            result = subprocess.run(cmd, check=True)
            print("✅ Agent completed successfully")
            return True
        except subprocess.CalledProcessError as e:
            print(f"❌ Agent failed with return code {e.returncode}")
            step.fail()
            return False

def detect_submodules(rtl_file):
    """Detect submodule instantiations in an RTL file.

    Args:
        rtl_file (str): Path to the RTL file to analyze

    Returns:
        set: Set of unique submodule names found in the RTL file
    """
    submodules = set()

    if not os.path.exists(rtl_file):
        print(f"⚠️  RTL file not found: {rtl_file}")
        return submodules

    try:
        with open(rtl_file, 'r') as f:
            content = f.read()

        # Remove comments from the entire content first
        # Remove multi-line comments
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        # Remove single-line comments
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)

        # Split into lines for line number tracking
        lines = content.split('\n')

        # SystemVerilog keywords and built-in types to filter out
        sv_keywords = {
            'logic', 'wire', 'reg', 'bit', 'byte', 'shortint', 'int', 'longint',
            'input', 'output', 'inout', 'parameter', 'localparam', 'const',
            'assign', 'always', 'always_ff', 'always_comb', 'initial',
            'if', 'else', 'case', 'casex', 'casez', 'default', 'for', 'while',
            'repeat', 'foreach', 'do', 'generate', 'genvar', 'begin', 'end',
            'function', 'task', 'return', 'void', 'class', 'interface', 'modport',
            'typedef', 'struct', 'union', 'enum', 'string', 'event',
            'mailbox', 'semaphore', 'process', 'time', 'realtime',
            'and', 'or', 'not', 'nand', 'nor', 'xor', 'xnor', 'buf', 'bufif0', 'bufif1',
            'assert', 'assume', 'cover', 'property', 'sequence', 'clocking', 'disable',
            'module', 'endmodule',
            # Block enders: without these, "endcase" on one line followed by an
            # instantiation on the next is read as a module named endcase.
            'endcase', 'endfunction', 'endtask', 'endgenerate', 'endinterface',
            'endpackage', 'endclass', 'endproperty', 'endsequence', 'endclocking',
            'endprogram', 'endspecify', 'endchecker', 'endconfig',
            'unique', 'unique0', 'priority', 'forever', 'break', 'continue',
            'wait', 'fork', 'join', 'join_any', 'join_none', 'package',
            'import', 'export', 'virtual', 'extern', 'static', 'automatic',
        }

        # Pattern to match module instantiations (handles multi-line)
        # This matches: module_name [#(...)] instance_name (
        # The pattern allows for whitespace and newlines between components
        # Whitespace between the module name and the instance name must stay on
        # one line unless a #(...) parameter list separates them. Allowing a bare
        # newline there makes any keyword ending a block (endcase, end) pair up
        # with the instantiation on the following line.
        module_inst_pattern = r'''
            ^[ \t]*                       # Start of line
            (\w+)                         # Module name (group 1)
            (?:
                \s*\#[ \t]*\(             # Parameterised: #( ... ) may span lines
                    (?:[^()]|             # Match non-parentheses OR
                       \([^()]*\)         # Match balanced simple parentheses like (8)
                    )*                    # Any number of the above
                \)\s*                     # End of parameter list: )
              |
                [ \t]+                    # Plain: instance name on the same line
            )
            (\w+)                         # Instance name (group 2)
            \s*\(                         # Opening parenthesis for port connections
        '''

        # Find all module instantiations using multiline search
        matches = re.finditer(module_inst_pattern, content, re.MULTILINE | re.VERBOSE | re.DOTALL)

        for match in matches:
            module_name = match.group(1)
            instance_name = match.group(2)

            # Filter out SystemVerilog keywords and built-in types
            if (module_name not in sv_keywords and
                not module_name.startswith('$') and
                module_name.isidentifier() and
                not module_name.isdigit()):

                # Find the line number for reporting
                match_start = match.start()
                line_num = content[:match_start].count('\n') + 1

                submodules.add(module_name)
                print(f"📍 Found submodule: {module_name} (instance: {instance_name}) at line {line_num}")

        # Also use the original line-by-line approach as a fallback for patterns not caught above
        for line_num, line in enumerate(lines, 1):
            line_clean = line.strip()

            # Skip empty lines
            if not line_clean:
                continue

            # Skip certain SystemVerilog constructs that aren't module instantiations
            skip_patterns = [
                r'^\s*module\s+',      # Module definition
                r'^\s*endmodule',      # End of module
                r'^\s*function\s+',    # Function definition
                r'^\s*task\s+',        # Task definition
                r'^\s*typedef\s+',     # Type definition
                r'^\s*parameter\s+',   # Parameter declaration
                r'^\s*localparam\s+',  # Local parameter declaration
                r'^\s*logic\s+',       # Logic declaration
                r'^\s*wire\s+',        # Wire declaration
                r'^\s*reg\s+',         # Register declaration
                r'^\s*input\s+',       # Input declaration
                r'^\s*output\s+',      # Output declaration
                r'^\s*inout\s+',       # Inout declaration
                r'^\s*assign\s+',      # Assignment
                r'^\s*always\s+',      # Always block
                r'^\s*initial\s+',     # Initial block
                r'^\s*if\s*\(',        # If statement
                r'^\s*else\s+',        # Else statement
                r'^\s*case\s*\(',      # Case statement
                r'^\s*for\s*\(',       # For loop
                r'^\s*while\s*\(',     # While loop
                r'^\s*generate\s*',    # Generate block
                r'^\s*genvar\s+',      # Generate variable
                r'^\s*end\s*$',        # End statement
                r'^\s*begin\s*$',      # Begin statement
            ]

            # Check if this line should be skipped
            should_skip = False
            for pattern in skip_patterns:
                if re.match(pattern, line_clean, re.IGNORECASE):
                    should_skip = True
                    break

            if should_skip:
                continue

            # Try simple pattern without parameters (for single-line instantiations)
            simple_match = re.match(r'\s*(\w+)\s+(\w+)\s*\(', line_clean)
            if simple_match and not line_clean.startswith('module'):
                module_name = simple_match.group(1)
                instance_name = simple_match.group(2)

                # Filter out SystemVerilog keywords and built-in types
                if (module_name not in sv_keywords and
                    not module_name.startswith('$') and
                    module_name.isidentifier() and
                    not module_name.isdigit() and
                    module_name not in submodules):  # Avoid duplicates

                    submodules.add(module_name)
                    print(f"📍 Found submodule: {module_name} (instance: {instance_name}) at line {line_num}")

    except Exception as e:
        print(f"❌ Error parsing RTL file {rtl_file}: {str(e)}")

    return submodules

#: Subtrees that must not supply submodule RTL. AssertLLM and similar corpora
#: keep mutation / buggy copies of the same module names beside the golden
#: sources; picking those makes the harness prove a mutant instead of the DUT.
_SKIP_SUBMODULE_SEARCH_DIRS = frozenset({
    'mutations', 'buggy_artifacts', 'figures', '__pycache__', '.git',
})


def find_submodule_files(submodule_names, sources, includes):
    """Find the actual file paths for detected submodules.

    Args:
        submodule_names (set): Set of submodule names to find
        sources (list): List of source directories
        includes (str): Include directory path

    Returns:
        dict: Dictionary mapping submodule names to their file paths
    """
    submodule_files = {}
    # Prefer the include directory: OpenCores-style trees put leaf modules
    # there, while the parent source tree may also contain mutations/.
    search_dirs = []
    if includes:
        search_dirs.append(includes)
    for source_dir in (sources or ['sources']):
        if source_dir and source_dir not in search_dirs:
            search_dirs.append(source_dir)

    print(f"🔍 Searching for {len(submodule_names)} submodules in directories: {search_dirs}")

    for submodule_name in submodule_names:
        found = False

        # Try different file naming conventions
        possible_names = [
            f"{submodule_name}.sv",
            f"{submodule_name}.v",
            f"modules/{submodule_name}.sv",
            f"modules/{submodule_name}.v"
        ]

        # Search in all source directories
        for search_dir in search_dirs:
            if not os.path.exists(search_dir):
                continue

            for possible_name in possible_names:
                # Try direct path in search directory
                candidate_path = os.path.join(search_dir, possible_name)
                if os.path.exists(candidate_path):
                    submodule_files[submodule_name] = candidate_path
                    print(f"✅ Found {submodule_name} at: {candidate_path}")
                    found = True
                    break

                # Try searching recursively for the file
                for root, dirs, files in os.walk(search_dir):
                    dirs[:] = [d for d in dirs
                               if d not in _SKIP_SUBMODULE_SEARCH_DIRS
                               and not d.startswith('.')]
                    for file in files:
                        if file == f"{submodule_name}.sv" or file == f"{submodule_name}.v":
                            candidate_path = os.path.join(root, file)
                            submodule_files[submodule_name] = candidate_path
                            print(f"✅ Found {submodule_name} at: {candidate_path}")
                            found = True
                            break
                    if found:
                        break
                if found:
                    break
            if found:
                break

        if not found:
            print(f"⚠️  Could not find file for submodule: {submodule_name}")

    return submodule_files

def find_rtl_module_file(rtl_module, sources, includes):
    """Find the top-level RTL module file if not in current directory.

    Args:
        rtl_module (str): RTL module name or path
        sources (list): List of source directories
        includes (str): Include directory path

    Returns:
        str: Full path to RTL module file, or original rtl_module if not found
    """
    # If it already exists as specified, return it
    if os.path.exists(rtl_module):
        return rtl_module

    # Build search directories
    search_dirs = list(sources) if sources else ['sources']
    if includes and includes not in search_dirs:
        search_dirs.append(includes)

    print(f"🔍 Top-level module '{rtl_module}' not found in current directory.")
    print(f"🔍 Searching in source directories: {search_dirs}")

    # Extract just the filename if a path was provided
    module_filename = os.path.basename(rtl_module)

    # Try different search strategies
    search_patterns = [
        rtl_module,           # Original name (could be a relative path)
        module_filename,      # Just the filename
    ]

    for search_dir in search_dirs:
        if not os.path.exists(search_dir):
            continue

        for pattern in search_patterns:
            # Try direct path in search directory
            candidate_path = os.path.join(search_dir, pattern)
            if os.path.exists(candidate_path):
                print(f"✅ Found top-level module at: {candidate_path}")
                return candidate_path

            # Try searching recursively for the file
            for root, dirs, files in os.walk(search_dir):
                if pattern in files:
                    candidate_path = os.path.join(root, pattern)
                    print(f"✅ Found top-level module at: {candidate_path}")
                    return candidate_path

    # If not found, return original and let the error handling deal with it
    print(f"❌ Could not locate '{rtl_module}' in source directories.")
    return rtl_module

def discover_include_dirs(scan_roots):
    """Discover +incdir directories needed to resolve `include "..." headers.

    Verilog `include resolution needs the directory that, joined with the include
    string, yields the header file. Headers are often referenced with a leading
    sub-directory (e.g. `include "common_cells/registers.svh"), where the file
    physically lives in .../common_cells/include/common_cells/registers.svh and the
    required incdir is .../common_cells/include (the PARENT of the directory that
    holds the .svh). To cover both `file.svh and `dir/file.svh styles we add each
    directory that contains a .svh header AND its parent directory.

    Args:
        scan_roots (iterable): Directories to walk searching for .svh headers.

    Returns:
        list: Sorted absolute directory paths to be emitted as +incdir+.
    """
    inc_dirs = set()
    visited_roots = set()
    for root in scan_roots:
        if not root or not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        if root in visited_roots:
            continue
        visited_roots.add(root)
        for dirpath, subdirs, files in os.walk(root):
            # Skip hidden/VCS directories (e.g. .git) to avoid huge, useless walks.
            subdirs[:] = [d for d in subdirs if not d.startswith('.')]
            if any(f.endswith('.svh') for f in files):
                abs_dir = os.path.abspath(dirpath)
                inc_dirs.add(abs_dir)
                inc_dirs.add(os.path.dirname(abs_dir))
    return sorted(inc_dirs)


def _language_selection_flags(verilog_std='2001'):
    """The file-list flags that pick a language per file extension.

    ``analyze -format sverilog`` applies one language to the whole list, which a
    Verilog design does not survive as soon as it uses a word SystemVerilog
    later reserved: the OpenCores RAM has a port named ``do``.  Selecting per
    extension compiles the DUT as Verilog and the checker, which is always
    SystemVerilog whatever the DUT is, as SystemVerilog.

    Verilog-2001 is the default because it is the later standard.  A Verilog-95
    design that uses a 2001 keyword (``generate``, ``localparam``, ``signed``)
    as an identifier needs ``verilog_std='1995'``.
    """
    return ''.join(f'+{flag}\n' for flag in (
        f'verilog{verilog_std}ext+.v',
        f'verilog{verilog_std}ext+.vh',
        'systemverilogext+.sv',
        'systemverilogext+.svh',
    ))


def generate_fpv_vcf_tcl(rtl_module, module_type, regenerate_after_submodule_merge=False,
                         verilog_std='2001', dut_root_override='',
                         elaboration_overrides=None):
    """Generate FPV_vcf.tcl for VC Formal from the scaffold-generated FPV.tcl and files.vc.

    the scaffolder generates FPV.tcl (JasperGold format) and files.vc (a VCS-compatible file
    list that uses Tcl variable references like ${DUT_PATH}).  VC Formal passes the
    file list to VCS which does NOT perform Tcl variable expansion, so we cannot reuse
    files.vc directly.

    This function:
      1. Parses FPV.tcl to recover variable values (DUT_PATH, PROP_PATH, …) and the
         clock / reset configuration.
      2. Reads files.vc and pre-expands all ${VAR} references using those values,
         writing the result to files_vcf.vc (a plain, fully-resolved VCS file list).
      3. Writes FPV_vcf.tcl that calls:
           set_fml_appmode FPV
           analyze -format sverilog -vcs "-f <files_vcf.vc>"
           elaborate -cov all -sva <module>
             (+ -vcs -pvalue when parent constants resolved)
             (+ -cm_libs vy only when hierarchy-wide coverage is requested)
           fml_vacuity_on / fml_witness_on   (vacuity + witness goals)
           create_clock / create_reset       (for sequential modules)
           check_constraints -block          (conflicting constraints / deadends)
           check_fv -block                   (blocking = per-property PROP_I_RESULT lines)
           compute_formal_core + reports     (COI, constraint dependence, vacuity)
           compute_formal_core_coverage      (top-level code coverage by default)
           save_formal_core_results          (coverage database Verdi annotates with)

    The reports land in vcf_projs/<module>/reports/ and are parsed by proof_status.py
    and coi.py to qualify each property and to measure assumption dependence. The
    coverage database lands in vcf_projs/<module>/formal_cov.vdb.

    Args:
        rtl_module: Path to the RTL file for this formal testbench (top or leaf).
        module_type: 'sequential' or 'combinational'.
        regenerate_after_submodule_merge: When True, use a shorter log banner — call
            after `update_top_level_files_vc_with_submodules` so files_vcf.vc picks up
            submodule *_prop.sv / *_bind.svh lines that were inserted into files.vc.
        elaboration_overrides: Parent compile-time literals to apply at
            elaborate via VCS ``-pvalue``. When omitted, the JSON beside the
            checker is reloaded so a files.vc regenerate keeps the same values.

    Returns True on success, False on failure.
    """
    title = (
        "📋 VC Formal: Regenerating files_vcf.vc (files.vc now includes submodules)"
        if regenerate_after_submodule_merge
        else "📋 STEP 1.7: Generating FPV_vcf.tcl for VC Formal"
    )
    with cli_log.get().step(title) as step:
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        ft_dir = f'ft_{module_name}'
        jg_tcl_path  = os.path.join(ft_dir, 'FPV.tcl')
        vcf_tcl_path = os.path.join(ft_dir, 'FPV_vcf.tcl')
        files_vc_path  = os.path.join(ft_dir, 'files.vc')
        files_vcf_path = os.path.join(ft_dir, 'files_vcf.vc')

        if not os.path.exists(ft_dir):
            print(f"❌ FT directory not found: {ft_dir}")
            print("   Make sure scaffolding completed successfully before this step.")
            step.fail()
            return False

        # ------------------------------------------------------------------
        # 1. Parse FPV.tcl to collect variable values and clock/reset info
        # ------------------------------------------------------------------
        svapshot_root = (
            os.environ.get('SVAPSHOT_ROOT')
            or os.getcwd()
        )
        # ``run_scaffold`` temporarily sets DUT_ROOT and restores it before this
        # function runs. Production callers pass the derived root explicitly;
        # direct callers without either value still fail closed.
        dut_root = os.environ.get('DUT_ROOT') or dut_root_override

        var_map = {
            'SVAPSHOT_ROOT': svapshot_root,
            'SVAPSHOT_ROOT': svapshot_root,
            'DUT_ROOT':     dut_root,
        }

        clk_sig        = None
        rst_sig        = None
        rst_active_low = False

        def _expand(s, vmap):
            """Expand ${VAR} references using vmap; leave unknown refs intact."""
            return re.sub(r'\$\{(\w+)\}', lambda m: vmap.get(m.group(1), m.group(0)), s)

        if os.path.exists(jg_tcl_path):
            print(f"   📖 Parsing {jg_tcl_path}")
            with open(jg_tcl_path) as fh:
                for raw_line in fh:
                    line = raw_line.strip()

                    # set VAR value  (skip lines that source from $env())
                    m = re.match(r'^set\s+(\w+)\s+(.+)$', line)
                    if m and '$env(' not in m.group(2):
                        var_map[m.group(1)] = _expand(m.group(2).strip(), var_map)

                    # clock <signal>  (skip "clock -none")
                    m = re.match(r'^\s*clock\s+(?!-none)(\w+)', line)
                    if m:
                        clk_sig = m.group(1)

                    # reset -expression ((!?)signal)
                    m = re.match(r'^\s*reset\s+-expression\s+\((!?)(\w+)\)', line)
                    if m:
                        rst_active_low = bool(m.group(1))   # '!' prefix → active-low
                        rst_sig = m.group(2)
        else:
            print(f"   ⚠️  FPV.tcl not found at {jg_tcl_path}")
            print(f"      Make sure scaffolding ran before this step.")

        # Ensure PROP_PATH is available (the scaffolder always sets it in FPV.tcl)
        if 'PROP_PATH' not in var_map:
            var_map['PROP_PATH'] = os.path.abspath(os.path.join(ft_dir, 'sva'))

        # ------------------------------------------------------------------
        # 2. Discover +incdir directories so `include "..." headers (e.g.
        #    common_cells/registers.svh, which defines macros like `FFLARNC) resolve.
        #    VCS only searches +incdir+ dirs for `include (not -y library dirs).
        # ------------------------------------------------------------------
        scan_roots = [os.path.dirname(os.path.abspath(rtl_module))]
        for key in ('DUT_PATH', 'INC_PATH', 'DUT_ROOT'):
            if var_map.get(key):
                scan_roots.append(var_map[key])
        for key, value in var_map.items():
            if key.startswith('SRC_PATH') and value:
                scan_roots.append(value)

        include_dirs = discover_include_dirs(scan_roots)
        incdir_block = ''.join(f'+incdir+{d}\n' for d in include_dirs)
        if include_dirs:
            print(f"   📁 Adding {len(include_dirs)} +incdir directories for `include resolution")

        # ------------------------------------------------------------------
        # 3. Expand files.vc → files_vcf.vc (all ${VAR} replaced by values)
        # ------------------------------------------------------------------
        if os.path.exists(files_vc_path):
            with open(files_vc_path) as fh:
                raw_vc = fh.read()

            # A variable that is unset or empty expands to nothing and turns
            # ${DUT_ROOT}/rtl/foo.sv into /rtl/foo.sv: a path that does not exist,
            # written without complaint, and only noticed when VCS cannot find the
            # design. Refuse instead, naming the variable that is missing.
            unresolved = sorted({name for name in re.findall(r'\$\{(\w+)\}', raw_vc)
                                 if not var_map.get(name)})
            if unresolved:
                print(f"   ❌ {files_vc_path} references {', '.join(unresolved)}, "
                      f"which {'is' if len(unresolved) == 1 else 'are'} unset or empty.")
                for name in unresolved:
                    if name in ('DUT_ROOT', 'SVAPSHOT_ROOT'):
                        print(f"      Export {name} before running, or pass -src so it can be derived.")
                    else:
                        print(f"      {name} is normally set by the scaffolder in {jg_tcl_path}.")
                print("      Refusing to write a file list with unresolved paths.")
                step.fail()
                return False

            expanded_vc = _expand(raw_vc, var_map)
            # Direct children sometimes live in a sibling directory (tlb →
            # mmu/rtl/common/pseudoLRU.sv). DUT_PATH -y does not see them.
            # Adding those dirs as -y keeps VCS able to elaborate the leaf
            # without generating properties for the children.
            lib_lines = []
            for lib in extra_library_dirs(rtl_module):
                flag = f'-y {lib}'
                if flag not in expanded_vc and f'-y{lib}' not in expanded_vc:
                    lib_lines.append(f'+incdir+{lib}\n{flag}\n')
            if lib_lines:
                expanded_vc = ''.join(lib_lines) + expanded_vc
                print(
                    f"   📁 Adding {len(lib_lines)} sibling -y directories "
                    "for child RTL outside DUT_PATH")
            # the scaffolder leaves a Verilog DUT out of files.vc on purpose: JasperGold
            # gets it through a second 'analyze -v2k'. VC Formal has no such second
            # command, so without this the design is only found if the library
            # search happens to match the file name to the module name. Put it after
            # package/dependency file lists but before the generated checker: a
            # package referenced by the DUT must have been analyzed first.
            dut_abs = os.path.abspath(rtl_module)
            if dut_abs not in expanded_vc:
                print(f"   📎 File list did not name the DUT; adding {dut_abs}")
                expanded_lines = expanded_vc.splitlines(keepends=True)
                checker_suffixes = (
                    f'{module_name}_prop.sv',
                    f'{module_name}_bind.svh',
                )
                insert_at = next(
                    (index for index, line in enumerate(expanded_lines)
                     if line.strip().endswith(checker_suffixes)),
                    len(expanded_lines),
                )
                expanded_lines.insert(insert_at, f'{dut_abs}\n')
                expanded_vc = ''.join(expanded_lines)
            with open(files_vcf_path, 'w') as fh:
                fh.write(incdir_block)
                fh.write(_language_selection_flags(verilog_std))
                fh.write(expanded_vc)
            print(f"   ✅ Wrote expanded file list → {files_vcf_path}")
        else:
            print(f"   ⚠️  files.vc not found at {files_vc_path}, generating minimal file list")
            prop_path = var_map['PROP_PATH']
            rtl_abs   = os.path.abspath(rtl_module)
            with open(files_vcf_path, 'w') as fh:
                fh.write('+libext+.v\n+libext+.sv\n')
                fh.write(_language_selection_flags(verilog_std))
                fh.write(incdir_block)
                fh.write(f'{rtl_abs}\n')
                fh.write(f'{prop_path}/{module_name}_prop.sv\n')
                fh.write(f'{prop_path}/{module_name}_bind.svh\n')
            print(f"   ✅ Wrote minimal file list → {files_vcf_path}")

        # ------------------------------------------------------------------
        # 4. Write FPV_vcf.tcl
        # ------------------------------------------------------------------
        # Use [file dirname [info script]] so the path is correct regardless
        # of the working directory when vcf is invoked.
        files_vcf_basename = os.path.basename(files_vcf_path)

        tcl = []
        tcl.append(f'# VC Formal FPV Tcl script for {module_name}')
        tcl.append(f'# Auto-generated by main.py from FPV.tcl / files.vc')
        tcl.append( '#')
        # -o is not a vcf option; the runner redirects instead. Keep this line in
        # step with the repository FPV launcher, since it is what a reader copies.
        tcl.append(
            '# Run via:  ./fpv_app_scripts/run_vcf_batch.sh {}'.format(module_name))
        tcl.append( '#           (vcf -f ft_{}/FPV_vcf.tcl -batch > vcf_projs/{}/vcf.log 2>&1)'.format(
                        module_name, module_name))
        tcl.append('')
        tcl.append('## Design Import')
        tcl.append(f'set top {module_name}')
        tcl.append('')
        tcl.append('# ---------------------------------------------------------------------------')
        tcl.append('# Optional FTA (Formal Testbench Analysis) — keep commented during normal')
        tcl.append('# FPV runs. Uncomment after experiments for signoff analysis (PTW-style example).')
        tcl.append('# ---------------------------------------------------------------------------')
        tcl.append('# signoff_config -type all')
        tcl.append('#')
        tcl.append('# fta_init -top $top')
        tcl.append('# ---------------------------------------------------------------------------')
        tcl.append('')
        tcl.append('set_fml_appmode FPV')
        tcl.append('')
        tcl.append('# files_vcf.vc is a pre-expanded copy of files.vc (no Tcl vars inside)')
        tcl.append('# so it can be passed directly to VCS without variable-expansion issues.')
        tcl.append(f'set files_vcf [file join [file dirname [info script]] {files_vcf_basename}]')
        # Checkers keep $display for simulation reuse; Simon ignores them for
        # formal and otherwise floods the log with Warning-[SM_UST] once per call.
        tcl.append('suppress_message SM_UST')
        tcl.append('analyze -format sverilog -vcs "-f $files_vcf"')
        tcl.append('')
        tcl.append('## Coverage configuration')
        tcl.append('# Top-level coverage remains enabled by default. Instrumenting modules found')
        tcl.append('# through -y/-v library paths requires -cm_libs vy, but on fpnew_top that')
        tcl.append('# expanded the formal model to ~1.1M operators / 267k flop bits and the')
        tcl.append('# server disconnected while building check_fv. Keep hierarchy-wide')
        tcl.append('# instrumentation opt-in until the run has enough memory.')
        tcl.append('#')
        tcl.append('# SVAPSHOT_COVERAGE=0 disables coverage instrumentation and all post-proof')
        tcl.append('# coverage analysis. Compilation, constraint sanity, FPV, vacuity/witness')
        tcl.append('# goals and the per-property proof report still run.')
        tcl.append('# SVAPSHOT_HIERARCHICAL_COVERAGE=1 instruments library modules too.')
        tcl.append('set collect_coverage 1')
        tcl.append('if {[info exists env(SVAPSHOT_COVERAGE)]} {')
        tcl.append('    set collect_coverage $env(SVAPSHOT_COVERAGE)')
        tcl.append('}')
        tcl.append('set hierarchical_coverage 0')
        tcl.append('if {[info exists env(SVAPSHOT_HIERARCHICAL_COVERAGE)]} {')
        tcl.append('    set hierarchical_coverage $env(SVAPSHOT_HIERARCHICAL_COVERAGE)')
        tcl.append('}')
        tcl.append('')
        if elaboration_overrides is None:
            elaboration_overrides = read_elaboration_overrides(module_name)
        pvalue_str = format_vcs_pvalue_string(
            module_name, elaboration_overrides or {})

        tcl.append('## Elaborate')
        if pvalue_str:
            print(f"   📎 Elaborate -pvalue from parent constants: {pvalue_str}")
            tcl.append('# Parent-resolved compile-time constants shared by every')
            tcl.append('# direct instance. VCS -pvalue so the leaf elaborates as')
            tcl.append('# the parent uses it, not with illegal header defaults.')
            tcl.append(f'set pvalues {{{pvalue_str}}}')
            tcl.append('if {!$collect_coverage} {')
            tcl.append('    elaborate -sva $top -vcs $pvalues')
            tcl.append('} elseif {$hierarchical_coverage} {')
            tcl.append('    elaborate -cov all -sva $top -vcs "$pvalues -cm_libs vy"')
            tcl.append('} else {')
            tcl.append('    elaborate -cov all -sva $top -vcs $pvalues')
            tcl.append('}')
        else:
            tcl.append('if {!$collect_coverage} {')
            tcl.append('    elaborate -sva $top')
            tcl.append('} elseif {$hierarchical_coverage} {')
            tcl.append('    elaborate -cov all -sva $top -vcs "-cm_libs vy"')
            tcl.append('} else {')
            tcl.append('    elaborate -cov all -sva $top')
            tcl.append('}')
        tcl.append('')
        tcl.append('## Vacuity and witness goals')
        tcl.append('# fml_vacuity_on creates a vacuity goal for every |-> / |=> assertion and')
        tcl.append('# assumption, so a "proven" result can be told apart from a proof whose')
        tcl.append('# antecedent is never reachable. fml_witness_on does the same for the')
        tcl.append('# reachability of assumptions.')
        tcl.append('set_fml_var fml_vacuity_on true')
        tcl.append('set_fml_var fml_witness_on true')
        tcl.append('')
        tcl.append('## Clock / Reset constraints')

        if module_type == 'combinational':
            formal_clk = rtl_clocking.combinational_clock_hier(module_name)
            tcl.append('# Combinational DUT: sample on the checker-local formal_clk.')
            tcl.append(f'create_clock {{{formal_clk}}} -period 100')
            tcl.append(f'set_change_at -default -posedge -clock {{{formal_clk}}}')
        else:
            if clk_sig:
                tcl.append(f'create_clock {clk_sig} -period 100')
                tcl.append(f'set_change_at -default -posedge -clock {clk_sig}')
            else:
                tcl.append('# TODO: uncomment and set your clock signal')
                tcl.append('# create_clock clk_i -period 100')
                tcl.append('# set_change_at -default -posedge -clock clk_i')

            if rst_sig:
                sense = 'low' if rst_active_low else 'high'
                tcl.append(f'create_reset {rst_sig} -sense {sense}')
            else:
                tcl.append('# TODO: uncomment and set your reset signal')
                tcl.append('# create_reset rstn_i -sense low')

        tcl.append('')
        tcl.append('## Reports consumed by the qualification pipeline (proof_status.py, coi.py).')
        tcl.append('## Every report is wrapped in `catch` so that a command missing from a given')
        tcl.append('## VC Formal version degrades the report instead of aborting the run — the')
        tcl.append('## agent must always get its PROP_I_RESULT lines back.')
        tcl.append(f'set report_dir "vcf_projs/{module_name}/reports"')
        tcl.append(f'set covdb "vcf_projs/{module_name}/formal_cov"')
        tcl.append('file delete -force $report_dir')
        tcl.append('file delete -force $covdb.vdb $covdb.el')
        tcl.append('file mkdir $report_dir')
        tcl.append('')
        tcl.append('## Proof budget. The defaults are twelve hours per blocking check command')
        tcl.append('## and no per-property limit, so on a large design one hard property can')
        tcl.append('## consume the run and not a single PROP_I_RESULT line is ever written —')
        tcl.append('## the flow then has nothing to qualify. A property that runs out of time')
        tcl.append('## is reported inconclusive, which the qualification pipeline already reads')
        tcl.append('## as "not evidence about the design". Both limits are overridable so a')
        tcl.append('## long proof run needs no regenerated script.')
        tcl.append(f'set command_time_budget "{VCF_COMMAND_TIME_BUDGET}"')
        tcl.append('if {[info exists env(SVAPSHOT_FML_MAX_TIME)]} {')
        tcl.append('    set command_time_budget $env(SVAPSHOT_FML_MAX_TIME)')
        tcl.append('}')
        tcl.append(f'set property_time_budget "{VCF_PROPERTY_TIME_BUDGET}"')
        tcl.append('if {[info exists env(SVAPSHOT_FML_PROPERTY_TIME)]} {')
        tcl.append('    set property_time_budget $env(SVAPSHOT_FML_PROPERTY_TIME)')
        tcl.append('}')
        tcl.append('catch {set_fml_var fml_max_time $command_time_budget}')
        tcl.append('catch {set_fml_var fml_property_time_limit $property_time_budget}')
        tcl.append('')
        tcl.append('## Constraint sanity: an assumption set with no solution proves every')
        tcl.append('## assertion, so a conflict or a deadend has to be known before the')
        tcl.append('## PROP_I_RESULT lines below can be read as evidence. With no assumptions')
        tcl.append('## there is nothing that can conflict, and the check is a formal run of its')
        tcl.append('## own: on fpnew_top it opened 135 goals to answer a question whose answer')
        tcl.append('## was already known, ahead of every proof.')
        tcl.append('##')
        tcl.append('## What must be counted is the assumptions the property set contributed,')
        tcl.append('## not the constraints the run has. The clock and reset setup above adds')
        tcl.append('## its own — a reset shows up as rst==1, a constconstraint of class')
        tcl.append('## `script` — so a plain count is never zero and would skip nothing. Those')
        tcl.append('## are subtracted rather than a threshold guessed, because how many there')
        tcl.append('## are is a property of the design: a combinational module has none and a')
        tcl.append('## multi-clock one has several. The count is taken at run time because the')
        tcl.append('## agent rewrites the property file between iterations. Both queries default')
        tcl.append('## to counting, so a version that rejects one runs the check rather than')
        tcl.append('## skipping it on a number it could not obtain.')
        tcl.append('set assume_count 0')
        tcl.append('set script_assume_count 0')
        tcl.append('catch {set assume_count [sizeof_collection [get_props -usage assume]]}')
        tcl.append('catch {set script_assume_count '
                   '[sizeof_collection [get_props -usage assume -class script]]}')
        tcl.append('if {$assume_count - $script_assume_count > 0} {')
        tcl.append('    catch {check_constraints -block}')
        tcl.append('    catch {redirect -file $report_dir/constraints.rpt '
                   '{report_constraints -verbose}}')
        tcl.append('} else {')
        tcl.append('    echo "CHECK_CONSTRAINTS_SKIPPED: the property set declares no assumptions'
                   ' ($assume_count constraint(s), all created by this script)"')
        tcl.append('}')
        tcl.append('')
        tcl.append('## Proof  (-block = wait for all results; required for PROP_I_RESULT messages)')
        tcl.append('## A failure here (an empty or uncompilable property set) must not abort')
        tcl.append('## the script: VC Formal stops at the first uncaught error, and the reports')
        tcl.append('## below are how the agent learns what happened.')
        tcl.append('if {[catch {check_fv -block} check_fv_error]} {')
        tcl.append('    echo "CHECK_FV_FAILED: $check_fv_error"')
        tcl.append('}')
        tcl.append('')
        tcl.append('## Per-property status including the vacuity goal of every implication')
        tcl.append('catch {redirect -file $report_dir/properties.rpt {report_fv -verbose}}')
        tcl.append('')
        tcl.append('## Counterexample traces for semantic repair. fvtrace writes FSDB')
        tcl.append('## (VC Formal has no VCD export). Isolated repair re-runs check_fv')
        tcl.append('## with one assertion; the next prompt shrinks a sibling .vcd / .txt')
        tcl.append('## into a cycle table when one is present. Wrapped in catch so a')
        tcl.append('## missing command or a proven property without a trace cannot abort')
        tcl.append('## the PROP_I_RESULT path.')
        tcl.append('file mkdir $report_dir/cex')
        tcl.append('catch {')
        tcl.append('    foreach_in_collection prop [get_props -usage assert] {')
        tcl.append('        set prop_name [get_attribute $prop name]')
        tcl.append('        set safe [regsub -all {[^A-Za-z0-9_]} $prop_name {_}]')
        tcl.append('        echo "CEX_DUMP_BEGIN $prop_name"')
        tcl.append('        catch {fvtrace -property $prop_name -file $report_dir/cex/${safe}.fsdb}')
        tcl.append('        echo "CEX_DUMP_END $prop_name"')
        tcl.append('    }')
        tcl.append('}')
        tcl.append('')
        tcl.append('if {$collect_coverage} {')
        tcl.append('## Formal core: the registers, inputs and *constraints* each proof relied on.')
        tcl.append('## This is what separates an assumption-dependent proof from a standalone one,')
        tcl.append('## and gives a tool-computed COI per property.')
        tcl.append('catch {compute_formal_core -block}')
        tcl.append('catch {redirect -file $report_dir/formal_core.rpt {report_formal_core -list}}')
        tcl.append('catch {redirect -file $report_dir/formal_core_verbose.rpt '
                   '{report_formal_core -verbose -no_summary}}')
        tcl.append('')
        tcl.append('## Constraints actually needed by each proof (subset of the formal core)')
        tcl.append('## This is constraint analysis, so it is useful only when the property set')
        tcl.append('## supplied a user assumption. With only clock/reset script constraints VC')
        tcl.append('## Formal reports CRC_E_NO_CONSTRAINTS_FOUND.')
        tcl.append('if {$assume_count - $script_assume_count > 0} {')
        tcl.append('    catch {compute_reduced_constraints -block}')
        tcl.append('    catch {redirect -file $report_dir/reduced_constraints.rpt '
                   '{report_reduced_constraints -verbose}}')
        tcl.append('} else {')
        tcl.append('    echo "REDUCED_CONSTRAINTS_SKIPPED: no user assumptions"')
        tcl.append('}')
        tcl.append('')
        tcl.append('## Tool-reported checker coverage. Property density is the share of design')
        tcl.append('## registers (and primary inputs) that lie in the COI of at least one assert;')
        tcl.append('## the formal core above is the subset those proofs actually needed. Reporting')
        tcl.append('## both bounds what the snapshot inspects and what it establishes.')
        tcl.append('catch {')
        tcl.append('    redirect -file $report_dir/assertion_density.rpt {')
        tcl.append('        echo "DENSITY_SCOPE reg"')
        tcl.append('        catch {report_assertion_density -list -status all -type reg}')
        tcl.append('        echo "DENSITY_SCOPE pi"')
        tcl.append('        catch {report_assertion_density -list -status all -type pi}')
        tcl.append('    }')
        tcl.append('}')
        tcl.append('')
        tcl.append('## Structural COI per asserted property, used for the diversity metrics')
        tcl.append('catch {')
        tcl.append('    redirect -file $report_dir/coi.rpt {')
        tcl.append('        foreach_in_collection prop [get_props -usage assert] {')
        tcl.append('            # get_object_name rejects property objects; the name attribute works.')
        tcl.append('            set prop_name [get_attribute $prop name]')
        tcl.append('            echo "PROPERTY_COI_BEGIN $prop_name"')
        tcl.append('            catch {report_fv_complexity -property $prop_name -list -limit -1}')
        tcl.append('            echo "PROPERTY_COI_END $prop_name"')
        tcl.append('        }')
        tcl.append('    }')
        tcl.append('}')
        tcl.append('')
        tcl.append('## Map the computed formal core directly onto instrumented coverage goals')
        tcl.append('## and write the database Verdi uses to annotate RTL sources. -structural')
        tcl.append('## is the documented no-extra-analysis mode: the FPV proofs above remain the')
        tcl.append('## evidence, rather than launching a second formal task for coverage goals.')
        tcl.append('## This covers the top by default; set SVAPSHOT_HIERARCHICAL_COVERAGE=1 to')
        tcl.append('## include modules VCS discovers through -y/-v library paths.')
        tcl.append('if {$collect_coverage} {')
        tcl.append('    catch {compute_formal_core_coverage -par_task FPV -structural -block}')
        tcl.append('    catch {save_formal_core_results -db_name $covdb.vdb '
                   '-elfile_name $covdb.el -mark fcore_plus_coi}')
        tcl.append('}')
        tcl.append('')
        tcl.append('## Medium signoff: observation coverage (OC) and formal coverage (FC).')
        tcl.append('## SVAPSHOT_SIGNOFF=0 skips this. Every command is caught so an older')
        tcl.append('## VC Formal spelling cannot abort the PROP_I_RESULT path.')
        tcl.append('set run_signoff 1')
        tcl.append('if {[info exists env(SVAPSHOT_SIGNOFF)]} {')
        tcl.append('    set run_signoff $env(SVAPSHOT_SIGNOFF)')
        tcl.append('}')
        tcl.append('if {$run_signoff} {')
        tcl.append('    # X-2025.06: signoff_config requires -type; -effort is rejected.')
        tcl.append('    catch {signoff_config -type all}')
        tcl.append('    catch {signoff_config -type {oc fc}}')
        tcl.append('    catch {check_fv -block}')
        tcl.append('    catch {check_fv -block -type {oc fc}}')
        tcl.append('    catch {redirect -file $report_dir/fv_coverage.rpt {report_fv_coverage}}')
        tcl.append('    catch {redirect -file $report_dir/fv_coverage_verbose.rpt {report_fv_coverage -verbose}}')
        tcl.append('    # report_cov -type oc|fc is invalid here. OC maps to "observe".')
        tcl.append('    foreach kind {observe cover structural line toggle condition branch assert assume} {')
        tcl.append('        catch {redirect -file $report_dir/cov_${kind}.rpt {report_cov -type $kind}}')
        tcl.append('    }')
        tcl.append('    catch {redirect -file $report_dir/cov.rpt {report_cov}}')
        tcl.append('    catch {redirect -file $report_dir/report_fv.rpt {report_fv}}')
        tcl.append('    catch {set_fml_appmode COV}')
        tcl.append('    catch {redirect -file $report_dir/report_fv_cov.rpt {report_fv}}')
        tcl.append('} else {')
        tcl.append('    echo "SIGNOFF_SKIPPED: SVAPSHOT_SIGNOFF=0"')
        tcl.append('}')
        tcl.append('} else {')
        tcl.append('    echo "COVERAGE_ANALYSIS_SKIPPED: SVAPSHOT_COVERAGE=0"')
        tcl.append('}')
        tcl.append('')

        with open(vcf_tcl_path, 'w') as fh:
            fh.write('\n'.join(tcl))

        print(f"   ✅ Wrote {vcf_tcl_path}")

        if module_type != 'combinational':
            if not clk_sig:
                print(f"   ⚠️  Clock signal not detected in FPV.tcl.")
                print(f"      Edit {vcf_tcl_path} and fill in the create_clock command.")
            if not rst_sig:
                print(f"   ⚠️  Reset signal not detected in FPV.tcl.")
                print(f"      Edit {vcf_tcl_path} and fill in the create_reset command.")

        return True




def generate_density_probe_tcl(rtl_module, module_type='sequential'):
    """Write ``FPV_density_probe.tcl``: elaborate + assertion density, no ``check_fv``.

    Used as a fallback after the static internal-port rewrite when hierarchical
    bind paths remain unresolved. Does not replace the normal FPV script.
    """
    import bind_internals

    module_name = os.path.splitext(os.path.basename(rtl_module))[0]
    ft_dir = f'ft_{module_name}'
    if not os.path.isdir(ft_dir):
        print(f"❌ FT directory not found: {ft_dir}")
        return False

    clk_sig = rst_sig = None
    rst_active_low = True
    jg_tcl_path = os.path.join(ft_dir, 'FPV.tcl')
    if os.path.isfile(jg_tcl_path):
        with open(jg_tcl_path, encoding='utf-8', errors='replace') as fh:
            for raw_line in fh:
                line = raw_line.strip()
                match = re.match(r'^\s*clock\s+(?!-none)(\w+)', line)
                if match:
                    clk_sig = match.group(1)
                match = re.match(
                    r'^\s*reset\s+-expression\s+\((!?)(\w+)\)', line)
                if match:
                    rst_active_low = bool(match.group(1))
                    rst_sig = match.group(2)

    probe_path = os.path.join(ft_dir, 'FPV_density_probe.tcl')
    with open(probe_path, 'w', encoding='utf-8') as fh:
        fh.write(bind_internals.generate_density_probe_tcl(
            module_name,
            module_type=module_type,
            clk_sig=clk_sig,
            rst_sig=rst_sig,
            rst_active_low=rst_active_low,
        ))
    print(f"   ✅ Wrote density probe → {probe_path}")
    return True


def prepare_formal_harness(
        rtl_module, module_type='auto', sources=None, includes='packages',
        formal_tool='vcformal', verilog_std='2001',
        disable_package_detection=False, instantiation_context='',
        elaboration_overrides=None):
    """Create ``ft_<module>/`` plus FPV scripts. No LLM calls.

    One-shot and isolated DATE seeds need a checker shell without inheriting
    another seed's property file.
    """
    if cli_log.get() is None:
        cli_log.configure('medium')
    sources = sources or ['sources']
    if not run_scaffold(rtl_module, sources, includes, module_type):
        cli_log.get().error(f"❌ Failed at scaffold step for {rtl_module}")
        return False
    if not validate_and_fix_property_bind_files(rtl_module):
        cli_log.get().error(f"❌ Failed at property/bind file validation step for {rtl_module}")
        return False
    if instantiation_context and instantiation_context.strip():
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        context_path = os.path.join(
            f'ft_{module_name}', 'sva', 'instantiation_context.txt')
        os.makedirs(os.path.dirname(context_path), exist_ok=True)
        with open(context_path, 'w', encoding='utf-8') as handle:
            handle.write(instantiation_context.strip() + '\n')
        print(f"📎 Wrote parent instantiation context → {context_path}")
    module_name = os.path.splitext(os.path.basename(rtl_module))[0]
    write_elaboration_overrides(module_name, elaboration_overrides or {})
    if elaboration_overrides:
        print(
            f"📎 Wrote parent elaborate constants → "
            f"ft_{module_name}/sva/elaboration_parameters.json")
    if not disable_package_detection:
        if not enhanced_create_manual_sub_and_inject_packages(rtl_module, sources, includes):
            cli_log.get().error(
                f"❌ Failed at enhanced package detection and injection step for {rtl_module}")
            return False
    else:
        cli_log.get().detail(
            "📦 Package auto-detection disabled, using default manual_sub.vc and property files")
    if formal_tool == 'vcformal':
        if not generate_fpv_vcf_tcl(
                rtl_module, module_type, verilog_std=verilog_std,
                dut_root_override=os.path.dirname(os.path.abspath(rtl_module)),
                elaboration_overrides=elaboration_overrides or {}):
            cli_log.get().error(f"❌ Failed to generate FPV_vcf.tcl for {rtl_module}")
            return False
    return True


def run_single_module_flow(rtl_module, llm_model, module_type, execution_type, verbosity, sources, includes, assertion_source, initial_file, disable_package_detection=False, formal_tool='jaspergold', verilog_std='2001', instantiation_context='', elaboration_overrides=None, stop_after_extension=False, resume_from_semantic=False, resume_from_extension=False, stop_after_scaffold=False):
    """Run the complete tool flow for a single module.

    Args:
        rtl_module (str): RTL source module path
        llm_model (str): LLM model to use
        module_type (str): Type of RTL module ('sequential' or 'combinational')
        execution_type (str): Type of execution ('golden' or 'validation')
        verbosity (str): Verbosity level
        sources (list): Source directories
        includes (str): Include directory
        assertion_source (str): Source of initial assertions ('rtl' or 'file')
        initial_file (str): File containing initial assertions (when using 'file' source)
        disable_package_detection (bool): Whether to disable automatic package detection
        formal_tool (str): Formal verification tool to use ('jaspergold' or 'vcformal').
            When 'vcformal', step 1.7 generates FPV_vcf.tcl and the pre-expanded
            files_vcf.vc alongside the JasperGold FPV.tcl produced by the scaffolder.
        instantiation_context (str): Optional PARENT INSTANTIATION CONTEXT block
            for a direct submodule; written beside the checker and included in
            the initial and agent prompts so config-gated properties target the
            parent's overrides.
        elaboration_overrides (dict): Optional parent compile-time literals
            applied at leaf FPV elaborate via VCS ``-pvalue``.

    Returns:
        bool: True if successful, False otherwise
    """
    cli = cli_log.configure(verbosity)
    if cli.quiet_steps:
        print(f"🚀 Running AI-SVA flow for module: {rtl_module}")
    else:
        print(f"\n{'='*60}")
        print(f"🚀 Running AI-SVA flow for module: {rtl_module}")
        print(f"{'='*60}")

    if resume_from_semantic:
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        prop_path = os.path.join(f'ft_{module_name}', 'sva', f'{module_name}_prop.sv')
        if not os.path.isfile(prop_path):
            cli_log.get().error(f"❌ Cannot resume semantic correction: missing {prop_path}")
            return False
        print(f"↩️  Resuming from existing harness ({prop_path})")
        if not run_agent(
                rtl_module, llm_model, module_type, execution_type, verbosity,
                formal_tool, resume_from_semantic=True):
            cli_log.get().error(f"❌ Failed at Agent resume step for {rtl_module}")
            return False
        print(f"✅ Successfully completed AI-SVA semantic resume for {rtl_module}")
        return True

    if resume_from_extension:
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        prop_path = os.path.join(f'ft_{module_name}', 'sva', f'{module_name}_prop.sv')
        if not os.path.isfile(prop_path):
            cli_log.get().error(f"❌ Cannot resume set extension: missing {prop_path}")
            return False
        print(f"↩️  Resuming set extension from existing after-syntax set ({prop_path})")
        if not run_agent(
                rtl_module, llm_model, module_type, execution_type, verbosity,
                formal_tool, stop_after_extension=stop_after_extension,
                resume_from_extension=True):
            cli_log.get().error(f"❌ Failed at Agent extension-resume step for {rtl_module}")
            return False
        print(f"✅ Successfully completed AI-SVA extension resume for {rtl_module}")
        return True

    if not prepare_formal_harness(
            rtl_module, module_type, sources, includes, formal_tool,
            verilog_std, disable_package_detection, instantiation_context,
            elaboration_overrides):
        return False

    if stop_after_scaffold:
        print(f"✅ Harness ready; stopping before generation ({rtl_module})")
        return True

    # Step 2: Get initial assertions from RTL/SVA rules or a supplied file.
    if assertion_source == 'rtl':
        if not run_initial_assertion_generation(
                rtl_module, llm_model,
                instantiation_context=instantiation_context,
                module_type=module_type):
            cli_log.get().error(f"❌ Failed at initial RTL assertion generation for {rtl_module}")
            return False
    else:  # assertion_source == 'file'
        if not read_initial_file(initial_file, module_type=module_type):
            cli_log.get().error(f"❌ Failed at reading initial file for {rtl_module}")
            return False

    # Step 3: Create base file from initial assertions
    if not create_base_from_initial_assertions(rtl_module, module_type=module_type):
        cli_log.get().error(
            f"❌ Failed at creating base file from initial assertions for {rtl_module}")
        return False

    # Step 4: Run reset_tb.sh
    if not run_reset_tb(rtl_module):
        cli_log.get().error(f"❌ Failed at reset_tb step for {rtl_module}")
        return False

    # Step 5: Run Agent
    if not run_agent(
            rtl_module, llm_model, module_type, execution_type, verbosity,
            formal_tool, stop_after_extension=stop_after_extension):
        cli_log.get().error(f"❌ Failed at Agent step for {rtl_module}")
        return False

    print(f"✅ Successfully completed AI-SVA flow for {rtl_module}")
    return True

def validate_and_fix_property_bind_files(rtl_module):
    """Validate and fix property and bind files produced by the harness scaffolder.

    Ensures:
    1. Only ASSERT_INPUTS should be assigned a value (0) in parameters
    2. Other parameters should be passed through without assignment
    3. Remove extra commas that can cause syntax errors
    4. ASSERT_INPUTS parameter is always present in both files

    Args:
        rtl_module (str): RTL source module path

    Returns:
        bool: True if validation/fixing succeeded, False otherwise
    """
    with cli_log.get().step("🔧 STEP 1.5: Validating and fixing property and bind files") as step:
        # Get module name and file paths
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        ft_dir = f'ft_{module_name}'
        sva_dir = os.path.join(ft_dir, 'sva')
        prop_file_path = os.path.join(sva_dir, f'{module_name}_prop.sv')
        bind_file_path = os.path.join(sva_dir, f'{module_name}_bind.svh')

        if not os.path.exists(prop_file_path):
            print(f"❌ Property file not found: {prop_file_path}")
            step.fail()
            return False

        if not os.path.exists(bind_file_path):
            print(f"❌ Bind file not found: {bind_file_path}")
            step.fail()
            return False

        try:
            # Fix property file
            success_prop, prop_fixes = fix_property_file(prop_file_path, module_name)

            # Fix bind file (now passing the RTL module path)
            success_bind, bind_fixes = fix_bind_file(bind_file_path, module_name, rtl_module)

            if success_prop and success_bind:
                print("✅ Property and bind files validated and fixed successfully")

                # Report what was fixed
                total_fixes = prop_fixes + bind_fixes
                if total_fixes > 0:
                    print(f"📊 Applied {total_fixes} fixes:")
                    if prop_fixes > 0:
                        print(f"   • Property file: {prop_fixes} fixes")
                    if bind_fixes > 0:
                        print(f"   • Bind file: {bind_fixes} fixes")
                else:
                    print("📊 No fixes needed - files were already correct")

                return True
            else:
                print("❌ Failed to fix property and/or bind files")
                step.fail()
                return False

        except Exception as e:
            print(f"❌ Error during validation and fixing: {str(e)}")
            step.fail()
            return False

def fix_property_file(prop_file_path, module_name):
    """Fix parameter declarations in property file.

    Args:
        prop_file_path (str): Path to the property file
        module_name (str): Module name

    Returns:
        tuple: (success: bool, fixes_count: int)
    """
    try:
        with open(prop_file_path, 'r') as f:
            content = f.read()

        lines = content.split('\n')
        fixed_lines = []
        in_parameter_section = False
        found_assert_inputs = False
        fixes_count = 0

        for i, line in enumerate(lines):
            stripped = line.strip()
            original_line = line

            # Detect start of parameter section
            if stripped.startswith('#(') or (stripped.startswith('module') and '#(' in line):
                in_parameter_section = True
                fixed_lines.append(line)
                continue

            # Detect end of parameter section
            if in_parameter_section and (stripped.startswith(')') and '(' not in stripped):
                in_parameter_section = False
                # Before closing the parameter section, ensure ASSERT_INPUTS is present
                if not found_assert_inputs:
                    # Add ASSERT_INPUTS parameter if not found
                    if fixed_lines and not fixed_lines[-1].strip().endswith(','):
                        # Add comma to previous line if needed
                        fixed_lines[-1] = fixed_lines[-1].rstrip() + ','
                        fixes_count += 1
                    fixed_lines.append('\t\tparameter ASSERT_INPUTS = 0')
                    fixes_count += 1
                    print(f"   ➕ Added missing ASSERT_INPUTS parameter")
                fixed_lines.append(line)
                continue

            # Process parameter lines
            if in_parameter_section:
                if 'parameter' in stripped and 'ASSERT_INPUTS' in stripped:
                    # Ensure ASSERT_INPUTS is correctly assigned
                    if '=' not in stripped:
                        line = line.replace('ASSERT_INPUTS', 'ASSERT_INPUTS = 0')
                        fixes_count += 1
                        print(f"   🔧 Fixed ASSERT_INPUTS assignment")
                    elif not '= 0' in line:
                        # Fix the assignment value
                        line = re.sub(r'ASSERT_INPUTS\s*=\s*[^,\s)]+', 'ASSERT_INPUTS = 0', line)
                        fixes_count += 1
                        print(f"   🔧 Corrected ASSERT_INPUTS value to 0")
                    found_assert_inputs = True

                # Clean up comma formatting
                fixed_line = fix_comma_formatting(line, i, lines, in_parameter_section)
                if fixed_line != line:
                    fixes_count += 1
                    print(f"   🔧 Fixed comma formatting")
                line = fixed_line

            fixed_lines.append(line)

        # Write the fixed content back
        with open(prop_file_path, 'w') as f:
            f.write('\n'.join(fixed_lines))

        print(f"📄 Validated property file: {prop_file_path}")
        return True, fixes_count

    except Exception as e:
        print(f"❌ Error fixing property file {prop_file_path}: {str(e)}")
        return False, 0

def extract_localparams_from_rtl(rtl_file_path):
    """Extract localparam names from an RTL module file.

    Args:
        rtl_file_path (str): Path to the RTL module file

    Returns:
        set: Set of localparam names found in the module
    """
    localparams = set()

    try:
        with open(rtl_file_path, 'r') as f:
            content = f.read()

        # Remove comments to avoid false matches
        # Remove single-line comments
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        # Remove multi-line comments
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)

        # Find all localparam declarations
        # Pattern matches: localparam PARAM_NAME = value
        localparam_pattern = r'localparam\s+(?:\w+\s+)?(\w+)\s*='
        matches = re.findall(localparam_pattern, content, re.IGNORECASE)

        for match in matches:
            localparams.add(match)

        print(f"   📋 Found {len(localparams)} localparams in RTL: {', '.join(sorted(localparams))}")

    except Exception as e:
        print(f"   ⚠️ Could not parse RTL file {rtl_file_path}: {str(e)}")
        # Fall back to common localparams if parsing fails
        localparams = {'DATA_WIDTH', 'ALU_OPS', 'WIDTH', 'DATA_WIDTH_BITS', 'ALU_OPS_BITS'}
        print(f"   📋 Using fallback localparams: {', '.join(sorted(localparams))}")

    return localparams

def extract_parameters_from_rtl(rtl_file_path):
    """Return parameter names declared on the module header (#(...) port list)."""
    try:
        with open(rtl_file_path, 'r') as f:
            content = f.read()
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        header = re.search(r'module\s+\w+\s*#\s*\((.*?)\)\s*\(', content, re.DOTALL)
        if not header:
            return []
        # The type before a parameter name may be package-qualified, packed,
        # signed, or simply ``type``. Matching only ``\w+`` type tokens drops
        # declarations such as ``parameter fpnew_pkg::opgroup_e OpGroup =``.
        # The identifier immediately before ``=`` is the parameter name.
        return re.findall(
            r'\bparameter\b[^,]*?\b([A-Za-z_]\w*)\s*=',
            header.group(1),
            re.IGNORECASE | re.DOTALL,
        )
    except Exception:
        return []

def ensure_bind_module_parameters(bind_file_path, rtl_module_path):
    """Add .PARAM (PARAM) pass-throughs from the RTL module header into the bind file."""
    param_names = extract_parameters_from_rtl(rtl_module_path)
    if not param_names:
        return 0

    with open(bind_file_path, 'r') as f:
        content = f.read()

    existing = set(re.findall(r'\.(\w+)\s*\(', content))
    missing = [p for p in param_names if p not in existing]
    if not missing:
        return 0

    insert = ''.join(f'\t\t.{p} ({p}),\n' for p in missing)
    if '.ASSERT_INPUTS' in content:
        content, n = re.subn(
            r'\n(\s*\.ASSERT_INPUTS\s*\()',
            '\n' + insert + r'\1',
            content,
            count=1,
        )
    else:
        content, n = re.subn(
            r'(#\(\s*\n)',
            r'\1' + insert,
            content,
            count=1,
        )

    with open(bind_file_path, 'w') as f:
        f.write(content)
    return len(missing)

def sync_submodule_bind_files_from_parent(parent_rtl_module, processed_submodules):
    """Ensure each submodule bind passes module parameters used by the parent instance."""
    for submodule_name, submodule_path in processed_submodules.items():
        bind_path = os.path.join(
            f'ft_{submodule_name}', 'sva', f'{submodule_name}_bind.svh')
        if os.path.exists(bind_path) and os.path.exists(submodule_path):
            ensure_bind_module_parameters(bind_path, submodule_path)

def fix_bind_file(bind_file_path, module_name, rtl_module):
    """Fix parameter assignments in bind file.

    Args:
        bind_file_path (str): Path to the bind file
        module_name (str): Module name
        rtl_module (str): RTL module path

    Returns:
        tuple: (success: bool, fixes_count: int)
    """
    try:
        with open(bind_file_path, 'r') as f:
            content = f.read()

        lines = content.split('\n')
        fixed_lines = []
        in_parameter_section = False
        found_assert_inputs = False
        fixes_count = 0

        # Try to find the corresponding RTL file to extract localparams
        rtl_localparams = set()

        # Use the provided RTL module path directly
        if os.path.exists(rtl_module):
            print(f"   📁 Using provided RTL file: {rtl_module}")
            rtl_localparams = extract_localparams_from_rtl(rtl_module)
        else:
            print(f"   ⚠️ Provided RTL file not found: {rtl_module}, using fallback localparams")
            # Fallback to common localparams if RTL file not found
            rtl_localparams = {'DATA_WIDTH', 'ALU_OPS', 'WIDTH', 'DATA_WIDTH_BITS', 'ALU_OPS_BITS'}

        # Common parameters that need default values
        PARAMETER_DEFAULTS = {
            'CONFIG32': '1\'b0',
            'CONFIG64': '1\'b1',
            'ASSERT_INPUTS': '0'
        }

        for i, line in enumerate(lines):
            stripped = line.strip()
            original_line = line

            # Detect start of parameter section
            if stripped.startswith('#('):
                in_parameter_section = True
                fixed_lines.append(line)
                continue

            # Detect end of parameter section
            if in_parameter_section and stripped.startswith(')'):
                in_parameter_section = False
                # Before closing the parameter section, ensure ASSERT_INPUTS is present
                if not found_assert_inputs:
                    # Add ASSERT_INPUTS parameter if not found
                    fixed_lines.append('\t\t.ASSERT_INPUTS (0)')
                    fixes_count += 1
                    print(f"   ➕ Added missing .ASSERT_INPUTS (0) parameter")
                fixed_lines.append(line)
                continue

            # Process parameter lines in bind file
            if in_parameter_section and stripped.startswith('.'):
                # Extract parameter name
                match = re.search(r'\.(\w+)\s*\([^)]*\)', stripped)
                if match:
                    param_name = match.group(1)

                    # Skip localparams entirely (now using dynamically extracted list)
                    if param_name in rtl_localparams:
                        fixes_count += 1
                        print(f"   🗑️ Removed localparam .{param_name} (found in RTL as localparam)")
                        continue

                    # Handle ASSERT_INPUTS specifically
                    if param_name == 'ASSERT_INPUTS':
                        new_line = re.sub(r'\.ASSERT_INPUTS\s*\([^)]*\)', '.ASSERT_INPUTS (0)', line)
                        if new_line != line:
                            fixes_count += 1
                            print(f"   🔧 Fixed .ASSERT_INPUTS assignment to (0)")
                        line = new_line
                        found_assert_inputs = True

                    # Handle other known parameters with default values
                    elif param_name in PARAMETER_DEFAULTS:
                        default_value = PARAMETER_DEFAULTS[param_name]
                        new_line = re.sub(r'\.{}\s*\([^)]*\)'.format(re.escape(param_name)),
                                        f'.{param_name} ({default_value})', line)
                        if new_line != line:
                            fixes_count += 1
                            print(f"   🔧 Fixed parameter .{param_name} to use default value ({default_value})")
                        line = new_line

                    # For unknown parameters, try to keep them as pass-through if they look valid
                    else:
                        # Check if the parameter assignment looks like a pass-through (PARAM_NAME)
                        current_value_match = re.search(r'\.{}\s*\(([^)]*)\)'.format(re.escape(param_name)), stripped)
                        if current_value_match:
                            current_value = current_value_match.group(1).strip()
                            # If it's trying to pass through the same name, it's probably wrong
                            if current_value == param_name:
                                print(f"   ⚠️ Parameter .{param_name} may need a specific value instead of pass-through")

                # Clean up comma formatting
                fixed_line = fix_comma_formatting(line, i, lines, in_parameter_section)
                if fixed_line != line:
                    fixes_count += 1
                    print(f"   🔧 Fixed comma formatting")
                line = fixed_line

            fixed_lines.append(line)

        # Write the fixed content back
        with open(bind_file_path, 'w') as f:
            f.write('\n'.join(fixed_lines))

        param_fixes = 0
        if os.path.exists(rtl_module):
            param_fixes = ensure_bind_module_parameters(bind_file_path, rtl_module)
            fixes_count += param_fixes

        print(f"📄 Validated bind file: {bind_file_path}")
        return True, fixes_count

    except Exception as e:
        print(f"❌ Error fixing bind file {bind_file_path}: {str(e)}")
        return False, 0

def fix_comma_formatting(line, line_index, all_lines, in_parameter_section):
    """Fix comma formatting to avoid syntax errors.

    Args:
        line (str): Current line
        line_index (int): Index of current line
        all_lines (list): All lines in the file
        in_parameter_section (bool): Whether we're in parameter section

    Returns:
        str: Fixed line
    """
    if not in_parameter_section:
        return line

    stripped = line.strip()

    # Skip lines that don't contain parameters
    if not ('parameter' in stripped or 'localparam' in stripped or stripped.startswith('.')):
        return line

    # Check if this is the last parameter line before closing parenthesis
    is_last_param = False
    
    # First, check if the parameter-list closing parenthesis is on the same line.
    # A parameter value may itself contain balanced parentheses (e.g. a function
    # call like `max_num_lanes(...)`), so those must NOT be mistaken for the end of
    # the parameter list. The list closer is only present when there are more
    # closing than opening parentheses on the line (an unbalanced ')').
    if stripped.count(')') > stripped.count('('):
        # There's an unmatched closing parenthesis on this line, so this is the
        # last parameter of the list.
        is_last_param = True
    else:
        # Look ahead to see if there are more parameters
        for j in range(line_index + 1, len(all_lines)):
            next_stripped = all_lines[j].strip()
            if next_stripped.startswith(')'):
                is_last_param = True
                break
            elif ('parameter' in next_stripped or 'localparam' in next_stripped or next_stripped.startswith('.')):
                # Found another parameter, so this is not the last one
                is_last_param = False
                break

    # Remove trailing comma if this is the last parameter
    if is_last_param:
        # Handle cases like "parameter ASSERT_INPUTS = 0)," where both comma and closing paren are present
        if line.rstrip().endswith(','):
            line = line.rstrip()[:-1]  # Remove the trailing comma
        # Also handle cases like "parameter ASSERT_INPUTS = 0)," -> "parameter ASSERT_INPUTS = 0)"
        # by removing comma before closing parenthesis
        line = re.sub(r',(\s*\))', r'\1', line)

    # Add comma if this is not the last parameter and doesn't have one
    elif not is_last_param and not line.rstrip().endswith(',') and stripped:
        line = line.rstrip() + ','

    return line

def detect_packages_from_rtl(rtl_file, sources, includes):
    """Detect packages used by an RTL file through import statements and usage patterns.

    Args:
        rtl_file (str): Path to the RTL file to analyze
        sources (list): List of source directories to search for packages
        includes (str): Include directory path

    Returns:
        set: Set of unique package files found and their paths
    """
    packages = set()
    package_usage = set()
    # name -> [(kind, line_num), ...]; printed as one summary line per package
    # at every verbosity so repeated scope/type hits do not flood the CLI.
    package_hits = {}

    if not os.path.exists(rtl_file):
        print(f"⚠️  RTL file not found: {rtl_file}")
        return packages

    def _hit(name, kind, line_num):
        package_usage.add(name)
        package_hits.setdefault(name, []).append((kind, line_num))

    try:
        with open(rtl_file, 'r') as f:
            content = f.read()

        # Remove comments from the content first
        # Remove multi-line comments
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        # Remove single-line comments but preserve end-of-line
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)

        # Split into lines for analysis
        lines = content.split('\n')

        print(f"🔍 Analyzing RTL file for package dependencies: {rtl_file}")

        # Pattern 1: Direct import statements
        # import pkg_name::*; or import pkg_name::item;
        import_pattern = r'^\s*import\s+(\w+)::'

        # Pattern 2: Package scope usage without import
        # pkg_name::type_name or pkg_name::CONSTANT
        scope_pattern = r'(\w+)::\w+'

        # Pattern 3: Common SystemVerilog package naming patterns
        # Look for _pkg usage in typedef, parameter, or signal declarations
        pkg_usage_pattern = r'(\w*_pkg)\s*::'

        # Pattern 4: Include statements that might reference packages
        include_pattern = r'^\s*`include\s+["\']([^"\']*(?:_pkg|pkg)[^"\']*)["\']'

        # Pattern 5: Package-like identifiers in signal/type declarations
        # Look for patterns like: pkg_name_t, pkg_name_e, etc.
        type_pattern = r'(\w+_pkg)(?:_[a-z])?(?:\s|$|[^\w])'

        for line_num, line in enumerate(lines, 1):
            line_clean = line.strip()

            if not line_clean:
                continue

            # Check for import statements
            for match in re.findall(import_pattern, line_clean, re.IGNORECASE):
                _hit(match, 'import', line_num)

            # Check for package scope usage
            for match in re.findall(scope_pattern, line_clean):
                if not match.isdigit() and match != 'super':  # Filter out numeric literals and 'super'
                    _hit(match, 'scope', line_num)

            # Check for _pkg pattern usage
            for match in re.findall(pkg_usage_pattern, line_clean, re.IGNORECASE):
                _hit(match, 'pattern', line_num)

            # Check for include statements
            for match in re.findall(include_pattern, line_clean, re.IGNORECASE):
                # Extract package name from include path
                pkg_name = os.path.splitext(os.path.basename(match))[0]
                if '_pkg' in pkg_name or pkg_name.endswith('_pkg'):
                    _hit(pkg_name, 'include', line_num)

            # Check for package-like type patterns
            for match in re.findall(type_pattern, line_clean, re.IGNORECASE):
                _hit(match, 'type', line_num)

        for line in cli_log.summarize_package_hits(package_hits):
            print(line)

    except Exception as e:
        print(f"❌ Error parsing RTL file {rtl_file}: {str(e)}")
        return packages

    # Now find the actual package files
    if package_usage:
        print(f"🔍 Detected {len(package_usage)} potential packages: {', '.join(sorted(package_usage))}")
        package_files = find_package_files(package_usage, sources, includes, rtl_file)
        packages.update(package_files)
    else:
        print("📊 No package dependencies detected in RTL file")

    return packages

def _is_svapshot_checkout(path):
    """True when *path* is this repo, not a nested DUT clone."""
    return os.path.isfile(os.path.join(path, 'experiments', 'date_reduced.yaml'))


def _is_nested_suite_root(path):
    """AssertLLM2 / FVEval are suite clones; walking them pulls the wrong RAM."""
    name = os.path.basename(os.path.abspath(path))
    return name in {'AssertLLM2', 'FVEval'}


def extra_library_dirs(rtl_module):
    """Directories VCS ``-y`` should search for sibling RTL next to the leaf.

    ``files.vc`` only adds ``-y ${DUT_PATH}``. A leaf such as ``tlb`` still
    instantiates ``pseudoLRU`` from ``mmu/rtl/common``. Without that
    directory on the library path, elaborate fails after a clean scaffold.

    DATE DUTs live inside this checkout. Walking the SVApshot root as a
    library path lets AssertLLM2's ``generic_dpram`` win over the OpenCores
    RAM next to ``generic_fifo_sc_a``. Walking AssertLLM2 itself hides
    ``versatile_counter/include/versatile_counter_defines.v`` behind every
    other core's copy of the same name.
    """
    roots = []
    dut = os.path.dirname(os.path.abspath(rtl_module)) if rtl_module else ''
    if dut:
        roots.append(dut)
        include = os.path.join(dut, 'include')
        if os.path.isdir(include):
            roots.append(include)
        parent = os.path.dirname(dut)
        grand = os.path.basename(os.path.dirname(parent)) if parent else ''
        if parent and os.path.basename(parent) not in {'benchmarks', 'designs'} and grand != 'designs':
            roots.append(parent)
    tree = design_tree_root(rtl_module)
    if tree and not _is_svapshot_checkout(tree) and not _is_nested_suite_root(tree):
        roots.append(tree)
    found = []
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, subdirs, files in os.walk(root):
            _prune_dirs(subdirs)
            if not any(name.endswith(('.sv', '.v')) for name in files):
                continue
            absdir = os.path.abspath(dirpath)
            if absdir in seen:
                continue
            seen.add(absdir)
            found.append(absdir)
    return found


def design_tree_roots(rtl_file, limit=8):
    """Enclosing git checkouts from the DUT outward (innermost first).

    ``mmu`` is its own clone inside ``sargantana``. Stopping at the first
    ``.git`` hid ``sargantana/includes/riscv_pkg.sv`` even though the RTL
    uses ``riscv_pkg::XLEN``. Keep walking so parent-repo packages stay
    visible; callers that want only the nearest checkout use
    :func:`design_tree_root`.
    """
    roots = []
    if not rtl_file:
        return roots
    directory = os.path.dirname(os.path.abspath(rtl_file))
    for _ in range(limit):
        if not directory or directory == os.sep:
            break
        for marker in ('.git', '.gitmodules'):
            if os.path.exists(os.path.join(directory, marker)):
                roots.append(directory)
                break
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return roots


def design_tree_root(rtl_file, limit=8):
    """Walk up from an RTL file to the nearest repository that owns it.

    Package definitions often sit outside the -src directory: Sargantana keeps
    drac_pkg.sv in sargantana/includes while the sources are under
    sargantana/rtl. Without the enclosing tree the search falls through to a
    same-named copy elsewhere on the search path.
    """
    roots = design_tree_roots(rtl_file, limit=limit)
    return roots[0] if roots else None


def suite_package_trees(rtl_file, limit=8):
    """Git trees from the DUT out to the ``benchmarks/<suite>`` checkout.

    Stops after the suite root (parent named ``benchmarks``) so the search
    does not walk the whole AI-SVA repo.
    """
    trees = []
    for tree in design_tree_roots(rtl_file, limit=limit):
        trees.append(tree)
        if os.path.basename(os.path.dirname(tree)) == 'benchmarks':
            break
    return trees


def package_search_roots(rtl_file, sources, includes):
    """Directories to search for package definitions, most specific first.

    Order is what decides correctness when several trees hold a file of the same
    name: the design's own tree has to win over the generic fallback
    directories, which may hold an unrelated or outdated copy.
    """
    roots = []

    def add(directory):
        if directory and os.path.isdir(directory) and directory not in roots:
            roots.append(directory)

    if rtl_file:
        add(os.path.dirname(os.path.abspath(rtl_file)))
        for tree in suite_package_trees(rtl_file):
            add(tree)
    for source in (sources or []):
        add(source)
    add(includes)
    for fallback in ('packages', 'pkg', 'common', 'include'):
        add(fallback)
    return roots


def find_package_files(package_names, sources, includes, rtl_file=None):
    """Find the actual file paths for detected packages.

    A package is resolved to a file that declares it, searched in the design's
    own tree first. Matching on filename alone picks up include headers and
    stale copies that merely share a name, which the tool then compiles in place
    of the real definition.

    Args:
        package_names (set): Set of package names to find
        sources (list): List of source directories
        includes (str): Include directory path
        rtl_file (str): The DUT, used to locate its design tree

    Returns:
        set: Set of package file paths found
    """
    package_files = set()
    search_dirs = package_search_roots(rtl_file, sources, includes)
    if not search_dirs:
        search_dirs = ['sources']

    print(f"🔍 Searching for {len(package_names)} packages in directories: {search_dirs}")

    # Files that really declare a package, indexed over the same roots in the
    # same order, so the first (most specific) definition wins.
    declared = index_package_definitions(search_dirs)

    for package_name in package_names:
        if package_name in declared:
            path = declared[package_name]
            package_files.add(path)
            print(f"✅ Found package {package_name} declared in: {path}")
            continue

        found = False

        # Try different file naming conventions for packages
        possible_names = [
            f"{package_name}.sv",
            f"{package_name}.svh",
            f"{package_name}.v",
            f"{package_name}.vh",
            f"{package_name}_pkg.sv",
            f"{package_name}_pkg.svh",
        ]

        # Search in all directories
        for search_dir in search_dirs:
            if not os.path.exists(search_dir):
                continue

            for possible_name in possible_names:
                # Try direct path in search directory
                candidate_path = os.path.join(search_dir, possible_name)
                if os.path.exists(candidate_path):
                    package_files.add(candidate_path)
                    print(f"✅ Found package {package_name} at: {candidate_path}")
                    found = True
                    break

                # Try searching recursively for the file
                for root, dirs, files in os.walk(search_dir):
                    _prune_dirs(dirs)
                    if possible_name in files:
                        candidate_path = os.path.join(root, possible_name)
                        package_files.add(candidate_path)
                        print(f"✅ Found package {package_name} at: {candidate_path}")
                        found = True
                        break
                if found:
                    break
            if found:
                break

        if not found:
            print(f"⚠️  Could not find file for package: {package_name}")

    return package_files

_PKG_DEF_RE = re.compile(r'^\s*package\s+(\w+)\s*;', re.MULTILINE)
_PKG_SCOPE_RE = re.compile(r'(\w+)\s*::')

def _read_text(path):
    try:
        with open(path, 'r', errors='ignore') as fh:
            return fh.read()
    except OSError:
        return ''

def _strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'//.*$', '', text, flags=re.MULTILINE)
    return text

# Directories that hold verification/example code (not synthesizable DUT sources)
# and must be skipped so we don't pull testbench packages into the analysis file list.
_NON_DUT_DIRS = {'test', 'tests', 'tb', 'vendor', 'waves', 'doc', 'docs', 'examples'}

def _prune_dirs(subdirs):
    """In-place filter for os.walk subdirs: drop hidden and non-DUT directories."""
    subdirs[:] = [d for d in subdirs
                  if not d.startswith('.') and d.lower() not in _NON_DUT_DIRS]

def index_package_definitions(scan_roots):
    """Map package_name -> definition file path by walking scan_roots.

    Only ``.sv`` files are considered (packages compiled into the file list are always
    .sv; ``.svh`` files such as assertions.svh/registers.svh are include headers, not
    standalone-compiled packages). Roots are processed in order and the first
    definition wins, so callers should list the most-canonical tree (e.g. the DUT's
    own source dir) first to avoid picking a duplicate copy elsewhere in the repo
    (which would cause redefinition).
    """
    index = {}
    visited = set()
    for root in scan_roots:
        if not root or not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        if root in visited:
            continue
        visited.add(root)
        for dirpath, subdirs, files in os.walk(root):
            _prune_dirs(subdirs)
            for fname in files:
                if not fname.endswith('.sv'):
                    continue
                text = _strip_comments(_read_text(os.path.join(dirpath, fname)))
                for name in _PKG_DEF_RE.findall(text):
                    index.setdefault(name, os.path.join(dirpath, fname))
    return index

def collect_referenced_packages(scan_roots, known_packages):
    """Return the subset of known_packages referenced (via ``name::``) anywhere in
    the source trees rooted at scan_roots."""
    referenced = set()
    visited = set()
    for root in scan_roots:
        if not root or not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        if root in visited:
            continue
        visited.add(root)
        for dirpath, subdirs, files in os.walk(root):
            _prune_dirs(subdirs)
            for fname in files:
                if not fname.endswith(('.sv', '.svh')):
                    continue
                text = _strip_comments(_read_text(os.path.join(dirpath, fname)))
                for name in _PKG_SCOPE_RE.findall(text):
                    if name in known_packages:
                        referenced.add(name)
    return referenced

def order_packages_by_dependency(name_to_file):
    """Topologically order package files so a package is listed before any package
    that references it (dependencies first). Cycles are broken arbitrarily."""
    names = set(name_to_file)
    deps = {}
    for name, fpath in name_to_file.items():
        text = _strip_comments(_read_text(fpath))
        deps[name] = {r for r in _PKG_SCOPE_RE.findall(text) if r in names and r != name}

    ordered = []
    visited = set()

    def visit(n, stack):
        if n in visited or n in stack:
            return
        stack.add(n)
        for d in sorted(deps.get(n, ())):
            visit(d, stack)
        stack.discard(n)
        visited.add(n)
        ordered.append(name_to_file[n])

    for n in sorted(names):
        visit(n, set())
    return ordered

def close_package_dependencies(name_to_file, sources, includes, rtl_file=None):
    """Add the packages the collected packages themselves need, recursively.

    Only what the design imports directly is discovered, and ordering silently
    drops a reference to a package that is not already in the set. div_4bits
    imports drac_pkg alone; drac_pkg is written against riscv_pkg, which nothing
    then asked for, and the analysis failed on a package no one had listed.
    div_unit imports both by hand, which is why this went unnoticed.
    """
    closed = dict(name_to_file)
    pending = list(closed)
    while pending:
        text = _strip_comments(_read_text(closed[pending.pop()]))
        missing = {name for name in _PKG_SCOPE_RE.findall(text) if name not in closed}
        if not missing:
            continue
        for path in find_package_files(missing, sources, includes, rtl_file):
            name = package_name_of_file(path)
            if name and name not in closed:
                print(f"   ↳ {name} is required by a package already listed")
                closed[name] = path
                pending.append(name)
    return closed


def _package_texts_for_parent(rtl_module, sources, includes):
    """Read package sources referenced by ``rtl_module`` for constant folding.

    ``discover_used_packages`` returns a dependency-ordered *list of paths*,
    not a name→path dict. Index by the package each file declares.
    """
    texts = {}
    try:
        needed = discover_used_packages(rtl_module, sources, includes)
    except Exception:
        needed = []
    if isinstance(needed, dict):
        items = list(needed.items())
    else:
        items = []
        for path in needed or []:
            name = package_name_of_file(path)
            if name:
                items.append((name, path))
    for name, path in items:
        if not path or not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8', errors='replace') as handle:
            texts[name] = handle.read()
    return texts


def discover_used_packages(rtl_module, sources, includes):
    """Find definition files for every package referenced (transitively via the DUT
    source tree) and return them ordered dependencies-first.

    detect_packages_from_rtl only inspects the top module and its direct submodules,
    so packages used deep in the hierarchy (e.g. cf_math_pkg via lzc, or
    defs_div_sqrt_mvp via the div/sqrt unit) are missed. VCS does not auto-compile
    packages from -y libraries, so they must be listed explicitly in the file list.
    """
    dut_src_dir = os.path.dirname(os.path.abspath(rtl_module))

    # Package definitions may live in the DUT tree or the wider source dirs, but we
    # only pull in a package if it is actually referenced from within the DUT source
    # subtree — this avoids dragging in unrelated core packages (riscv_pkg, drac_pkg,
    # mmu_pkg, …) that the DUT never uses. DUT dir is listed first so its local copy
    # of a duplicated package (e.g. cf_math_pkg) wins.
    def_roots = [dut_src_dir]
    for s in (sources or []):
        def_roots.append(s)
    if includes:
        def_roots.append(includes)

    pkg_index = index_package_definitions(def_roots)
    referenced = collect_referenced_packages([dut_src_dir], set(pkg_index))
    needed = {name: pkg_index[name] for name in referenced}
    return order_packages_by_dependency(needed)

def package_name_of_file(path):
    """Return the first package name defined in a file, or None."""
    m = _PKG_DEF_RE.search(_strip_comments(_read_text(path)))
    return m.group(1) if m else None

def inject_packages_into_property_file(rtl_module, detected_packages):
    """Inject package imports into the property file.

    Args:
        rtl_module (str): RTL source module path
        detected_packages (set): Set of detected package file paths

    Returns:
        bool: True if successful, False otherwise
    """
    with cli_log.get().step("📦 STEP 1.7: Injecting package imports into property file") as step:
        # Get module name and file paths
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        ft_dir = f'ft_{module_name}'
        sva_dir = os.path.join(ft_dir, 'sva')
        prop_file_path = os.path.join(sva_dir, f'{module_name}_prop.sv')

        if not os.path.exists(prop_file_path):
            print(f"❌ Property file not found: {prop_file_path}")
            print("Make sure scaffold step completed successfully.")
            step.fail()
            return False

        if not detected_packages:
            print("📦 No packages detected, skipping property file injection")
            return True

        try:
            # Read the current property file
            with open(prop_file_path, 'r') as f:
                content = f.read()

            # Extract package names from file paths and create import statements
            package_imports = []
            package_names_seen = set()

            for package_path in detected_packages:
                # The name to import is the one the file declares. Deriving it from
                # the filename produces an import of something that does not exist
                # whenever the two differ, and the property file then fails to
                # compile for a reason that points at the wrong place.
                package_name = package_name_of_file(package_path)
                if not package_name:
                    print(f"⚠️  {package_path} declares no package; not importing it")
                    continue

                # Avoid duplicate imports
                if package_name not in package_names_seen:
                    package_names_seen.add(package_name)
                    # Create import statement
                    import_stmt = f"import {package_name}::*;"
                    package_imports.append(import_stmt)

            # Check if packages are already imported to avoid duplicates
            lines = content.split('\n')
            existing_imports = set()
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('import ') and stripped.endswith('::*;'):
                    # Extract package name from existing import
                    existing_package = stripped.replace('import ', '').replace('::*;', '').strip()
                    existing_imports.add(existing_package)

            # Filter out packages that are already imported
            filtered_imports = []
            for import_stmt in package_imports:
                package_name = import_stmt.replace('import ', '').replace('::*;', '').strip()
                if package_name not in existing_imports:
                    filtered_imports.append(import_stmt)

            if not filtered_imports:
                print("📦 All detected packages are already imported in the property file")
                return True

            # Find the best insertion point for package imports
            insert_index = 0

            # Look for the module declaration line
            for i, line in enumerate(lines):
                if line.strip().startswith('module ') and '_prop' in line:
                    insert_index = i + 1
                    break

            # Insert package imports after module declaration
            if insert_index > 0:
                # Add package imports with proper formatting
                import_lines = []
                import_lines.append('')
                import_lines.append('// Automatically detected package imports')
                import_lines.extend(filtered_imports)
                import_lines.append('')

                # Insert the import lines
                lines[insert_index:insert_index] = import_lines

                # Write the modified content back
                with open(prop_file_path, 'w') as f:
                    f.write('\n'.join(lines))

                print(f"✅ Injected {len(filtered_imports)} package imports into property file:")
                for import_stmt in filtered_imports:
                    print(f"   • {import_stmt}")

                return True
            else:
                print(f"❌ Could not find module declaration in property file")
                step.fail()
                return False

        except Exception as e:
            print(f"❌ Error injecting packages into property file: {str(e)}")
            step.fail()
            return False

def enhanced_create_manual_sub_and_inject_packages(rtl_module, sources, includes):
    """Create enhanced manual_sub.vc file and inject packages into property file.

    Args:
        rtl_module (str): RTL source module path
        sources (list): Source directories
        includes (str): Include directory

    Returns:
        bool: True if successful, False otherwise
    """
    with cli_log.get().step("📦 STEP 1.6: Enhanced package detection and injection") as step:
        # Get module name and file paths
        module_name = os.path.splitext(os.path.basename(rtl_module))[0]
        ft_dir = f'ft_{module_name}'
        manual_sub_path = os.path.join(ft_dir, 'manual_sub.vc')

        if not os.path.exists(ft_dir):
            print(f"❌ FT directory not found: {ft_dir}")
            print("Make sure scaffold step completed successfully.")
            step.fail()
            return False

        try:
            # Detect packages from the RTL module
            detected_packages = detect_packages_from_rtl(rtl_module, sources, includes)

            # Also detect packages from submodules if any
            submodules = detect_submodules(rtl_module)
            if submodules:
                submodule_files = find_submodule_files(submodules, sources, includes)
                for submodule_name, submodule_path in submodule_files.items():
                    print(f"🔍 Checking submodule for packages: {submodule_name}")
                    sub_packages = detect_packages_from_rtl(submodule_path, sources, includes)
                    detected_packages.update(sub_packages)

            # Transitive discovery: packages used deep in the hierarchy (pulled in via -y
            # libraries) are not seen by the top/direct-submodule scan above. VCS does not
            # auto-compile packages from -y dirs, so gather every referenced package's
            # definition file and order them dependencies-first.
            ordered_package_files = discover_used_packages(rtl_module, sources, includes)

            # Merge the two scans, dedup by package NAME so duplicate copies are not
            # listed twice (which would be a redefinition), then order the union.
            # Appending the extras to an already-ordered list is what put drac_pkg,
            # which uses riscv_pkg::VLEN, ahead of riscv_pkg: VCS compiles a file
            # list in order, so the analysis failed on a package that was present.
            by_name = {}
            for path in list(ordered_package_files) + sorted(detected_packages):
                name = package_name_of_file(path)
                if name and name not in by_name:
                    by_name[name] = path
            by_name = close_package_dependencies(by_name, sources, includes, rtl_module)
            ordered_package_files = order_packages_by_dependency(by_name)

            # Create enhanced manual_sub.vc content
            content_lines = []
            content_lines.append("// Add here defines if needed, for example")
            # Left commented: passed through as-is, VCS reports it as an unknown
            # option on every run and the real warnings get lost among them.
            content_lines.append("//+define+MACRO")
            content_lines.append("// Add here further dependencies not captured automatically")
            content_lines.append("//${INC_PATH}/pkg.sv")
            content_lines.append("")

            if ordered_package_files:
                content_lines.append("// Automatically detected packages (dependency order)")
                for package_path in ordered_package_files:
                    # Convert absolute paths to relative paths when possible
                    if package_path.startswith('./'):
                        content_lines.append(package_path)
                    else:
                        # Try to make relative path
                        current_dir = os.getcwd()
                        try:
                            rel_path = os.path.relpath(package_path, current_dir)
                            if not rel_path.startswith('../'):
                                content_lines.append(f"./{rel_path}")
                            else:
                                content_lines.append(package_path)
                        except ValueError:
                            # Can't make relative path (different drives on Windows)
                            content_lines.append(package_path)
                content_lines.append("")
            else:
                content_lines.append("// No packages automatically detected")
                content_lines.append("")

            content_lines.append("")
            content_lines.append("//${SRC_PATH}/submodule.sv")
            content_lines.append("//${DUT_PATH}/submodule.sv")
            content_lines.append("")
            content_lines.append("// Or blackbox modules like")
            content_lines.append("//-bbox_m submodule")
            content_lines.append("")
            content_lines.append("// Or add Macros like")
            content_lines.append("//+define+XPROP=1")

            # Write the enhanced manual_sub.vc file
            with open(manual_sub_path, 'w') as f:
                f.write('\n'.join(content_lines))

            print(f"✅ Created enhanced manual_sub.vc at: {manual_sub_path}")

            if ordered_package_files:
                print(f"📦 Automatically included {len(ordered_package_files)} package files in manual_sub.vc (dependency order):")
                for package_path in ordered_package_files:
                    print(f"   • {package_path}")
            else:
                print("📦 No packages were automatically detected")

            # Now inject package imports into the property file
            if not inject_packages_into_property_file(rtl_module, detected_packages):
                print(f"⚠️  Failed to inject packages into property file, but manual_sub.vc was created")
                step.fail()
                return False

            return True

        except Exception as e:
            print(f"❌ Error in enhanced package detection and injection: {str(e)}")
            step.fail()
            return False

def update_top_level_files_vc_with_submodules(top_level_rtl_module, processed_submodules):
    """Update the top-level module's files.vc file to include submodule property and bind files.

    This allows for better coverage collection when running the top-level module verification,
    as it will also include properties from submodules.

    Args:
        top_level_rtl_module (str): Path to the top-level RTL module
        processed_submodules (dict): Dictionary mapping submodule names to their file paths

    Returns:
        bool: True if successful, False otherwise
    """
    with cli_log.get().step("📋 STEP 6: Updating top-level files.vc with submodule references") as step:
        if not processed_submodules:
            print("📦 No submodules were processed, skipping files.vc update")
            return True

        # Get top-level module info
        top_level_module_name = os.path.splitext(os.path.basename(top_level_rtl_module))[0]
        ft_dir = f'ft_{top_level_module_name}'
        files_vc_path = os.path.join(ft_dir, 'files.vc')

        if not os.path.exists(files_vc_path):
            print(f"❌ Top-level files.vc not found: {files_vc_path}")
            step.fail()
            return False

        try:
            # Read the current files.vc content
            with open(files_vc_path, 'r') as f:
                lines = f.readlines()

            # Find the insertion point (after top-level prop/bind files, before RTL file)
            top_level_prop_line = f"${{PROP_PATH}}/{top_level_module_name}_prop.sv\n"
            top_level_bind_line = f"${{PROP_PATH}}/{top_level_module_name}_bind.svh\n"

            insert_index = -1

            # Look for the top-level bind file line and insert after it
            for i, line in enumerate(lines):
                if line.strip() == top_level_bind_line.strip():
                    insert_index = i + 1
                    break

            if insert_index == -1:
                print(f"❌ Could not find insertion point in files.vc")
                print(f"   Looking for: {top_level_bind_line.strip()}")
                step.fail()
                return False

            # Create submodule file entries
            submodule_lines = []
            submodule_lines.append("// Submodule property and bind files for enhanced coverage\n")

            # Sort submodules for consistent output
            for submodule_name in sorted(processed_submodules.keys()):
                submodule_lines.append(f"${{SVAPSHOT_ROOT}}/ft_{submodule_name}/sva/{submodule_name}_prop.sv\n")
                submodule_lines.append(f"${{SVAPSHOT_ROOT}}/ft_{submodule_name}/sva/{submodule_name}_bind.svh\n")

            # Insert the submodule lines
            lines[insert_index:insert_index] = submodule_lines

            # Write the updated content back
            with open(files_vc_path, 'w') as f:
                f.writelines(lines)

            print(f"✅ Updated files.vc with {len(processed_submodules)} submodule references:")
            for submodule_name in sorted(processed_submodules.keys()):
                print(f"   • {submodule_name}: prop and bind files")

            print(f"📄 Updated file: {files_vc_path}")

            sync_submodule_bind_files_from_parent(
                top_level_rtl_module, processed_submodules)

            return True

        except Exception as e:
            print(f"❌ Error updating files.vc: {str(e)}")
            step.fail()
            return False

def detect_submodule_type(submodule_file):
    """Detect if a submodule is sequential or combinational.

    Args:
        submodule_file (str): Path to the submodule RTL file

    Returns:
        str: 'sequential' or 'combinational'
    """
    # The same rule as for the top module, deliberately: a submodule that is
    # judged differently from its parent gets a testbench built on a different
    # notion of what a clock is. Unreadable files fall back to the safer label,
    # since a clocking block on a module that has no clock cannot elaborate.
    return detect_module_type(submodule_file) or 'combinational'


def apply_external_parent_context(parent_rtl, leaf_rtl, sources, includes):
    """Attach parent #() / folded ``-pvalue`` context while the leaf stays top.

    Passing the leaf as the CLI RTL makes it TOP-LEVEL, so the usual
    submodule walk never sees the parent's instantiations. ``--parent-rtl``
    builds that context without generating the parent.
    """
    if not parent_rtl or not leaf_rtl:
        return '', {}
    parent_path = parent_rtl
    if not os.path.isfile(parent_path):
        parent_path = find_rtl_module_file(parent_rtl, sources, includes)
    if not os.path.isfile(parent_path):
        raise FileNotFoundError(parent_rtl)
    leaf_name = os.path.splitext(os.path.basename(os.path.abspath(leaf_rtl)))[0]
    package_texts = _package_texts_for_parent(parent_path, sources, includes)
    package_texts.update(_package_texts_for_parent(leaf_rtl, sources, includes))
    contexts = build_contexts_for_parent(
        os.path.abspath(parent_path),
        {leaf_name: os.path.abspath(leaf_rtl)},
        package_texts,
    )
    context = contexts.get(leaf_name)
    if context is None:
        return '', {}
    return (
        format_instantiation_prompt(context),
        dict(context.resolved_elaboration_overrides),
    )


def main():
    """Main orchestration function."""
    _print_startup_banner()

    # Parse arguments
    args = parse_arguments()
    cli = cli_log.configure(args.verbosity)
    if not cli.quiet_steps:
        print("=" * 80)
        print(f"🔧 Original DUT_ROOT={os.environ.get('DUT_ROOT')}")
        print(f"📋 Configuration:")
        print(f"   RTL Module: {args.rtl_module}")
        print(f"   LLM Model: {args.llm_model}")
        print(f"   Module Type: {args.module_type}"
              f"{' (detected per module from the RTL)' if args.module_type == 'auto' else ''}")
        print(f"   Execution Type: {args.execution_type}")
        print(f"   Verbosity: {args.verbosity}")
        print(f"   Sources: {args.sources}")
        print(f"   Includes: {args.includes}")
        print(f"   Assertion Source: {args.assertion_source}")
        print(f"   Initial File: {args.initial_file}")
        print(f"   Package Auto-detection: {'DISABLED' if args.disable_package_detection else 'ENABLED'}")
        print(f"   Formal Tool: {args.formal_tool}")
        if args.parent_rtl:
            print(f"   Parent RTL: {args.parent_rtl}")
        print()

    # Find the RTL module file (check current dir first, then source dirs)
    rtl_module_path = find_rtl_module_file(args.rtl_module, args.sources, args.includes)
    if not cli.quiet_steps:
        print(f"🔧 Original DUT_ROOT={os.environ.get('DUT_ROOT')}")
    if not os.path.exists(rtl_module_path):
        cli.error(f"❌ RTL module not found: {args.rtl_module}")
        cli.error(
            f"   Searched in directories: "
            f"{args.sources + [args.includes] if args.includes else args.sources}")
        sys.exit(1)

    # Update the module path for all subsequent operations
    args.rtl_module = rtl_module_path
    if not cli.quiet_steps:
        print(f"📄 Using RTL module: {args.rtl_module}")

    # PHASE 1: Detect submodules from the top-level module
    cli.banner("🔍 PHASE 1: Detecting submodules in top-level RTL", width=80)

    detected_submodules = detect_submodules(args.rtl_module)
    if not cli.quiet_steps:
        print(f"🔧 Original DUT_ROOT={os.environ.get('DUT_ROOT')}")
    if detected_submodules:
        if not cli.quiet_steps:
            print(f"📊 Found {len(detected_submodules)} unique submodules:")
            for submodule in sorted(detected_submodules):
                print(f"   • {submodule}")

        # Find actual file paths for the submodules
        submodule_files = find_submodule_files(detected_submodules, args.sources, args.includes)

        if not cli.quiet_steps:
            print(f"\n📂 Located {len(submodule_files)} submodule files:")
            for name, path in submodule_files.items():
                print(f"   • {name}: {path}")

        if len(submodule_files) < len(detected_submodules):
            missing = detected_submodules - set(submodule_files.keys())
            print(f"\n⚠️  Could not locate {len(missing)} submodules: {', '.join(sorted(missing))}")

        # Direct instantiations only: the flow generates properties for these
        # leaves, not for modules deeper in the hierarchy. Shared overrides
        # become polarity targets; differing ones stay parametric in the prompt.
        # Parent constants that fold to literals are also applied at leaf
        # FPV elaborate so header defaults cannot form an illegal combo.
        package_texts = _package_texts_for_parent(
            args.rtl_module, args.sources, args.includes)
        parent_contexts = build_contexts_for_parent(
            args.rtl_module, submodule_files, package_texts)
        instantiation_contexts = {
            name: format_instantiation_prompt(context)
            for name, context in parent_contexts.items()
        }
        elaboration_overrides_by_module = {
            name: dict(context.resolved_elaboration_overrides)
            for name, context in parent_contexts.items()
        }
        for name, text in sorted(instantiation_contexts.items()):
            if text and not cli.quiet_steps:
                print(f"📎 Parent instantiation context for {name}:")
                for line in text.splitlines()[:8]:
                    print(f"   {line}")
                if text.count('\n') >= 8:
                    print("   …")
            resolved = elaboration_overrides_by_module.get(name) or {}
            if resolved and not cli.quiet_steps:
                shown = ', '.join(
                    f'{key}={value}' for key, value in sorted(resolved.items()))
                print(f"📎 Parent constants for {name} FPV elaborate: {shown}")
    else:
        if not cli.quiet_steps:
            print("📊 No submodules detected in the top-level RTL module.")
        submodule_files = {}
        instantiation_contexts = {}
        elaboration_overrides_by_module = {}

    leaf_parent_instantiation = ''
    leaf_parent_overrides = {}
    if args.parent_rtl:
        try:
            leaf_parent_instantiation, leaf_parent_overrides = (
                apply_external_parent_context(
                    args.parent_rtl, args.rtl_module, args.sources,
                    args.includes))
        except FileNotFoundError:
            cli.error(f"❌ Parent RTL not found: {args.parent_rtl}")
            sys.exit(1)
        if leaf_parent_instantiation and not cli.quiet_steps:
            print(f"📎 External parent context from {args.parent_rtl}:")
            for line in leaf_parent_instantiation.splitlines()[:8]:
                print(f"   {line}")
            if leaf_parent_instantiation.count('\n') >= 8:
                print("   …")
        if leaf_parent_overrides and not cli.quiet_steps:
            shown = ', '.join(
                f'{key}={value}'
                for key, value in sorted(leaf_parent_overrides.items()))
            print(f"📎 Parent constants for leaf FPV elaborate: {shown}")
        elif not cli.quiet_steps:
            print(
                "📎 Parent RTL attached; no folded -pvalue literals "
                "(empty #() or unresolvable expressions).")

    # PHASE 2: Create processing order (top-level first, then submodules)
    cli.banner("📋 PHASE 2: Planning module processing order", width=80)

    modules_to_process = [(args.rtl_module, 'TOP-LEVEL')]

    # Add submodules that were successfully located
    for submodule_name, submodule_path in submodule_files.items():
        modules_to_process.append((submodule_path, submodule_name))

    if not cli.quiet_steps:
        print(f"📝 Processing order ({len(modules_to_process)} modules):")
        for i, (module_path, module_label) in enumerate(modules_to_process, 1):
            print(f"   {i}. {module_label}: {module_path}")

    # PHASE 3: Process each module
    cli.banner("🚀 PHASE 3: Processing modules", width=80)

    successful_modules = []
    failed_modules = []
    successful_submodules = {}  # Track successfully processed submodules for files.vc update

    start_time = time.time()

    for i, (module_path, module_label) in enumerate(modules_to_process, 1):
        if not cli.quiet_steps:
            print(f"\n{'🔥' if module_label == 'TOP-LEVEL' else '🧩'} Processing module {i}/{len(modules_to_process)}: {module_label}")

        # Validate and resolve module path for consistency
        module_path_abs = os.path.abspath(module_path)
        if not os.path.exists(module_path_abs):
            cli.error(f"❌ Module file not found: {module_path_abs}")
            failed_modules.append((module_path, module_label))
            continue

        if not cli.quiet_steps:
            print(f"   Module path: {module_path_abs}")
            print(f"   Module exists: ✅")

        # Use original settings for top-level, but may need to adjust for submodules
        current_assertion_source = args.assertion_source
        current_initial_file = args.initial_file

        # A supplied initial file applies only to the top-level.  Submodules
        # always receive their own RTL-driven initial generation.
        if module_label != 'TOP-LEVEL' and args.assertion_source == 'file':
            if not cli.quiet_steps:
                print(f"   ℹ️  Generating initial assertions from RTL for submodule {module_label}")
            current_assertion_source = 'rtl'

        # Every module is classified from its own RTL, top level included. A
        # sequential parent says nothing about a combinational child, and the
        # command line says nothing about either.
        current_module_type = resolve_module_type(module_path_abs, args.module_type)
        if not cli.quiet_steps:
            print(f"   🔍 Module type for {module_label}: {current_module_type}")

        success = run_single_module_flow(
            module_path_abs,  # Use the validated absolute path
            args.llm_model,
            current_module_type,
            args.execution_type,
            args.verbosity,
            args.sources,
            args.includes,
            current_assertion_source,
            current_initial_file,
            args.disable_package_detection,
            args.formal_tool,
            args.verilog_std,
            instantiation_context=(
                leaf_parent_instantiation
                if module_label == 'TOP-LEVEL'
                else instantiation_contexts.get(module_label, '')
            ),
            elaboration_overrides=(
                leaf_parent_overrides
                if module_label == 'TOP-LEVEL'
                else elaboration_overrides_by_module.get(module_label, {})
            ),
            stop_after_extension=args.stop_after_extension,
            resume_from_semantic=args.resume_from_semantic,
            resume_from_extension=args.resume_from_extension,
            stop_after_scaffold=args.stop_after_scaffold,
        )

        if success:
            successful_modules.append((module_path_abs, module_label))
            # Track successful submodules for files.vc update
            if module_label != 'TOP-LEVEL':
                successful_submodules[module_label] = module_path_abs
            print(f"✅ Successfully completed module: {module_label}")
        else:
            failed_modules.append((module_path_abs, module_label))
            cli.error(f"❌ Failed to process module: {module_label}")

            # Ask user if they want to continue with remaining modules
            if i < len(modules_to_process):
                print(f"\n⚠️  Module {module_label} failed. {len(modules_to_process) - i} modules remaining.")
                try:
                    response = input("Continue with remaining modules? (y/n): ").strip().lower()
                    if response not in ['y', 'yes']:
                        print("🛑 User chose to stop processing.")
                        break
                except KeyboardInterrupt:
                    print("\n🛑 Processing interrupted by user.")
                    break

    end_time = time.time()
    total_time = end_time - start_time

    # PHASE 4: Update top-level files.vc with submodule references (if successful)
    if successful_modules and successful_submodules:
        # Find the top-level module from successful modules
        top_level_module_info = None
        for module_path, module_label in successful_modules:
            if module_label == 'TOP-LEVEL':
                top_level_module_info = (module_path, module_label)
                break

        if top_level_module_info:
            cli.banner("📋 PHASE 4: Enhancing top-level testbench with submodule coverage", width=80)

            update_success = update_top_level_files_vc_with_submodules(
                top_level_module_info[0],
                successful_submodules
            )

            if update_success:
                print("✅ Top-level files.vc successfully enhanced with submodule references")
                if args.formal_tool == 'vcformal':
                    if not cli.quiet_steps:
                        print("\n📋 Refreshing VC Formal file list so files_vcf.vc includes submodule prop/bind files…")
                    if not generate_fpv_vcf_tcl(
                        top_level_module_info[0],
                        resolve_module_type(top_level_module_info[0], args.module_type),
                        regenerate_after_submodule_merge=True,
                        verilog_std=args.verilog_std,
                        dut_root_override=os.path.dirname(
                            os.path.abspath(top_level_module_info[0])),
                    ):
                        print("⚠️  Failed to regenerate files_vcf.vc / FPV_vcf.tcl after submodule merge")
                    else:
                        print("✅ files_vcf.vc and FPV_vcf.tcl updated for VC Formal")
            else:
                print("⚠️  Failed to update top-level files.vc, but module processing was successful")
        else:
            print("\n⚠️  Top-level module was not processed successfully, skipping files.vc update")
    else:
        if not cli.quiet_steps:
            print("\n📦 No submodules were successfully processed, skipping files.vc update")

    # FINAL SUMMARY — always printed
    print("\n" + "=" * 80)
    print("🎉 AI-SVA EXECUTION SUMMARY")
    print("=" * 80)

    print(f"⏱️  Total execution time: {total_time:.2f} seconds")
    print(f"📊 Modules processed: {len(successful_modules) + len(failed_modules)}/{len(modules_to_process)}")
    print(f"✅ Successful: {len(successful_modules)}")
    print(f"❌ Failed: {len(failed_modules)}")

    if successful_modules:
        print(f"\n✅ Successfully processed modules:")
        for module_path, module_label in successful_modules:
            module_name = os.path.basename(module_path).replace('.sv', '').replace('.v', '')
            print(f"   • {module_label}: {module_name}")
            print(f"     Property files: ft_{module_name}/sva/")

    if failed_modules:
        print(f"\n❌ Failed modules:")
        for module_path, module_label in failed_modules:
            print(f"   • {module_label}: {module_path}")

    if not cli.quiet_steps:
        print(f"\n📂 Configuration used:")
        print(f"   Assertion source: {args.assertion_source}")
        if args.assertion_source == 'file':
            print(f"   Initial file: {args.initial_file}")
        print(f"   Execution type: {args.execution_type}")
        print(f"   Formal tool: {args.formal_tool}")
        print(f"   Submodules detected: {len(detected_submodules)}")
        print(f"   Submodules located: {len(submodule_files)}")
        print(f"   Package auto-detection: {'DISABLED' if args.disable_package_detection else 'ENABLED'}")

        print(f"\n💡 Check the GUI output from the agent for detailed results of each module.")
        if not args.disable_package_detection:
            print(f"💡 Manual_sub.vc files and property files have been enhanced with automatically detected packages.")
        else:
            print(f"💡 Manual_sub.vc and property files use default template (package auto-detection was disabled).")

        if successful_submodules:
            print(f"💡 Top-level files.vc has been enhanced with submodule property/bind files for better coverage collection.")
        else:
            print(f"💡 No submodules were processed, so top-level files.vc uses standard configuration.")

    # Exit with appropriate code
    if failed_modules:
        print(f"\n⚠️  Some modules failed. Check logs above for details.")
        sys.exit(1)
    else:
        print(f"\n🎉 All modules processed successfully!")
        sys.exit(0)


if __name__ == '__main__':
    main()