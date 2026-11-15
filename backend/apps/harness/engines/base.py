"""Shared execution contract for harness engines.

The service, native loop and external engines depend on these execution values;
this module never imports an engine implementation or application orchestration.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..access.base import HARNESS_WORKSPACE_ROOT
from ..constants import DEFAULT_MAX_DEPTH
from ..providers.base import LLMMessage, Usage
from ..steering import SteeringChannel

EmitCallback = Callable[[dict[str, Any]], Awaitable[None]]
PermissionCallback = Callable[..., Awaitable[str]]
QuestionCallback = Callable[..., Awaitable[list[Any]]]


@dataclass
class RunOptions:
    """Per-turn settings shared by all harness engines."""

    max_steps: int | None = None
    history: list[LLMMessage] = field(default_factory=list)
    session_id: str = ""
    workspace_id: str = ""
    organization_id: str = ""
    cwd: str = HARNESS_WORKSPACE_ROOT
    small_model: str = ""
    skills: list[str] = field(default_factory=list)
    permission_timeout: float | None = None
    question_timeout: float | None = None
    auto_approve: bool = False
    on_permission: PermissionCallback | None = None
    on_question: QuestionCallback | None = None
    authorize_file_read: Callable[[str], Awaitable[bool]] | None = None
    run_subagent: Any | None = None
    depth: int = 0
    max_depth: int = DEFAULT_MAX_DEPTH
    # Per-run cache of once-approved (tool, action) pairs, reset at
    # the start of every run() call. ``always`` continues to flow
    # through the allowlist/service; this set only suppresses repeat
    # ``once`` callbacks for identical calls in one run (never shared
    # across runs, even when RunOptions is reused).
    once_approved: set[tuple[str, str]] = field(default_factory=set)
    context_length: int = 0
    max_output_tokens: int = 0
    auto_compact: bool = True
    current_user_message_id: str = ""
    last_step_prompt_tokens: int = 0
    last_step_completion_tokens: int = 0
    last_step_total_tokens: int = 0
    #: Queue of user messages the model should see mid-run (root runs only).
    steering: SteeringChannel | None = None


@dataclass
class RunResult:
    """Aggregated outcome of one harness engine turn."""

    output: str
    steps: int
    usage: Usage
    cost: float
    finish_reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


class HarnessEngine(Protocol):
    """Engine interface consumed by the persistent harness service."""

    async def run(
        self,
        prompt: str,
        agent: str,
        model: str,
        mode: str,
        opts: RunOptions,
    ) -> RunResult:
        """Run one agent turn and return its aggregated result."""
        ...
