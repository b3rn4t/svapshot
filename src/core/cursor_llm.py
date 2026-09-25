"""Cursor SDK text backend for SVApshot.

One-shot ``Agent.prompt`` with ``tools=[]`` so generation, repair, and
assumption prompts look like an LLM API call rather than an IDE agent.
The catalog model id is selected the same way OpenAI/Vertex names are:
``--llm composer-2.5`` or ``--llm cursor:composer-2.5``.

Grok 4.6 (and other catalog models) take SDK ``ModelSelection.params``:
reasoning ``effort`` (``xhigh|high|medium|low``) and the Fast switch.
Encode them on the name (``cursor:grok-4.6:high:fast``) or export
``SVAPSHOT_CURSOR_EFFORT`` / ``SVAPSHOT_CURSOR_FAST``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_CURSOR_MODEL = 'composer-2.5'
CURSOR_EFFORTS = frozenset({'xhigh', 'high', 'medium', 'low'})


def _truthy(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'fast'}:
        return True
    if text in {'0', 'false', 'no', 'nofast', 'standard'}:
        return False
    return None


def parse_cursor_selector(
    name: str,
) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
    """Split a Cursor selector into catalog id, effort, and Fast.

    Returns ``(None, None, None)`` when ``name`` is not a Cursor SDK id.
    """
    raw = (name or '').strip()
    if not raw:
        return None, None, None
    prefixed = False
    rest = raw
    if raw.startswith('cursor:'):
        prefixed = True
        rest = raw.split(':', 1)[1].strip()
    elif raw.startswith('cursor/'):
        prefixed = True
        rest = raw.split('/', 1)[1].strip()
    elif raw in {'cursor', 'cursor-sdk'}:
        prefixed = True
        rest = os.environ.get('CURSOR_MODEL', DEFAULT_CURSOR_MODEL)
    if not rest:
        if prefixed:
            rest = os.environ.get('CURSOR_MODEL', DEFAULT_CURSOR_MODEL)
        else:
            return None, None, None
    parts = [part for part in rest.replace('+', ':').split(':') if part]
    catalog_parts: List[str] = []
    effort: Optional[str] = None
    fast: Optional[bool] = None
    for part in parts:
        lower = part.lower()
        if lower in CURSOR_EFFORTS:
            effort = lower
            continue
        if lower in {'fast', 'fast=true'}:
            fast = True
            continue
        if lower in {'nofast', 'fast=false', 'standard'}:
            fast = False
            continue
        catalog_parts.append(part)
    catalog = ':'.join(catalog_parts)
    if not catalog:
        if prefixed:
            catalog = os.environ.get('CURSOR_MODEL', DEFAULT_CURSOR_MODEL)
        else:
            return None, None, None
    if not prefixed and not (
        catalog.startswith('composer-') or catalog.startswith('grok-')
    ):
        return None, None, None
    return catalog, effort, fast


def catalog_model_id(name: str) -> Optional[str]:
    """Return the Cursor catalog id if ``name`` is a Cursor SDK selector."""
    catalog, _effort, _fast = parse_cursor_selector(name)
    return catalog


def resolve_cursor_runtime(
    name: str,
) -> Tuple[str, Optional[str], Optional[bool]]:
    """Catalog id plus effort/Fast from the name, then DATE env fallbacks."""
    catalog, effort, fast = parse_cursor_selector(name)
    if catalog is None:
        catalog = name
    if effort is None:
        effort = (os.environ.get('SVAPSHOT_CURSOR_EFFORT') or '').strip() or None
        if effort and effort not in CURSOR_EFFORTS:
            raise ValueError(
                f'SVAPSHOT_CURSOR_EFFORT={effort!r} is not in {sorted(CURSOR_EFFORTS)}')
    if fast is None:
        fast = _truthy(os.environ.get('SVAPSHOT_CURSOR_FAST'))
    return catalog, effort, fast


def sdk_model_params(model: str) -> Tuple[str, List[Dict[str, str]]]:
    """Return ``(catalog_id, [{id, value}, ...])`` for ``ModelSelection``."""
    catalog, effort, fast = resolve_cursor_runtime(model)
    params: List[Dict[str, str]] = []
    if effort:
        params.append({'id': 'effort', 'value': effort})
    if fast is True:
        params.append({'id': 'fast', 'value': 'true'})
    elif fast is False:
        params.append({'id': 'fast', 'value': 'false'})
    return catalog, params


def _text_from_result(result: Any) -> str:
    text = getattr(result, 'result', None)
    if text is None:
        text = getattr(result, 'text', None)
    if text is None:
        return ''
    if isinstance(text, str):
        return text
    return str(text)


def generate(model: str, prompt: str, cwd: Optional[str] = None) -> str:
    """One Cursor SDK completion. Raises if the SDK or key is missing."""
    try:
        from cursor_sdk import Agent, AgentOptions, LocalAgentOptions
    except ImportError as exc:
        raise ImportError(
            'cursor-sdk is required for Cursor SDK models; '
            'pip install cursor-sdk'
        ) from exc

    api_key = os.environ.get('CURSOR_API_KEY')
    if not api_key:
        raise ValueError('CURSOR_API_KEY environment variable not set')

    workspace = cwd or os.getcwd()
    catalog, params = sdk_model_params(model)
    if params:
        try:
            from cursor_sdk import ModelParameterValue, ModelSelection
        except ImportError as exc:
            raise ImportError(
                'cursor-sdk is too old for ModelSelection.params '
                '(effort/fast); upgrade cursor-sdk'
            ) from exc
        sdk_model: Any = ModelSelection(
            id=catalog,
            params=[
                ModelParameterValue(id=item['id'], value=item['value'])
                for item in params
            ],
        )
    else:
        sdk_model = catalog
    options = AgentOptions(
        model=sdk_model,
        api_key=api_key,
        local=LocalAgentOptions(cwd=workspace),
        tools=[],
    )
    result = Agent.prompt(prompt, options)
    status = getattr(result, 'status', None)
    if status == 'error':
        run_id = getattr(result, 'id', '') or getattr(result, 'run_id', '')
        raise RuntimeError(f'Cursor SDK run failed (status=error id={run_id})')
    text = _text_from_result(result).strip()
    if not text:
        raise RuntimeError('Cursor SDK returned an empty completion')
    return text
