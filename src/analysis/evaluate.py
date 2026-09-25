#!/usr/bin/env python3
"""Mutation-validated evaluation of an SVApshot snapshot.

The question this answers is the one the reviewers asked: *does the snapshot
detect unintended change in the RTL?*  Anecdotes about a bug that happened to be
found do not answer it.  This harness does, by construction:

1. Generate a unique population of single-point mutants spread over the six
   fault classes in :mod:`mutation` (control, datapath, timing, reset,
   interface, state update).
2. Reduce the property file to the *contract*: exactly the assertions that were
   proved non-vacuously on the reference RTL.  A property that already failed on
   the reference would fail on every mutant and inflate the score.
3. Re-run the unchanged contract against each mutant under the identical formal
   harness, and call the mutant detected when a contract property is falsified.
4. Explain every survivor, using the cone of influence to say whether the
   mutated logic was outside the snapshot's field of view or inside it but
   unconstrained.

The result is a detection rate with a per-fault-class breakdown, plus the
regression-sensitivity figures consumed by :mod:`metrics_report`.

Usage::

    # generate and inspect the mutant population (no formal runs)
    python3 src/analysis/evaluate.py mutants --rtl benchmarks/sargantana/.../ptw_arb.sv

    # full mutation-validated evaluation
    python3 src/analysis/evaluate.py sensitivity --rtl benchmarks/sargantana/.../ptw_arb.sv \\
        --formal-tool vcformal --max-mutants 40

    # check that the snapshot's assumptions do not hide detectable faults
    python3 src/analysis/evaluate.py assumptions --rtl benchmarks/sargantana/.../ptw_arb.sv

    # check one candidate RTL revision against a frozen snapshot
    python3 src/analysis/evaluate.py regression --rtl ptw_arb.sv --candidate ptw_arb_new.sv
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

CORE_DIR = Path(__file__).resolve().parents[1] / 'core'
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import coi as coi_analysis
import mutation
import proof_status
from formal_harness import FormalHarness, contract_names, contract_property_file
from metrics_report import SensitivityReport
from mutation import (
    EquivalenceVerdict,
    FaultClass,
    Mutant,
    MutationConfig,
    score_mutants,
    format_mutation_report,
)
from proof_status import Qualification


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

#: Qualifications that mean "this property produced a counterexample".
_FAILING = (
    Qualification.FAILING_PROPERTY_MISMATCH,
    Qualification.FAILING_MISSING_ASSUMPTION,
)


@dataclass
class MutantOutcome:
    """What one mutant did to the contract."""

    mutant_id: str
    detected_by: List[str] = field(default_factory=list)
    #: Contract properties that stopped being provable without being falsified.
    weakened: List[str] = field(default_factory=list)
    compile_failed: bool = False
    #: The run was killed before the tool finished, so nothing was decided.
    timed_out: bool = False
    seconds: float = 0.0
    escape_reason: str = ''

    def to_dict(self) -> dict:
        return {
            'mutant_id': self.mutant_id,
            'detected': bool(self.detected_by),
            'detected_by': self.detected_by,
            'weakened': self.weakened,
            'compile_failed': self.compile_failed,
            'timed_out': self.timed_out,
            'seconds': round(self.seconds, 1),
            'escape_reason': self.escape_reason,
        }


def classify_mutant_run(
    contract: Sequence[str],
    reference: proof_status.FormalRunResult,
    mutant_run: proof_status.FormalRunResult,
) -> MutantOutcome:
    """Compare a mutant's results against the reference for the contract only.

    A mutant is detected when a property that held non-vacuously on the
    reference is falsified on the mutant.  A property that merely becomes
    inconclusive is recorded as *weakened*: the change was noticed by the
    engine but not proved wrong, and counting it as a detection would make the
    rate depend on proof effort rather than on the properties.
    """
    outcome = MutantOutcome(mutant_id='')
    outcome.compile_failed = mutant_run.summary.compile_failed

    for name in contract:
        before = reference.records.get(name)
        after = mutant_run.records.get(name)
        if before is None or before.qualification is not Qualification.PROVED_NON_VACUOUS:
            continue
        if after is None:
            continue
        if after.qualification in _FAILING:
            outcome.detected_by.append(name)
        elif after.qualification is Qualification.INCONCLUSIVE:
            outcome.weakened.append(name)

    return outcome


# ---------------------------------------------------------------------------
# Escape analysis
# ---------------------------------------------------------------------------


class EscapeAnalyser:
    """Explain why a mutant survived, in terms the user can act on."""

    def __init__(self, rtl_text: str, module_name: str, assertions: Sequence[str],
                 language: str = 'sv'):
        self.graph = coi_analysis.build_signal_graph(rtl_text, module_name, language)
        self.cones = {
            cone.name: cone
            for cone in coi_analysis.compute_property_cones(assertions, self.graph)
        }
        self.union: Set[str] = set()
        for cone in self.cones.values():
            self.union |= cone.cone

    def signals_of(self, line: str) -> Set[str]:
        return coi_analysis.extract_identifiers(line) & self.graph.all_signals

    def parameters_of(self, line: str) -> Set[str]:
        return coi_analysis.extract_identifiers(line) & self.graph.parameters

    def explain(self, mutant: Mutant, outcome: MutantOutcome) -> str:
        if outcome.compile_failed:
            return 'mutant did not elaborate; excluded from the population'
        if outcome.timed_out:
            return 'run exceeded the time limit; no verdict for this mutant'

        touched = self.signals_of(mutant.original_line) | self.signals_of(mutant.mutated_line)
        if not touched:
            parameters = (self.parameters_of(mutant.original_line)
                          | self.parameters_of(mutant.mutated_line))
            if parameters:
                return (
                    'changes an elaboration-time condition on '
                    + ', '.join(sorted(parameters)[:4])
                    + ', so a different generate branch is built; no property '
                      'distinguishes the two structures'
                )
            return 'mutated line references no signal the RTL parser resolved'

        inside = touched & self.union
        if not inside:
            return (
                'mutated logic is outside every property cone: '
                + ', '.join(sorted(touched)[:6])
            )

        watching = sorted(
            name for name, cone in self.cones.items() if cone.cone & touched
        )
        if outcome.weakened:
            return (
                'in the cone of ' + ', '.join(watching[:4])
                + '; those properties became inconclusive rather than failing '
                  '(deepen the proof or bound the property)'
            )
        return (
            'in the cone of ' + ', '.join(watching[:4])
            + '; no property constrains the mutated behaviour precisely enough'
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class PropertySetRejected(RuntimeError):
    """The property set under evaluation does not elaborate.

    Raised instead of a bare ``RuntimeError`` so a baseline comparison can score
    the method zero and carry on, rather than treating an unusable property set
    as a broken harness.
    """


@dataclass
class EvaluationConfig:
    rtl: str
    formal_tool: str = 'vcformal'
    max_mutants: int = 40
    max_per_class: int = 8
    fault_classes: Sequence[FaultClass] = tuple(FaultClass)
    timeout_s: Optional[float] = None
    output_dir: str = ''
    keep_mutant_sources: bool = True
    contract_override: Sequence[str] = ()
    #: Property set to measure. ``None`` uses the file already in the testbench
    #: (the SVApshot snapshot); a baseline supplies its own text here so every
    #: method is measured under the same harness and the same mutants.
    property_text: Optional[str] = None
    #: Label used in logs and report names.
    method: str = 'svapshot'


class MutationEvaluator:
    """Drive the reference run, the mutant runs, and the scoring."""

    def __init__(self, config: EvaluationConfig, log=None):
        self.config = config
        self.log = log or (lambda message: print(message, flush=True))
        self.harness = FormalHarness(
            config.rtl, config.formal_tool, log=self.log, timeout_s=config.timeout_s)
        self.module = self.harness.paths.module
        self.output_dir = config.output_dir or os.path.join(
            'mutation_reports', self.module)
        self.reference: Optional[proof_status.FormalRunResult] = None
        self.contract: List[str] = []
        self.outcomes: Dict[str, MutantOutcome] = {}
        self.last_sensitivity: Optional[SensitivityReport] = None

    # -- steps -------------------------------------------------------------

    def property_text(self) -> str:
        """The property set under evaluation."""
        if self.config.property_text is not None:
            return self.config.property_text
        return self.harness.read_properties()

    @contextmanager
    def _property_set_installed(self, text: Optional[str] = None):
        """Put the property set under evaluation into the testbench."""
        text = text if text is not None else self.config.property_text
        if text is None:
            yield
        else:
            with self.harness.properties(text):
                yield

    def establish_contract(self) -> List[str]:
        """Run the reference design and keep the properties it proves.

        Returns:
            list[str]: Contract property names.
        """
        if self.config.contract_override:
            self.contract = list(self.config.contract_override)
            self.log(f'Contract taken from manifest: {len(self.contract)} properties')

        self.log(f'Reference run on unmodified {self.module} ...')
        with self._property_set_installed():
            run = self.harness.run(label='reference')
        self.reference = run.result

        if run.result.summary.compile_failed:
            if self.config.property_text is not None:
                # A supplied property set is the only thing that changed, so the
                # elaboration failure belongs to it. For a baseline without
                # syntax repair this is a real result, not a harness fault.
                raise PropertySetRejected(
                    f'The {self.config.method} property set does not elaborate; '
                    f'it establishes nothing about the design.')
            raise RuntimeError(
                'The reference design does not elaborate; fix the testbench '
                'before measuring sensitivity.')

        proved = run.result.names_with(Qualification.PROVED_NON_VACUOUS)

        # Designs such as the Sargantana common cells ship their own SVA. Those
        # assertions are proved in the same run but are not part of the
        # snapshot, and crediting their detections to SVApshot would overstate
        # the result, so the contract is intersected with the property file.
        snapshot_names = set(contract_names(self.property_text()))
        native = [name for name in proved if name not in snapshot_names]
        if native:
            self.log(
                f'Excluded {len(native)} assertion(s) native to the RTL from the '
                f'contract: {", ".join(native)}')
        proved = [name for name in proved if name in snapshot_names]

        if self.config.contract_override:
            missing = sorted(set(self.contract) - set(proved))
            if missing:
                self.log(
                    'Warning: manifest properties not reproducible on this '
                    'reference run and dropped from the contract: '
                    + ', '.join(missing))
            self.contract = [name for name in self.contract if name in proved]
        else:
            self.contract = proved

        self.log(
            f'Contract: {len(self.contract)} properties proved non-vacuously '
            f'out of {len(run.result.records)} in the file '
            f'({run.seconds:.0f}s)')

        if self.harness.timeout_s is None:
            # The reference run measures what this design costs to prove, so it
            # is the natural budget for a mutant. Without a limit one mutant
            # that makes a property hard can stall a whole batch; the multiple
            # is generous enough that a mutant is only cut off when its proof
            # has genuinely stopped converging.
            self.harness.timeout_s = max(600.0, 5.0 * run.seconds)
            self.log(
                f'Per-mutant time limit set to '
                f'{self.harness.timeout_s:.0f}s (5x the reference run)')
        return self.contract

    def build_population(self) -> List[Mutant]:
        rtl_text = self.harness.read_rtl()
        mutants = mutation.generate_mutants(
            rtl_text,
            self.module,
            MutationConfig(
                fault_classes=tuple(self.config.fault_classes),
                max_per_class=self.config.max_per_class,
                max_total=self.config.max_mutants,
            ),
        )
        by_class: Dict[str, int] = {}
        for mutant in mutants:
            by_class[mutant.fault_class.value] = by_class.get(mutant.fault_class.value, 0) + 1
        self.log(
            f'Generated {len(mutants)} unique mutants: '
            + ', '.join(f'{name} {count}' for name, count in sorted(by_class.items())))
        return mutants

    def evaluate_one(
        self,
        mutant: Mutant,
        *,
        rtl_already_installed: bool = False,
        golden_rtl: Optional[str] = None,
    ) -> Optional[Mutant]:
        """Score one mutant against the frozen contract.

        Returns the mutant when it stays in the scored population, or
        ``None`` when the run is excluded (timeout with no cex, or the
        mutant does not elaborate).

        When ``rtl_already_installed`` is true the file at ``rtl_source``
        is already this mutant — a private, equally-named copy so the
        filelist stays unchanged. The harness must not rewrite a shared
        golden path. ``golden_rtl`` is then the unmodified source used
        for escape analysis; the sequential path can omit it because
        ``run_with_rtl`` restores the original file.
        """
        if self.reference is None:
            raise RuntimeError('establish_contract() must run before evaluate()')

        property_text = self.property_text()
        contract_text = contract_property_file(property_text, self.contract)
        rtl_for_escape = (
            golden_rtl if golden_rtl is not None else self.harness.read_rtl())
        analyser = EscapeAnalyser(
            rtl_for_escape,
            self.module,
            _contract_assertion_blocks(property_text, self.contract),
            coi_analysis.language_of(self.harness.rtl_source),
        )

        start = time.time()
        with self.harness.properties(contract_text):
            if rtl_already_installed:
                run = self.harness.run(label=mutant.mutant_id)
            else:
                run = self.harness.run_with_rtl(
                    mutant.source, label=mutant.mutant_id)
        outcome = classify_mutant_run(self.contract, self.reference, run.result)
        outcome.mutant_id = mutant.mutant_id
        outcome.seconds = time.time() - start
        outcome.timed_out = run.timed_out

        if outcome.timed_out and not outcome.detected_by:
            # Nothing was decided, so calling the mutant a survivor would
            # blame the snapshot for a proof that never finished.
            mutant.equivalence = EquivalenceVerdict.UNKNOWN
            mutant.escape_reason = (
                'run exceeded the time limit; no verdict for this mutant')
            outcome.escape_reason = mutant.escape_reason
            self.outcomes[mutant.mutant_id] = outcome
            self.log('    excluded: proof did not finish within the time limit')
            return None

        if outcome.compile_failed:
            # An uncompilable mutant measures the mutation engine, not the
            # snapshot, so it leaves the population entirely.
            mutant.equivalence = EquivalenceVerdict.UNKNOWN
            mutant.escape_reason = analyser.explain(mutant, outcome)
            outcome.escape_reason = mutant.escape_reason
            self.outcomes[mutant.mutant_id] = outcome
            self.log('    excluded: mutant does not elaborate')
            return None

        mutant.detected_by = outcome.detected_by
        if outcome.detected_by:
            self.log(
                f'    detected by {len(outcome.detected_by)} propert'
                f'{"y" if len(outcome.detected_by) == 1 else "ies"}: '
                + ', '.join(outcome.detected_by[:4]))
        else:
            mutant.escape_reason = analyser.explain(mutant, outcome)
            outcome.escape_reason = mutant.escape_reason
            self.log(f'    survived: {mutant.escape_reason}')

        self.outcomes[mutant.mutant_id] = outcome
        return mutant

    def evaluate(self, mutants: Sequence[Mutant]) -> List[Mutant]:
        """Run every mutant against the frozen contract."""
        if self.reference is None:
            raise RuntimeError('establish_contract() must run before evaluate()')

        applied: List[Mutant] = []
        total = len(mutants)
        for index, mutant in enumerate(mutants, start=1):
            self.log(
                f'[{index}/{total}] {mutant.mutant_id} '
                f'{mutant.description()}')
            kept = self.evaluate_one(mutant)
            if kept is not None:
                applied.append(kept)
        return applied

    # -- reporting ---------------------------------------------------------

    def sensitivity_report(self, applied: Sequence[Mutant], generated: int) -> SensitivityReport:
        score = score_mutants(applied)
        report = SensitivityReport(
            mutants_generated=generated,
            mutants_equivalent=generated - len(applied),
            mutants_applied=score.population,
            mutants_detected=score.detected,
            surviving_mutants=[m.mutant_id for m in applied if not m.detected],
        )
        for fault_class, bucket in score.per_class.items():
            report.detection_by_class[fault_class] = {
                'applied': bucket['population'],
                'detected': bucket['detected'],
            }
        return report

    def write_reports(self, mutants: Sequence[Mutant], applied: Sequence[Mutant]) -> str:
        os.makedirs(self.output_dir, exist_ok=True)
        score = score_mutants(applied)
        report = self.sensitivity_report(applied, len(mutants))
        self.last_sensitivity = report

        payload = {
            'module': self.module,
            'method': self.config.method,
            'formal_tool': self.config.formal_tool,
            'rtl': os.path.abspath(self.config.rtl),
            'rtl_sha256': mutation.fingerprint_source(self.harness.read_rtl()),
            'contract': self.contract,
            'reference': self.reference.to_dict() if self.reference else {},
            'score': score.to_dict(),
            'sensitivity': report.to_dict(),
            'mutants': [m.to_dict() for m in mutants],
            'outcomes': {k: v.to_dict() for k, v in sorted(self.outcomes.items())},
            'formal_seconds': round(self.harness.total_formal_seconds, 1),
        }
        stem = f'{self.module}_{self.config.method}'
        path = os.path.join(self.output_dir, f'{stem}_sensitivity.json')
        with open(path, 'w') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)

        text_path = os.path.join(self.output_dir, f'{stem}_sensitivity.txt')
        with open(text_path, 'w') as handle:
            handle.write(format_mutation_report(score, applied))
            handle.write('\n')

        if self.config.keep_mutant_sources:
            mutation.write_mutants(mutants, os.path.join(self.output_dir, 'mutants'))

        self.log('')
        self.log(format_mutation_report(score, applied))
        self.log('')
        self.log(f'Reports written to {self.output_dir}/')
        return path

    def measure(self, mutants: Optional[Sequence[Mutant]] = None):
        """Establish the contract and score it against a mutant population.

        Args:
            mutants: Population to reuse. Passing the same list to several
                methods is what makes their detection rates comparable; when it
                is ``None`` a fresh population is generated.

        Returns:
            tuple: ``(SensitivityReport, applied_mutants, all_mutants)``.
        """
        self.establish_contract()
        if not self.contract:
            self.log(
                'No property was proved non-vacuously on the reference design, '
                'so there is no contract to test.')
            return SensitivityReport(), [], list(mutants or [])

        generated = list(mutants) if mutants is not None else self.build_population()
        unscreened = [
            mutant.mutant_id for mutant in generated
            if mutant.equivalence in {
                EquivalenceVerdict.NOT_SCREENED,
                EquivalenceVerdict.UNKNOWN,
            }
        ]
        if unscreened:
            raise RuntimeError(
                'mutation population is not equivalence-screened: '
                + ', '.join(unscreened[:8])
                + (' ...' if len(unscreened) > 8 else ''))
        population = mutation.valid_population(generated)
        if not population:
            self.log('The mutation engine produced no mutant for this module.')
            return SensitivityReport(), [], []

        applied = self.evaluate(population)
        return self.sensitivity_report(
            applied, len(generated)), applied, population

    def run(self) -> int:
        if not self.harness.is_ready():
            return 1
        _, applied, mutants = self.measure()
        if not mutants:
            return 1
        self.write_reports(mutants, applied)
        return 0


def _contract_assertion_blocks(property_text: str, contract: Sequence[str]) -> List[str]:
    """Return the source text of just the contract assertions."""
    from assertion_template import get_assertion_name, split_designer_assertion_blocks

    keep = set(contract)
    blocks = split_designer_assertion_blocks(property_text)
    selected = [b for b in blocks if get_assertion_name(b) in keep]
    return selected or blocks


# ---------------------------------------------------------------------------
# Assumption screening against the mutant population
# ---------------------------------------------------------------------------


@dataclass
class AssumptionScreenResult:
    """What the accepted assumptions cost and bought in detection power."""

    contract_with: List[str] = field(default_factory=list)
    contract_without: List[str] = field(default_factory=list)
    detected_with: List[str] = field(default_factory=list)
    detected_without: List[str] = field(default_factory=list)
    #: Mutants the snapshot caught before the assumptions and misses after.
    masked: List[str] = field(default_factory=list)
    #: Mutants only caught once the assumptions are in force.
    gained: List[str] = field(default_factory=list)
    verdict: str = 'skipped'

    def to_dict(self) -> dict:
        return {
            'verdict': self.verdict,
            'contract_with': self.contract_with,
            'contract_without': self.contract_without,
            'detected_with': self.detected_with,
            'detected_without': self.detected_without,
            'masked': self.masked,
            'gained': self.gained,
        }

    def format_report(self) -> str:
        lines = [
            '=' * 78,
            'ASSUMPTION MUTATION-SENSITIVITY SCREEN',
            '=' * 78,
            f'  contract without assumptions   {len(self.contract_without)} properties',
            f'  contract with assumptions      {len(self.contract_with)} properties',
            f'  mutants detected without       {len(self.detected_without)}',
            f'  mutants detected with          {len(self.detected_with)}',
            '',
            f'  verdict: {self.verdict.upper()}',
        ]
        if self.masked:
            lines += [
                '',
                '  The assumptions hide these mutants, which the same snapshot',
                '  detected in the unconstrained environment. Each one is a',
                '  defect the shipped snapshot would no longer catch:',
            ]
            lines += [f'    - {name}' for name in self.masked]
            lines.append('')
            lines.append('  Weaken or drop the assumptions before freezing the snapshot.')
        if self.gained:
            lines += [
                '',
                '  Detected only because the assumptions make the environment',
                '  legal, so these are the detections the constraints buy:',
            ]
            lines += [f'    - {name}' for name in self.gained]
        return '\n'.join(lines)


def screen_assumptions_with_mutation(
    rtl: str,
    formal_tool: str = 'vcformal',
    max_mutants: int = 18,
    max_per_class: int = 3,
    fault_classes: Sequence[FaultClass] = tuple(FaultClass),
    timeout_s: Optional[float] = None,
    output_dir: str = '',
    log=print,
) -> AssumptionScreenResult:
    """Check that the snapshot's assumptions do not hide defects.

    The last of the screens the flow promises for a generated assumption, and
    the only one that cannot run inside the generation loop: it needs a mutant
    population and two full evaluations.  The same property set is measured with
    the accepted assumptions in force and with the assumption block stripped
    out, against the identical mutants.

    An assumption set that removes a mutant from the detected set has narrowed
    the input space past the point of usefulness — it is exactly the
    overrestrictive constraint that makes a snapshot look clean while defects
    walk through it.
    """
    import assumption_gen

    harness = FormalHarness(rtl, formal_tool, log=log, timeout_s=timeout_s)
    if not harness.is_ready():
        return AssumptionScreenResult(verdict='error')

    with_text = harness.read_properties()
    without_text = assumption_gen.strip_assumptions(with_text)
    if without_text.strip() == with_text.strip():
        log('The property file carries no SVApshot assumption block; '
            'nothing to screen.')
        return AssumptionScreenResult(verdict='skipped')

    mutants = mutation.generate_mutants(
        harness.read_rtl(),
        harness.paths.module,
        MutationConfig(
            fault_classes=tuple(fault_classes),
            max_per_class=max_per_class,
            max_total=max_mutants,
        ),
    )
    if not mutants:
        log('The mutation engine produced no mutant for this module.')
        return AssumptionScreenResult(verdict='skipped')
    log(f'Shared mutant population: {len(mutants)} mutants')

    result = AssumptionScreenResult()
    detections = {}
    for label, text in (('without_assumptions', without_text),
                        ('with_assumptions', with_text)):
        log('')
        log('=' * 72)
        log(f'CONFIGURATION: {label}')
        log('=' * 72)
        evaluator = MutationEvaluator(
            EvaluationConfig(
                rtl=rtl,
                formal_tool=formal_tool,
                timeout_s=timeout_s,
                output_dir=output_dir,
                keep_mutant_sources=False,
                property_text=text,
                method=label,
            ),
            log=log,
        )
        _, applied, population = evaluator.measure(mutants)
        evaluator.write_reports(population, applied)
        detections[label] = sorted(m.mutant_id for m in applied if m.detected)
        if label == 'with_assumptions':
            result.contract_with = list(evaluator.contract)
        else:
            result.contract_without = list(evaluator.contract)

    result.detected_without = detections['without_assumptions']
    result.detected_with = detections['with_assumptions']

    verdict, masked = assumption_gen.screen_mutation_sensitivity(
        result.detected_without, result.detected_with)
    result.masked = masked
    result.gained = sorted(set(result.detected_with) - set(result.detected_without))
    result.verdict = verdict.value

    log('')
    log(result.format_report())
    return result


# ---------------------------------------------------------------------------
# Regression check against a candidate revision
# ---------------------------------------------------------------------------


def check_regression(
    rtl: str,
    candidate: str,
    formal_tool: str = 'vcformal',
    contract: Optional[Sequence[str]] = None,
    timeout_s: Optional[float] = None,
    intended_changes: Sequence[str] = (),
    log=print,
) -> int:
    """Apply a frozen snapshot to a new RTL revision.

    This is the everyday use of a snapshot: the properties are not regenerated,
    they are replayed.  A contract property that fails is a deviation from
    established behaviour.

    Evaluating an AI-generated refactor needs one more distinction, because such
    a change is usually meant to preserve most behaviour and deliberately alter
    some of it.  ``intended_changes`` names the contract properties the author
    expects to break; those are reported as *re-specification needed* rather
    than as regressions, and everything else that fails is an unintended change.
    A declared property that keeps holding is reported too: either the intended
    change did not happen, or the snapshot cannot observe it.

    Returns:
        int: Process exit status — 0 when the candidate keeps the contract,
        3 when it breaks something that was not declared.
    """
    harness = FormalHarness(rtl, formal_tool, log=log, timeout_s=timeout_s)
    if not harness.is_ready():
        return 1

    reference = harness.run(label='reference')
    snapshot_names = set(contract_names(harness.read_properties()))
    proved = [
        name for name in reference.result.names_with(Qualification.PROVED_NON_VACUOUS)
        if name in snapshot_names
    ]
    names = [n for n in (contract or proved) if n in proved]
    if not names:
        log('No contract to replay: the reference proves nothing non-vacuously.')
        return 1
    log(f'Replaying {len(names)} contract properties on {candidate}')

    with open(candidate, 'r', errors='replace') as handle:
        candidate_text = handle.read()

    contract_text = contract_property_file(harness.read_properties(), names)
    with harness.properties(contract_text):
        run = harness.run_with_rtl(candidate_text, label='candidate')

    if run.result.summary.compile_failed:
        log('Candidate RTL does not elaborate under the formal harness.')
        return 2

    outcome = classify_mutant_run(names, reference.result, run.result)

    declared = set(intended_changes)
    unknown = sorted(declared - set(names))
    if unknown:
        log('')
        log('Declared as intentionally changed but not in the contract: '
            + ', '.join(unknown))
    intended = [n for n in outcome.detected_by if n in declared]
    unintended = [n for n in outcome.detected_by if n not in declared]
    unobserved = sorted(declared & set(names) - set(outcome.detected_by))

    def describe(name: str) -> str:
        record = run.result.records.get(name)
        return (record.note or 'counterexample found') if record else 'counterexample'

    if intended:
        log('')
        log(f'INTENTIONAL CHANGE: {len(intended)} declared propert'
            f'{"y" if len(intended) == 1 else "ies"} no longer hold. The '
            f'behaviour they described was replaced, so they need '
            f're-specification, not repair:')
        for name in intended:
            log(f'  - {name}: {describe(name)}')

    if unobserved:
        log('')
        log('Declared as intentionally changed but still proved: '
            + ', '.join(unobserved)
            + '. Either the change did not take effect, or the snapshot '
              'cannot observe it.')

    if unintended:
        log('')
        log(f'REGRESSION: {len(unintended)} contract propert'
            f'{"y" if len(unintended) == 1 else "ies"} now fail and were not '
            f'declared as intentional:')
        for name in unintended:
            log(f'  - {name}: {describe(name)}')
        return 3

    if outcome.weakened:
        log('')
        log('No failure, but these properties are no longer conclusive: '
            + ', '.join(outcome.weakened))
        return 4

    log('')
    preserved = len(names) - len(intended)
    if intended:
        log(f'Candidate preserves the {preserved} contract propert'
            f'{"y" if preserved == 1 else "ies"} outside the declared change.')
    else:
        log(f'Candidate preserves all {len(names)} contract properties.')
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fault_classes(value: str) -> Sequence[FaultClass]:
    if not value or value == 'all':
        return tuple(FaultClass)
    selected = []
    for name in value.split(','):
        name = name.strip()
        try:
            selected.append(FaultClass(name))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f'unknown fault class {name!r}; choose from '
                + ', '.join(f.value for f in FaultClass))
    return tuple(selected)


def _contract_from_manifest(path: Optional[str]) -> Sequence[str]:
    """Read the contract (non-vacuous proofs) out of a snapshot manifest."""
    if not path:
        return ()
    try:
        with open(path, 'r') as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as error:
        raise SystemExit(
            f'Cannot read the snapshot manifest {path}: {error}\n'
            'Runs made before manifests were written have none; omit --manifest '
            'and the contract is re-established by running the property file.')
    contract = tuple(
        entry['name'] for entry in manifest.get('properties', [])
        if entry.get('qualification') == 'proved_non_vacuous'
    )
    if not contract:
        raise SystemExit(
            f'{path} records no property proved non-vacuously, so there is no '
            'contract to measure. Qualify the snapshot first.')
    return contract


def _update_manifest(path: str, payload: dict, log, key: str = 'mutation') -> None:
    """Store mutation results in the manifest that describes the snapshot.

    Sensitivity is a property of a snapshot, not of a separate report, so it
    belongs next to the proofs it was measured against.
    """
    try:
        with open(path) as handle:
            manifest = json.load(handle)
        manifest[key] = payload
        with open(path, 'w') as handle:
            json.dump(manifest, handle, indent=2)
        log(f'Mutation results recorded in {path}')
    except (OSError, ValueError) as error:
        log(f'Could not update {path}: {error}')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    def add_common(target):
        target.add_argument('--rtl', required=True, help='Path to the DUT RTL file')
        target.add_argument('--formal-tool', default='vcformal',
                            choices=['vcformal', 'jaspergold'])
        target.add_argument('--timeout', type=float, default=None,
                            help='Per-run wall-clock limit in seconds. Default: '
                                 'five times the reference run, so one mutant '
                                 'with a hard proof cannot stall the batch.')

    mutants = sub.add_parser('mutants', help='Generate the mutant population only')
    add_common(mutants)
    mutants.add_argument('--max-mutants', type=int, default=40)
    mutants.add_argument('--max-per-class', type=int, default=8)
    mutants.add_argument('--classes', type=_fault_classes, default=tuple(FaultClass))
    mutants.add_argument('--out', default='')

    sensitivity = sub.add_parser(
        'sensitivity', help='Full mutation-validated evaluation of the snapshot')
    add_common(sensitivity)
    sensitivity.add_argument('--max-mutants', type=int, default=40)
    sensitivity.add_argument('--max-per-class', type=int, default=8)
    sensitivity.add_argument('--classes', type=_fault_classes, default=tuple(FaultClass))
    sensitivity.add_argument('--out', default='')
    sensitivity.add_argument('--manifest', default='',
                             help='Snapshot manifest whose contract to replay')
    sensitivity.add_argument('--no-mutant-sources', action='store_true')

    assumptions = sub.add_parser(
        'assumptions',
        help='Check that the snapshot assumptions do not mask detectable faults')
    add_common(assumptions)
    assumptions.add_argument('--max-mutants', type=int, default=18)
    assumptions.add_argument('--max-per-class', type=int, default=3)
    assumptions.add_argument('--classes', type=_fault_classes, default=tuple(FaultClass))
    assumptions.add_argument('--out', default='')
    assumptions.add_argument('--manifest', default='',
                             help='Snapshot manifest to record the verdict in')

    regression = sub.add_parser(
        'regression', help='Replay a frozen snapshot on a candidate RTL revision')
    add_common(regression)
    regression.add_argument('--candidate', required=True,
                            help='Path to the modified RTL to check')
    regression.add_argument('--manifest', default='')
    regression.add_argument(
        '--changed', default='',
        help='Comma-separated contract properties the change is meant to '
             'break. They are reported as needing re-specification instead of '
             'as regressions, which is what separates a deliberate refactor '
             'from an unintended one.')

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == 'mutants':
        with open(args.rtl, 'r', errors='replace') as handle:
            rtl_text = handle.read()
        module = os.path.splitext(os.path.basename(args.rtl))[0]
        mutants = mutation.generate_mutants(
            rtl_text, module,
            MutationConfig(
                fault_classes=tuple(args.classes),
                max_per_class=args.max_per_class,
                max_total=args.max_mutants,
            ))
        out = args.out or os.path.join('mutation_reports', module)
        os.makedirs(out, exist_ok=True)
        mutation.write_mutants(mutants, os.path.join(out, 'mutants'))
        mutation.dump_mutants(mutants, os.path.join(out, f'{module}_mutants.json'))
        for mutant in mutants:
            print(f'{mutant.mutant_id}  {mutant.description()}')
        print(f'\n{len(mutants)} mutants written to {out}/mutants/')
        return 0

    if args.command == 'sensitivity':
        config = EvaluationConfig(
            rtl=args.rtl,
            formal_tool=args.formal_tool,
            max_mutants=args.max_mutants,
            max_per_class=args.max_per_class,
            fault_classes=tuple(args.classes),
            timeout_s=args.timeout,
            output_dir=args.out,
            keep_mutant_sources=not args.no_mutant_sources,
            contract_override=_contract_from_manifest(args.manifest),
        )
        evaluator = MutationEvaluator(config)
        status = evaluator.run()
        if status == 0 and args.manifest and evaluator.last_sensitivity is not None:
            _update_manifest(
                args.manifest, evaluator.last_sensitivity.to_dict(), evaluator.log)
        return status

    if args.command == 'assumptions':
        result = screen_assumptions_with_mutation(
            args.rtl,
            formal_tool=args.formal_tool,
            max_mutants=args.max_mutants,
            max_per_class=args.max_per_class,
            fault_classes=tuple(args.classes),
            timeout_s=args.timeout,
            output_dir=args.out,
        )
        if args.manifest and result.verdict != 'error':
            _update_manifest(
                args.manifest, result.to_dict(), print,
                key='assumption_mutation_screen')
        # A masking assumption is a failure the caller should act on.
        return 0 if result.verdict in ('pass', 'skipped') else 5

    if args.command == 'regression':
        return check_regression(
            args.rtl,
            args.candidate,
            formal_tool=args.formal_tool,
            contract=_contract_from_manifest(args.manifest) or None,
            timeout_s=args.timeout,
            intended_changes=[
                name.strip() for name in args.changed.split(',') if name.strip()
            ],
        )

    return 1


if __name__ == '__main__':
    sys.exit(main())
