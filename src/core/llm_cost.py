"""Token accounting and monetary cost tracking for SVApshot LLM calls.

Reporting a snapshot's cost requires real token counts, not the
``len(text) // 4`` approximation the first prototype used: that estimate drifts
badly on SystemVerilog, which tokenises far more densely than English.  This
module prefers, in order:

1. the ``usage`` block returned by the provider (exact),
2. a local ``tiktoken`` encoding (close),
3. the character heuristic (last resort, and flagged as such).

Prices are per one million tokens and are recorded in the snapshot manifest
alongside the figures they produced, so a cost quoted in a paper can be
recomputed later even after list prices change.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional


@dataclass(frozen=True)
class ModelPrice:
    """List price of a model in currency units per one million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    currency: str = 'EUR'


#: Provider list prices converted to EUR.  ``PRICE_SNAPSHOT_DATE`` is recorded in
#: every manifest so a reported cost stays reproducible when prices move.
PRICE_SNAPSHOT_DATE = '2026-08-04'
USD_TO_EUR = 0.92

_USD = lambda i, o: ModelPrice(i * USD_TO_EUR, o * USD_TO_EUR, 'EUR')  # noqa: E731

MODEL_PRICES: Dict[str, ModelPrice] = {
    'gpt-4.1-mini': _USD(0.40, 1.60),
    'gpt-4-turbo-preview': _USD(10.00, 30.00),
    'gpt-5.3-codex': _USD(1.25, 10.00),
    'gpt-5.6-terra': _USD(2.00, 12.00),
    'gpt-5.6-luna': _USD(2.00, 12.00),
    'gpt-5.6-sol': _USD(2.00, 12.00),
    'o3': _USD(2.00, 8.00),
    'o3-mini': _USD(1.10, 4.40),
    'o4-mini': _USD(1.10, 4.40),
    'o4-mini-2025-04-16': _USD(1.10, 4.40),
    'meta/llama-3.1-405b-instruct': _USD(3.00, 3.00),
    'meta/llama-3.3-70b-instruct': _USD(0.60, 0.60),
    'moonshotai/kimi-k2.5': _USD(0.60, 2.50),
    'deepseek-ai/deepseek-r1-0528': _USD(0.55, 2.19),
    'nvdev/deepseek-ai/deepseek-r1': _USD(0.55, 2.19),
    'nvdev/meta/llama-4-maverick-17b-128e-instruct': _USD(0.27, 0.85),
    'nvdev/nvidia/nemotron-4-340b-instruct-128k': _USD(1.20, 1.20),
    # Subscription-billed CLI agents: usage is metered, not charged per token.
    'gemini-2.5-pro': _USD(1.25, 10.00),
    'gemini-2.5-flash': _USD(0.30, 2.50),
    'copilot': ModelPrice(0.0, 0.0),
    'cursor': ModelPrice(0.0, 0.0),
    'cursor-sdk': ModelPrice(0.0, 0.0),
    'cursor-cli': ModelPrice(0.0, 0.0),
    'composer-2.5': ModelPrice(0.0, 0.0),
    # Cursor Grok 4.6 Fast (DATE common model). Standard is $2/$6.
    'grok-4.6': _USD(4.00, 12.00),
    'cursor:grok-4.6': _USD(4.00, 12.00),
}

DEFAULT_PRICE = ModelPrice(0.0, 0.0)


def price_for_model(model: str) -> ModelPrice:
    """Look up list prices, including ``cursor:<id>`` aliases."""
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    if model.startswith('cursor:'):
        return MODEL_PRICES.get(model.split(':', 1)[1], DEFAULT_PRICE)
    return DEFAULT_PRICE


class TokenCounter:
    """Count tokens for a model, degrading gracefully when offline."""

    def __init__(self, model: str):
        self.model = model
        self._encoding = None
        self.method = 'char_heuristic'
        try:
            import tiktoken

            try:
                self._encoding = tiktoken.encoding_for_model(model)
            except KeyError:
                self._encoding = tiktoken.get_encoding('cl100k_base')
            self.method = 'tiktoken'
        except Exception:
            self._encoding = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            try:
                return len(self._encoding.encode(text))
            except Exception:
                pass
        return max(1, len(text) // 4)


@dataclass
class StageCost:
    """Token and money totals for one pipeline stage."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CostLedger:
    """Running token, money, and latency ledger for one SVApshot run."""

    model: str = ''
    currency: str = 'EUR'
    price_snapshot_date: str = PRICE_SNAPSHOT_DATE
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0
    #: How the token counts were obtained: exact provider usage, tiktoken, or
    #: the character heuristic.  Reported so cost figures carry their accuracy.
    accounting_method: str = 'unknown'
    exact_usage_calls: int = 0
    estimated_usage_calls: int = 0
    stages: Dict[str, StageCost] = field(default_factory=dict)

    def __post_init__(self):
        price = price_for_model(self.model)
        self.input_price_per_mtok = price.input_per_mtok
        self.output_price_per_mtok = price.output_per_mtok
        self.currency = price.currency
        self._counter = TokenCounter(self.model)
        if self.accounting_method == 'unknown':
            self.accounting_method = self._counter.method

    def count_tokens(self, text: str) -> int:
        return self._counter.count(text)

    def price_of(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_price_per_mtok
            + output_tokens / 1_000_000 * self.output_price_per_mtok
        )

    def record(
        self,
        stage: str,
        input_tokens: int,
        output_tokens: int,
        seconds: float = 0.0,
        exact: bool = False,
    ) -> float:
        """Add one LLM call to the ledger and return its cost."""
        bucket = self.stages.setdefault(stage, StageCost())
        cost = self.price_of(input_tokens, output_tokens)

        bucket.calls += 1
        bucket.input_tokens += input_tokens
        bucket.output_tokens += output_tokens
        bucket.cost += cost
        bucket.seconds += seconds

        if exact:
            self.exact_usage_calls += 1
        else:
            self.estimated_usage_calls += 1
        return cost

    @property
    def accounting_used(self) -> str:
        """How the recorded tokens were actually obtained.

        ``accounting_method`` names the local fallback the ledger would use; it
        says nothing about whether the provider returned exact counts. Quoting it
        makes measured figures look estimated, so reports use this instead.
        """
        if self.exact_usage_calls and not self.estimated_usage_calls:
            return 'provider_usage (exact)'
        if self.exact_usage_calls:
            return (f'mixed: {self.exact_usage_calls} exact from provider, '
                    f'{self.estimated_usage_calls} by {self.accounting_method}')
        return self.accounting_method

    @property
    def total_calls(self) -> int:
        return sum(s.calls for s in self.stages.values())

    @property
    def total_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.stages.values())

    @property
    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.stages.values())

    @property
    def total_cost(self) -> float:
        return sum(s.cost for s in self.stages.values())

    @property
    def total_seconds(self) -> float:
        return sum(s.seconds for s in self.stages.values())

    def to_dict(self) -> dict:
        return {
            'model': self.model,
            'currency': self.currency,
            'price_snapshot_date': self.price_snapshot_date,
            'input_price_per_mtok': self.input_price_per_mtok,
            'output_price_per_mtok': self.output_price_per_mtok,
            'accounting_method': self.accounting_method,
            'accounting_used': self.accounting_used,
            'exact_usage_calls': self.exact_usage_calls,
            'estimated_usage_calls': self.estimated_usage_calls,
            'total_calls': self.total_calls,
            'total_input_tokens': self.total_input_tokens,
            'total_output_tokens': self.total_output_tokens,
            'total_cost': round(self.total_cost, 6),
            'total_llm_seconds': round(self.total_seconds, 2),
            'stages': {name: bucket.to_dict() for name, bucket in sorted(self.stages.items())},
        }

    def format_summary(self) -> str:
        lines = [
            f'LLM cost ({self.model}, prices as of {self.price_snapshot_date}, '
            f'{self.accounting_used} token accounting)',
            '=' * 72,
            f'  {"STAGE":<24}{"CALLS":>7}{"IN TOK":>11}{"OUT TOK":>11}{"COST":>12}',
        ]
        for name, bucket in sorted(self.stages.items()):
            lines.append(
                f'  {name:<24}{bucket.calls:>7}{bucket.input_tokens:>11}'
                f'{bucket.output_tokens:>11}{bucket.cost:>11.4f}{self.currency[:1]}'
            )
        lines.append(
            f'  {"TOTAL":<24}{self.total_calls:>7}{self.total_input_tokens:>11}'
            f'{self.total_output_tokens:>11}{self.total_cost:>11.4f}{self.currency[:1]}'
        )
        if self.estimated_usage_calls:
            lines.append(
                f'  note: {self.estimated_usage_calls}/{self.total_calls} calls used '
                f'estimated token counts (provider returned no usage block)'
            )
        return '\n'.join(lines)

    def dump(self, path: str) -> None:
        with open(path, 'w') as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)


def extract_usage(completion) -> Optional[Dict[str, int]]:
    """Pull exact token counts from a provider response, if present.

    Handles both the chat-completions shape (``prompt_tokens`` /
    ``completion_tokens``) and the responses-API shape (``input_tokens`` /
    ``output_tokens``).
    """
    usage = getattr(completion, 'usage', None)
    if usage is None:
        return None

    def read(*names):
        for name in names:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            if value is not None:
                return int(value)
        return None

    input_tokens = read('prompt_tokens', 'input_tokens')
    output_tokens = read('completion_tokens', 'output_tokens')
    if input_tokens is None and output_tokens is None:
        return None

    return {
        'input_tokens': input_tokens or 0,
        'output_tokens': output_tokens or 0,
    }


def load_price_overrides(path: Optional[str] = None) -> None:
    """Load site-specific prices from JSON, overriding the built-in table.

    Reads ``$SVAPSHOT_PRICES`` when no path is given, allowing negotiated rates
    to be applied without editing the source.
    """
    path = path or os.environ.get('SVAPSHOT_PRICES')
    if not path or not os.path.exists(path):
        return

    with open(path) as handle:
        data = json.load(handle)

    for model, entry in data.get('models', {}).items():
        MODEL_PRICES[model] = ModelPrice(
            input_per_mtok=float(entry['input_per_mtok']),
            output_per_mtok=float(entry['output_per_mtok']),
            currency=entry.get('currency', 'EUR'),
        )
