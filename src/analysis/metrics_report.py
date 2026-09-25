"""Consolidated snapshot metrics for SVApshot.

Reviewers of the first study objected, reasonably, that a high coverage figure
sitting next to a small number of passing assertions is not evidence of
anything: the number was quoted without saying what the tool measured.  This
module fixes the vocabulary.  Every figure that leaves SVApshot is defined here
once, with the configuration it was measured under, and checker coverage is
demoted to a secondary metric reported alongside its exact definition.

The primary quality signals are, in order:

1. **Proof status and vacuity** — how many properties are proved
   non-vacuously, the only qualification that earns a place in the snapshot.
2. **Cone-of-influence diversity** — whether those properties look at
   different parts of the design or restate the same fact.
3. **Assumption dependence** — how much of the snapshot holds only under
   generated environment constraints.
4. **Regression sensitivity** — the fraction of non-equivalent mutants the
   unchanged snapshot detects, split by fault class.
5. **Runtime and LLM cost** — what the snapshot cost to produce.

Checker coverage is reported last, and never on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


#: The exact meaning of every coverage number SVApshot prints.
#:
#: Two different things are commonly called "coverage" in this context and the
#: first study conflated them. They are kept apart here.
CHECKER_COVERAGE_DEFINITION = """\
checker_coverage (structural, tool-independent)
    |union of the cones of influence of all snapshot-worthy properties, \
restricted to design signals| / |design signals|

    design signals  = input ports + output ports + declared internal signals of
                      the DUT module, including those declared inside generate
                      blocks, and excluding parameters, localparams, generate
                      loop variables, compiler directives, and signals declared
                      inside the property file.
    cone            = the transitive fan-in of every signal a property
                      references, computed from the RTL by coi.py.
    snapshot-worthy = qualification == proved_non_vacuous. Vacuous proofs,
                      failing properties and inconclusive results contribute
                      nothing, because they establish no behaviour.

    This metric answers "how much of the design does the snapshot look at",
    not "how much of the design is verified". A signal inside a cone is
    observed by some property; it is not thereby proved correct.

property_density (tool-reported, VC Formal)
    |registers in the COI of at least one assert| / |registers in scope|, taken
    verbatim from `report_assertion_density -list -status all -type reg`, which
    the generated FPV_vcf.tcl runs after `check_fv -block`. Cover properties are
    excluded: the tool reports asserts, covers and asserts+covers separately and
    only the assert figure is read, because a cover establishes reachability,
    not behaviour. The primary-input scope (`-type pi`) is reported next to it.

formal_core_coverage (tool-reported, VC Formal)
    |registers in the union of the formal cores of the proved_non_vacuous
    properties| / |registers in scope|, from `compute_formal_core` followed by
    `report_formal_core -verbose`, with the same denominator as property
    density. The formal core is the subset of the COI the engine actually used
    to close each proof, so this figure is always <= property density and is
    the closest thing the tool offers to "how much of the design the snapshot
    establishes something about".

    The gap between the two is diagnostic, and is the gap the first study was
    criticised for hiding: a high density with a small core means the
    assertions reference much of the design while their proofs depend on very
    little of it.

mutation_detection_rate (primary regression metric)
    |mutants detected by the unchanged snapshot| / |unique, non-equivalent
    mutants applied|. A mutant is detected when at least one snapshot property
    that was proved non-vacuously on the reference RTL fails on the mutant.
"""


@dataclass
class QualificationMetrics:
    """The five-way qualification of the final property set."""

    total_properties: int = 0
    proved_non_vacuous: int = 0
    proved_vacuous: int = 0
    failing_property_mismatch: int = 0
    failing_missing_assumption: int = 0
    inconclusive: int = 0
    proven_assumption_dependent: int = 0
    proven_standalone: int = 0
    vacuity_checked_proofs: int = 0
    vacuity_unchecked_proofs: int = 0
    constraint_conflict_status: str = ''

    @property
    def snapshot_worthy(self) -> int:
        return self.proved_non_vacuous

    @property
    def snapshot_yield(self) -> float:
        """Fraction of generated properties that earn a place in the snapshot."""
        if not self.total_properties:
            return 0.0
        return self.proved_non_vacuous / self.total_properties

    def to_dict(self) -> dict:
        data = asdict(self)
        data['snapshot_worthy'] = self.snapshot_worthy
        data['snapshot_yield'] = round(self.snapshot_yield, 4)
        return data


@dataclass
class DiversityReport:
    """Cone-of-influence spread of the snapshot-worthy properties."""

    property_count: int = 0
    design_signal_count: int = 0
    union_cone_size: int = 0
    checker_coverage: float = 0.0
    mean_cone_size: float = 0.0
    distinct_cone_signatures: int = 0
    mean_pairwise_jaccard_distance: float = 0.0
    redundancy: float = 0.0
    #: Tool-computed COI sizes (report_fv_complexity), when available.
    tool_mean_coi_size: float = 0.0
    tool_union_coi_size: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SensitivityReport:
    """Regression sensitivity measured by mutation."""

    mutants_generated: int = 0
    mutants_equivalent: int = 0
    mutants_applied: int = 0
    mutants_detected: int = 0
    detection_by_class: Dict[str, Dict[str, int]] = field(default_factory=dict)
    surviving_mutants: List[str] = field(default_factory=list)

    @property
    def detection_rate(self) -> float:
        if not self.mutants_applied:
            return 0.0
        return self.mutants_detected / self.mutants_applied

    def class_rates(self) -> Dict[str, float]:
        rates = {}
        for fault_class, counts in sorted(self.detection_by_class.items()):
            applied = counts.get('applied', 0)
            rates[fault_class] = counts.get('detected', 0) / applied if applied else 0.0
        return rates

    def to_dict(self) -> dict:
        data = asdict(self)
        data['detection_rate'] = round(self.detection_rate, 4)
        data['detection_rate_by_class'] = {
            name: round(rate, 4) for name, rate in self.class_rates().items()
        }
        return data


@dataclass
class EffortReport:
    """What the snapshot cost in time and money."""

    total_seconds: float = 0.0
    formal_seconds: float = 0.0
    llm_seconds: float = 0.0
    preprocessing_seconds: float = 0.0
    syntax_seconds: float = 0.0
    extension_seconds: float = 0.0
    semantic_seconds: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_cost: float = 0.0
    currency: str = 'EUR'
    formal_runs: int = 0
    repair_attempts: int = 0
    repair_attempts_saved: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def llm_efficiency_score(
    proved_non_vacuous: int,
    detection_rate: float,
    llm_cost: float,
    cost_floor: float = 0.01,
) -> float:
    """Snapshot value delivered per unit of LLM spend.

    Defined for this work as::

        LES = proved_non_vacuous * mutation_detection_rate / max(llm_cost, floor)

    The mutation term is what stops a method from winning by emitting many
    cheap, trivially provable properties: a snapshot that catches no injected
    fault scores zero no matter how many proofs it contains.  ``cost_floor``
    keeps the score finite for methods with negligible or subscription-billed
    LLM usage; the floor used is always reported next to the score.

    LES is a comparison aid between generation methods measured under the same
    formal harness and the same mutant set.  It is not comparable across
    designs, and both of its inputs are always reported separately.
    """
    return proved_non_vacuous * detection_rate / max(llm_cost, cost_floor)


@dataclass
class SnapshotMetrics:
    """Every figure SVApshot reports about one snapshot, in one place."""

    module: str = ''
    method: str = 'svapshot'
    model: str = ''
    formal_tool: str = ''
    qualification: QualificationMetrics = field(default_factory=QualificationMetrics)
    diversity: DiversityReport = field(default_factory=DiversityReport)
    sensitivity: SensitivityReport = field(default_factory=SensitivityReport)
    effort: EffortReport = field(default_factory=EffortReport)
    #: Verbatim tool-reported coverage, with the configuration that produced it.
    fpv_checker_coverage: Optional[Dict[str, object]] = None
    les_cost_floor: float = 0.01

    @property
    def les(self) -> float:
        return llm_efficiency_score(
            self.qualification.proved_non_vacuous,
            self.sensitivity.detection_rate,
            self.effort.llm_cost,
            self.les_cost_floor,
        )

    def to_dict(self) -> dict:
        return {
            'module': self.module,
            'method': self.method,
            'model': self.model,
            'formal_tool': self.formal_tool,
            'qualification': self.qualification.to_dict(),
            'diversity': self.diversity.to_dict(),
            'sensitivity': self.sensitivity.to_dict(),
            'effort': self.effort.to_dict(),
            'fpv_checker_coverage': self.fpv_checker_coverage,
            'llm_efficiency_score': round(self.les, 4),
            'les_cost_floor': self.les_cost_floor,
            'definitions': CHECKER_COVERAGE_DEFINITION,
        }

    def dump(self, path: str) -> None:
        with open(path, 'w') as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)

    def format_report(self) -> str:
        qual = self.qualification
        div = self.diversity
        sens = self.sensitivity
        effort = self.effort

        lines = [
            '=' * 78,
            f'SNAPSHOT METRICS — {self.module} ({self.method}, {self.model}, '
            f'{self.formal_tool})',
            '=' * 78,
            '',
            '1. Proof status and vacuity (primary)',
            f'   properties generated            {qual.total_properties}',
            f'   proved non-vacuously            {qual.proved_non_vacuous}',
            f'   proved vacuously                {qual.proved_vacuous}',
            f'   failing: property mismatch      {qual.failing_property_mismatch}',
            f'   failing: missing assumption     {qual.failing_missing_assumption}',
            f'   inconclusive                    {qual.inconclusive}',
            f'   snapshot yield                  {qual.snapshot_yield:.1%}',
            f'   proofs with explicit vacuity check  {qual.vacuity_checked_proofs}'
            f' (no antecedent: {qual.vacuity_unchecked_proofs})',
            '',
            '2. Assumption dependence',
            f'   proofs relying on assumptions   {qual.proven_assumption_dependent}',
            f'   standalone proofs               {qual.proven_standalone}',
            f'   constraint conflict check       '
            f'{qual.constraint_conflict_status or "not run"}',
            '',
            '3. Cone-of-influence diversity',
            f'   design signals                  {div.design_signal_count}',
            f'   union cone size                 {div.union_cone_size}',
            f'   mean cone size                  {div.mean_cone_size:.1f}',
            f'   distinct cone signatures        {div.distinct_cone_signatures}',
            f'   mean pairwise Jaccard distance  {div.mean_pairwise_jaccard_distance:.3f}',
            f'   cone redundancy                 {div.redundancy:.1%}',
        ]

        if div.tool_union_coi_size:
            lines.append(
                f'   tool COI: mean {div.tool_mean_coi_size:.0f} objects, '
                f'union {div.tool_union_coi_size}')

        lines += [
            '',
            '4. Regression sensitivity (mutation)',
            f'   mutants generated               {sens.mutants_generated}',
            f'   dropped as equivalent           {sens.mutants_equivalent}',
            f'   applied                         {sens.mutants_applied}',
            f'   detected                        {sens.mutants_detected}',
            f'   detection rate                  {sens.detection_rate:.1%}',
        ]
        for fault_class, rate in sens.class_rates().items():
            counts = sens.detection_by_class[fault_class]
            lines.append(
                f'     {fault_class:<28}{rate:>6.0%}'
                f'  ({counts.get("detected", 0)}/{counts.get("applied", 0)})')

        lines += [
            '',
            '5. Runtime and LLM cost',
            f'   total wall time                 {effort.total_seconds:.1f}s',
            f'     formal                        {effort.formal_seconds:.1f}s',
            f'     LLM                           {effort.llm_seconds:.1f}s',
            f'   formal runs                     {effort.formal_runs}',
            f'   LLM calls                       {effort.llm_calls}',
            f'   tokens in / out                 {effort.input_tokens} / '
            f'{effort.output_tokens}',
            f'   LLM cost                        {effort.llm_cost:.4f} {effort.currency}',
        ]
        if effort.repair_attempts_saved:
            lines.append(
                f'   repair attempts avoided         {effort.repair_attempts_saved} '
                f'(adaptive stopping)')

        lines += [
            '',
            '6. Checker coverage (secondary — see definition below)',
            f'   structural checker coverage     {div.checker_coverage:.1%}',
        ]
        if self.fpv_checker_coverage:
            tool = self.fpv_checker_coverage
            density = tool.get('property_density')
            core = tool.get('formal_core_coverage')
            if density is not None:
                lines.append(
                    f'   property density (registers)    {float(density):.1%}'
                    f'  ({tool.get("property_density_covered", 0)}/'
                    f'{tool.get("property_density_total", 0)})')
            if tool.get('property_density_pi') is not None:
                lines.append(
                    f'   property density (inputs)       '
                    f'{float(tool["property_density_pi"]):.1%}'
                    f'  ({tool.get("property_density_pi_covered", 0)}/'
                    f'{tool.get("property_density_pi_total", 0)})')
            if core is not None:
                lines.append(
                    f'   formal core coverage            {float(core):.1%}'
                    f'  ({tool.get("formal_core_coverage_count", 0)} registers '
                    f'used by the non-vacuous proofs)')
            if tool.get('formal_core_coverage_pi') is not None:
                lines.append(
                    f'   formal core inputs used         '
                    f'{float(tool["formal_core_coverage_pi"]):.1%}'
                    f'  ({tool.get("formal_core_coverage_pi_count", 0)}/'
                    f'{tool.get("property_density_pi_total", 0)})')
            if tool.get('formal_core_note'):
                lines.append(f'   note: {tool["formal_core_note"]}')
        else:
            lines.append('   FPV tool coverage               not collected')

        lines += [
            '',
            f'LLM efficiency score (LES)         {self.les:.2f}'
            f'   [= {qual.proved_non_vacuous} proofs x '
            f'{sens.detection_rate:.2f} detection / '
            f'max({effort.llm_cost:.4f}, {self.les_cost_floor}) {effort.currency}]',
            '',
            CHECKER_COVERAGE_DEFINITION,
        ]
        return '\n'.join(lines)


def metrics_from_run(
    module: str,
    formal_result,
    diversity_metrics=None,
    cost_ledger=None,
    timing: Optional[Dict[str, float]] = None,
    sensitivity: Optional[SensitivityReport] = None,
    model: str = '',
    formal_tool: str = '',
    method: str = 'svapshot',
    stopping_policy=None,
) -> SnapshotMetrics:
    """Assemble :class:`SnapshotMetrics` from the objects a run produces.

    Every argument is optional so that partial runs (for example, a baseline
    that skips mutation) still produce a well-formed report.
    """
    metrics = SnapshotMetrics(
        module=module, model=model, formal_tool=formal_tool, method=method)

    if formal_result is not None:
        counts = formal_result.counts_by_qualification()
        audit = formal_result.vacuity_audit()
        dependence = formal_result.assumption_dependence_audit()
        metrics.qualification = QualificationMetrics(
            total_properties=len(formal_result.records),
            proved_non_vacuous=counts.get('proved_non_vacuous', 0),
            proved_vacuous=counts.get('proved_vacuous', 0),
            failing_property_mismatch=counts.get('failing_property_mismatch', 0),
            failing_missing_assumption=counts.get('failing_missing_assumption', 0),
            inconclusive=counts.get('inconclusive', 0),
            proven_assumption_dependent=int(dependence['proven_assumption_dependent']),
            proven_standalone=int(dependence['proven_standalone']),
            vacuity_checked_proofs=(
                audit['proven_vacuity_non_vacuous'] + audit['proven_vacuity_vacuous']
            ),
            vacuity_unchecked_proofs=audit['proven_vacuity_not_checked'],
            constraint_conflict_status=formal_result.summary.constraint_conflict_status,
        )

        tool_coverage = (
            formal_result.tool_checker_coverage()
            if hasattr(formal_result, 'tool_checker_coverage') else {}
        )
        if tool_coverage:
            metrics.fpv_checker_coverage = tool_coverage

        tool_cones = [
            record.coi_size for record in formal_result.records.values()
            if record.coi_size
        ]
        if tool_cones:
            union = set()
            for record in formal_result.records.values():
                union |= set(record.coi_objects)
            metrics.diversity.tool_mean_coi_size = sum(tool_cones) / len(tool_cones)
            metrics.diversity.tool_union_coi_size = len(union)

    if diversity_metrics is not None:
        metrics.diversity.property_count = diversity_metrics.property_count
        metrics.diversity.design_signal_count = diversity_metrics.design_signal_count
        metrics.diversity.union_cone_size = diversity_metrics.union_cone_size
        metrics.diversity.checker_coverage = diversity_metrics.coi_coverage
        metrics.diversity.mean_cone_size = diversity_metrics.mean_cone_size
        metrics.diversity.distinct_cone_signatures = (
            diversity_metrics.distinct_cone_signatures
        )
        metrics.diversity.mean_pairwise_jaccard_distance = (
            diversity_metrics.mean_pairwise_jaccard_distance
        )
        metrics.diversity.redundancy = diversity_metrics.redundancy

    if cost_ledger is not None:
        metrics.effort.llm_calls = cost_ledger.total_calls
        metrics.effort.input_tokens = cost_ledger.total_input_tokens
        metrics.effort.output_tokens = cost_ledger.total_output_tokens
        metrics.effort.llm_cost = cost_ledger.total_cost
        metrics.effort.llm_seconds = cost_ledger.total_seconds
        metrics.effort.currency = cost_ledger.currency

    if timing:
        for key, value in timing.items():
            if hasattr(metrics.effort, key):
                setattr(metrics.effort, key, value)

    if sensitivity is not None:
        metrics.sensitivity = sensitivity

    if stopping_policy is not None:
        metrics.effort.repair_attempts = stopping_policy.total_attempts
        summary = stopping_policy.to_dict()
        metrics.effort.repair_attempts_saved = summary['attempts_saved_vs_fixed_budget']

    return metrics
