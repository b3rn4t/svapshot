"""LLM assumption generation with formal screening.

A counterexample on a semantically correct property usually means the input
space is underconstrained, not that the property is wrong.  The first SVApshot
prototype left that judgement to the engineer; this module automates it while
guarding against the failure mode that makes automatic assumptions dangerous —
an overrestrictive constraint that hides real defects.

Assumptions are produced as *separate, named artifacts* (an ``m_``-prefixed
``assume property`` in its own ``*_assume.svh`` file) so a snapshot always
states what environment it was proved under.  Each candidate must clear four
screens before it is accepted:

1. **Lint and compile** — SVALint-clean and elaborable, like any property.
2. **Consistency** — the constraint set must not be self-contradictory.  A
   contradictory environment proves everything, so a reachability cover
   (``c_env_reachable``) must remain coverable.
3. **Reachability preservation** — properties that were proved non-vacuously
   before must not become vacuous.  This is what catches a constraint that
   quietly deletes the behaviour under test.
4. **Mutation sensitivity preservation** — mutants the snapshot detected before
   the assumption must still be detected after it.  This is the direct test for
   "does this assumption hide defects?".

Only an assumption that unblocks its target property *and* passes all four is
accepted; the target is then qualified ``failing_missing_assumption`` rather
than ``failing_property_mismatch``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Iterable, List, Sequence, Tuple

from provenance import AssumptionRecord
from proof_status import FormalRunResult, Qualification, VacuityStatus

ASSUME_FILE_SUFFIX = '_assume.svh'
REACHABILITY_COVER_NAME = 'c_env_reachable'


class ScreenVerdict(str, Enum):
    PASS = 'pass'
    FAIL = 'fail'
    SKIPPED = 'skipped'
    ERROR = 'error'


@dataclass
class AssumptionCandidate:
    """A proposed environment constraint before screening."""

    name: str
    expression: str
    rationale: str = ''
    targets: List[str] = field(default_factory=list)

    def to_sv(self, indent: str = '') -> str:
        """Render as a lint-compliant named assume with a failure action block."""
        expression = self.expression.strip()
        if not (expression.startswith('(') and expression.endswith(')')):
            expression = f'({expression})'
        message = _escape(self.rationale.strip() or f'Assumption {self.name} violated')
        return (
            f'{indent}{self.name}: assume property ({expression})\n'
            f'{indent}else begin\n'
            f'{indent}    $display("{message}");\n'
            f'{indent}end'
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('"', '\\"')


def normalise_assumption_name(name: str) -> str:
    """Force the ``m_`` prefix that SVALint's ASSUME_NAMING rule requires."""
    cleaned = re.sub(r'[^A-Za-z0-9_]', '_', name.strip())
    for prefix in ('m_', 'as__', 'a_'):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    cleaned = cleaned.lstrip('_') or 'assumption'
    return f'm_{cleaned}'


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

ASSUMPTION_RULES = '''
You are an expert in SystemVerilog Assertions writing ENVIRONMENT ASSUMPTIONS
for formal property verification of RTL module MODULE.

A property of MODULE failed in the formal tool. Decide whether the
counterexample describes a legal input scenario or an illegal one. Only if the
counterexample requires an input behaviour that the real environment can never
produce should you propose an assumption.

Deliver assumptions as structured entries only. Do NOT write SystemVerilog
syntax. For each assumption output exactly this block, separated by --- lines:
---
id: <short_snake_case_name>
property: <SVA expression only>
rationale: <one sentence: which illegal input behaviour this rules out>
targets: <comma-separated names of the failing properties this should unblock>

Rules for the property field:
- Expression only: no assume property, no semicolon, no @(posedge clk).
- Constrain ONLY module inputs (signals ending in _i) and their relationships.
  Never constrain internal signals or outputs: that would assume the conclusion.
- Use |-> for same-cycle and |=> for next-cycle implications.
- Prefer the weakest constraint that removes the illegal scenario. An
  overrestrictive assumption hides real bugs and will be rejected.
- Do NOT constrain the clock or the reset.
- DO NOT use generate loops, within, or first_match.

Hard requirement: the assumption must describe a protocol or interface rule that
a real environment genuinely satisfies. If the counterexample is legal input
behaviour, do not stay silent — output exactly this block and no assumptions:

---
decision: none
rationale: <one sentence: why this counterexample is legal environment behaviour>
---
'''

ASSUMPTION_EXAMPLE = '''
---
id: req_stable_until_grant
property: req_i && !gnt_i |=> req_i && $stable(addr_i)
rationale: The upstream master holds request and address stable until granted
targets: as__addr_latched_on_grant
---
id: no_simultaneous_flush_and_request
property: !(flush_i && req_i)
rationale: The control unit never issues a flush in the same cycle as a request
targets: as__ocup_div_unit_shift_right_no_set
'''

ASSUMPTION_NONE_EXAMPLE = '''
---
decision: none
rationale: The counterexample drives legal handshake inputs; the property is wrong
---
'''

ASSUMPTION_RETRY_INSTRUCTION = (
    'Your previous reply was empty or not in the required format. '
    'This stage cannot continue without a visible answer. Output either '
    'assumption blocks in the --- id/property/rationale/targets --- format, '
    'or a single --- decision: none / rationale: ... --- block. '
    'Do not reply with an empty message.'
)


def build_assumption_prompt(
    module_name: str,
    rtl_text: str,
    failing_properties: Sequence[Tuple[str, str]],
    counterexample_text: str = '',
    existing_assumptions: Sequence[AssumptionCandidate] = (),
    rejected_feedback: str = '',
) -> str:
    """Assemble the assumption-generation prompt for one repair round."""
    rules = ASSUMPTION_RULES.replace('MODULE', module_name)
    failing_block = '\n\n'.join(
        f'Failing property {name}:\n{text}' for name, text in failing_properties
    )
    existing_block = (
        'Assumptions already in force (do not repeat or contradict these):\n'
        + '\n'.join(f'  {a.name}: {a.expression}' for a in existing_assumptions)
        if existing_assumptions else
        'No assumptions are currently in force.'
    )

    parts = [
        rules,
        f'Example output format:\n{ASSUMPTION_EXAMPLE}',
        f'Example when no assumption is justified:\n{ASSUMPTION_NONE_EXAMPLE}',
        existing_block,
        failing_block,
    ]
    if counterexample_text:
        parts.append(f'Counterexample evidence from the formal tool:\n{counterexample_text}')
    if rejected_feedback:
        parts.append(
            'Previously proposed assumptions were REJECTED by screening. '
            'Do not repeat them:\n' + rejected_feedback
        )
    parts.append(f'RTL of {module_name}:\n{rtl_text}')
    parts.append(
        'Output ONLY structured blocks: either assumption '
        '--- id/property/rationale/targets --- entries, or a single '
        '--- decision: none --- block. Never reply with an empty message.'
    )
    return '\n\n'.join(parts)


def assumption_decision_is_none(response: str) -> bool:
    """True when the model explicitly said no environment assumption is needed."""
    if not response or not str(response).strip():
        return False
    if parse_assumption_response(response):
        return False
    return bool(re.search(
        r'^\s*decision\s*:\s*none\b', str(response),
        flags=re.IGNORECASE | re.MULTILINE,
    ))


def assumption_reply_usable(response: str) -> bool:
    """True when the reply is either candidates or an explicit none decision."""
    if parse_assumption_response(response):
        return True
    return assumption_decision_is_none(response)


def parse_assumption_response(response: str) -> List[AssumptionCandidate]:
    """Parse structured assumption blocks out of an LLM response."""
    candidates: List[AssumptionCandidate] = []
    for block in re.split(r'^---\s*$', response, flags=re.MULTILINE):
        block = block.strip()
        if not block:
            continue

        fields: Dict[str, str] = {}
        for line in block.splitlines():
            match = re.match(
                r'^\s*(id|property|rationale|targets)\s*:\s*(.*)$', line, re.IGNORECASE
            )
            if match:
                fields[match.group(1).lower()] = match.group(2).strip()

        if 'id' not in fields or not fields.get('property'):
            continue

        targets = [t.strip() for t in fields.get('targets', '').split(',') if t.strip()]
        candidates.append(AssumptionCandidate(
            name=normalise_assumption_name(fields['id']),
            expression=fields['property'],
            rationale=fields.get('rationale', ''),
            targets=targets,
        ))
    return candidates


# ---------------------------------------------------------------------------
# Artifact rendering
# ---------------------------------------------------------------------------

_ASSUME_FILE_TEMPLATE = '''// SVApshot environment assumptions for {module}
// Generated {timestamp}
//
// These constraints are part of the snapshot contract: every proof recorded in
// the manifest holds under exactly this environment. Each assumption passed
// consistency, reachability-preservation and mutation-sensitivity screening.
//
// {accepted} accepted assumption(s).

`ifndef SVAPSHOT_ASSUME_{guard}
`define SVAPSHOT_ASSUME_{guard}

{body}

`endif
'''


def render_assumption_file(
    module_name: str,
    assumptions: Sequence[AssumptionCandidate],
    timestamp: str = '',
    include_reachability_cover: bool = True,
) -> str:
    """Render accepted assumptions as a standalone, includable artifact."""
    from datetime import datetime, timezone

    blocks = []
    for assumption in assumptions:
        if assumption.rationale:
            blocks.append(f'// {assumption.rationale}')
        blocks.append(assumption.to_sv())
        blocks.append('')

    if include_reachability_cover:
        blocks.append(
            '// Consistency witness: an inconsistent assumption set makes this\n'
            '// cover unreachable, which is how a contradictory environment is\n'
            '// detected instead of silently proving everything.'
        )
        blocks.append(f'{REACHABILITY_COVER_NAME}: cover property (1\'b1);')

    return _ASSUME_FILE_TEMPLATE.format(
        module=module_name,
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(timespec='seconds'),
        accepted=len(assumptions),
        guard=module_name.upper(),
        body='\n'.join(blocks).rstrip(),
    )


def inject_assumptions(property_file_text: str, assumption_block: str) -> str:
    """Insert an assumption block into a property file before ``endmodule``.

    Assumptions live inside the bound checker module so they constrain the same
    instance the properties observe.  Any block already present is replaced, so
    repeated injection cannot declare the same assumption twice.
    """
    property_file_text = strip_assumptions(property_file_text)
    marker = '\nendmodule'
    index = property_file_text.rfind(marker)
    if index == -1:
        return property_file_text + '\n' + assumption_block + '\n'
    return (
        property_file_text[:index]
        + '\n// ---- SVApshot environment assumptions ----\n'
        + assumption_block
        + '\n'
        + property_file_text[index:]
    )


def strip_assumptions(property_file_text: str) -> str:
    """Remove a previously injected assumption block."""
    return re.sub(
        r'\n// ---- SVApshot environment assumptions ----\n.*?(?=\nendmodule)',
        '',
        property_file_text,
        flags=re.DOTALL,
    )


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------


@dataclass
class ScreeningResult:
    """Verdicts from every screen applied to one assumption."""

    unblocks_target: ScreenVerdict = ScreenVerdict.SKIPPED
    consistency: ScreenVerdict = ScreenVerdict.SKIPPED
    reachability_preserved: ScreenVerdict = ScreenVerdict.SKIPPED
    mutation_sensitivity_preserved: ScreenVerdict = ScreenVerdict.SKIPPED
    lint_clean: ScreenVerdict = ScreenVerdict.SKIPPED
    #: Properties that became vacuous because of this assumption.
    newly_vacuous: List[str] = field(default_factory=list)
    #: Mutants that were detected before but escape with this assumption.
    masked_mutants: List[str] = field(default_factory=list)
    unblocked: List[str] = field(default_factory=list)
    detail: str = ''

    @property
    def accepted(self) -> bool:
        """An assumption is accepted only if it helps and breaks nothing."""
        blocking = (
            self.consistency,
            self.reachability_preserved,
            self.mutation_sensitivity_preserved,
            self.lint_clean,
        )
        if any(v is ScreenVerdict.FAIL or v is ScreenVerdict.ERROR for v in blocking):
            return False
        return self.unblocks_target is ScreenVerdict.PASS

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in (
            'unblocks_target', 'consistency', 'reachability_preserved',
            'mutation_sensitivity_preserved', 'lint_clean',
        ):
            data[key] = getattr(self, key).value
        data['accepted'] = self.accepted
        return data

    def rejection_reason(self) -> str:
        if self.accepted:
            return ''
        if self.lint_clean in (ScreenVerdict.FAIL, ScreenVerdict.ERROR):
            return 'assumption is not lint-clean or does not elaborate'
        if self.consistency in (ScreenVerdict.FAIL, ScreenVerdict.ERROR):
            return 'assumption set is inconsistent (environment became unreachable)'
        if self.reachability_preserved is ScreenVerdict.FAIL:
            return (
                'assumption made previously non-vacuous properties vacuous: '
                + ', '.join(self.newly_vacuous)
            )
        if self.mutation_sensitivity_preserved is ScreenVerdict.FAIL:
            return (
                'assumption masked previously detected mutants: '
                + ', '.join(self.masked_mutants)
            )
        if self.unblocks_target is not ScreenVerdict.PASS:
            return 'assumption did not turn any targeted counterexample into a proof'
        return 'rejected'


def screen_consistency(result: FormalRunResult) -> Tuple[ScreenVerdict, str]:
    """Check that the environment remains satisfiable.

    An inconsistent assumption set makes every property provable, which is the
    most dangerous outcome available to an automatic tool.  The reachability
    cover is the witness: if the tool reports it unreachable, or reports every
    property as vacuous, the environment is contradictory.
    """
    cover = result.records.get(REACHABILITY_COVER_NAME)
    if cover is not None:
        from proof_status import ProofStatus

        if cover.proof_status is ProofStatus.FALSIFIED:
            # For a cover, "falsified" means unreachable.
            return ScreenVerdict.FAIL, 'reachability cover is unreachable'
        if cover.proof_status is ProofStatus.PROVEN:
            return ScreenVerdict.PASS, 'reachability cover is covered'

    if result.summary.covers_found and result.summary.covers_unreachable:
        return ScreenVerdict.FAIL, (
            f'{result.summary.covers_unreachable} cover(s) became unreachable'
        )

    checked = [
        record for record in result.records.values()
        if record.vacuity_status is not VacuityStatus.NOT_CHECKED
    ]
    if checked and all(r.vacuity_status is VacuityStatus.VACUOUS for r in checked):
        return ScreenVerdict.FAIL, 'every vacuity-checked property became vacuous'

    if not result.records:
        return ScreenVerdict.ERROR, 'no property results produced under the assumption'

    return ScreenVerdict.PASS, 'no evidence of an inconsistent environment'


def screen_reachability_preservation(
    baseline: FormalRunResult,
    candidate: FormalRunResult,
) -> Tuple[ScreenVerdict, List[str]]:
    """Reject assumptions that turn established behaviour vacuous."""
    newly_vacuous = []
    for name, before in baseline.records.items():
        if before.qualification is not Qualification.PROVED_NON_VACUOUS:
            continue
        after = candidate.records.get(name)
        if after is None:
            continue
        if after.qualification is Qualification.PROVED_VACUOUS:
            newly_vacuous.append(name)

    verdict = ScreenVerdict.FAIL if newly_vacuous else ScreenVerdict.PASS
    return verdict, sorted(newly_vacuous)


def screen_mutation_sensitivity(
    detected_before: Iterable[str],
    detected_after: Iterable[str],
) -> Tuple[ScreenVerdict, List[str]]:
    """Reject assumptions that stop the snapshot detecting known defects.

    This is the concrete guard against overrestrictive constraints: an
    assumption that removes a mutant from the detected set has narrowed the
    verified input space far enough to hide a real fault.
    """
    before = set(detected_before)
    after = set(detected_after)
    masked = sorted(before - after)
    verdict = ScreenVerdict.FAIL if masked else ScreenVerdict.PASS
    return verdict, masked


def screen_unblocks_targets(
    candidate: AssumptionCandidate,
    baseline: FormalRunResult,
    with_assumption: FormalRunResult,
) -> Tuple[ScreenVerdict, List[str]]:
    """Check that at least one targeted counterexample became a real proof."""
    targets = candidate.targets or [
        name for name, record in baseline.records.items()
        if record.qualification is Qualification.FAILING_PROPERTY_MISMATCH
    ]

    unblocked = []
    for name in targets:
        before = baseline.records.get(name)
        after = with_assumption.records.get(name)
        if before is None or after is None:
            continue
        was_failing = before.qualification in (
            Qualification.FAILING_PROPERTY_MISMATCH,
            Qualification.FAILING_MISSING_ASSUMPTION,
        )
        if was_failing and after.qualification is Qualification.PROVED_NON_VACUOUS:
            unblocked.append(name)

    verdict = ScreenVerdict.PASS if unblocked else ScreenVerdict.FAIL
    return verdict, sorted(unblocked)


def to_records(
    candidates: Sequence[AssumptionCandidate],
    screening: Dict[str, ScreeningResult],
) -> List[AssumptionRecord]:
    """Convert screened candidates into manifest records."""
    records = []
    for candidate in candidates:
        result = screening.get(candidate.name, ScreeningResult())
        records.append(AssumptionRecord(
            name=candidate.name,
            expression=candidate.expression,
            rationale=candidate.rationale,
            source='llm',
            targets=candidate.targets,
            screening=result.to_dict(),
            accepted=result.accepted,
        ))
    return records


def format_screening_report(
    candidates: Sequence[AssumptionCandidate],
    screening: Dict[str, ScreeningResult],
) -> str:
    """Render a human-readable screening table."""
    if not candidates:
        return 'No assumption candidates were proposed.'

    lines = [
        'Assumption screening',
        '=' * 72,
    ]
    for candidate in candidates:
        result = screening.get(candidate.name, ScreeningResult())
        status = 'ACCEPTED' if result.accepted else 'REJECTED'
        lines.append(f'  {candidate.name}  [{status}]')
        lines.append(f'    expression   {candidate.expression}')
        if candidate.rationale:
            lines.append(f'    rationale    {candidate.rationale}')
        lines.append(f'    lint         {result.lint_clean.value}')
        lines.append(f'    consistency  {result.consistency.value}')
        lines.append(f'    reachability {result.reachability_preserved.value}')
        lines.append(f'    mutation     {result.mutation_sensitivity_preserved.value}')
        lines.append(f'    unblocks     {result.unblocks_target.value}'
                     + (f' ({", ".join(result.unblocked)})' if result.unblocked else ''))
        if not result.accepted:
            lines.append(f'    rejected because: {result.rejection_reason()}')
        lines.append('')

    accepted = sum(1 for c in candidates if screening.get(c.name, ScreeningResult()).accepted)
    lines.append(f'  {accepted}/{len(candidates)} candidate assumptions accepted')
    return '\n'.join(lines)


def dump_assumptions(
    candidates: Sequence[AssumptionCandidate],
    screening: Dict[str, ScreeningResult],
    path: str,
) -> None:
    payload = [
        {
            **candidate.to_dict(),
            'screening': screening.get(candidate.name, ScreeningResult()).to_dict(),
        }
        for candidate in candidates
    ]
    with open(path, 'w') as handle:
        json.dump(payload, handle, indent=2)
