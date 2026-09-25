"""Vertex AI Gemini client for SVApshot (Google Cloud).

Uses Application Default Credentials. Project/location come from
``GOOGLE_CLOUD_PROJECT`` / ``GOOGLE_CLOUD_LOCATION`` (default ``global``).
"""

from __future__ import annotations

import os
from typing import Optional


def _project() -> str:
    project = os.environ.get('GOOGLE_CLOUD_PROJECT') or os.environ.get('GCLOUD_PROJECT')
    if not project:
        raise RuntimeError('Set GOOGLE_CLOUD_PROJECT for Vertex models')
    return project


def _location() -> str:
    return os.environ.get('GOOGLE_CLOUD_LOCATION') or 'global'


_CLIENT = None


def client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    try:
        from google import genai
    except ImportError as exc:
        raise ImportError(
            'google-genai is required for Vertex models; '
            'pip install google-genai'
        ) from exc
    _CLIENT = genai.Client(
        vertexai=True,
        project=_project(),
        location=_location(),
    )
    return _CLIENT


def generate(model: str, prompt: str, max_output_tokens: int = 16384) -> str:
    """One-shot text generation. Returns the visible model text."""
    response = client().models.generate_content(
        model=model,
        contents=prompt,
        config={
            'max_output_tokens': max_output_tokens,
            'temperature': 0.2,
            'automatic_function_calling': {'disable': True},
        },
    )
    text = getattr(response, 'text', None) or ''
    if text.strip():
        return text
    # Fallback: walk candidates when .text is empty.
    chunks = []
    for candidate in getattr(response, 'candidates', None) or ():
        content = getattr(candidate, 'content', None)
        for part in getattr(content, 'parts', None) or ():
            part_text = getattr(part, 'text', None)
            if part_text:
                chunks.append(part_text)
    return ''.join(chunks)


def usage_tokens(response) -> Optional[dict]:
    meta = getattr(response, 'usage_metadata', None)
    if meta is None:
        return None
    inp = getattr(meta, 'prompt_token_count', None)
    out = getattr(meta, 'candidates_token_count', None)
    if inp is None and out is None:
        return None
    return {
        'input_tokens': int(inp or 0),
        'output_tokens': int(out or 0),
    }
