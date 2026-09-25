"""Keep snapshot properties that still talk about the rewritten DUT.

After an RTL refactor, a frozen snapshot can name internals that were deleted
or renamed. Those properties will not elaborate and are not a useful
regression net for the change.

A property is retained when every identifier it uses is in the intersection
of the property's signal set and the rewritten DUT's signal set (ports,
internals, parameters). SVA built-ins are stripped first.

``mode='touched'`` further requires a non-empty intersection with the
baseline↔rewrite signal symmetric difference, so properties that only
mention untouched ports can be dropped.

This is a post-rewrite filter. It does not generate assertions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set

from coi import (
    assertion_name,
    build_signal_graph,
    extract_identifiers,
    property_expression,
)

#: Names that appear in SVA but are not DUT signals.
_SVA_BUILTINS = frozenset({
    'rose', 'fell', 'stable', 'changed', 'past', 'sampled',
    'onehot', 'onehot0', 'countones', 'isunknown',
    'implied', 'iff', 'throughout', 'until', 'until_with',
    's_until', 's_until_with', 'nexttime', 's_nexttime',
    'always', 's_always', 'eventually', 's_eventually',
    'and', 'or', 'not', 'intersect', 'first_match',
    'clk', 'clock', 'rst', 'rst_n', 'reset', 'reset_n',
})


@dataclass
class RetainDecision:
    name: str
    assertion: str
    assertion_signals: Set[str] = field(default_factory=set)
    intersection: Set[str] = field(default_factory=set)
    missing: Set[str] = field(default_factory=set)
    kept: bool = False
    reason: str = ''

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'kept': self.kept,
            'reason': self.reason,
            'assertion_signals': sorted(self.assertion_signals),
            'intersection': sorted(self.intersection),
            'missing': sorted(self.missing),
        }


def dut_signal_set(rtl_text: str, module_name: str = '') -> Set[str]:
    """Ports, internals, and parameters of one module."""
    graph = build_signal_graph(rtl_text, module_name)
    return set(graph.all_signals) | set(graph.parameters)


def property_signal_set(assertion_text: str) -> Set[str]:
    """Identifiers in a property, minus SVA built-ins."""
    return extract_identifiers(property_expression(assertion_text)) - _SVA_BUILTINS


def retain_after_refactor(
    assertions: Sequence[str],
    refactored_rtl: str,
    *,
    module_name: str = '',
    baseline_rtl: Optional[str] = None,
    mode: str = 'exists',
) -> List[RetainDecision]:
    """Filter ``assertions`` against the rewritten DUT signal set.

    ``mode``:

    - ``exists``: keep iff every property identifier is in the rewritten DUT
      (``A ⊆ R``, equivalently ``A == A ∩ R``).
    - ``touched``: also require ``A ∩ (R △ B)`` non-empty, where ``B`` is
      the baseline DUT signal set. ``baseline_rtl`` is required.
    """
    if mode not in ('exists', 'touched'):
        raise ValueError(f'unknown retain mode: {mode}')
    if mode == 'touched' and baseline_rtl is None:
        raise ValueError("mode='touched' needs baseline_rtl")

    rewritten = dut_signal_set(refactored_rtl, module_name)
    baseline: Set[str] = set()
    touched: Set[str] = set()
    if baseline_rtl is not None:
        baseline = dut_signal_set(baseline_rtl, module_name)
        touched = rewritten ^ baseline

    decisions: List[RetainDecision] = []
    for index, assertion in enumerate(assertions):
        name = assertion_name(assertion) or f'unnamed_{index}'
        raw_signals = property_signal_set(assertion)
        # Package enums / struct fields are identifiers but not DUT signals.
        # When a baseline is available, only names that were DUT signals then
        # can fail the subset check; otherwise keep the strict A ⊆ R rule.
        assertion_signals = (
            raw_signals & (baseline | rewritten) if baseline else raw_signals
        )
        intersection = assertion_signals & rewritten
        missing = assertion_signals - rewritten
        kept = not missing and bool(assertion_signals)
        reason = 'kept' if kept else (
            'no dut signals' if not assertion_signals
            else 'names missing from rewritten dut: ' + ', '.join(sorted(missing))
        )
        if kept and mode == 'touched' and not (assertion_signals & touched):
            kept = False
            reason = 'no intersection with rewritten-signal diff'
        decisions.append(RetainDecision(
            name=name,
            assertion=assertion,
            assertion_signals=assertion_signals,
            intersection=intersection,
            missing=missing,
            kept=kept,
            reason=reason,
        ))
    return decisions


def kept_assertions(decisions: Iterable[RetainDecision]) -> List[str]:
    return [row.assertion for row in decisions if row.kept]
