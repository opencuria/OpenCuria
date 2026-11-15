"""Pinned Codex capabilities, separate from the native ChatGPT adapter catalog.

models.json records unauthenticated model/list capabilities from the official
0.166.0 bundled runtime, not the account's live subscription entitlements. No
native-provider defaults are used: Codex's default and effort support differ.
OpenCuria starts ChatGPT Codex models at high effort when supported.
Upstream OpenAI Codex (Apache-2.0); see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from apps.harness.providers.models_catalog import ProviderModel

CODEX_VERSION = "0.166.0"
CODEX_PROVIDERS = ("chatgpt", "openrouter")
CODEX_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_SPECS = json.loads(Path(__file__).with_name("models.json").read_text())["models"]
_DEFAULT = next(spec for spec in _SPECS if spec["is_default"])
DEFAULT_CODEX_MODEL = "chatgpt/" + _DEFAULT["id"]
DEFAULT_CODEX_EFFORT = "high"
_MODEL_REF = re.compile(r"^(chatgpt|openrouter)/[A-Za-z0-9][A-Za-z0-9._:/+@-]*$")


def chatgpt_models() -> list[ProviderModel]:
    """Return the runtime's explicit ChatGPT subscription model allowlist."""
    return [
        ProviderModel(
            id="chatgpt/" + spec["id"],
            name=spec["name"],
            provider="chatgpt",
            reasoning_efforts=tuple(spec["reasoning_efforts"]),
            default_effort=(
                DEFAULT_CODEX_EFFORT
                if DEFAULT_CODEX_EFFORT in spec["reasoning_efforts"]
                else spec["default_effort"]
            ),
            supports_tools=True,
        )
        for spec in _SPECS
    ]


def normalize_codex_model(value: str | None, *, provider: str | None = None) -> str:
    """Require an existing namespaced provider ref and immutable provider routing."""
    model = DEFAULT_CODEX_MODEL if value is None else value.strip()
    if not _MODEL_REF.fullmatch(model):
        raise ValueError("Codex model must use chatgpt/ or openrouter/ namespace")
    selected_provider = model.split("/", 1)[0]
    if provider is not None and selected_provider != provider:
        raise ValueError("Codex conversation provider cannot be changed")
    if selected_provider == "chatgpt" and model not in {m.id for m in chatgpt_models()}:
        raise ValueError(f"Unsupported Codex ChatGPT model {model!r}")
    return model


def codex_provider(model: str, engine_state: dict | None = None) -> str:
    """Validate routing against the persisted binding, including direct run APIs."""
    bound = (engine_state or {}).get("provider_id")
    normalized = normalize_codex_model(model, provider=bound)
    return normalized.split("/", 1)[0]


def normalize_codex_effort(value: str | None, model: ProviderModel) -> str:
    """Enforce the model's metadata allowlist, including ultra only if supported."""
    effort = (value or model.default_effort).strip().lower()
    if effort and effort not in model.reasoning_efforts:
        raise ValueError(f"Invalid Codex reasoning effort {value!r} for {model.id}")
    return effort
