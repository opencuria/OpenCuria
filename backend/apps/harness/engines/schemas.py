"""Request/response schemas and DTO mappers for the harness engine adapters.

Shared by the REST router (``engines/api.py``) and the MCP tools so both
expose identical payloads without importing each other.
"""

from __future__ import annotations

from ninja import Schema


class HarnessEngineOut(Schema):
    """Public engine availability and connection state."""

    id: str
    name: str
    modes: list[str]
    connected: bool
    default_model: str | None = None
    default_effort: str | None = None


class EngineModelOut(Schema):
    """Harness model using the shared picker model shape."""

    id: str
    name: str
    provider: str = "claude"
    reasoning_efforts: list[str] = []
    default_effort: str = ""
    supports_tools: bool = True
    context_length: int = 0
    max_output_tokens: int = 0


ClaudeModelOut = EngineModelOut  # Compatibility for existing Claude clients.


def claude_model_to_out(model) -> ClaudeModelOut:  # type: ignore[no-untyped-def]
    """Convert the shared provider-model shape into this engine's API schema."""
    return ClaudeModelOut(
        id=model.id,
        name=model.name,
        provider=model.provider,
        reasoning_efforts=list(model.reasoning_efforts),
        default_effort=model.default_effort,
        supports_tools=model.supports_tools,
        context_length=model.context_length,
        max_output_tokens=model.max_output_tokens,
    )


engine_model_to_out = claude_model_to_out
