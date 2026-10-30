"""Keep one persisted tool part per tool call across its stream events.

Engines announce a call with ``tool_queued`` (possibly under a provisional
call id), flip it with ``tool_started`` and settle it with ``tool_completed``
or ``tool_error``. ``ToolCallParts`` matches every event to the same
``HarnessPart`` row, rebinding provisional ids instead of creating duplicates,
and indexes the rows by call id in the run context's ``tool_parts``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from asgiref.sync import sync_to_async

from .media.attachments import select_persisted_tool_attachments
from .models import HarnessMessage, HarnessPart, HarnessSession
from .repositories import HarnessPartRepository


def _event_tool_attachments(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sanitizable attachment dicts from a tool_completed event."""
    attachments = event.get("attachments", [])
    if not isinstance(attachments, list):
        return []
    return [item for item in attachments if isinstance(item, dict)]


class ToolCallParts:
    """Persist tool-call lifecycle events onto one part per call."""

    def __init__(
        self,
        *,
        parts: type[HarnessPartRepository],
        run_context: Callable[[str], dict[str, Any]],
    ) -> None:
        """Bind the part repository and the live run-context lookup."""
        self.parts = parts
        self._run_context = run_context

    async def record_queued(
        self, session: HarnessSession, assistant: HarnessMessage, event: dict[str, Any]
    ) -> HarnessPart:
        """Persist one tool card per non-empty call ID without regressing state.

        Some engines announce a call once with a provisional empty payload and
        again after arguments are available. The later event enriches the same
        pending card; it never creates an orphan card or reopens a terminal one.
        Calls with no ID retain the legacy create-per-event behavior.
        """
        call_id = str(event.get("call_id", "") or "")
        run_ctx = self._run_context(str(session.id))
        tool_parts = run_ctx.setdefault("tool_parts", {})
        part = None
        if call_id:
            part_id = tool_parts.get(call_id)
            if part_id:
                part = await sync_to_async(
                    lambda: self.parts.find_by_call_id(
                        assistant.id, call_id, type="tool", part_id=part_id
                    )
                )()
            if part is None:
                part = await sync_to_async(
                    lambda: self.parts.find_by_call_id(
                        assistant.id, call_id, type="tool"
                    )
                )()

        incoming_tool = str(event.get("tool", "") or "")
        incoming_arguments = event.get("arguments", "")
        if part is not None:
            if call_id:
                tool_parts[call_id] = str(part.id)
            if part.state not in ("pending", "running"):
                return part
            current_input = dict(part.input or {})
            merged_input = dict(current_input)
            if incoming_tool:
                merged_input["tool"] = incoming_tool
            if incoming_arguments not in (None, "", {}, []):
                merged_input["arguments"] = incoming_arguments
            meta = dict(part.meta or {})
            if event.get("step") is not None:
                meta["step"] = event.get("step")
            await sync_to_async(self.parts.set_input)(part, merged_input)
            title = str(event.get("title", "") or part.title or "")
            if title != part.title or meta != dict(part.meta or {}):
                part = await sync_to_async(self.parts.mark_state)(
                    part,
                    part.state,
                    title=title,
                    meta=meta,
                )
            if call_id:
                tool_parts[call_id] = str(part.id)
            return part

        part = await sync_to_async(self.parts.create)(
            message_id=assistant.id,
            type="tool",
            state="pending",
            call_id=call_id,
            title=str(event.get("title", "") or ""),
            input={
                "tool": incoming_tool,
                "arguments": incoming_arguments or "",
            },
            meta={"step": event.get("step")},
        )
        tool_parts[call_id] = str(part.id)
        return part

    async def record_started(
        self, session: HarnessSession, assistant: HarnessMessage, event: dict[str, Any]
    ) -> HarnessPart:
        """Update-or-create the tool part for ``tool_started`` (running).

        The normal path updates the ``tool_queued`` row for the same
        ``call_id`` to running and re-emits ``delta.tool_started`` with the
        *same* ``part_id`` (frontend dedupes). When the queued ``call_id``
        was provisional, ``queued_id`` identifies the part to rebind to the
        final ``call_id``. Without any queued row this falls back to
        creating the part here (legacy runner path).
        """
        call_id = str(event.get("call_id", ""))
        step = event.get("step")
        run_ctx = self._run_context(str(session.id))
        part = await self._lookup_tool_part(run_ctx, assistant, call_id)
        queued_id = str(event.get("queued_id", "") or "")
        if part is None and queued_id:
            part = await self._lookup_tool_part(run_ctx, assistant, queued_id)
        if part is None:
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="tool",
                state="running",
                call_id=call_id,
                title=str(event.get("title", "")),
                input={
                    "tool": event.get("tool", ""),
                    "arguments": event.get("arguments", ""),
                },
                meta={"step": step},
            )
            run_ctx.get("tool_parts", {})[call_id] = str(part.id)
            return part
        if call_id and part.call_id != call_id:
            rebased_meta = dict(part.meta or {})
            rebased_meta["step"] = step
            await sync_to_async(self.parts.rebind_call_id)(
                part, call_id, meta=rebased_meta
            )
            # Rebind the run-context index to the real call id.
            tool_parts = run_ctx.get("tool_parts", {})
            for old_key, old_part_id in list(tool_parts.items()):
                if old_part_id == str(part.id) and old_key != call_id:
                    del tool_parts[old_key]
            tool_parts[call_id] = str(part.id)
        await sync_to_async(self.parts.mark_state)(
            part,
            "running",
            title=str(event.get("title", "") or part.title),
            meta={"step": step},
        )
        run_ctx.get("tool_parts", {})[call_id] = str(part.id)
        # Refresh title/arguments observed at start (queued row may only
        # have had provisional values).
        part.title = str(event.get("title", "") or part.title)
        await sync_to_async(self.parts.set_input)(
            part,
            {
                "tool": event.get("tool", "") or (part.input or {}).get("tool", ""),
                "arguments": event.get("arguments", "")
                or (part.input or {}).get("arguments", ""),
            },
        )
        return part

    async def _lookup_tool_part(
        self, run_ctx: dict[str, Any], assistant: HarnessMessage, call_id: str
    ) -> HarnessPart | None:
        """Return the tool part for *call_id* (run ctx first, then DB)."""
        part = None
        part_id = (run_ctx.get("tool_parts", {}) or {}).get(call_id)
        if part_id is not None:
            part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is None and call_id:
            part = await sync_to_async(self.parts.find_by_call_id)(
                assistant.id, call_id
            )
        return part

    async def record_finished(
        self, assistant: HarnessMessage, event: dict[str, Any], *, state: str
    ) -> HarnessPart:
        """Transition the matching tool part to completed/error.

        ``tool_completed`` events persist sanitized attachments into
        ``HarnessPart.meta["attachments"]`` and full output in ``output``;
        image/PDF bytes survive restarts and are rehydrated in
        ``ProviderHistoryService.build``. These full details never enter
        socket deltas.
        ``mark_state`` merges ``meta`` (existing keys like ``step`` from
        the ``tool_started`` part are preserved). Tool *results* never
        create a new part: completion only updates the queued/started row;
        the no-queued fallback creates the row here for legacy runners.
        """
        call_id = str(event.get("call_id", ""))
        output = str(event.get("output", "") or event.get("error", "") or "")
        part_id = (
            self._run_context(str(assistant.session_id))
            .get("tool_parts", {})
            .get(call_id)
        )
        part = None
        if part_id is not None:
            part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is None and call_id:
            part = await sync_to_async(self.parts.find_by_call_id)(
                assistant.id, call_id
            )
        queued_id = str(event.get("queued_id", "") or "")
        if part is None and queued_id:
            part = await self._lookup_tool_part(
                self._run_context(str(assistant.session_id)), assistant, queued_id
            )
            if part is not None and call_id != part.call_id:
                await sync_to_async(self.parts.rebind_call_id)(part, call_id)
                self._run_context(str(assistant.session_id)).get("tool_parts", {})[
                    call_id
                ] = str(part.id)
        if part is None:
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="tool",
                state="pending",
                call_id=call_id,
                title=str(event.get("tool", "")),
            )
        meta: dict[str, Any] | None = None
        if state == "completed":
            attachments = select_persisted_tool_attachments(
                _event_tool_attachments(event)
            )
            if attachments:
                meta = {"attachments": attachments}
        await sync_to_async(self.parts.mark_state)(
            part, state, output=output or part.output, meta=meta
        )
        return part
