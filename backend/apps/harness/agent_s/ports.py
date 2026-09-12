"""Ports: completion, OCR, code execution, action materialization.

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

All ports are ``async`` and injectable so the core stays testable without
network, OCR binaries, Django, or subprocesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

CallPurpose = Literal[
    "worker", "reflection", "grounding", "text_span", "code", "code_summary"
]


@dataclass(frozen=True)
class Usage:
    """Per-call token usage (provider-supplied; defaults to zero)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


@dataclass(frozen=True)
class Completion:
    """One LLM completion: text plus optional usage."""

    text: str
    usage: Usage = field(default_factory=Usage)


@runtime_checkable
class CompletionPort(Protocol):
    """Agent-S wire-message completion backend.

    Implementations receive the exact Agent-S message list (system + user +
    assistant dicts with PNG data-URL image parts) and return the raw text.
    ``purpose`` tags the call (worker/reflection/grounding/text_span/code/
    code_summary) for tests and routing; ``temperature``/``use_thinking`` are
    forwarded verbatim (Claude thinking model list lives in prompts).
    """

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        purpose: CallPurpose,
        temperature: float | None,
        use_thinking: bool,
    ) -> Completion: ...


@runtime_checkable
class OcrPort(Protocol):
    """Word-level OCR over screenshot bytes (``pytesseract`` lives here)."""

    async def read_words(self, screenshot: bytes) -> list[dict[str, Any]]:
        """Return rows with ``text/block_num/left/top/width/height`` keys."""
        ...


@runtime_checkable
class CodeExecutionPort(Protocol):
    """Sandboxed code execution (no local subprocess in the domain)."""

    async def run_python(self, code: str) -> dict[str, Any]: ...

    async def run_bash(self, code: str, timeout: int = 30) -> dict[str, Any]: ...


@dataclass
class MaterializationResult:
    """Outcome of materializing one parsed action."""

    exec_code: str
    """PyAutoGUI-style snippet (or ``DONE``/``FAIL``/``WAIT`` terminal)."""
    terminal: str | None = None
    """``DONE``/``FAIL``/``WAIT`` when the action terminates/waits."""
    code_agent_result: dict[str, Any] | None = None
    """Set when ``call_code_agent`` ran (stored for the next worker turn)."""


@runtime_checkable
class ActionMaterializer(Protocol):
    """Turn a parsed action into executable code (may call LLM/OCR/code).

    The same instance is invoked once during the ``CODE_VALID`` format check
    *and* again for the accepted plan, preserving Agent-S's observable
    double-materialization quirk (duplicate grounding/notes/code-agent side
    effects are therefore possible by design).
    """

    async def materialize(
        self,
        action: Any,  # ParsedAction
        obs: dict[str, Any],
    ) -> MaterializationResult: ...
