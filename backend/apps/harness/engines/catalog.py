"""Claude Code engine identifiers, model catalog, and validation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.harness.providers.models_catalog import ProviderModel

ENGINE_IDS = ("native", "claude")
CLAUDE_AGENT_SDK_VERSION = "0.2.164"
CLAUDE_CODE_CLI_VERSION = "2.1.292"
DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_CLAUDE_EFFORT = "high"
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Pass SDK aliases through unchanged so Claude Code selects its current model
# alias rather than pinning a stale model version in OpenCuria.
CLAUDE_MODEL_ALIASES: dict[str, str] = {
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku": "haiku",
}
_CLAUDE_MODEL_PATTERN = re.compile(
    r"^claude-(?:sonnet|opus|haiku)-\d+(?:-\d+)*(?:-\d{8})?$"
)


@dataclass(frozen=True)
class ClaudeModelSpec:
    """Static capability information for one selectable Claude model."""

    id: str
    name: str
    sdk_model: str
    context_length: int
    max_output_tokens: int

    def to_provider_model(self) -> ProviderModel:
        """Expose a Claude entry using the shared model-picker data shape."""
        return ProviderModel(
            id=self.id,
            name=self.name,
            reasoning_efforts=CLAUDE_EFFORTS,
            default_effort=DEFAULT_CLAUDE_EFFORT,
            supports_tools=True,
            context_length=self.context_length,
            max_output_tokens=self.max_output_tokens,
            provider="claude",
        )


CLAUDE_MODELS: tuple[ClaudeModelSpec, ...] = (
    ClaudeModelSpec("sonnet", "Claude Sonnet", "sonnet", 200_000, 64_000),
    ClaudeModelSpec("opus", "Claude Opus", "opus", 200_000, 64_000),
    ClaudeModelSpec("haiku", "Claude Haiku", "haiku", 200_000, 64_000),
)


def list_claude_models() -> list[ProviderModel]:
    """Return the Claude engine's models, isolated from provider adapters."""
    return [spec.to_provider_model() for spec in CLAUDE_MODELS]


def normalize_claude_model(value: str | None) -> str:
    """Validate an alias or constrained Claude SDK model ID.

    Provider-qualified IDs and arbitrary strings are rejected; this catalog is
    for the Claude harness engine, not the provider-adapter registry.
    """
    if value is None:
        model = DEFAULT_CLAUDE_MODEL
    else:
        model = value.strip().lower()
        if not model:
            raise ValueError("Claude model must not be empty")
    if model in CLAUDE_MODEL_ALIASES:
        return model
    if _CLAUDE_MODEL_PATTERN.fullmatch(model):
        return model
    raise ValueError(f"Invalid Claude model {value!r}")


def resolve_claude_sdk_model(value: str | None) -> str:
    """Return the SDK's accepted model name for an alias or explicit ID."""
    model = normalize_claude_model(value)
    return CLAUDE_MODEL_ALIASES.get(model, model)


def normalize_claude_effort(value: str | None) -> str:
    """Normalize Claude reasoning effort and enforce the engine allowlist."""
    if value is None:
        effort = DEFAULT_CLAUDE_EFFORT
    else:
        effort = value.strip().lower()
    if effort not in CLAUDE_EFFORTS:
        raise ValueError(f"Invalid Claude reasoning effort {value!r}")
    return effort
