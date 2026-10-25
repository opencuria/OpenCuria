"""Session titles and summaries, independent of agent run orchestration.

The caller schedules background tasks and owns provider-turn holds. This service
uses their frozen account authority to generate, validate and persist metadata;
callbacks publish changes without depending on the session lifecycle service.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from common.exceptions import NotFoundError

from .engines.base import RunOptions
from .models import HarnessMessage, HarnessPart
from .provider_turns import ProviderTurnService
from .providers.base import ProviderAdapter
from .repositories import HarnessMessageRepository, HarnessSessionRepository
from .services import ProviderConfigService
from .small_model_service import build_small_model_runner

log = structlog.get_logger(__name__)

FRONTEND_EVENT_SUMMARY = "harness.session_summary"


class SessionMetadataService:
    """Generate session metadata without owning or scheduling agent runs."""

    def __init__(
        self,
        *,
        emit: Callable[[str, dict[str, Any], str], Awaitable[None]],
        conversations_changed: Callable[[uuid.UUID], Awaitable[None]],
        sessions: type[HarnessSessionRepository] | None = None,
        messages: type[HarnessMessageRepository] | None = None,
        turns: ProviderTurnService | None = None,
        provider_factory: Callable[[uuid.UUID], ProviderAdapter] | None = None,
    ) -> None:
        """Inject persistence, provider authority and change notifications."""
        self.sessions = sessions or HarnessSessionRepository
        self.messages = messages or HarnessMessageRepository
        self.turns = turns or ProviderTurnService()
        self._provider_factory = provider_factory
        self._emit = emit
        self._conversations_changed = conversations_changed

    async def is_enabled(self, organization_id: uuid.UUID) -> bool:
        """Return whether the organization configured a metadata model."""
        try:
            config = await sync_to_async(ProviderConfigService().get_config)(
                organization_id
            )
        except NotFoundError:
            return False
        return bool((config.small_model or "").strip())

    def summary_material(self, session_id: uuid.UUID, assistant_id: uuid.UUID) -> str:
        """Collect user requests and closing replies through the settled turn."""
        message = self.messages.get_by_id(assistant_id)
        if (
            message is None
            or message.session_id != session_id
            or message.role != "assistant"
        ):
            return ""
        sections: list[str] = []
        has_request = False
        for row in self.messages.list_for_summary(session_id, message.position):
            if row.role == "user":
                if row.content.strip():
                    sections.append(f"User:\n{row.content.strip()}")
                    has_request = True
                continue
            parts = row.summary_parts
            for part in parts:
                if part.type == "steer" and part.summary_text.strip():
                    sections.append(f"User:\n{part.summary_text.strip()}")
                    has_request = True
            answer = _assistant_summary_content(row, parts)
            if answer:
                sections.append(f"Agent's final reply:\n{answer}")
        return "\n\n".join(sections) if has_request else ""

    async def generate_summary(
        self,
        *,
        session_id: uuid.UUID,
        assistant_id: uuid.UUID,
        workspace_id: uuid.UUID,
        organization_id: uuid.UUID,
        provider_turn_id: uuid.UUID | str | None = None,
        account_ids: dict[str, str] | None = None,
        initiated_by_id: int | None = None,
    ) -> None:
        """Store a brief recap of the user's goal after a run settles.

        Runs for every engine (the small model comes from the org's provider
        config, not from the session's engine) and never raises.
        """
        try:
            if provider_turn_id and await sync_to_async(self.turns.cancellation_reason)(
                provider_turn_id
            ):
                return
            small = await build_small_model_runner(
                organization_id,
                user_id=initiated_by_id,
                account_ids=account_ids,
                provider_factory=self._provider_factory,
                on_account_used=(
                    partial(self.turns.register_use, provider_turn_id)
                    if provider_turn_id
                    else None
                ),
            )
            if small is None:
                return
            runner, small_model = small
            material = await sync_to_async(self.summary_material)(
                session_id, assistant_id
            )
            if not material:
                return
            result = await runner.run(
                material,
                "summary",
                small_model,
                "build",
                RunOptions(auto_approve=True),
            )
            if provider_turn_id and await sync_to_async(self.turns.cancellation_reason)(
                provider_turn_id
            ):
                return
            summary = _normalize_generated_summary(result.output or "")
            if not summary:
                return
            stored = await sync_to_async(self.sessions.set_summary_for_turn)(
                session_id, assistant_id, summary
            )
            if stored:
                await self._emit(
                    FRONTEND_EVENT_SUMMARY,
                    {
                        "workspace_id": str(workspace_id),
                        "session_id": str(session_id),
                        "summary": summary,
                    },
                    str(workspace_id),
                )
        except Exception:  # pragma: no cover - summary must never break runs
            log.warning("harness_summary_generation_failed", session_id=str(session_id))

    async def generate_title(
        self,
        *,
        session_id: uuid.UUID,
        prompt: str,
        organization_id: uuid.UUID,
        provider_turn_id: uuid.UUID | str | None = None,
        account_ids: dict[str, str] | None = None,
        initiated_by_id: int | None = None,
    ) -> None:
        """Run the hidden title agent on the initiating answer's snapshot."""
        try:
            session = await sync_to_async(self.sessions.get_by_id)(session_id)
            if session is None or getattr(session, "harness_id", "native") != "native":
                return
            if provider_turn_id and await sync_to_async(self.turns.cancellation_reason)(
                provider_turn_id
            ):
                return
            small = await build_small_model_runner(
                organization_id,
                user_id=initiated_by_id,
                account_ids=account_ids,
                provider_factory=self._provider_factory,
                on_account_used=(
                    partial(self.turns.register_use, provider_turn_id)
                    if provider_turn_id
                    else None
                ),
            )
            if small is None:
                return
            runner, small_model = small
            result = await runner.run(
                prompt,
                "title",
                small_model,
                "build",
                RunOptions(auto_approve=True),
            )
            if provider_turn_id and await sync_to_async(self.turns.cancellation_reason)(
                provider_turn_id
            ):
                return
            title = _normalize_generated_title(result.output or "")
            if not title:
                return
            session = await sync_to_async(self.sessions.get_by_id)(session_id)
            if session is None:
                return
            if provider_turn_id and await sync_to_async(self.turns.cancellation_reason)(
                provider_turn_id
            ):
                return
            auto_title = title_from_prompt(prompt)
            if (session.title or "").strip() != auto_title:
                return
            await sync_to_async(self.sessions.set_title)(session, title)
            if session.parent_id is None:
                await self._conversations_changed(session.workspace_id)
        except Exception:  # pragma: no cover - title must never break runs
            log.warning("harness_title_generation_failed", session_id=str(session_id))


def _normalize_generated_title(text: str) -> str:
    """Clamp a generated title to at most eight words and 255 chars."""
    title = " ".join((text or "").strip().split())
    words = title.split()
    if len(words) > 8:
        title = " ".join(words[:8])
    return title[:255]


def title_from_prompt(prompt: str) -> str:
    """Derive a short session title from the first prompt line."""
    first_line = (prompt or "").strip().splitlines()[0] if prompt.strip() else ""
    title = first_line.strip()[:80]
    return title or "Harness session"


def _normalize_generated_summary(text: str) -> str:
    """Reduce model output to one plain line of at most 100 chars."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    summary = " ".join(lines[0].split()) if lines else ""
    summary = summary.replace("*", "").replace("`", "").strip("\"'“”„ ")
    if len(summary) > 100:
        summary = summary[:100].rsplit(" ", 1)[0].rstrip(",;:- ") + "…"
    return summary


def _assistant_summary_content(
    message: HarnessMessage, parts: list[HarnessPart]
) -> str:
    """Select closing prose outside Worked, like chat-core's message-copy rule.

    Summary rows project only text/steer output into ``summary_text``. Walk
    backwards through visible text; work, plans, compaction, and steers end the
    final answer. Never fall back to aggregate content when parts exist.
    """
    if not parts:
        return (message.content or "").strip()
    has_subtask = any(part.type == "subtask" for part in parts)
    patched_calls = {
        part.call_id for part in parts if part.type == "patch" and part.call_id
    }
    answer: list[str] = []
    for part in reversed(parts):
        if part.type in {"step-start", "step-finish"}:
            continue
        text = part.summary_text
        if part.type == "text":
            if text:
                answer.append(text)
            continue
        if part.type == "steer" and not text:
            continue
        if part.type == "tool":
            tool = str((part.display or {}).get("tool") or "").strip().lower()
            if tool in {"question", "ask_user"} and part.state in {
                "pending",
                "running",
            }:
                continue
            if has_subtask and (tool == "task" or part.title.startswith("Subagent:")):
                continue
            if (
                tool in {"edit", "write"}
                and part.state != "error"
                and part.call_id in patched_calls
            ):
                continue
        break
    return "".join(reversed(answer)).strip()
