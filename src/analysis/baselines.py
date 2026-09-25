#!/usr/bin/env python3
"""Baseline comparison for SVApshot under a single shared formal harness.

A claim that SVApshot is better than one-shot prompting, LASSO or PALM
is only meaningful if every method is measured the same way.  This module makes
that the default: each method contributes nothing but a *property set*, and the
identical pipeline then

* installs it in the same ``ft_<module>`` testbench, with the same clock, reset,
  elaboration settings and assumptions,
* qualifies every property with the same five-way parser (:mod:`proof_status`),
* replays the same mutant population against it (:mod:`evaluate`), and
* prices it with the same token accounting (:mod:`llm_cost`).

The comparison then reports, per method: properties proved non-vacuously,
cone-of-influence diversity, mutation detection rate, wall time, LLM spend, and
the LLM Efficiency Score

    LES = proved_non_vacuous x mutation_detection_rate / max(cost, floor)

which exists precisely so that a method cannot win by emitting many cheap,
trivially provable properties: a property set that catches no injected fault
scores zero.

Methods
-------
``svapshot``    the property file produced by a completed agent run.
``one_shot``    one LLM call with the same templated prompt and no repair loop,
                which isolates the contribution of lint and formal feedback.
``imported``    a property file produced elsewhere — this is how published
                LASSO and PALM artifacts are brought under the same harness,
                with their reported cost declared explicitly.

Usage::

    python3 src/analysis/baselines.py compare --rtl benchmarks/sargantana/.../rr_arb_tree.sv \\
        --methods svapshot,one_shot --mutants 18 --llm gpt-4.1-mini

    python3 src/analysis/baselines.py compare --rtl ... --methods imported \\
        --import lasso=artifacts/lasso_rr_arb_tree.sv --import-cost lasso=0.42
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

CORE_DIR = Path(__file__).resolve().parents[1] / 'core'
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import coi as coi_analysis
import mutation
from assertion_template import (
    DESIGNER_MARKER,
    STRUCTURED_OUTPUT_EXAMPLE,
    TEMPLATE_RULES,
    extract_assertions_from_response,
    get_assertion_name,
    split_designer_assertion_blocks,
)
from evaluate import EvaluationConfig, MutationEvaluator, PropertySetRejected
from formal_harness import FormalHarness
from llm_cost import CostLedger, extract_usage, load_price_overrides
from metrics_report import SensitivityReport, SnapshotMetrics, metrics_from_run
from mutation import Mutant, MutationConfig


# ---------------------------------------------------------------------------
# Property sets
# ---------------------------------------------------------------------------


@dataclass
class PropertySet:
    """What a generation method contributes to the comparison."""

    method: str
    assertions: List[str] = field(default_factory=list)
    #: Full property-file text, assembled against the shared checker shell.
    text: str = ''
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    currency: str = 'EUR'
    generation_seconds: float = 0.0
    #: Where the properties came from, for the provenance record.
    origin: str = ''
    available: bool = True
    unavailable_reason: str = ''
    #: True when the property set was produced but does not elaborate.
    compile_failed: bool = False

    def to_dict(self) -> dict:
        return {
            'method': self.method,
            'assertion_count': len(self.assertions),
            'llm_calls': self.llm_calls,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'cost': round(self.cost, 6),
            'currency': self.currency,
            'generation_seconds': round(self.generation_seconds, 1),
            'origin': self.origin,
            'available': self.available,
            'unavailable_reason': self.unavailable_reason,
            'compile_failed': self.compile_failed,
        }


def checker_shell(property_text: str) -> str:
    """Everything in the property file before the generated assertions.

    Keeping this fixed is what makes the harness shared: the module header,
    clocking, reset and bind structure are identical for every method, and only
    the assertions differ.
    """
    if DESIGNER_MARKER in property_text:
        return property_text.split(DESIGNER_MARKER, 1)[0] + DESIGNER_MARKER + '\n'
    return property_text.split('endmodule', 1)[0]


def assertion_cap() -> int:
    """DATE / snapshot cap. 0 means do not truncate."""
    raw = os.environ.get('SVAPSHOT_MAX_ASSERTIONS') or ''
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def keep_first_assertion_ids(assertions: Sequence[str]) -> List[str]:
    """Drop later copies of the same label so VC Formal does not IPD."""
    seen = set()
    unique: List[str] = []
    for assertion in assertions:
        name = get_assertion_name(assertion) or assertion.strip()
        if name in seen:
            continue
        seen.add(name)
        unique.append(assertion)
    return unique


def sanitize_one_shot_assertions(assertions: Sequence[str]) -> List[str]:
    """Unique labels, then the shared assertion cap. Not a second LLM call."""
    unique = keep_first_assertion_ids(assertions)
    cap = assertion_cap()
    if cap and len(unique) > cap:
        return unique[:cap]
    return unique


def assemble_property_file(shell: str, assertions: Sequence[str]) -> str:
    body = shell if shell.endswith('\n') else shell + '\n'
    for assertion in keep_first_assertion_ids(assertions):
        body += '\n' + assertion.rstrip() + '\n'
    return body + '\nendmodule\n'


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------


class BaselineMethod:
    """A source of properties for one module."""

    name = 'baseline'

    def produce(self, harness: FormalHarness, log) -> PropertySet:
        raise NotImplementedError


class SnapshotMethod(BaselineMethod):
    """The property file left behind by a completed SVApshot run."""

    name = 'svapshot'

    def __init__(self, cost_report: str = ''):
        self.cost_report = cost_report

    def produce(self, harness: FormalHarness, log) -> PropertySet:
        text = harness.read_properties()
        assertions = split_designer_assertion_blocks(text)
        result = PropertySet(
            method=self.name,
            assertions=assertions,
            text=text,
            origin=harness.paths.property_file,
        )

        # The agent writes its ledger next to the property file; reading it is
        # the only way the comparison can price SVApshot honestly, including the
        # repair calls that the baselines never make.
        path = self.cost_report or os.path.join(
            os.path.dirname(harness.paths.property_file), 'llm_cost.json')
        if os.path.exists(path):
            with open(path) as handle:
                ledger = json.load(handle)
            result.llm_calls = ledger.get('total_calls', 0)
            result.input_tokens = ledger.get('total_input_tokens', 0)
            result.output_tokens = ledger.get('total_output_tokens', 0)
            result.cost = float(ledger.get('total_cost', 0.0))
            result.currency = ledger.get('currency', 'EUR')
            result.generation_seconds = float(ledger.get('total_llm_seconds', 0.0))
        else:
            log(f'  no cost ledger at {path}; SVApshot cost reported as unknown (0)')
        return result


class OneShotMethod(BaselineMethod):
    """One LLM call with the templated prompt and no repair of any kind.

    This is the control for the whole pipeline: same model, same rules, same
    output template, but no lint feedback and no formal feedback.  The gap
    between it and ``svapshot`` is what the repair loops buy.
    """

    name = 'one_shot'

    def __init__(self, model: str, temperature: Optional[float] = None):
        self.model = model
        self.temperature = temperature

    def produce(self, harness: FormalHarness, log) -> PropertySet:
        load_price_overrides()
        ledger = CostLedger(model=self.model)
        rtl = harness.read_rtl()
        module = harness.paths.module

        rules = TEMPLATE_RULES.replace('MODULE', module)
        prompt = (
            f'{rules}\n'
            f'Example output format:\n{STRUCTURED_OUTPUT_EXAMPLE}\n'
            f'The RTL module is {module}.\n'
            'Write assertions covering ALL functionality except reset behaviour.\n'
            'RTL:\n'
            f'{rtl}\n'
            'Use unqualified checker port and parameter names only. Do NOT write '
            f'{module}.signal — declare DUT internals as checker inputs instead.\n'
            'Return ONLY assertion blocks in the structured --- id/property/failure '
            '--- format.'
        )
        messages = [
            {'role': 'system',
             'content': 'You are a coding assistant. Your responses should ONLY '
                        'provide syntactically correct code'},
            {'role': 'user', 'content': prompt},
        ]

        start = time.time()
        response, usage = _single_completion(self.model, messages, self.temperature)
        seconds = time.time() - start

        if usage:
            input_tokens, output_tokens = usage['input_tokens'], usage['output_tokens']
            exact = True
        else:
            input_tokens = sum(ledger.count_tokens(m['content']) for m in messages)
            output_tokens = ledger.count_tokens(response)
            exact = False
        ledger.record('one_shot_generation', input_tokens, output_tokens, seconds, exact)

        assertions = extract_assertions_from_response(response)
        produced = len(assertions)
        assertions = sanitize_one_shot_assertions(assertions)
        cap = assertion_cap()
        extra = ''
        if produced != len(assertions):
            extra = f' (kept {len(assertions)} unique'
            extra += f', cap {cap}' if cap else ''
            extra += f' from {produced})'
        log(
            f'  one-shot produced {produced} assertion(s) in {seconds:.0f}s'
            f'{extra}'
        )

        return PropertySet(
            method=self.name,
            assertions=assertions,
            llm_calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=ledger.total_cost,
            currency=ledger.currency,
            generation_seconds=seconds,
            origin=f'single completion from {self.model}',
            available=bool(assertions),
            unavailable_reason='' if assertions else 'model returned no parsable assertion',
        )


class ImportedMethod(BaselineMethod):
    """A property set generated outside this repository.

    LASSO and PALM have no runnable implementation here; the defensible way to
    compare against them is to take their published artifact for the same
    module and measure it under this harness, recording the cost their authors
    report rather than inventing one.
    """

    def __init__(self, name: str, path: str, cost: float = 0.0,
                 llm_calls: int = 0, note: str = ''):
        self.name = name
        self.path = path
        self.cost = cost
        self.llm_calls = llm_calls
        self.note = note

    def produce(self, harness: FormalHarness, log) -> PropertySet:
        if not os.path.exists(self.path):
            return PropertySet(
                method=self.name, available=False,
                unavailable_reason=f'{self.path} not found')
        with open(self.path) as handle:
            text = handle.read()
        assertions = split_designer_assertion_blocks(text) or \
            extract_assertions_from_response(text)
        return PropertySet(
            method=self.name,
            assertions=assertions,
            cost=self.cost,
            llm_calls=self.llm_calls,
            origin=f'{os.path.abspath(self.path)} {self.note}'.strip(),
            available=bool(assertions),
            unavailable_reason='' if assertions else 'no assertions found in the file',
        )


def _single_completion(model: str, messages, temperature: Optional[float]):
    """Issue one completion, returning ``(text, usage)``.

    Mirrors the transport selection the agent uses so the baseline talks to the
    same endpoint with the same parameters; only the surrounding loop differs.
    """
    from agent import resolve_model_config
    from openai import OpenAI

    config = resolve_model_config(model)
    if config is None:
        raise ValueError(f'Model {model} is not in the SVApshot model table')

    if config['type'] == 'cursor_sdk':
        import cursor_llm

        prompt = '\n\n'.join(m['content'] for m in messages)
        text = cursor_llm.generate(config.get('cursor_model') or model, prompt)
        return text, None

    if config['type'] in ('copilot_cli', 'cursor_cli'):
        import subprocess

        prompt = '\n\n'.join(m['content'] for m in messages)
        command = (['copilot', '-p', prompt] if config['type'] == 'copilot_cli'
                   else ['agent', '-p', '--mode', 'ask', '--trust', prompt])
        completed = subprocess.run(command, capture_output=True, text=True)
        return completed.stdout.strip(), None

    if config['type'] == 'nvidia':
        client = OpenAI(api_key=os.environ.get('NVIDIA_API_KEY'),
                        base_url='https://integrate.api.nvidia.com/v1')
    else:
        client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))

    if config['type'] == 'openai_responses':
        completion = client.responses.create(
            model=model,
            reasoning={'effort': 'medium'},
            input='\n\n'.join(m['content'] for m in messages),
        )
        return completion.output_text or '', extract_usage(completion)

    params = {
        'model': model,
        'messages': messages,
        'temperature': temperature if temperature is not None else config['temperature'],
    }
    if config.get('supports_top_p'):
        params['top_p'] = 0.7
    if config['type'] == 'openai' and ('o3' in model or 'o4' in model):
        params['max_completion_tokens'] = config['max_completion_tokens']
    else:
        params['max_tokens'] = config['max_completion_tokens']

    completion = client.chat.completions.create(**params)
    text = completion.choices[0].message.content if completion.choices else ''
    return text or '', extract_usage(completion)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass
class ComparisonConfig:
    rtl: str
    formal_tool: str = 'vcformal'
    mutants: int = 18
    max_per_class: int = 3
    timeout_s: Optional[float] = None
    output_dir: str = ''
    les_cost_floor: float = 0.01
    model: str = ''
    mutant_manifest: str = ''
    mutant_source_dir: str = ''


class BaselineComparison:
    """Measure several property sets on one module under one harness."""

    def __init__(self, config: ComparisonConfig, methods: Sequence[BaselineMethod], log=None):
        self.config = config
        self.methods = list(methods)
        self.log = log or (lambda message: print(message, flush=True))
        self.harness = FormalHarness(
            config.rtl, config.formal_tool, log=self.log, timeout_s=config.timeout_s)
        self.module = self.harness.paths.module
        self.output_dir = config.output_dir or os.path.join('baseline_reports', self.module)
        self.results: Dict[str, SnapshotMetrics] = {}
        self.property_sets: Dict[str, PropertySet] = {}

    def shared_mutants(self) -> List[Mutant]:
        """One mutant population, shared by every method being compared."""
        if self.config.mutant_manifest:
            mutants = mutation.load_mutants(
                self.config.mutant_manifest, self.config.mutant_source_dir)
            if self.config.mutants == 0:
                mutants = []
            elif self.config.mutants and len(mutants) > self.config.mutants:
                by_class: Dict[object, List[Mutant]] = {}
                for mutant in mutants:
                    by_class.setdefault(mutant.fault_class, []).append(mutant)
                selected: List[Mutant] = []
                round_index = 0
                classes = sorted(by_class, key=lambda value: value.value)
                while len(selected) < self.config.mutants:
                    progressed = False
                    for fault_class in classes:
                        population = by_class[fault_class]
                        if (
                            round_index < len(population)
                            and round_index < self.config.max_per_class
                        ):
                            selected.append(population[round_index])
                            progressed = True
                            if len(selected) == self.config.mutants:
                                break
                    if not progressed:
                        break
                    round_index += 1
                mutants = selected
            origin = f'frozen population {self.config.mutant_manifest}'
        else:
            mutants = mutation.generate_mutants(
                self.harness.read_rtl(),
                self.module,
                MutationConfig(max_per_class=self.config.max_per_class,
                               max_total=self.config.mutants),
            )
            origin = 'generated population'
        self.log(f'Shared mutant population: {len(mutants)} mutants ({origin})')
        return mutants

    def measure(self, property_set: PropertySet, mutants: Sequence[Mutant]) -> SnapshotMetrics:
        config = EvaluationConfig(
            rtl=self.config.rtl,
            formal_tool=self.config.formal_tool,
            timeout_s=self.config.timeout_s,
            output_dir=self.output_dir,
            keep_mutant_sources=False,
            property_text=property_set.text,
            method=property_set.method,
        )
        evaluator = MutationEvaluator(config, log=self.log)
        try:
            sensitivity, applied, population = evaluator.measure(mutants)
        except PropertySetRejected as rejected:
            # An unusable property set scores zero on every axis. That is the
            # honest outcome for a method with no syntax repair, and hiding it
            # behind an aborted comparison would flatter the baseline.
            self.log(f'  {rejected}')
            property_set.compile_failed = True
            property_set.unavailable_reason = str(rejected)
            metrics = metrics_from_run(
                module=self.module, formal_result=None,
                sensitivity=SensitivityReport(mutants_generated=len(mutants)),
                model=self.config.model, formal_tool=self.config.formal_tool,
                method=property_set.method)
            metrics.les_cost_floor = self.config.les_cost_floor
            metrics.qualification.total_properties = len(property_set.assertions)
            metrics.effort.llm_calls = property_set.llm_calls
            metrics.effort.input_tokens = property_set.input_tokens
            metrics.effort.output_tokens = property_set.output_tokens
            metrics.effort.llm_cost = property_set.cost
            metrics.effort.currency = property_set.currency
            metrics.effort.llm_seconds = property_set.generation_seconds
            metrics.effort.formal_seconds = evaluator.harness.total_formal_seconds
            metrics.effort.formal_runs = len(evaluator.harness.runs)
            return metrics
        evaluator.write_reports(population, applied)

        diversity = None
        try:
            graph = coi_analysis.build_signal_graph(
                self.harness.read_rtl(), self.module,
                coi_analysis.language_of(self.harness.rtl_source))
            contract_assertions = [
                block for block in property_set.assertions
                if coi_analysis.assertion_name(block) in set(evaluator.contract)
            ]
            cones = coi_analysis.compute_property_cones(contract_assertions, graph)
            diversity = coi_analysis.compute_diversity(cones, graph)
        except Exception as error:
            self.log(f'  cone analysis failed: {error}')

        metrics = metrics_from_run(
            module=self.module,
            formal_result=evaluator.reference,
            diversity_metrics=diversity,
            sensitivity=sensitivity,
            model=self.config.model,
            formal_tool=self.config.formal_tool,
            method=property_set.method,
        )
        metrics.les_cost_floor = self.config.les_cost_floor
        metrics.effort.llm_calls = property_set.llm_calls
        metrics.effort.input_tokens = property_set.input_tokens
        metrics.effort.output_tokens = property_set.output_tokens
        metrics.effort.llm_cost = property_set.cost
        metrics.effort.currency = property_set.currency
        metrics.effort.llm_seconds = property_set.generation_seconds
        metrics.effort.formal_seconds = evaluator.harness.total_formal_seconds
        metrics.effort.formal_runs = len(evaluator.harness.runs)
        return metrics

    def run(self) -> int:
        if not self.harness.is_ready():
            self.log('Formal testbench missing; scaffolding a fresh harness...')
            try:
                import main as ai_sva_main
            except ImportError:
                self.log(
                    'Formal testbench is incomplete; cannot import main.py '
                    'to scaffold ft_<module>/.')
                return 1
            if not ai_sva_main.prepare_formal_harness(
                    self.harness.rtl_source,
                    formal_tool=self.config.formal_tool):
                self.log('Formal testbench scaffolding failed.')
                return 1
            if not self.harness.is_ready():
                self.log(
                    'Formal testbench is incomplete after scaffolding; '
                    'run main.py --stop-after-scaffold for this module first.')
                return 1

        shell = checker_shell(self.harness.read_properties())
        mutants = self.shared_mutants()
        if not mutants:
            self.log(
                'No mutants in the shared population; scoring qualification only.')

        for method in self.methods:
            self.log('')
            self.log('=' * 72)
            self.log(f'METHOD: {method.name}')
            self.log('=' * 72)

            property_set = method.produce(self.harness, self.log)
            if not property_set.available:
                self.log(f'  not applicable: {property_set.unavailable_reason}')
                self.property_sets[method.name] = property_set
                continue

            if method.name in {'one_shot', 'no_rag'}:
                property_set.assertions = sanitize_one_shot_assertions(
                    property_set.assertions)
            if not property_set.text:
                property_set.text = assemble_property_file(shell, property_set.assertions)
            elif method.name in {'one_shot', 'no_rag'}:
                property_set.text = assemble_property_file(
                    checker_shell(property_set.text), property_set.assertions)
            if method.name != 'svapshot':
                self._promote_internals(property_set)
            self.property_sets[method.name] = property_set
            self._save_property_file(property_set)

            self.results[method.name] = self.measure(property_set, mutants)

        self._report(mutants)
        if not self.results:
            self.log(
                'No method produced a scoreable property set; comparison failed.')
            return 2
        return 0

    def _promote_internals(self, property_set: PropertySet) -> None:
        """Declare DUT internals as checker ports, same as SVApshot.

        One-shot used to drop ``cycle_reg`` / hierarchical names into the
        scaffold shell. VC Formal then reported Error-[IND] and the method
        scored as 'does not elaborate'. Port promotion is not syntax repair;
        it is the same bind rewrite the snapshot path already applies.
        """
        try:
            import bind_internals
            import coi as coi_analysis
            import rtl_clocking
        except ImportError as error:
            self.log(f'  internal-port promotion unavailable: {error}')
            return
        rtl = self.harness.read_rtl()
        module = self.harness.paths.module
        try:
            graph = coi_analysis.build_signal_graph(
                rtl, module, coi_analysis.language_of(self.harness.rtl_source))
        except Exception as error:
            self.log(f'  signal graph failed; leaving one-shot text as written: {error}')
            return
        bind_path = self.harness.paths.bind_file
        bind_text = ''
        if os.path.isfile(bind_path):
            with open(bind_path, encoding='utf-8', errors='replace') as handle:
                bind_text = handle.read()
        result, new_prop, new_bind = bind_internals.apply_port_promotion(
            property_set.assertions,
            module_name=module,
            graph=graph,
            rtl_text=rtl,
            prop_text=property_set.text,
            bind_text=bind_text,
        )
        if rtl_clocking.module_type(rtl) == 'combinational':
            new_prop = rtl_clocking.ensure_combinational_clocking(new_prop)
        property_set.text = new_prop
        if result.assertions:
            property_set.assertions = list(result.assertions)
        if new_bind and new_bind != bind_text and bind_path:
            os.makedirs(os.path.dirname(os.path.abspath(bind_path)) or '.', exist_ok=True)
            with open(bind_path, 'w', encoding='utf-8') as handle:
                handle.write(new_bind)
        self.log(
            f'  promoted {len(result.needed_ports)} internal checker port(s) '
            f'for one-shot elaboration'
        )

    def _save_property_file(self, property_set: PropertySet) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        path = os.path.join(
            self.output_dir, f'{self.module}_{property_set.method}_prop.sv')
        with open(path, 'w') as handle:
            handle.write(property_set.text)

    def _report(self, mutants: Sequence[Mutant]) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        payload = {
            'module': self.module,
            'formal_tool': self.config.formal_tool,
            'les_cost_floor': self.config.les_cost_floor,
            'mutant_population': [m.mutant_id for m in mutants],
            'property_sets': {n: s.to_dict() for n, s in self.property_sets.items()},
            'metrics': {n: m.to_dict() for n, m in self.results.items()},
        }
        path = os.path.join(self.output_dir, f'{self.module}_baselines.json')
        with open(path, 'w') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)

        for name, metrics in self.results.items():
            snapshot = metrics.to_dict()
            property_set = self.property_sets.get(name)
            if property_set is not None and property_set.compile_failed:
                snapshot['compile_failed'] = True
                qualification = dict(snapshot.get('qualification') or {})
                qualification['compile_failed'] = True
                snapshot['qualification'] = qualification
            metrics_path = os.path.join(self.output_dir, 'snapshot_metrics.json')
            with open(metrics_path, 'w') as handle:
                json.dump(snapshot, handle, indent=2, sort_keys=True, default=str)
            break

        table = format_comparison(self.module, self.results, self.property_sets)
        with open(os.path.join(self.output_dir, f'{self.module}_baselines.txt'), 'w') as handle:
            handle.write(table + '\n')
        self.log('')
        self.log(table)
        self.log('')
        self.log(f'Comparison written to {path}')


def format_comparison(
    module: str,
    results: Dict[str, SnapshotMetrics],
    property_sets: Dict[str, PropertySet],
) -> str:
    lines = [
        '=' * 96,
        f'BASELINE COMPARISON — {module} (shared formal harness, shared mutant set)',
        '=' * 96,
        f'{"METHOD":<14}{"PROPS":>7}{"PROVED":>8}{"VACUOUS":>9}{"COI%":>7}'
        f'{"DETECT%":>9}{"COST":>10}{"LES":>9}',
    ]
    for name, metrics in results.items():
        qual = metrics.qualification
        row = (
            f'{name:<14}{qual.total_properties:>7}{qual.proved_non_vacuous:>8}'
            f'{qual.proved_vacuous:>9}{metrics.diversity.checker_coverage * 100:>6.0f}%'
            f'{metrics.sensitivity.detection_rate * 100:>8.0f}%'
            f'{metrics.effort.llm_cost:>9.4f}{metrics.effort.currency[:1]}'
            f'{metrics.les:>9.1f}'
        )
        if property_sets.get(name) and property_sets[name].compile_failed:
            row += '   (does not elaborate)'
        lines.append(row)

    skipped = [
        (name, s.unavailable_reason) for name, s in property_sets.items()
        if not s.available
    ]
    if skipped:
        lines.append('')
        lines.append('Not applicable:')
        for name, reason in skipped:
            lines.append(f'  {name}: {reason}')

    lines += [
        '',
        'PROVED   = proved non-vacuously (the only properties that enter a snapshot)',
        'COI%     = structural checker coverage of the proved properties',
        'DETECT%  = share of the shared, non-equivalent mutant set detected',
        'LES      = proved x detection / max(cost, floor); zero when nothing is detected',
        '',
        'Every method ran with the same RTL revision, the same testbench, the same',
        'elaboration settings and the same mutants; only the properties differ.',
    ]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_pairs(values: Sequence[str]) -> Dict[str, str]:
    pairs = {}
    for value in values or ():
        if '=' not in value:
            raise argparse.ArgumentTypeError(f'expected name=value, got {value!r}')
        name, _, payload = value.partition('=')
        pairs[name.strip()] = payload.strip()
    return pairs


def build_methods(args) -> List[BaselineMethod]:
    imports = _parse_pairs(args.import_property or [])
    costs = _parse_pairs(args.import_cost or [])

    methods: List[BaselineMethod] = []
    for name in args.methods.split(','):
        name = name.strip()
        if not name:
            continue
        if name == 'svapshot':
            methods.append(SnapshotMethod(cost_report=args.cost_report))
        elif name == 'one_shot':
            methods.append(OneShotMethod(model=args.llm))
        elif name == 'imported':
            for label, path in imports.items():
                methods.append(ImportedMethod(
                    name=label, path=path, cost=float(costs.get(label, 0.0))))
            if not imports:
                raise SystemExit(
                    'method "imported" needs at least one --import name=path')
        else:
            raise SystemExit(f'unknown method {name!r}')
    return methods


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    compare = sub.add_parser('compare', help='Compare generation methods')
    compare.add_argument('--rtl', required=True)
    compare.add_argument('--formal-tool', default='vcformal',
                         choices=['vcformal', 'jaspergold'])
    compare.add_argument('--methods', default='svapshot,one_shot',
                         help='Comma-separated: svapshot, one_shot, imported')
    compare.add_argument('--llm', default='gpt-4.1-mini',
                         help='Model for the one-shot baseline')
    compare.add_argument('--mutants', type=int, default=18)
    compare.add_argument('--max-per-class', type=int, default=3)
    compare.add_argument(
        '--mutant-manifest', default='',
        help='Frozen mutant metadata JSON shared across all methods')
    compare.add_argument(
        '--mutant-source-dir', default='',
        help='Directory containing <mutant_id>.sv sources for the frozen manifest')
    compare.add_argument('--timeout', type=float, default=None)
    compare.add_argument('--out', default='')
    compare.add_argument('--cost-report', default='',
                         help='llm_cost.json of the SVApshot run being compared')
    compare.add_argument('--import', dest='import_property', action='append',
                         metavar='NAME=PATH',
                         help='External property file, e.g. lasso=artifacts/lasso.sv')
    compare.add_argument('--import-cost', action='append', metavar='NAME=EUR',
                         help='Reported LLM cost of an imported method')
    compare.add_argument('--les-floor', type=float, default=0.01)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != 'compare':
        return 1

    config = ComparisonConfig(
        rtl=args.rtl,
        formal_tool=args.formal_tool,
        mutants=args.mutants,
        max_per_class=args.max_per_class,
        timeout_s=args.timeout,
        output_dir=args.out,
        les_cost_floor=args.les_floor,
        model=args.llm,
        mutant_manifest=args.mutant_manifest,
        mutant_source_dir=args.mutant_source_dir,
    )
    return BaselineComparison(config, build_methods(args)).run()


if __name__ == '__main__':
    sys.exit(main())
