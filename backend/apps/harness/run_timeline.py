"""Project a run's stream events onto the persisted chat timeline.

``RunTimeline`` turns runner stream events into ``HarnessPart`` rows and the
matching bounded live deltas (``harness.part_updated``, subtask and todo
events), and settles parts a run left open. It owns the per-run stream cursors
kept in the run context (``text_part_id``, ``reasoning_part_id``,
``step_parts`` and ``subtask_parts``); tool calls are matched to their part by
:class:`ToolCallParts`. Run admission, abort, user gates and finalization stay
with ``HarnessService``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from asgiref.sync import sync_to_async

from .models import HarnessMessage, HarnessPart, HarnessSession
from .providers.base import Usage
from .repositories import HarnessMessageRepository, HarnessPartRepository
from .tool_call_parts import ToolCallParts

FRONTEND_EVENT_PART = "harness.part_updated"
FRONTEND_EVENT_TODO = "harness.todo_updated"
FRONTEND_EVENT_SUBTASK_STARTED = "harness.subtask_started"
FRONTEND_EVENT_SUBTASK_FINISHED = "harness.subtask_finished"

#: Frontend fan-out: ``(event, data, workspace_id)``.
FrontendEmit = Callable[[str, dict[str, Any], str], Awaitable[None]]


def _socket_text(value: object, *, limit: int) -> str:
    """Bound runner-controlled text included in a live socket projection."""
    return str(value or "")[:limit]


def _tool_socket_identity(
    event: dict[str, Any], part: HarnessPart, *, state: str
) -> dict[str, str]:
    """Project a tool event to identifiers and a small display title only."""
    return {
        "tool": _socket_text(
            event.get("tool") or (part.input or {}).get("tool", ""), limit=100
        ),
        "title": _socket_text(event.get("title") or part.title, limit=240),
        "call_id": _socket_text(event.get("call_id") or part.call_id, limit=255),
        "state": state,
    }


def tail_remainder(existing: str, tail: str) -> str:
    """Return the genuinely-new suffix of a runner final ``output``.

    Streaming already appended every text delta to the message, so the
    final ``output`` is the full assembled answer: appending it blindly
    duplicates content (content-only drift). Exact suffix/prefix overlaps
    return ``""``; only a true remainder is returned for persistence.
    """
    existing = existing or ""
    tail = tail or ""
    if not tail:
        return ""
    if not existing:
        return tail
    if existing.endswith(tail):
        return ""
    if tail.startswith(existing):
        return tail[len(existing) :]
    if existing.startswith(tail):
        return ""
    # A divergent final string is not a suffix of the streamed response.
    # Appending it would duplicate or scramble the answer; the persisted
    # deltas remain authoritative instead.
    return ""


class RunTimeline:
    """Persist runner stream events as timeline parts and publish live deltas."""

    def __init__(
        self,
        *,
        messages: type[HarnessMessageRepository],
        parts: type[HarnessPartRepository],
        run_context: Callable[[str], dict[str, Any]],
        emit: FrontendEmit,
    ) -> None:
        """Bind repositories, the live run-context lookup and frontend fan-out.

        ``run_context`` returns the mutable context of the session's live run,
        or a throwaway empty dict once the run is no longer tracked.
        """
        self.messages = messages
        self.parts = parts
        self._run_context = run_context
        self._emit = emit
        self.tool_calls = ToolCallParts(parts=parts, run_context=run_context)

    async def record(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> None:
        """Persist a runner streaming event and forward it to frontend.

        Contract shared by the runner, persisted parts, and frontend:

        - every ``harness.part_updated`` payload carries ``message_id``
          (the current assistant shell) so live deltas route to the right
          turn; text/reasoning deltas additionally carry the persisted
          ``part_id`` of the running stream part.
        - ``harness.subtask_started/finished`` carry ``message_id`` too.
        - ``harness.session_status`` busy/idle carries ``message_id`` of the
          *current* assistant (see ``HarnessService._session_status_payload``).
        - provider-position handling: the runner emits
          ``tool_queued`` ``{type, step, call_id, tool, title, arguments}``
          in observed order. We persist all tool details, but live
          ``delta.tool_started`` contains only bounded tool/title/call_id/state
          fields plus the ``part_id``. The later ``tool_started`` for the
          same call flips the same part to running and re-emits that small
          projection. ``tool_queued`` ``call_id`` may be provisional: when
          the real ``tool_started`` carries a *different* id, its ``queued_id``
          identifies the part to rebind instead of creating a second row.
        """
        etype = str(event.get("type", ""))
        workspace_id = str(session.workspace_id)
        session_id = str(session.id)
        message_id = str(assistant.id)
        if etype == "part_updated":
            part_id = await self._persist_part_updated(session, assistant, event)
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": event.get("delta", {}),
                    "step": event.get("step"),
                    **({"part_id": part_id} if part_id is not None else {}),
                },
                workspace_id,
            )
        elif etype == "tool_queued":
            await self.close_stream_parts(session)
            part = await self.tool_calls.record_queued(session, assistant, event)
            identity = _tool_socket_identity(event, part, state="pending")
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "tool_started": identity["tool"],
                        "title": identity["title"],
                        "call_id": identity["call_id"],
                        "state": identity["state"],
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "step_start":
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="step-start",
                state="running",
                meta={"step": event.get("step")},
            )
            self._run_context(session_id).get("step_parts", {})[
                str(event.get("step"))
            ] = str(part.id)
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {"step_start": event.get("step")},
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "step_finish":
            tokens = event.get("tokens", {}) or {}
            context = (
                {"context": event["context"]}
                if isinstance(event.get("context"), dict)
                else {}
            )
            usage = Usage(
                prompt_tokens=int(tokens.get("prompt_tokens", 0)),
                completion_tokens=int(tokens.get("completion_tokens", 0)),
                total_tokens=int(tokens.get("total_tokens", 0)),
                cost=float(event.get("cost", 0.0) or 0.0),
            )
            await sync_to_async(self.messages.add_usage)(
                assistant,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cost=float(event.get("cost", 0.0) or 0.0),
            )
            # Close the matching step-start part (DB-side; it stayed
            # ``running`` because steps used to be frontend-only). A
            # missing step part (e.g. legacy rows) is not an error.
            step_part_id = (
                self._run_context(session_id)
                .get("step_parts", {})
                .pop(str(event.get("step")), None)
            )
            if step_part_id is not None:
                step_part = await sync_to_async(self.parts.get_by_id)(step_part_id)
                if step_part is not None:
                    await sync_to_async(self.parts.mark_state)(step_part, "completed")
            # Close the per-step reasoning and text parts (DB-side; the
            # frontend already closes them live on step_finish). Resetting
            # the ids makes the next step start fresh parts so the final
            # answer stays after tools in created_at order ("Worked for"
            # above the answer after idle fetchParts).
            run_ctx = self._run_context(session_id)
            reasoning_part_id = run_ctx.pop("reasoning_part_id", None)
            if reasoning_part_id is not None:
                reasoning_part = await sync_to_async(self.parts.get_by_id)(
                    reasoning_part_id
                )
                if reasoning_part is not None:
                    await sync_to_async(self.parts.mark_state)(
                        reasoning_part, "completed"
                    )
            text_part_id = run_ctx.pop("text_part_id", None)
            if text_part_id is not None:
                text_part = await sync_to_async(self.parts.get_by_id)(text_part_id)
                if text_part is not None:
                    await sync_to_async(self.parts.mark_state)(text_part, "completed")
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="step-finish",
                state="completed",
                meta={
                    "step": event.get("step"),
                    "tokens": tokens,
                    "cost": event.get("cost", 0.0),
                    **context,
                },
            )
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "step_finish": event.get("step"),
                        "cost": event.get("cost", 0.0),
                        "tokens": tokens,
                        **context,
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "tool_started":
            await self.close_stream_parts(session)
            part = await self.tool_calls.record_started(session, assistant, event)
            identity = _tool_socket_identity(event, part, state="running")
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "tool_started": identity["tool"],
                        "title": identity["title"],
                        "call_id": identity["call_id"],
                        "state": identity["state"],
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "tool_completed":
            part = await self.tool_calls.record_finished(
                assistant, event, state="completed"
            )
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "tool_completed": _socket_text(
                            event.get("tool")
                            or ((part.input or {}).get("tool", "") if part else ""),
                            limit=100,
                        ),
                        "title": _socket_text(
                            event.get("title") or (part.title if part else ""),
                            limit=240,
                        ),
                        "call_id": _socket_text(
                            event.get("call_id") or (part.call_id if part else ""),
                            limit=255,
                        ),
                        "state": "completed",
                        **(
                            {"display": dict(part.display or {})}
                            if part is not None
                            else {}
                        ),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id) if part is not None else None,
                },
                workspace_id,
            )
        elif etype == "tool_error":
            part = await self.tool_calls.record_finished(
                assistant, event, state="error"
            )
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "tool_error": "Tool failed",
                        "call_id": _socket_text(event.get("call_id", ""), limit=255),
                        "state": "error",
                        **(
                            {"display": dict(part.display or {})}
                            if part is not None
                            else {}
                        ),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id) if part is not None else None,
                },
                workspace_id,
            )
        elif etype in ("retry_scheduled", "retry_attempt"):
            # Lightweight forward without a DB part, mirroring
            # step_start/step_finish (no persisted part, frontend-only).
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {etype: event.get("attempt")},
                    "step": event.get("step"),
                },
                workspace_id,
            )
        elif etype == "todo_updated":
            await self._emit(
                FRONTEND_EVENT_TODO,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "todos": event.get("todos", []),
                    "step": event.get("step"),
                },
                workspace_id,
            )
        elif etype == "subtask_started":
            # A non-text part opens: close any running text/reasoning stream
            # first so nothing merges across the boundary (text/tool/text
            # stays three rows in observed order).
            await self.close_stream_parts(session)
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="subtask",
                state="running",
                title=str(event.get("description", "")),
                meta={
                    "subtask_id": event.get("subtask_id", ""),
                    "agent": event.get("agent", ""),
                    "child_session_id": event.get("child_session_id", ""),
                    "model": event.get("model", ""),
                    "reasoning_effort": event.get("reasoning_effort", ""),
                },
            )
            self._run_context(session_id).get("subtask_parts", {})[
                str(event.get("subtask_id", ""))
            ] = str(part.id)
            await self._emit(
                FRONTEND_EVENT_SUBTASK_STARTED,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "subtask_id": event.get("subtask_id", ""),
                    "child_session_id": event.get("child_session_id", ""),
                    "agent": event.get("agent", ""),
                    "description": event.get("description", ""),
                    "part_id": str(part.id),
                    "model": event.get("model", ""),
                    "reasoning_effort": event.get("reasoning_effort", ""),
                },
                workspace_id,
            )
        elif etype == "subtask_finished":
            await self._finish_subtask_part(session, assistant, event)
            await self._emit(
                FRONTEND_EVENT_SUBTASK_FINISHED,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "subtask_id": event.get("subtask_id", ""),
                    "child_session_id": event.get("child_session_id", ""),
                    "status": event.get("status", ""),
                    "summary": event.get("summary", ""),
                },
                workspace_id,
            )
        elif etype == "patch":
            await self.close_stream_parts(session)
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="patch",
                state="completed",
                call_id=str(event.get("call_id", "")),
                title=str(event.get("title", "")),
                output=str(event.get("unified_diff", "") or ""),
                meta={
                    "path": event.get("path", ""),
                    "step": event.get("step"),
                    "tool": event.get("tool", ""),
                },
            )
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    # The bounded preview lets clients render the card without
                    # refetching the whole timeline for every file edit.
                    "delta": {
                        "patch": event.get("path", ""),
                        "display": dict(part.display or {}),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "compaction":
            await self.close_stream_parts(session)
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="compaction",
                state="completed",
                title="Session compacted",
                output=str(event.get("summary", "") or ""),
                meta={
                    "auto": bool(event.get("auto", True)),
                    "overflow": bool(event.get("overflow", False)),
                    "tail_start_id": str(event.get("tail_start_id", "") or ""),
                },
            )
            await self._emit(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {"compaction": True},
                    "part_id": str(part.id),
                },
                workspace_id,
            )

    async def _persist_part_updated(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> str | None:
        """Persist text/reasoning and context updates on their running parts.

        Reasoning parts stay step-attributed via ``meta["step"]`` (normal
        LLM reasoning without a step keeps working — ``step`` is simply
        ``None``). ``step_finish`` closes the running text and reasoning
        parts DB-side and resets ``text_part_id`` / ``reasoning_part_id``
        so the next step starts fresh ones (see :meth:`record`).

        A text/reasoning *kind transition* closes the other running stream
        first so text and reasoning never merge across boundaries; a new
        tool/subtask/patch/agent/compaction part also closes both via
        ``close_stream_parts``. Completion alone never closes.
        Returns the persisted ``part_id`` when a text/reasoning delta was
        stored (so live events carry it), else ``None``.
        """
        delta = event.get("delta", {}) or {}
        text = str(delta.get("text", "") or "")
        reasoning = str(delta.get("reasoning", "") or "")
        step = event.get("step")
        key = str(session.id)
        run_ctx = self._run_context(key)
        touched_id: str | None = None
        if isinstance(delta.get("context"), dict):
            part_id = run_ctx.get("step_parts", {}).get(str(step))
            part = (
                await sync_to_async(self.parts.get_by_id)(part_id)
                if part_id is not None
                else None
            )
            if (
                part is not None
                and part.message_id == assistant.id
                and part.type == "step-start"
                and part.state == "running"
            ):
                await sync_to_async(self.parts.mark_state)(
                    part, "running", meta={"context": delta["context"]}
                )
                touched_id = str(part.id)
        if text:
            await self._complete_stream_part(run_ctx, "reasoning_part_id")
            part_id = run_ctx.get("text_part_id")
            if part_id is None:
                part = await sync_to_async(self.parts.create)(
                    message_id=assistant.id,
                    type="text",
                    state="running",
                    meta={"step": step},
                )
                run_ctx["text_part_id"] = str(part.id)
                part_id = str(part.id)
            await sync_to_async(self.messages.append_content)(
                assistant, text, text_part_id=part_id
            )
            touched_id = str(part_id)
        if reasoning:
            await self._complete_stream_part(run_ctx, "text_part_id")
            part_id = run_ctx.get("reasoning_part_id")
            if part_id is None:
                part = await sync_to_async(self.parts.create)(
                    message_id=assistant.id,
                    type="reasoning",
                    state="running",
                    meta={"step": step},
                )
                run_ctx["reasoning_part_id"] = str(part.id)
                part_id = str(part.id)
            await sync_to_async(self.parts.append_output_by_id)(part_id, reasoning)
            touched_id = str(part_id)
        return touched_id

    async def _complete_stream_part(self, run_ctx: dict[str, Any], slot: str) -> None:
        """Complete+clear one running text/reasoning stream slot (best-effort)."""
        part_id = run_ctx.pop(slot, None)
        if part_id is None:
            return
        part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is not None:
            await sync_to_async(self.parts.mark_state)(part, "completed")

    async def close_stream_parts(self, session: HarnessSession) -> None:
        """Close open text/reasoning streams before a non-text part boundary."""
        run_ctx = self._run_context(str(session.id))
        await self._complete_stream_part(run_ctx, "text_part_id")
        await self._complete_stream_part(run_ctx, "reasoning_part_id")

    async def _finish_subtask_part(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> None:
        """Transition the matching subtask part to completed/error."""
        subtask_id = str(event.get("subtask_id", ""))
        status = str(event.get("status", ""))
        part_id = (
            self._run_context(str(session.id)).get("subtask_parts", {}).get(subtask_id)
        )
        part = None
        if part_id is not None:
            part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is None:
            return
        state = "completed" if status == "completed" else "error"
        await sync_to_async(self.parts.mark_state)(
            part,
            state,
            meta={
                "subtask_id": subtask_id,
                "agent": event.get("agent", ""),
                "status": status,
                "child_session_id": event.get("child_session_id", ""),
            },
        )

    async def settle_open_stream_parts(self, assistant: HarnessMessage) -> None:
        """Mark leftover running text/reasoning parts completed.

        The runner closes these in-memory on ``step_finish``, but the DB
        rows stay ``running`` unless we persist the completed state.
        """
        open_parts = await sync_to_async(
            lambda: self.parts.list_open_for_message(
                assistant.id, types=("text", "reasoning")
            )
        )()
        for part in open_parts:
            await sync_to_async(self.parts.mark_state)(part, "completed")

    async def fail_open_parts(
        self,
        assistant: HarnessMessage,
        *,
        state: str,
        output: str,
        session: HarnessSession | None = None,
    ) -> None:
        """Mark pending/running parts of *assistant* as failed/aborted.

        Text and reasoning keep their streamed content and are marked
        completed — aborting a run does not abort a thought that already
        happened. Tool/subtask parts become *state* and keep any partial
        output already written. When *session* is set, open tool and
        subtask parts are also published so the timeline leaves ``running``
        without waiting for a later refetch.
        """
        open_parts = await sync_to_async(self.parts.list_open_for_message)(assistant.id)
        for part in open_parts:
            if part.type in ("text", "reasoning"):
                await sync_to_async(self.parts.mark_state)(part, "completed")
                continue
            stored = part.output or output
            await sync_to_async(self.parts.mark_state)(part, state, output=stored)
            if session is not None and part.type in ("tool", "subtask"):
                await self._emit_failed_open_part(
                    session, assistant, part, output=stored
                )

    async def _emit_failed_open_part(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        part: HarnessPart,
        *,
        output: str,
    ) -> None:
        """Publish a terminal tool or subtask part after a user abort."""
        workspace_id = str(session.workspace_id)
        session_id = str(session.id)
        message_id = str(assistant.id)
        if part.type == "subtask":
            meta = part.meta or {}
            await self._emit(
                FRONTEND_EVENT_SUBTASK_FINISHED,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "subtask_id": str(meta.get("subtask_id", "")),
                    "child_session_id": str(meta.get("child_session_id", "")),
                    "agent": str(meta.get("agent", "")),
                    "status": "aborted",
                    "summary": _socket_text(output, limit=240),
                },
                workspace_id,
            )
            return
        await self._emit(
            FRONTEND_EVENT_PART,
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "message_id": message_id,
                "delta": {
                    "tool_error": _socket_text(output, limit=240) or "Tool failed",
                    "title": _socket_text(part.title or "", limit=240),
                    "call_id": _socket_text(part.call_id or "", limit=255),
                    "state": "error",
                },
                "part_id": str(part.id),
            },
            workspace_id,
        )

    async def append_tail_text(
        self, session: HarnessSession, assistant: HarnessMessage, remainder: str
    ) -> None:
        """Persist the non-streamed tail remainder as its own text part."""
        run_ctx = self._run_context(str(session.id))
        part_id = run_ctx.get("text_part_id")
        part = None
        if part_id is not None:
            part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is None:
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="text",
                state="running",
                meta={"step": None, "tail": True},
            )
            run_ctx["text_part_id"] = str(part.id)
        await sync_to_async(self.parts.append_output)(part, remainder)
