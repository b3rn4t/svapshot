"""Prompt context for counterexample-aware semantic repair.

The formal tool already knows why a property failed (vacuous vs falsified,
bound depth, engine, formal core, COI). Repair used to paste only the SVA
text and the full RTL. This module turns those records — plus an optional
cycle table parsed from a dumped VCD — into the block the repair prompt
needs, without requiring VC Formal in the unit tests.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from proof_status import ProofStatus, PropertyRecord, VacuityStatus


_SV_KEYWORDS = frozenset({
    'logic', 'wire', 'reg', 'bit', 'byte', 'int', 'input', 'output',
    'parameter', 'localparam', 'assign', 'always', 'always_ff', 'always_comb',
    'if', 'else', 'begin', 'end', 'module', 'endmodule', 'assert', 'assume',
    'cover', 'property', 'disable', 'iff', 'posedge', 'negedge', 'or', 'and',
    'not', 'throughout', 'implies', '##',
})

_IDENT_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')
_CLOCK_RESET_NAMES = frozenset({
    'clk', 'clk_i', 'clk_in', 'clock', 'rst', 'rst_n', 'rstn', 'rst_ni',
    'rstn_i', 'reset', 'reset_n', 'reset_ni',
})
_VCD_VAR_RE = re.compile(
    r'\$var\s+\w+\s+\d+\s+(\S+)\s+(\S+)(?:\s+\[[^\]]+\])?\s+\$end')
_VCD_TIME_RE = re.compile(r'^#(\d+)\s*$')
_VCD_SCALAR_RE = re.compile(r'^([01xzXZ])(.+)$')
_VCD_VECTOR_RE = re.compile(r'^[bB]([01xzXZ]+)\s+(\S+)$')


@dataclass
class CycleTable:
    """Compact waveform: one value per signal per cycle."""

    cycles: List[Dict[str, str]] = field(default_factory=list)
    fail_cycle: Optional[int] = None
    fail_signal: str = ''
    source: str = ''

    def is_empty(self) -> bool:
        return not self.cycles


def describe_depth(bound_depth: Optional[int]) -> str:
    """Bound depth in words a model can act on."""
    if bound_depth is None:
        return 'the tool did not report a bound'
    if bound_depth <= 0:
        return 'combinational (same-cycle CEX, depth 0)'
    if bound_depth == 1:
        return 'a 1-cycle path (consequent fails on the next cycle)'
    return f'a {bound_depth}-cycle path (falsified at depth {bound_depth})'


def describe_failure(record: Optional[PropertyRecord]) -> str:
    """Vacuous vs falsified vs inconclusive, plus depth and engine."""
    if record is None:
        return (
            'Failure kind: unknown (no per-property record). '
            'Treat this as a possible property mismatch.'
        )
    engine = record.engine or 'unspecified engine'
    depth = describe_depth(record.bound_depth)
    if record.vacuity_status is VacuityStatus.VACUOUS:
        return (
            f'Failure kind: VACUOUS (antecedent never fired). '
            f'Do NOT invent a waveform. Weaken or change the trigger so a '
            f'real scenario can satisfy the antecedent. Engine {engine}; {depth}.'
        )
    missing = ''
    if record.missing_assumptions:
        shown = ', '.join(record.missing_assumptions[:4])
        missing = f' Screened missing-assumption candidates: {shown}.'
    if record.proof_status is ProofStatus.FALSIFIED:
        return (
            f'Failure kind: FALSIFIED (antecedent fired; consequent died). '
            f'{depth.capitalize()}. Engine {engine}.{missing}'
        )
    return (
        f'Failure kind: {record.proof_status.value} '
        f'(vacuity {record.vacuity_status.value}). Engine {engine}; {depth}.{missing}'
    )


def solver_progress(
    previous_signature: Optional[str],
    latest_signature: Optional[str],
) -> str:
    """What the last edit changed in the solver, if anything."""
    if not previous_signature or not latest_signature:
        return ''
    if previous_signature == latest_signature:
        return (
            'Solver progress: the last edit did not change the solver\'s view '
            f'({latest_signature}). Try a different kind of edit — delay, '
            'polarity, or split the property. Do not restate the last attempt.'
        )
    return (
        f'Solver progress: the last edit moved the solver from '
        f'{previous_signature} to {latest_signature}.'
    )


def signals_from_property(property_text: str) -> List[str]:
    """Unqualified identifiers in an SVA expression, minus keywords."""
    names = []
    seen = set()
    for match in _IDENT_RE.finditer(property_text or ''):
        name = match.group(1)
        if name in _SV_KEYWORDS or name in seen:
            continue
        if name.startswith('a_') and name[2:3].isdigit():
            continue
        seen.add(name)
        names.append(name)
    return names


def core_and_coi_signals(record: Optional[PropertyRecord]) -> List[str]:
    """Formal-core inputs/registers plus tool COI names, de-duplicated."""
    if record is None:
        return []
    names: List[str] = []
    seen = set()
    for raw in (
        list(record.formal_core_input_names)
        + list(record.formal_core_register_names)
        + list(record.coi_objects)
    ):
        leaf = raw.split('.')[-1] if raw else ''
        if not leaf or leaf in seen:
            continue
        seen.add(leaf)
        names.append(leaf)
    return names


def format_assumptions(assumptions: Sequence[object]) -> str:
    """Active environment assumptions already in force."""
    if not assumptions:
        return 'Active assumptions: none.'
    lines = ['Active assumptions (already in force; do not contradict):']
    for assumption in assumptions:
        name = getattr(assumption, 'name', '') or 'assume'
        expr = getattr(assumption, 'expression', '') or str(assumption)
        lines.append(f'  {name}: {expr}')
    return '\n'.join(lines)


def format_siblings(proven: Sequence[str], *, limit: int = 8) -> str:
    """Sibling properties that already proved — the DUT's real polarities."""
    cleaned = [text.strip() for text in proven if text and text.strip()]
    if not cleaned:
        return 'Sibling proofs: none yet.'
    lines = [
        'Sibling properties that already proved '
        '(do not contradict their polarities or delays):',
    ]
    for text in cleaned[:limit]:
        one = ' '.join(text.split())
        if len(one) > 220:
            one = one[:217] + '...'
        lines.append(f'  {one}')
    extra = len(cleaned) - limit
    if extra > 0:
        lines.append(f'  … and {extra} more proven properties')
    return '\n'.join(lines)


def format_core_section(record: Optional[PropertyRecord]) -> str:
    """Formal core / COI list the model should prefer over the full RTL."""
    names = core_and_coi_signals(record)
    if not names:
        return (
            'Formal core / COI: not reported for this run. '
            'Still prefer signals named in the property over unrelated FSMs.'
        )
    shown = ', '.join(names[:24])
    extra = f' (+{len(names) - 24} more)' if len(names) > 24 else ''
    return (
        'Formal core / COI (prefer these over the rest of the RTL): '
        f'{shown}{extra}'
    )


def format_cycle_table(table: Optional[CycleTable], *, max_signals: int = 12) -> str:
    """ASCII time diagram. Empty or missing tables stay silent."""
    if table is None or table.is_empty():
        return ''
    signals: List[str] = []
    for row in table.cycles:
        for name in row:
            if name not in signals:
                signals.append(name)
    signals = signals[:max_signals]
    if not signals:
        return ''

    width = max(3, max(len(s) for s in signals), len('t'))
    header = f'{"t".ljust(width)} | ' + ' | '.join(s.ljust(width) for s in signals)
    rule = '-' * len(header)
    lines = [
        'CEX time diagram (one column per formal-core / property signal):',
        header,
        rule,
    ]
    for index, row in enumerate(table.cycles):
        mark = '  ← fail' if table.fail_cycle is not None and index == table.fail_cycle else ''
        cells = [str(row.get(s, 'x')).ljust(width) for s in signals]
        lines.append(f'{str(index).ljust(width)} | ' + ' | '.join(cells) + mark)
    if table.fail_signal:
        lines.append(
            f'Consequent failed on {table.fail_signal} '
            f'at t={table.fail_cycle if table.fail_cycle is not None else "?"}.'
        )
    if table.source:
        lines.append(f'(source: {table.source})')
    return '\n'.join(lines)


def parse_vcd_table(
    vcd_text: str,
    wanted: Sequence[str],
    *,
    fail_cycle: Optional[int] = None,
    fail_signal: str = '',
    source: str = 'vcd',
) -> CycleTable:
    """Parse a minimal VCD into a cycle table for ``wanted`` leaf names."""
    id_to_name: Dict[str, str] = {}
    wanted_set = set(wanted)
    for match in _VCD_VAR_RE.finditer(vcd_text):
        ident, raw = match.group(1), match.group(2)
        leaf = re.sub(r'\[.*', '', raw.split('.')[-1])
        if wanted_set and leaf not in wanted_set:
            continue
        id_to_name[ident] = leaf

    values: Dict[str, str] = {name: 'x' for name in id_to_name.values()}
    cycles: List[Dict[str, str]] = []
    seen_time = False
    in_dump = False
    for raw_line in vcd_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('$date') or line.startswith('$version'):
            continue
        if line.startswith('$dumpvars'):
            in_dump = True
            continue
        if line.startswith('$end') and in_dump:
            in_dump = False
            continue
        time_match = _VCD_TIME_RE.match(line)
        if time_match:
            if seen_time:
                cycles.append(dict(values))
            seen_time = True
            continue
        scalar = _VCD_SCALAR_RE.match(line)
        if scalar and scalar.group(2) in id_to_name:
            values[id_to_name[scalar.group(2)]] = scalar.group(1).lower()
            continue
        vector = _VCD_VECTOR_RE.match(line)
        if vector and vector.group(2) in id_to_name:
            values[id_to_name[vector.group(2)]] = vector.group(1).lower()

    if seen_time or any(value != 'x' for value in values.values()):
        cycles.append(dict(values))

    if fail_cycle is None and cycles:
        fail_cycle = len(cycles) - 1
    return CycleTable(
        cycles=cycles,
        fail_cycle=fail_cycle,
        fail_signal=fail_signal,
        source=source,
    )


def parse_text_table(text: str) -> CycleTable:
    """Parse the compact ``t sig1 sig2`` text dump tests and Tcl can write."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith('#')]
    if not lines:
        return CycleTable()
    header = lines[0].split()
    if not header or header[0] != 't':
        return CycleTable()
    signals = header[1:]
    cycles: List[Dict[str, str]] = []
    fail_cycle = None
    fail_signal = ''
    for line in lines[1:]:
        marked = '←' in line or '<--' in line or 'fail' in line.lower()
        cells = re.sub(r'←|<--|fail.*', ' ', line, flags=re.IGNORECASE).split()
        if len(cells) < 2:
            continue
        row = {signals[i]: cells[i + 1] for i in range(min(len(signals), len(cells) - 1))}
        if marked:
            fail_cycle = len(cycles)
        cycles.append(row)
    meta = re.search(r'fail_signal=(\S+)', text)
    if meta:
        fail_signal = meta.group(1)
    return CycleTable(
        cycles=cycles,
        fail_cycle=fail_cycle if fail_cycle is not None else (len(cycles) - 1 if cycles else None),
        fail_signal=fail_signal,
        source='text',
    )


def load_cycle_table(
    report_dir: str,
    property_name: str,
    wanted: Sequence[str],
    *,
    fail_cycle: Optional[int] = None,
    fail_signal: str = '',
) -> Optional[CycleTable]:
    """Load ``reports/cex/<property>.vcd`` or ``.txt`` when present."""
    safe = re.sub(r'[^A-Za-z0-9_]', '_', property_name or 'prop')
    cex_dir = os.path.join(report_dir, 'cex')
    txt = os.path.join(cex_dir, safe + '.txt')
    vcd = os.path.join(cex_dir, safe + '.vcd')
    if os.path.isfile(txt):
        with open(txt, encoding='utf-8', errors='replace') as handle:
            return parse_text_table(handle.read())
    if os.path.isfile(vcd):
        with open(vcd, encoding='utf-8', errors='replace') as handle:
            return parse_vcd_table(
                handle.read(), wanted,
                fail_cycle=fail_cycle, fail_signal=fail_signal,
                source=os.path.basename(vcd),
            )
    return None


def skeleton_cycle_table(
    signals: Sequence[str],
    depth: Optional[int],
    *,
    fail_signal: str = '',
) -> Optional[CycleTable]:
    """Signal × cycle grid when the tool dumped no waveform values."""
    names = [name for name in signals if name][:12]
    if not names:
        return None
    rows = 1 if depth is None else max(1, int(depth) + 1)
    return CycleTable(
        cycles=[{name: '?' for name in names} for _ in range(rows)],
        fail_cycle=rows - 1,
        fail_signal=fail_signal,
        source='depth-only (no waveform dump)',
    )


def rtl_focus(rtl_text: str, signals: Sequence[str], *, max_lines: int = 60) -> str:
    """RTL lines that mention formal-core / property signals.

    Full-module dumps drown the CEX. When no signal hits, the original RTL is
    returned unchanged so a small leaf still sees its declarations.
    """
    if not rtl_text or not signals:
        return rtl_text
    needles = [s for s in signals if s and s not in _CLOCK_RESET_NAMES]
    if not needles:
        return rtl_text
    pattern = re.compile(r'\b(?:' + '|'.join(re.escape(s) for s in needles) + r')\b')
    source_lines = rtl_text.splitlines()
    keep = set()
    for index, line in enumerate(source_lines):
        if pattern.search(line):
            for neighbour in range(max(0, index - 1), min(len(source_lines), index + 2)):
                keep.add(neighbour)
    if not keep:
        return rtl_text
    chosen = sorted(keep)[:max_lines]
    return '\n'.join(source_lines[i] for i in chosen)


def repair_instructions(
    record: Optional[PropertyRecord],
    include_cex: bool = True,
) -> str:
    """Kind-specific orders so vacuity and a real CEX are not treated alike."""
    if record is not None and record.vacuity_status is VacuityStatus.VACUOUS:
        return (
            'This proof is vacuous: change the antecedent so it can fire under '
            'the assumptions and parent instantiation context. Do not tighten '
            'the consequent.'
        )
    if include_cex:
        return (
            'This is a real counterexample: the antecedent held and the '
            'consequent failed. Repair the delay, polarity, or split the '
            'property so the CEX time diagram is no longer a violation. '
            'Prefer formal-core signals.'
        )
    return (
        'The antecedent held and the consequent failed. Repair the delay, '
        'polarity, or split the property using the formal-core RTL. Prefer '
        'formal-core signals. No CEX traces are provided.'
    )


def build_repair_context_block(
    *,
    record: Optional[PropertyRecord] = None,
    assumptions: Sequence[object] = (),
    proven_assertions: Sequence[str] = (),
    previous_signature: Optional[str] = None,
    latest_signature: Optional[str] = None,
    cycle_table: Optional[CycleTable] = None,
    property_text: str = '',
    include_cex: bool = True,
) -> str:
    """The SEMANTIC REPAIR CONTEXT block inserted ahead of the RTL."""
    parts = [
        'SEMANTIC REPAIR CONTEXT',
        describe_failure(record),
        format_assumptions(assumptions),
        format_core_section(record),
        format_siblings(proven_assertions),
    ]
    progress = solver_progress(previous_signature, latest_signature)
    if progress:
        parts.append(progress)
    if include_cex:
        diagram = format_cycle_table(cycle_table)
        if diagram:
            parts.append(diagram)
        elif record is not None and record.vacuity_status is VacuityStatus.VACUOUS:
            parts.append('CEX time diagram: none (vacuous — no witness).')
        elif record is not None and record.proof_status is ProofStatus.FALSIFIED:
            wanted = signals_from_property(property_text) + core_and_coi_signals(record)
            hint = ', '.join(wanted[:12]) if wanted else 'the property signals'
            parts.append(
                f'CEX time diagram: not dumped. The witness is '
                f'{describe_depth(record.bound_depth)}; inspect {hint}.'
            )
    else:
        parts.append('CEX time diagram: omitted (no_cex_in_prompt).')
    parts.append(repair_instructions(record, include_cex=include_cex))
    return '\n'.join(parts) + '\n'


def format_assumption_cex(
    records: Mapping[str, PropertyRecord],
    failing_names: Iterable[str],
    tables: Optional[Mapping[str, CycleTable]] = None,
) -> str:
    """Richer CEX text for assumption generation than status@depth alone."""
    lines: List[str] = []
    tables = tables or {}
    for name in failing_names:
        record = records.get(name)
        if record is None:
            lines.append(f'{name}: no formal record')
            continue
        lines.append(f'{name}: {describe_failure(record)}')
        diagram = format_cycle_table(tables.get(name))
        if diagram:
            lines.append(diagram)
    return '\n'.join(lines)
