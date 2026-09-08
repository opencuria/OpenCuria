"""
Namespaced model reference parsing for multi-provider harness runs.
"""

from __future__ import annotations

KNOWN_PROVIDERS = frozenset({"openrouter", "chatgpt", "amazon-bedrock"})


def parse_model_ref(ref: str) -> tuple[str, str]:
    """Split a model reference into ``(provider, bare_model_id)``.

    Namespaced refs use the first ``/`` as the provider boundary, e.g.
    ``openrouter/openai/gpt-5`` → ``("openrouter", "openai/gpt-5")``.

    When the first segment is not a known provider id, the whole string is
    treated as a legacy OpenRouter model id (``openai/gpt-5`` keeps working).
    """
    stripped = (ref or "").strip()
    if not stripped:
        return "openrouter", ""
    if "/" not in stripped:
        return "openrouter", stripped
    provider, bare_id = stripped.split("/", 1)
    if provider in KNOWN_PROVIDERS:
        return provider, bare_id
    return "openrouter", stripped


def namespaced_model_id(ref: str) -> str:
    """Return the canonical namespaced catalog id for *ref*."""
    provider, bare_id = parse_model_ref(ref)
    if not bare_id:
        return ""
    return f"{provider}/{bare_id}"
