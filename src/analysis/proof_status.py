"""Qualified proof status parsing for SVApshot.

The snapshot is only trustworthy if every property carries an explicit
qualification rather than a binary pass/fail.  This module turns raw formal-tool
logs into :class:`PropertyRecord` objects and assigns each property one of the
qualifications defined by the SVApshot methodology:

===========================  ==================================================
``proved_non_vacuous``       Proof holds and the antecedent is reachable.
``proved_vacuous``           Tool reports a proof, but the vacuity check shows
                             the property is trivially true.
``failing_property_mismatch``  Counterexample that survives assumption analysis:
                             the property genuinely disagrees with the RTL.
``failing_missing_assumption``  Counterexample that disappears once a screened
                             environment constraint is added.
``inconclusive``             Bounded proof, timeout, or no result at all.
===========================  ==================================================

Vacuity is reported by VC Formal as a separate ``vacuity``-typed result line, so
proof status and vacuity status are parsed independently and joined per
property.  ``failing_missing_assumption`` cannot be decided from a single log;
it is assigned later by :mod:`assumption_gen`, which re-runs the property under
candidate assumptions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Iterable, List, Optional


class Qualification(str, Enum):
    """Snapshot qualification of a single property."""

    PROVED_NON_VACUOUS = 'proved_non_vacuous'
    PROVED_VACUOUS = 'proved_vacuous'
    FAILING_PROPERTY_MISMATCH = 'failing_property_mismatch'
    FAILING_MISSING_ASSUMPTION = 'failing_missing_assumption'
    INCONCLUSIVE = 'inconclusive'

    @property
    def is_snapshot_worthy(self) -> bool:
        """True when the property may enter the regression contract.

        Only non-vacuous proofs carry regression value: a vacuous proof checks
        nothing, and a failing property has no established behaviour to defend.
        """
        return self is Qualification.PROVED_NON_VACUOUS


class VacuityStatus(str, Enum):
    NON_VACUOUS = 'non_vacuous'
    VACUOUS = 'vacuous'
    NOT_CHECKED = 'not_checked'


class ProofStatus(str, Enum):
    PROVEN = 'proven'
    FALSIFIED = 'falsified'
    BOUNDED_PROVEN = 'bounded_proven'
    INCONCLUSIVE = 'inconclusive'
    UNKNOWN = 'unknown'


#: Legacy three-value codes consumed by the existing agent control flow.
LEGACY_PROVEN = 'proven'
LEGACY_CEX = 'cex'
LEGACY_UNDEF = 'undef'


@dataclass
class PropertyRecord:
    """One property as reported by the formal tool, with its qualification."""

    name: str
    full_path: str = ''
    proof_status: ProofStatus = ProofStatus.UNKNOWN
    vacuity_status: VacuityStatus = VacuityStatus.NOT_CHECKED
    engine: str = ''
    proof_time: str = ''
    bound_depth: Optional[int] = None
    #: Witness verdict ('covered' / 'uncoverable') when fml_witness_on is set.
    witness_status: str = ''
    #: file:line of the property, from report_fv -verbose.
    source_location: str = ''
    #: Constraints the tool's formal core shows this proof relied on. A
    #: non-empty list means the result is only valid under those assumptions.
    assumption_dependence: List[str] = field(default_factory=list)
    #: Screened assumptions that turn this property's counterexample into a
    #: proof. Distinct from assumption_dependence: this is evidence that the
    #: *environment* was under-constrained, not that the property is wrong.
    missing_assumptions: List[str] = field(default_factory=list)
    formal_core_registers: int = 0
    formal_core_inputs: int = 0
    formal_core_constraints: int = 0
    #: Named registers and inputs in this proof's formal core, from
    #: report_formal_core -verbose. Kept apart because the coverage figures are
    #: measured against different denominators.
    formal_core_register_names: List[str] = field(default_factory=list)
    formal_core_input_names: List[str] = field(default_factory=list)
    #: Tool-computed cone of influence (report_fv_complexity).
    coi_size: int = 0
    coi_objects: List[str] = field(default_factory=list)
    #: Free-text explanation, used for surviving-mutant and escape analysis.
    note: str = ''

    @property
    def qualification(self) -> Qualification:
        if self.proof_status is ProofStatus.PROVEN:
            if self.vacuity_status is VacuityStatus.VACUOUS:
                return Qualification.PROVED_VACUOUS
            return Qualification.PROVED_NON_VACUOUS
        if self.proof_status is ProofStatus.FALSIFIED:
            if self.missing_assumptions:
                return Qualification.FAILING_MISSING_ASSUMPTION
            return Qualification.FAILING_PROPERTY_MISMATCH
        return Qualification.INCONCLUSIVE

    @property
    def legacy_result(self) -> str:
        """Map onto the 'proven'/'cex'/'undef' vocabulary used by the agent.

        A vacuous proof is deliberately *not* reported as ``proven``: the whole
        point of qualification is that a vacuous property must be repaired or
        dropped rather than banked as verified behaviour.
        """
        if self.proof_status is ProofStatus.PROVEN:
            if self.vacuity_status is VacuityStatus.VACUOUS:
                return LEGACY_CEX
            return LEGACY_PROVEN
        if self.proof_status is ProofStatus.FALSIFIED:
            return LEGACY_CEX
        return LEGACY_UNDEF

    def to_dict(self) -> dict:
        data = asdict(self)
        data['proof_status'] = self.proof_status.value
        data['vacuity_status'] = self.vacuity_status.value
        data['qualification'] = self.qualification.value
        return data


@dataclass
class FormalRunSummary:
    """Tool-reported totals plus provenance of the run that produced them."""

    tool: str = ''
    tool_version: str = ''
    assertions_found: int = 0
    assertions_proven: int = 0
    assertions_falsified: int = 0
    assertions_vacuous: int = 0
    assertions_inconclusive: int = 0
    vacuity_found: int = 0
    vacuity_non_vacuous: int = 0
    vacuity_vacuous: int = 0
    witness_found: int = 0
    witness_covered: int = 0
    witness_uncoverable: int = 0
    constraints_found: int = 0
    covers_found: int = 0
    covers_covered: int = 0
    covers_unreachable: int = 0
    #: Result of check_constraints: 'no_conflict', 'conflict', 'no_constraints'
    #: when the property set had no assumptions to check, or '' if not run.
    constraint_conflict_status: str = ''
    deadends_found: int = 0
    wall_time_s: Optional[float] = None
    cpu_time_s: Optional[float] = None
    peak_memory_mb: Optional[float] = None
    compile_failed: bool = False
    #: True when VC Formal never reached compilation because its shared session
    #: database was locked or the static server failed to start.
    infrastructure_failure: bool = False
    #: True when the tool exited without a license.  The run carries no evidence
    #: about the design, so its results must not be read as "nothing proved".
    license_failure: bool = False
    #: True when the log has no per-property lines at all (e.g. missing -block).
    no_per_property_results: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FormalRunResult:
    """Complete parse of one formal-tool invocation."""

    records: Dict[str, PropertyRecord] = field(default_factory=dict)
    summary: FormalRunSummary = field(default_factory=FormalRunSummary)
    #: ``report_assertion_density`` scopes, keyed by 'reg' / 'pi'. Empty when the
    #: tool did not produce the report (older versions, JasperGold runs).
    density: Dict[str, 'DensityScope'] = field(default_factory=dict)

    def legacy_results(self) -> Dict[str, str]:
        return {name: rec.legacy_result for name, rec in self.records.items()}

    def qualifications(self) -> Dict[str, Qualification]:
        return {name: rec.qualification for name, rec in self.records.items()}

    def counts_by_qualification(self) -> Dict[str, int]:
        counts = {q.value: 0 for q in Qualification}
        for rec in self.records.values():
            counts[rec.qualification.value] += 1
        return counts

    def names_with(self, qualification: Qualification) -> List[str]:
        return sorted(
            name for name, rec in self.records.items()
            if rec.qualification is qualification
        )

    def vacuity_audit(self) -> Dict[str, int]:
        """Split proofs by whether the tool actually ran a vacuity check.

        VC Formal only creates a vacuity check for properties that have an
        antecedent, so ``proven_vacuity_not_checked`` counts properties that
        cannot be vacuous by construction.  Reporting the split keeps the
        non-vacuity claim auditable instead of implying every proof was
        explicitly screened.
        """
        audit = {
            'proven_vacuity_non_vacuous': 0,
            'proven_vacuity_vacuous': 0,
            'proven_vacuity_not_checked': 0,
        }
        for record in self.records.values():
            if record.proof_status is not ProofStatus.PROVEN:
                continue
            if record.vacuity_status is VacuityStatus.NON_VACUOUS:
                audit['proven_vacuity_non_vacuous'] += 1
            elif record.vacuity_status is VacuityStatus.VACUOUS:
                audit['proven_vacuity_vacuous'] += 1
            else:
                audit['proven_vacuity_not_checked'] += 1
        return audit

    def assumption_dependence_audit(self) -> Dict[str, object]:
        """Report how many results lean on environment constraints.

        A proof whose formal core contains a constraint is conditional on that
        constraint; reporting the count next to the proof count keeps the
        snapshot honest about how much of it is assumption-supported.
        """
        proven = [r for r in self.records.values() if r.proof_status is ProofStatus.PROVEN]
        dependent = [r for r in proven if r.assumption_dependence]
        constraints = sorted({c for r in dependent for c in r.assumption_dependence})
        return {
            'proven': len(proven),
            'proven_assumption_dependent': len(dependent),
            'proven_standalone': len(proven) - len(dependent),
            'distinct_constraints_used': len(constraints),
            'constraints_used': constraints,
            'failing_due_to_missing_assumption': len(
                self.names_with(Qualification.FAILING_MISSING_ASSUMPTION)
            ),
        }

    def tool_checker_coverage(self) -> Dict[str, object]:
        """The tool's own coverage figures, with the scope each one was measured on.

        Two numbers, deliberately reported together, because the gap between
        them is the interesting part:

        ``property_density``
            registers in the cone of influence of at least one assert, over all
            registers in scope. Answers "what does the snapshot look at".
        ``formal_core_coverage``
            registers the non-vacuous proofs actually needed, over the same
            denominator. Always <= property density, since the formal core is a
            subset of the COI. Answers "what did the proofs use".

        A large density with a small core is the signature the first study was
        criticised for: assertions that touch much of the design but whose
        proofs lean on very little of it.  A core with no registers at all means
        every proof is combinational reasoning over the inputs, and no amount of
        density makes such a snapshot sensitive to state-holding faults.
        """
        coverage: Dict[str, object] = {}

        for kind, scope in sorted(self.density.items()):
            label = 'property_density' if kind == 'reg' else f'property_density_{kind}'
            coverage[label] = round(scope.density, 4)
            coverage[f'{label}_covered'] = len(scope.covered_by_asserts)
            coverage[f'{label}_total'] = scope.total_count

        proved = [
            record for record in self.records.values()
            if record.qualification is Qualification.PROVED_NON_VACUOUS
        ]
        cores = {
            'reg': {n for r in proved for n in r.formal_core_register_names},
            'pi': {n for r in proved for n in r.formal_core_input_names},
        }
        for kind, core in cores.items():
            scope = self.density.get(kind)
            if scope is None or not scope.total:
                if core:
                    coverage[f'formal_core_{kind}_count'] = len(core)
                continue
            hits = _scope_overlap(core, scope.total)
            label = ('formal_core_coverage' if kind == 'reg'
                     else f'formal_core_coverage_{kind}')
            coverage[label] = round(hits / scope.total_count, 4)
            coverage[f'{label}_count'] = hits

        if proved and not cores['reg']:
            coverage['formal_core_note'] = (
                'no proof needed a register: every property in this snapshot is '
                'established by combinational reasoning over the inputs, so '
                'faults in state-holding logic cannot be detected'
            )

        if coverage:
            coverage['source'] = (
                'VC Formal report_assertion_density (-type reg/-type pi) and '
                'report_formal_core -verbose, restricted to proved_non_vacuous '
                'properties'
            )
        return coverage

    def to_dict(self) -> dict:
        return {
            'summary': self.summary.to_dict(),
            'tool_checker_coverage': self.tool_checker_coverage(),
            'counts_by_qualification': self.counts_by_qualification(),
            'vacuity_audit': self.vacuity_audit(),
            'assumption_dependence_audit': self.assumption_dependence_audit(),
            'properties': {name: rec.to_dict() for name, rec in sorted(self.records.items())},
        }


# ---------------------------------------------------------------------------
# VC Formal
# ---------------------------------------------------------------------------

# [Info] PROP_I_RESULT: FPV  <path>  <engine>  <status>[:<depth>]  <type>  <time>
# The engine field is absent on intermediate "checking" lines, so engine and
# status are captured as two optional-ish fields and disambiguated afterwards.
_VCF_RESULT_RE = re.compile(
    r'PROP_I_RESULT:\s+(?P<task>\w+)\s+(?P<path>\S+)\s+'
    r'(?P<fields>.+?)\s+(?P<type>property|vacuity|cover|constraint)\s+'
    r'(?P<time>\d+:\d+:\d+)',
    re.MULTILINE,
)

# ``compute_formal_core_coverage`` runs a distinct coverage proof task. Its
# line/condition/toggle goals use the same PROP_I_RESULT form as user
# assertions, so admitting them would invent thousands of assertion records.
_VCF_COVERAGE_TASK_RE = re.compile(r'_COV_|^COV$')

_VCF_VERSION_RE = re.compile(r'Version\s+(\S+)\s+for\s+\S+\s+-\s+(.+)$', re.MULTILINE)

_VCF_PROOF_STATUS = {
    'proven': ProofStatus.PROVEN,
    # A "vacuous" status on a *property* line is still a proof: the tool proved
    # the assertion but its antecedent is unreachable. Recording it as PROVEN and
    # letting the vacuity field carry the caveat is what makes the distinction
    # between proved_non_vacuous and proved_vacuous possible.
    'vacuous': ProofStatus.PROVEN,
    'falsified': ProofStatus.FALSIFIED,
    'inconclusive': ProofStatus.INCONCLUSIVE,
    'bounded_proven': ProofStatus.BOUNDED_PROVEN,
    'undetermined': ProofStatus.INCONCLUSIVE,
    'error': ProofStatus.UNKNOWN,
}


def _split_status(fields: str):
    """Split the engine/status field group of a PROP_I_RESULT line.

    Returns ``(engine, status, depth)``.  Intermediate lines carry only
    ``checking`` with no engine; final lines carry ``<engine> <status>`` where
    the status may have a ``:<depth>`` bound suffix.
    """
    parts = fields.split()
    if not parts:
        return '', '', None

    raw_status = parts[-1]
    engine = parts[-2] if len(parts) >= 2 else ''

    depth = None
    if ':' in raw_status:
        status, _, depth_text = raw_status.partition(':')
        if depth_text.isdigit():
            depth = int(depth_text)
    else:
        status = raw_status

    return engine, status, depth


#: Echoed by the generated TCL in place of a constraint check it did not need.
_VCF_CONSTRAINT_CHECK_SKIPPED = 'CHECK_CONSTRAINTS_SKIPPED'


def parse_vcformal_log(log_text: str) -> FormalRunResult:
    """Parse a VC Formal FPV log into qualified property records."""
    result = FormalRunResult()
    result.summary.tool = 'vcformal'

    version_match = _VCF_VERSION_RE.search(log_text)
    if version_match:
        result.summary.tool_version = version_match.group(1).strip()

    for match in _VCF_RESULT_RE.finditer(log_text):
        path = match.group('path')
        result_type = match.group('type')
        engine, status, depth = _split_status(match.group('fields'))

        if _VCF_COVERAGE_TASK_RE.search(match.group('task')):
            continue
        if status == 'checking':
            continue  # intermediate progress line
        if result_type not in ('property', 'vacuity'):
            # Cover and constraint goals are reported on the same line shape but
            # are not assertions; creating records for them would pollute the
            # qualification counts with entries that have no proof status.
            continue

        name = path.rsplit('.', 1)[-1]
        record = result.records.get(name)
        if record is None:
            record = PropertyRecord(name=name, full_path=path)
            result.records[name] = record

        if result_type == 'property':
            record.proof_status = _VCF_PROOF_STATUS.get(status, ProofStatus.UNKNOWN)
            record.engine = engine
            record.proof_time = match.group('time')
            record.bound_depth = depth
            if status == 'vacuous':
                record.vacuity_status = VacuityStatus.VACUOUS
        elif result_type == 'vacuity':
            if status == 'non_vacuous':
                record.vacuity_status = VacuityStatus.NON_VACUOUS
            elif status == 'vacuous':
                record.vacuity_status = VacuityStatus.VACUOUS

    _parse_vcf_summary(log_text, result.summary)

    # An assumption set with no members cannot conflict with itself, so the
    # generated script skips check_constraints rather than opening a formal run
    # to establish it. That is a different state from never having asked.
    if _VCF_CONSTRAINT_CHECK_SKIPPED in log_text:
        result.summary.constraint_conflict_status = 'no_constraints'

    if not result.records:
        result.summary.no_per_property_results = True
        result.summary.license_failure = _vcf_license_failed(log_text)
        result.summary.infrastructure_failure = infrastructure_failure_in_log(log_text)
        result.summary.compile_failed = _vcf_compile_failed(log_text)

    return result


def _parse_int(pattern: str, text: str, default: int = 0) -> int:
    match = re.search(pattern, text, re.MULTILINE)
    return int(match.group(1)) if match else default


def _parse_float(pattern: str, text: str) -> Optional[float]:
    """Return the last match of a numeric pattern.

    Resource figures appear both in periodic progress lines and in the final
    summary block; the final occurrence is the run total.
    """
    matches = re.findall(pattern, text, re.MULTILINE)
    return float(matches[-1]) if matches else None


def _parse_vcf_summary(log_text: str, summary: FormalRunSummary) -> None:
    """Extract the 'Summary Results' block of a VC Formal log.

    The block is section-structured (``> Assertion``, ``> Vacuity``,
    ``> Constraint``, ``> Cover``) and several sections reuse the
    ``# found`` label, so each section body is isolated before scanning.
    """
    summary_match = re.search(
        r'Property Summary:\s*\w+(?P<body>.*?)(?:\n\s*\n\s*\w|Solver jobs summary|\Z)',
        log_text,
        re.DOTALL,
    )
    body = summary_match.group('body') if summary_match else ''

    sections = {}
    current = None
    for line in body.splitlines():
        section_match = re.match(r'\s*>\s*(\w+)', line)
        if section_match:
            current = section_match.group(1).lower()
            sections[current] = []
        elif current:
            sections[current].append(line)

    def section_text(key: str) -> str:
        return '\n'.join(sections.get(key, []))

    assertion_text = section_text('assertion')
    summary.assertions_found = _parse_int(r'#\s*found\s*:\s*(\d+)', assertion_text)
    summary.assertions_proven = _parse_int(r'#\s*proven\s*:\s*(\d+)', assertion_text)
    summary.assertions_falsified = _parse_int(r'#\s*falsified\s*:\s*(\d+)', assertion_text)
    # VC Formal counts a vacuous assertion under Assertion as well, separately
    # from "proven": the two categories do not overlap in the summary block.
    summary.assertions_vacuous = _parse_int(r'#\s+vacuous\s*:\s*(\d+)', assertion_text)
    summary.assertions_inconclusive = _parse_int(
        r'#\s*inconclusive\s*:\s*(\d+)', assertion_text
    )

    vacuity_text = section_text('vacuity')
    summary.vacuity_found = _parse_int(r'#\s*found\s*:\s*(\d+)', vacuity_text)
    summary.vacuity_non_vacuous = _parse_int(r'#\s*non_vacuous\s*:\s*(\d+)', vacuity_text)
    # "# vacuous" would also match inside "# non_vacuous"; require a word start.
    summary.vacuity_vacuous = _parse_int(r'#\s+vacuous\s*:\s*(\d+)', vacuity_text)

    witness_text = section_text('witness')
    summary.witness_found = _parse_int(r'#\s*found\s*:\s*(\d+)', witness_text)
    summary.witness_covered = _parse_int(r'#\s*covered\s*:\s*(\d+)', witness_text)
    summary.witness_uncoverable = _parse_int(r'#\s*uncoverable\s*:\s*(\d+)', witness_text)

    summary.constraints_found = _parse_int(r'#\s*found\s*:\s*(\d+)', section_text('constraint'))

    cover_text = section_text('cover')
    summary.covers_found = _parse_int(r'#\s*found\s*:\s*(\d+)', cover_text)
    summary.covers_covered = _parse_int(r'#\s*covered\s*:\s*(\d+)', cover_text)
    summary.covers_unreachable = _parse_int(r'#\s*unreachable\s*:\s*(\d+)', cover_text)

    # Anchor at line start so the per-engine figures ("Engine Wall Time(S)",
    # "Engine Peak Memory(MB)") do not shadow the run totals.
    summary.wall_time_s = _parse_float(r'^Total Time\(S\)\s*:\s*([\d.]+)', log_text)
    summary.cpu_time_s = _parse_float(r'^CPU Time\(S\)\s*:\s*([\d.]+)', log_text)
    summary.peak_memory_mb = _parse_float(r'^Peak Memory\(MB\)\s*:\s*([\d.]+)', log_text)


# ---------------------------------------------------------------------------
# VC Formal auxiliary reports (vcf_projs/<module>/reports/)
#
# `report_fv -verbose` is the authoritative source: it lists the final status,
# vacuity and witness verdicts, the engine, the proof depth and the source
# location of every property in one place, whereas the streaming log interleaves
# intermediate "checking" lines that have to be de-duplicated.  The streaming
# log stays as a fallback for runs where the report was not produced.
# ---------------------------------------------------------------------------

#   > ID: [8] falsified (depth=2)
_VCF_RPT_ENTRY_RE = re.compile(
    r'^\s*>\s*ID:\s*\[(?P<id>\d+)\]\s*(?P<status>[a-z_]+)(?:\s*\(depth=(?P<depth>\d+)\))?',
    re.MULTILINE,
)
_VCF_RPT_FIELD_RE = re.compile(r'^\s*-\s*(?P<key>[a-z_]+)\s*:\s*(?P<value>.*?)\s*$')

_VCF_VACUITY_STATUS = {
    'non_vacuous': VacuityStatus.NON_VACUOUS,
    'vacuous': VacuityStatus.VACUOUS,
}


def parse_vcformal_property_report(report_text: str) -> FormalRunResult:
    """Parse ``report_fv -verbose`` output into qualified property records."""
    result = FormalRunResult()
    result.summary.tool = 'vcformal'

    verbose_start = report_text.find('Verbose Results')
    summary_text = report_text[:verbose_start] if verbose_start >= 0 else report_text
    _parse_vcf_report_summary(summary_text, result.summary)

    body = report_text[verbose_start:] if verbose_start >= 0 else report_text
    entries = list(_VCF_RPT_ENTRY_RE.finditer(body))

    for index, entry in enumerate(entries):
        end = entries[index + 1].start() if index + 1 < len(entries) else len(body)
        fields = {}
        for line in body[entry.end():end].splitlines():
            field = _VCF_RPT_FIELD_RE.match(line)
            if field:
                fields[field.group('key')] = field.group('value')

        full_path = fields.get('name', '')
        if not full_path:
            continue
        # The same entry shape is used for covers and constraints; the usage
        # attribute is what separates them from assertions.
        if fields.get('usage', 'assert') != 'assert':
            continue

        record = PropertyRecord(
            name=full_path.rsplit('.', 1)[-1],
            full_path=full_path,
            proof_status=_VCF_PROOF_STATUS.get(entry.group('status'), ProofStatus.UNKNOWN),
            engine=fields.get('engine', ''),
            proof_time=fields.get('elapsed_time', ''),
        )

        depth = entry.group('depth') or fields.get('safe_depth') or fields.get('trace_depth')
        if depth and depth.isdigit():
            record.bound_depth = int(depth)

        vacuity = fields.get('vacuity', '')
        if vacuity in _VCF_VACUITY_STATUS:
            record.vacuity_status = _VCF_VACUITY_STATUS[vacuity]
        elif entry.group('status') == 'vacuous':
            record.vacuity_status = VacuityStatus.VACUOUS

        record.source_location = fields.get('location', '')
        record.witness_status = fields.get('witness', '')
        result.records[record.name] = record

    if not result.records:
        result.summary.no_per_property_results = True

    return result


def _parse_vcf_report_summary(summary_text: str, summary: FormalRunSummary) -> None:
    """Parse the 'Summary Results' block that opens ``report_fv -verbose``."""
    sections: Dict[str, List[str]] = {}
    current = None
    for line in summary_text.splitlines():
        section_match = re.match(r'\s*>\s*(\w+)\s*$', line)
        if section_match:
            current = section_match.group(1).lower()
            sections[current] = []
        elif current:
            sections[current].append(line)

    def value(section: str, label: str) -> int:
        text = '\n'.join(sections.get(section, []))
        return _parse_int(rf'#\s+{label}\s*:\s*(\d+)', text)

    summary.assertions_found = value('assertion', 'found')
    summary.assertions_proven = value('assertion', 'proven')
    summary.assertions_falsified = value('assertion', 'falsified')
    summary.assertions_vacuous = value('assertion', 'vacuous')
    summary.assertions_inconclusive = value('assertion', 'inconclusive')
    summary.vacuity_found = value('vacuity', 'found')
    summary.vacuity_non_vacuous = value('vacuity', 'non_vacuous')
    summary.vacuity_vacuous = value('vacuity', 'vacuous')
    summary.witness_found = value('witness', 'found')
    summary.witness_covered = value('witness', 'covered')
    summary.witness_uncoverable = value('witness', 'uncoverable')
    summary.constraints_found = value('constraint', 'found')
    summary.covers_found = value('cover', 'found')
    summary.covers_covered = value('cover', 'covered')
    summary.covers_unreachable = value('cover', 'unreachable')


@dataclass
class FormalCoreEntry:
    """One row of ``report_formal_core``: what a proof actually depended on."""

    property_path: str
    run_status: str = ''
    status: str = ''
    registers: int = 0
    inputs: int = 0
    constraints: int = 0
    depth: Optional[int] = None
    register_names: List[str] = field(default_factory=list)
    input_names: List[str] = field(default_factory=list)
    constraint_names: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.property_path.rsplit('.', 1)[-1]

    def to_dict(self) -> dict:
        return asdict(self)


# | completed |     0 |    3 |     0 |  success |    - |    - | rr_arb_tree.gen_arbiter.gnt0
_FORMAL_CORE_ROW_RE = re.compile(
    r'^\s*\|?\s*(?P<run_status>completed|scheduled|failed|no_status)\s*\|'
    r'\s*(?P<registers>\d+)\s*\|\s*(?P<inputs>\d+)\s*\|\s*(?P<constraints>\d+)\s*\|'
    r'\s*(?P<status>\S+)\s*\|\s*(?P<depth>\S+)\s*\|\s*(?P<subtype>\S+)\s*\|'
    r'\s*(?P<property>\S+)',
    re.MULTILINE,
)


def parse_formal_core_report(report_text: str) -> Dict[str, FormalCoreEntry]:
    """Parse ``report_formal_core -list`` into per-property dependency counts."""
    entries: Dict[str, FormalCoreEntry] = {}
    for match in _FORMAL_CORE_ROW_RE.finditer(report_text):
        depth_text = match.group('depth')
        entry = FormalCoreEntry(
            property_path=match.group('property'),
            run_status=match.group('run_status'),
            status=match.group('status'),
            registers=int(match.group('registers')),
            inputs=int(match.group('inputs')),
            constraints=int(match.group('constraints')),
            depth=int(depth_text) if depth_text.isdigit() else None,
        )
        entries[entry.name] = entry
    return entries


def parse_formal_core_verbose(report_text: str) -> Dict[str, FormalCoreEntry]:
    """Parse ``report_formal_core -verbose`` into named dependency sets.

    The named constraint list is what identifies an assumption-dependent proof:
    a property whose formal core contains a generated assumption is only valid
    under that assumption, and the snapshot must say so.
    """
    entries: Dict[str, FormalCoreEntry] = {}
    current: Optional[FormalCoreEntry] = None
    bucket: Optional[List[str]] = None

    for line in report_text.splitlines():
        header = re.match(r'^\s*(Property|SubType|Status|Depth)\s*:\s*(.*?)\s*$', line)
        if header:
            key, value = header.group(1), header.group(2)
            if key == 'Property':
                current = FormalCoreEntry(property_path=value.strip())
                entries[current.name] = current
                bucket = None
            elif current is not None and key == 'Status':
                current.status = value.strip()
            elif current is not None and key == 'Depth' and value.strip().isdigit():
                current.depth = int(value.strip())
            continue

        counter = re.match(r'^\s*#(Registers|Inputs|Constraints)\s*:\s*(\d+)', line)
        if counter and current is not None:
            kind, count = counter.group(1), int(counter.group(2))
            if kind == 'Registers':
                current.registers = count
                bucket = current.register_names
            elif kind == 'Inputs':
                current.inputs = count
                bucket = current.input_names
            else:
                current.constraints = count
                bucket = current.constraint_names
            continue

        if bucket is not None and line.strip() and line.startswith((' ', '\t')):
            bucket.append(line.strip())

    return entries


def _base_object_name(name: str) -> str:
    """Strip a trailing bit range so the same register matches across reports."""
    return re.sub(r'\s*\[[^\]]*\]\s*$', '', name.strip())


def _scope_overlap(core: Iterable[str], scope: Iterable[str]) -> int:
    """Count objects of ``scope`` that appear in ``core``.

    The two VC Formal reports do not spell the same object the same way: the
    density report gives a full hierarchical path with a bit range, the formal
    core report often gives a bare name relative to the property's scope.  A
    plain set intersection therefore reports zero overlap where the overlap is
    total, so names are compared by hierarchical suffix.
    """
    core_names = {_base_object_name(name) for name in core}
    if not core_names:
        return 0

    hits = 0
    for name in scope:
        target = _base_object_name(name)
        if target in core_names or any(
            target.endswith('.' + candidate) or candidate.endswith('.' + target)
            for candidate in core_names
        ):
            hits += 1
    return hits


@dataclass
class DensityScope:
    """One ``report_assertion_density`` scope: registers or primary inputs.

    ``covered`` holds the objects that lie in the cone of influence of at least
    one asserted property; ``total`` is every object of that kind in the design
    scope.  The ratio is VC Formal's own answer to "how much of the design do
    these assertions look at", and it is the tool-side counterpart of the
    structural figure ``coi.py`` computes from the RTL.
    """

    kind: str = 'reg'
    covered_by_asserts: List[str] = field(default_factory=list)
    uncovered_by_asserts: List[str] = field(default_factory=list)
    total: List[str] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        # `Total ... in given scope` is authoritative; fall back to the union of
        # the covered and uncovered lists when the tool omits it.
        if self.total:
            return len(self.total)
        return len(set(self.covered_by_asserts) | set(self.uncovered_by_asserts))

    @property
    def density(self) -> float:
        total = self.total_count
        return len(self.covered_by_asserts) / total if total else 0.0

    def to_dict(self) -> dict:
        return {
            'kind': self.kind,
            'covered': len(self.covered_by_asserts),
            'total': self.total_count,
            'density': round(self.density, 4),
            'uncovered_examples': sorted(self.uncovered_by_asserts)[:20],
        }


# "Covered Registers (asserts): 5" / "Total Registers in given scope : 5"
_DENSITY_BUCKET_RE = re.compile(
    r'^(?P<state>Covered|Uncovered)\s+(?P<kind>.+?)\s+'
    r'\((?P<props>asserts\+covers|asserts|covers)\)\s*:\s*(?P<count>\d+)\s*$'
)
_DENSITY_TOTAL_RE = re.compile(
    r'^Total\s+(?P<kind>.+?)\s+in\s+given\s+scope\s*:\s*(?P<count>\d+)\s*$'
)
_DENSITY_ITEM_RE = re.compile(r'^#\d+:\s*(?P<name>\S+)')
_DENSITY_SCOPE_RE = re.compile(r'^DENSITY_SCOPE\s+(?P<kind>\w+)\s*$')


def parse_assertion_density_report(report_text: str) -> Dict[str, DensityScope]:
    """Parse ``report_assertion_density -list -status all`` blocks.

    The generated Tcl emits one ``DENSITY_SCOPE <kind>`` marker per invocation so
    that the register and primary-input reports can be told apart; a report
    without markers is treated as a single register scope.
    """
    scopes: Dict[str, DensityScope] = {}
    current = DensityScope()
    bucket: Optional[List[str]] = None

    for line in report_text.splitlines():
        stripped = line.strip()

        marker = _DENSITY_SCOPE_RE.match(stripped)
        if marker:
            current = scopes.setdefault(
                marker.group('kind'), DensityScope(kind=marker.group('kind')))
            bucket = None
            continue

        header = _DENSITY_BUCKET_RE.match(stripped)
        if header:
            # Only the assert-only figures matter: cover properties establish
            # reachability, not behaviour, and must not inflate the number.
            if header.group('props') == 'asserts':
                bucket = (
                    current.covered_by_asserts if header.group('state') == 'Covered'
                    else current.uncovered_by_asserts
                )
                bucket.clear()
            else:
                bucket = None
            continue

        total = _DENSITY_TOTAL_RE.match(stripped)
        if total:
            bucket = current.total
            bucket.clear()
            continue

        item = _DENSITY_ITEM_RE.match(stripped)
        if item and bucket is not None:
            bucket.append(item.group('name'))
            continue

        if stripped and not item:
            bucket = None

    if not scopes and (current.total or current.covered_by_asserts):
        scopes['reg'] = current
    return {kind: scope for kind, scope in scopes.items() if scope.total_count}


def parse_constraints_report(report_text: str) -> Dict[str, object]:
    """Parse ``report_constraints``: conflicting constraints and deadends.

    A conflicting-constraint or deadend verdict invalidates every proof in the
    run, so this is checked before any property is qualified.
    """
    conflict = re.search(r'Status\s*:\s*(\S+)', report_text)
    no_deadends = 'No deadends found' in report_text
    deadend_ids = set(re.findall(r'[Dd]eadend\s*[Ii][Dd]\s*[:=]?\s*(\d+)', report_text))

    return {
        'conflict_status': conflict.group(1) if conflict else '',
        'has_conflict': bool(conflict and conflict.group(1) != 'no_conflict'),
        'deadends_found': 0 if no_deadends else len(deadend_ids),
        'deadend_check_ran': bool(report_text.strip()),
    }


# #12: gen_arbiter.gen_levels[4].gen_level[11].sel { net}
_COI_OBJECT_RE = re.compile(
    r'^#\d+:\s*(?P<object>\S+)\s*\{\s*(?P<kind>[a-z_]+)(?P<extra>[^}]*)\}',
    re.MULTILINE,
)


def parse_coi_report(report_text: str) -> Dict[str, Dict[str, object]]:
    """Parse the per-property ``report_fv_complexity`` blocks of ``coi.rpt``.

    Returns a mapping of short property name to its cone of influence, split by
    object kind so that state (``ff``) and interface (``port``) contributions can
    be counted separately from pure combinational nets.
    """
    cones: Dict[str, Dict[str, object]] = {}

    for block in re.finditer(
        r'PROPERTY_COI_BEGIN\s+(?P<name>\S+)(?P<body>.*?)PROPERTY_COI_END',
        report_text,
        re.DOTALL,
    ):
        full_path = block.group('name')
        objects: Dict[str, set] = {}
        for obj in _COI_OBJECT_RE.finditer(block.group('body')):
            objects.setdefault(obj.group('kind'), set()).add(obj.group('object'))

        all_objects = set().union(*objects.values()) if objects else set()
        cones[full_path.rsplit('.', 1)[-1]] = {
            'full_path': full_path,
            'objects': sorted(all_objects),
            'by_kind': {kind: sorted(names) for kind, names in sorted(objects.items())},
            'size': len(all_objects),
            'registers': len(objects.get('ff', set())),
            'ports': len(objects.get('port', set())),
        }

    return cones


def load_vcformal_reports(report_dir: str) -> Dict[str, object]:
    """Load every auxiliary report produced by the generated FPV_vcf.tcl.

    Missing files are tolerated: the reports are wrapped in ``catch`` on the Tcl
    side precisely so that an older tool version still produces proof results.
    """
    import os

    def read(name: str) -> str:
        path = os.path.join(report_dir, name)
        if not os.path.isfile(path):
            return ''
        with open(path, 'r', errors='replace') as handle:
            return handle.read()

    property_report = read('properties.rpt')
    return {
        'properties': (
            parse_vcformal_property_report(property_report) if property_report else None
        ),
        'formal_core': parse_formal_core_report(read('formal_core.rpt')),
        'formal_core_verbose': parse_formal_core_verbose(read('formal_core_verbose.rpt')),
        'constraints': parse_constraints_report(read('constraints.rpt')),
        'coi': parse_coi_report(read('coi.rpt')),
        'density': parse_assertion_density_report(read('assertion_density.rpt')),
    }


def merge_reports_into_result(
    result: FormalRunResult,
    reports: Dict[str, object],
) -> FormalRunResult:
    """Enrich a log-derived result with the auxiliary report data.

    The report parse wins wherever the two disagree, because the streaming log
    can end on an intermediate line for a property whose proof finished later.
    """
    report_result = reports.get('properties')
    if isinstance(report_result, FormalRunResult) and report_result.records:
        for name, record in report_result.records.items():
            existing = result.records.get(name)
            if existing is not None:
                record.assumption_dependence = existing.assumption_dependence
                record.missing_assumptions = existing.missing_assumptions
                record.note = existing.note
            result.records[name] = record
        summary = report_result.summary
        summary.tool_version = result.summary.tool_version
        summary.wall_time_s = result.summary.wall_time_s
        summary.cpu_time_s = result.summary.cpu_time_s
        summary.peak_memory_mb = result.summary.peak_memory_mb
        result.summary = summary

    core_verbose = reports.get('formal_core_verbose') or {}
    core_list = reports.get('formal_core') or {}
    for name, record in result.records.items():
        entry = core_verbose.get(name) or core_list.get(name)
        if entry is None:
            continue
        record.formal_core_registers = entry.registers
        record.formal_core_inputs = entry.inputs
        record.formal_core_constraints = entry.constraints
        record.formal_core_register_names = sorted(set(entry.register_names))
        record.formal_core_input_names = sorted(set(entry.input_names))
        if entry.constraint_names:
            # A named constraint in the formal core is a real assumption
            # dependence: the proof does not hold without it.
            record.assumption_dependence = sorted(
                set(record.assumption_dependence) | set(entry.constraint_names)
            )

    cones = reports.get('coi') or {}
    for name, record in result.records.items():
        cone = cones.get(name)
        if cone:
            record.coi_size = int(cone['size'])
            record.coi_objects = list(cone['objects'])

    density = reports.get('density') or {}
    if density:
        result.density = dict(density)

    constraints = reports.get('constraints') or {}
    if constraints:
        result.summary.constraint_conflict_status = str(constraints.get('conflict_status', ''))
        result.summary.deadends_found = int(constraints.get('deadends_found', 0))

    return result


#: VC Formal reports licensing problems as ``*** Error:`` / ``** Error:`` lines
#: and then exits before elaborating anything.
_VCF_LICENSE_RE = re.compile(
    r'unable to checkout required license'
    r'|[Ll]icense checkout failed'
    r'|Licensing\.\s*License'
    r'|No such feature exists.*(?:VC-FORMAL|VCS)',
    re.IGNORECASE,
)


def license_failure_in_log(log_text: str) -> bool:
    """Public form of the license check, for callers deciding whether to retry."""
    return _vcf_license_failed(log_text)


_VCF_INFRASTRUCTURE_RE = re.compile(
    r'Another session is already running'
    r'|vcst_rtdb/session\.lock'
    r'|VC Static Server exited'
    r'|failed to start.*VC Static'
    r'|vcf:\s*(?:command not found|not found)',
    re.IGNORECASE,
)


def infrastructure_failure_in_log(log_text: str) -> bool:
    """True when VC Formal stopped before reading the design."""
    return bool(_VCF_INFRASTRUCTURE_RE.search(log_text))


def _vcf_license_failed(log_text: str) -> bool:
    """True if the tool never ran because it could not obtain a license.

    Worth distinguishing from a compile error: the design and the properties may
    be perfectly good, and a run that failed this way carries no evidence about
    them at all.
    """
    return bool(_VCF_LICENSE_RE.search(log_text))


def _vcf_compile_failed(log_text: str) -> bool:
    if _vcf_license_failed(log_text) or infrastructure_failure_in_log(log_text):
        return False  # the tool never got as far as reading the sources
    # Errors are printed either at the start of a line or behind a ``***`` /
    # ``**`` banner, so both forms have to be recognised.
    if re.search(r'^\s*(?:\*+\s*)?Error', log_text, re.MULTILINE | re.IGNORECASE):
        return True
    return bool(re.search(r'[Ee]laboration\s+failed', log_text))


# ---------------------------------------------------------------------------
# JasperGold
# ---------------------------------------------------------------------------

# [N]  <module>.<bind_inst>.<assertion>   <status>
_JG_RESULT_RE = re.compile(
    r'^\[\d+\]\s+(?P<path>[A-Za-z0-9_.]+[A-Za-z0-9_])\s+(?P<status>\S+)',
    re.MULTILINE,
)

_JG_VERSION_RE = re.compile(r'JasperGold\s+(?:Version\s+)?(\d[\w.\-]*)', re.IGNORECASE)


def parse_jaspergold_log(log_text: str, module_name: str = '') -> FormalRunResult:
    """Parse a JasperGold log into qualified property records.

    JasperGold reports vacuity through a separate ``check_assert -vacuity``
    style summary rather than inline, so a property is marked
    ``NOT_CHECKED`` unless a vacuity report is present in the same log.
    """
    result = FormalRunResult()
    result.summary.tool = 'jaspergold'

    version_match = _JG_VERSION_RE.search(log_text)
    if version_match:
        result.summary.tool_version = version_match.group(1)

    bind_prefix = f'{module_name}.u_{module_name}_sva.' if module_name else ''

    for match in _JG_RESULT_RE.finditer(log_text):
        path = match.group('path')
        status = match.group('status').lower()

        name = path[len(bind_prefix):] if bind_prefix and path.startswith(bind_prefix) else path
        name = name.rsplit('.', 1)[-1]

        record = result.records.get(name) or PropertyRecord(name=name, full_path=path)

        if 'cex' in status:
            record.proof_status = ProofStatus.FALSIFIED
        elif 'proven' in status:
            record.proof_status = ProofStatus.PROVEN
        elif 'bounded' in status:
            record.proof_status = ProofStatus.BOUNDED_PROVEN
        else:
            record.proof_status = ProofStatus.INCONCLUSIVE

        result.records[name] = record

    _parse_jg_vacuity(log_text, result, bind_prefix)

    counts = {}
    for record in result.records.values():
        counts[record.proof_status] = counts.get(record.proof_status, 0) + 1
    result.summary.assertions_found = len(result.records)
    result.summary.assertions_proven = counts.get(ProofStatus.PROVEN, 0)
    result.summary.assertions_falsified = counts.get(ProofStatus.FALSIFIED, 0)
    result.summary.assertions_inconclusive = counts.get(ProofStatus.INCONCLUSIVE, 0)
    result.summary.vacuity_non_vacuous = sum(
        1 for r in result.records.values() if r.vacuity_status is VacuityStatus.NON_VACUOUS
    )
    result.summary.vacuity_vacuous = sum(
        1 for r in result.records.values() if r.vacuity_status is VacuityStatus.VACUOUS
    )
    result.summary.vacuity_found = (
        result.summary.vacuity_non_vacuous + result.summary.vacuity_vacuous
    )

    if not result.records:
        result.summary.no_per_property_results = True

    return result


def _parse_jg_vacuity(log_text: str, result: FormalRunResult, bind_prefix: str) -> None:
    """Attach JasperGold vacuity verdicts when a vacuity report is present."""
    for match in re.finditer(
        r'^\s*(?P<path>[A-Za-z0-9_.]+)\s+.*?\b(?P<verdict>vacuous|non[-_]?vacuous)\b',
        log_text,
        re.MULTILINE | re.IGNORECASE,
    ):
        path = match.group('path')
        name = path[len(bind_prefix):] if bind_prefix and path.startswith(bind_prefix) else path
        name = name.rsplit('.', 1)[-1]
        record = result.records.get(name)
        if record is None:
            continue
        verdict = match.group('verdict').lower().replace('-', '_')
        record.vacuity_status = (
            VacuityStatus.NON_VACUOUS if verdict.startswith('non') else VacuityStatus.VACUOUS
        )


def parse_formal_log(log_text: str, tool: str, module_name: str = '') -> FormalRunResult:
    """Dispatch log parsing on the configured formal tool."""
    if tool == 'vcformal':
        return parse_vcformal_log(log_text)
    if tool == 'jaspergold':
        return parse_jaspergold_log(log_text, module_name)
    raise ValueError(f'Unsupported formal tool: {tool}')


def format_qualification_table(result: FormalRunResult) -> str:
    """Render a per-property qualification table for the run log."""
    if not result.records:
        return 'No property results available.'

    name_width = max(len(name) for name in result.records) + 2
    header = (
        f'{"PROPERTY".ljust(name_width)}{"QUALIFICATION".ljust(30)}'
        f'{"PROOF".ljust(16)}{"VACUITY".ljust(14)}{"COI":>7}{"ASSUMES":>9}'
    )
    lines = [header, '-' * len(header)]
    for name, record in sorted(result.records.items()):
        lines.append(
            f'{name.ljust(name_width)}'
            f'{record.qualification.value.ljust(30)}'
            f'{record.proof_status.value.ljust(16)}'
            f'{record.vacuity_status.value.ljust(14)}'
            f'{record.coi_size:>7}'
            f'{len(record.assumption_dependence):>9}'
        )

    lines.append('')
    lines.append('Qualification totals:')
    for key, value in result.counts_by_qualification().items():
        lines.append(f'  {key:<34} {value}')
    lines.append('Vacuity audit:')
    for key, value in result.vacuity_audit().items():
        lines.append(f'  {key:<34} {value}')
    lines.append('Assumption dependence:')
    for key, value in result.assumption_dependence_audit().items():
        if key == 'constraints_used':
            continue
        lines.append(f'  {key:<34} {value}')
    if result.summary.constraint_conflict_status:
        lines.append(
            f'  {"constraint_conflict_status":<34} '
            f'{result.summary.constraint_conflict_status}'
        )
    return '\n'.join(lines)


def dump_qualification_json(result: FormalRunResult, path: str) -> None:
    with open(path, 'w') as handle:
        json.dump(result.to_dict(), handle, indent=2, sort_keys=True)
