"""Adaptive stopping policy for counterexample-guided semantic repair.

Semantic repair dominated the runtime of the first SVApshot study — 77% of total
time — while most successful corrections happened on the first attempt.  A fixed
budget of five attempts per property therefore spends most of its effort on
repairs that were never going to succeed.

This policy replaces the fixed budget with a decision made per attempt.  It
tracks how often repair succeeded at each attempt index *within the current run*
and stops when the estimated probability of success on the next attempt no
longer justifies its cost.  The estimate is a Beta-Bernoulli posterior mean,
which starts at an optimistic prior (so the first attempts are always taken) and
tightens as evidence accumulates:

    P(success at attempt k) ≈ (successes_k + α) / (trials_k + α + β)

Three further conditions stop a repair early regardless of the estimate, because
each is direct evidence that more attempts cannot help:

* the model returned an assertion equivalent to one already tried,
* the tool returned the same counterexample as the previous attempt,
* the run exhausted its global attempt, time, or cost budget.

Exposing the resulting quality–cost frontier is the point: ``StoppingPolicy``
records why every repair stopped, so the trade-off can be reported rather than
assumed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional


class StopReason(str, Enum):
    REPAIRED = 'repaired'
    MAX_ATTEMPTS = 'max_attempts'
    LOW_EXPECTED_VALUE = 'low_expected_value'
    NO_PROGRESS = 'no_progress'
    REPEATED_COUNTEREXAMPLE = 'repeated_counterexample'
    EMPTY_RESPONSE = 'empty_response'
    GLOBAL_BUDGET_EXHAUSTED = 'global_budget_exhausted'


@dataclass
class AdaptiveStopConfig:
    """Bounds and thresholds for the adaptive policy."""

    #: Hard ceiling retained as a safety net; the policy usually stops earlier.
    max_attempts_per_property: int = 5
    #: Attempts always taken before the estimator is trusted.
    min_attempts_per_property: int = 1
    #: Stop when the estimated success probability falls below this.
    success_probability_floor: float = 0.08
    #: Beta prior. A mean of α/(α+β) = 0.5 keeps early attempts optimistic.
    prior_alpha: float = 1.0
    prior_beta: float = 1.0
    #: Whole-run budgets. ``0`` disables a budget.
    max_total_attempts: int = 0
    max_wall_seconds: float = 0.0
    max_cost: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RepairOutcome:
    """What happened to one property's repair, and what it cost."""

    property_name: str
    attempts: int = 0
    repaired: bool = False
    stop_reason: StopReason = StopReason.MAX_ATTEMPTS
    seconds: float = 0.0
    cost: float = 0.0
    attempt_texts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data['stop_reason'] = self.stop_reason.value
        return data


def normalise_assertion(text: str) -> str:
    """Canonical form used to detect an LLM returning the same fix twice."""
    body = re.sub(r'//[^\n]*', ' ', text)
    body = re.sub(r'\$display\s*\([^)]*\)', ' ', body)
    return re.sub(r'\s+', '', body)


def normalise_counterexample(text: str) -> str:
    """Canonical form of a counterexample, ignoring timestamps and run ids."""
    body = re.sub(r'\d+:\d+:\d+', ' ', text)
    body = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', ' ', body)
    return re.sub(r'\s+', ' ', body).strip()


class StoppingPolicy:
    """Decide, per attempt, whether another repair attempt is worth making."""

    def __init__(self, config: Optional[AdaptiveStopConfig] = None):
        self.config = config or AdaptiveStopConfig()
        #: attempt index (1-based) -> [successes, trials]
        self._history: Dict[int, List[int]] = {}
        self.outcomes: List[RepairOutcome] = []
        self.total_attempts = 0
        self.total_cost = 0.0
        self._run_start = time.time()

    # -- estimation --------------------------------------------------------

    def success_probability(self, attempt_index: int) -> float:
        """Posterior mean probability that attempt ``attempt_index`` succeeds."""
        successes, trials = self._history.get(attempt_index, (0, 0))
        alpha = self.config.prior_alpha
        beta = self.config.prior_beta
        return (successes + alpha) / (trials + alpha + beta)

    def record_attempt(self, attempt_index: int, succeeded: bool, cost: float = 0.0) -> None:
        successes, trials = self._history.get(attempt_index, (0, 0))
        self._history[attempt_index] = [successes + int(succeeded), trials + 1]
        self.total_attempts += 1
        self.total_cost += cost

    # -- budgets -----------------------------------------------------------

    def elapsed_seconds(self) -> float:
        return time.time() - self._run_start

    def global_budget_exhausted(self) -> Optional[str]:
        config = self.config
        if config.max_total_attempts and self.total_attempts >= config.max_total_attempts:
            return f'attempt budget reached ({config.max_total_attempts})'
        if config.max_wall_seconds and self.elapsed_seconds() >= config.max_wall_seconds:
            return f'time budget reached ({config.max_wall_seconds:.0f}s)'
        if config.max_cost and self.total_cost >= config.max_cost:
            return f'cost budget reached ({config.max_cost:.2f})'
        return None

    # -- decision ----------------------------------------------------------

    def should_attempt(
        self,
        attempt_index: int,
        previous_attempts: List[str],
        latest_attempt: Optional[str] = None,
        previous_counterexample: Optional[str] = None,
        latest_counterexample: Optional[str] = None,
    ) -> tuple:
        """Decide whether to make attempt ``attempt_index`` (1-based).

        Returns ``(proceed, reason)`` where ``reason`` is a :class:`StopReason`
        when ``proceed`` is False and an explanatory string otherwise.
        """
        config = self.config

        exhausted = self.global_budget_exhausted()
        if exhausted:
            return False, StopReason.GLOBAL_BUDGET_EXHAUSTED

        if attempt_index > config.max_attempts_per_property:
            return False, StopReason.MAX_ATTEMPTS

        if attempt_index <= config.min_attempts_per_property:
            return True, 'minimum attempts not yet reached'

        # The model repeating a fix it already tried means the prompt carries no
        # new information; further attempts sample the same distribution.
        if latest_attempt is not None:
            latest = normalise_assertion(latest_attempt)
            if any(normalise_assertion(previous) == latest for previous in previous_attempts[:-1]):
                return False, StopReason.NO_PROGRESS

        # An identical counterexample means the last edit changed nothing the
        # tool could observe.
        if previous_counterexample and latest_counterexample:
            if normalise_counterexample(previous_counterexample) == \
                    normalise_counterexample(latest_counterexample):
                return False, StopReason.REPEATED_COUNTEREXAMPLE

        probability = self.success_probability(attempt_index)
        if probability < config.success_probability_floor:
            return False, StopReason.LOW_EXPECTED_VALUE

        return True, f'estimated success probability {probability:.2f}'

    def finish_property(self, outcome: RepairOutcome) -> None:
        self.outcomes.append(outcome)

    # -- reporting ---------------------------------------------------------

    def attempt_profile(self) -> Dict[int, Dict[str, float]]:
        """Per-attempt-index success statistics observed during the run."""
        return {
            index: {
                'successes': successes,
                'trials': trials,
                'observed_rate': successes / trials if trials else 0.0,
                'posterior_mean': self.success_probability(index),
            }
            for index, (successes, trials) in sorted(self._history.items())
        }

    def to_dict(self) -> dict:
        repaired = [o for o in self.outcomes if o.repaired]
        attempts_saved = sum(
            max(0, self.config.max_attempts_per_property - o.attempts)
            for o in self.outcomes
        )
        return {
            'config': self.config.to_dict(),
            'properties_processed': len(self.outcomes),
            'properties_repaired': len(repaired),
            'total_attempts': self.total_attempts,
            'attempts_saved_vs_fixed_budget': attempts_saved,
            'mean_attempts_per_property': (
                self.total_attempts / len(self.outcomes) if self.outcomes else 0.0
            ),
            'mean_attempts_to_success': (
                sum(o.attempts for o in repaired) / len(repaired) if repaired else 0.0
            ),
            'total_repair_cost': round(self.total_cost, 6),
            'total_repair_seconds': round(sum(o.seconds for o in self.outcomes), 2),
            'attempt_profile': self.attempt_profile(),
            'stop_reasons': self.stop_reason_counts(),
            'outcomes': [o.to_dict() for o in self.outcomes],
        }

    def stop_reason_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.stop_reason.value] = counts.get(outcome.stop_reason.value, 0) + 1
        return counts

    def format_summary(self) -> str:
        repaired = [o for o in self.outcomes if o.repaired]
        fixed_budget_attempts = len(self.outcomes) * self.config.max_attempts_per_property
        saved = fixed_budget_attempts - self.total_attempts

        lines = [
            'Adaptive semantic-repair stopping',
            '=' * 72,
            f'  properties processed      {len(self.outcomes)}',
            f'  properties repaired       {len(repaired)}',
            f'  attempts made             {self.total_attempts}',
            f'  attempts under fixed {self.config.max_attempts_per_property}x   '
            f'{fixed_budget_attempts}',
            f'  attempts avoided          {saved} '
            f'({saved / fixed_budget_attempts:.0%})' if fixed_budget_attempts else '',
            f'  repair cost               {self.total_cost:.4f}',
            '',
            '  Success probability by attempt index:',
            f'    {"ATTEMPT":<9}{"TRIALS":>8}{"SUCCESS":>9}{"RATE":>8}',
        ]
        for index, stats in self.attempt_profile().items():
            lines.append(
                f'    {index:<9}{int(stats["trials"]):>8}'
                f'{int(stats["successes"]):>9}{stats["observed_rate"]:>7.0%}'
            )

        lines.append('')
        lines.append('  Why repairs stopped:')
        for reason, count in sorted(self.stop_reason_counts().items()):
            lines.append(f'    {reason:<28}{count}')
        return '\n'.join(line for line in lines if line != '')
