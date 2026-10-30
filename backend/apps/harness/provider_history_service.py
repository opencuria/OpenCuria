"""Rebuild provider transcripts from persisted session messages and parts.

This service owns checkpoint selection, bounded loading and ordered replay.
Repositories own the ORM reads; callers own run admission and task scheduling.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from itertools import islice
from typing import Any

from asgiref.sync import sync_to_async

from .compaction import CHECKPOINT_PREFIX
from .media.attachments import (
    build_tool_message_content,
    select_persisted_tool_attachments,
)
from .models import HarnessMessage, HarnessPart, HarnessSession
from .providers.base import LLMMessage
from .repositories import HarnessMessageRepository, HarnessPartRepository
from .tools.truncate import truncate_tool_output

# Bound raw tool-output retention while amortizing queries and executor hops.
HISTORY_PART_BATCH_SIZE = 8


def _tool_part_attachments(part: HarnessPart) -> list[dict[str, Any]]:
    """Return persisted attachment dicts from a tool ``HarnessPart``."""
    meta = getattr(part, "meta", None) or {}
    if not isinstance(meta, dict):
        return []
    attachments = meta.get("attachments", [])
    if not isinstance(attachments, list):
        return []
    return [item for item in attachments if isinstance(item, dict)]


class ProviderHistoryService:
    """Project persisted conversations into provider messages without running them."""

    def __init__(
        self,
        *,
        messages: type[HarnessMessageRepository] | None = None,
        parts: type[HarnessPartRepository] | None = None,
    ) -> None:
        """Inject the same persistence ports used by the session lifecycle."""
        self.messages = messages or HarnessMessageRepository
        self.parts = parts or HarnessPartRepository

    async def build(
        self,
        session: HarnessSession,
        *,
        exclude_message_id: uuid.UUID | None = None,
        exclude_user_message_id: uuid.UUID | None = None,
        exclude_message_ids: list[uuid.UUID] | None = None,
    ) -> list[LLMMessage]:
        """Build provider history from persisted messages and parts.

        When a completed compaction part exists, only the checkpoint summary
        plus messages from ``tail_start_id`` onward are sent to the provider
        (OpenCode ``filterCompacted`` equivalent). Full history stays in the DB.
        """
        excluded_ids = {
            message_id
            for message_id in (
                exclude_message_id,
                exclude_user_message_id,
                *(exclude_message_ids or []),
            )
            if message_id is not None
        }
        # Resolve compaction before loading message bodies, using only scoped
        # positions. The unique (session, position) key defines the same suffix
        # as slicing the complete ordered transcript.
        checkpoint = await sync_to_async(self.parts.get_latest_completed_compaction)(
            session.id, exclude_message_ids=excluded_ids
        )
        checkpoint_summary = ""
        compaction_msg_id: uuid.UUID | None = None
        from_position = 0
        tail_id: uuid.UUID | None = None
        if checkpoint is not None:
            compaction_msg_id = checkpoint.message_id
            checkpoint_summary = str(checkpoint.output or "").strip()
            from_position = checkpoint.message_position
            raw_tail = str(
                (checkpoint.meta or {}).get("tail_start_id", "") or ""
            ).strip()
            try:
                tail_id = uuid.UUID(raw_tail)
            except ValueError:
                tail_id = None
        stored = await sync_to_async(self.messages.list_for_history)(
            session.id,
            from_position=from_position,
            from_message_id=tail_id,
            exclude_message_ids=excluded_ids,
        )
        history: list[LLMMessage] = []
        if checkpoint_summary:
            history.append(
                LLMMessage(
                    role="user",
                    content=f"{CHECKPOINT_PREFIX}\n{checkpoint_summary}",
                )
            )
        assistant_ids = iter(
            message.id for message in stored if message.role == "assistant"
        )
        parts_by_message: dict[uuid.UUID, list[HarnessPart]] = {}
        for message in stored:
            if message.role == "user":
                history.append(
                    LLMMessage(
                        role="user",
                        content=message.content or "",
                        message_id=str(message.id),
                    )
                )
                continue
            if message.role != "assistant":
                continue
            if not parts_by_message:
                parts_by_message = await sync_to_async(
                    self.parts.history_parts_by_message
                )(list(islice(assistant_ids, HISTORY_PART_BATCH_SIZE)))
            else:
                # Keep cancellation and scheduling points between assistant replays.
                await asyncio.sleep(0)
            parts = parts_by_message.pop(message.id)
            tool_parts = [
                part
                for part in parts
                if part.type == "tool"
                and part.state in ("completed", "error")
                and part.call_id
            ]
            assistant_content = self._assistant_history_content(
                message,
                parts,
                summary=checkpoint_summary if message.id == compaction_msg_id else "",
            )
            has_steers = any(part.type == "steer" and part.output for part in parts)
            if not tool_parts and not has_steers:
                history.append(
                    LLMMessage(
                        role="assistant",
                        content=assistant_content,
                        message_id=str(message.id),
                    )
                )
                continue
            # Rebuild conversational turns from ordered parts, not from the
            # flattened message.content. A response can contain text, tools,
            # more text, and more tools; provider replay must keep these
            # segments in the same order as the persisted timeline.
            tool_ids = {part.id for part in tool_parts}
            has_text_parts = any(part.type == "text" and part.output for part in parts)
            text = ""
            pending_calls: list[HarnessPart] = []
            if not has_text_parts and assistant_content:
                # Legacy messages without text parts only have a flattened
                # content field; place it before tools as the best effort.
                text = assistant_content

            def flush_segment() -> None:
                nonlocal text, pending_calls
                if not text and not pending_calls:
                    return
                calls: list[dict[str, Any]] = []
                for tool_part in pending_calls:
                    tool_input = dict(tool_part.input or {})
                    tool_name = str(
                        tool_input.get("tool", "") or tool_part.title or "tool"
                    )
                    raw_args = tool_input.get("arguments", "")
                    if isinstance(raw_args, dict):
                        raw_args = json.dumps(raw_args)
                    calls.append(
                        {
                            "id": tool_part.call_id,
                            "name": tool_name,
                            "arguments": str(raw_args or "{}"),
                        }
                    )
                history.append(
                    LLMMessage(
                        role="assistant",
                        content=text or None,
                        tool_calls=calls,
                        message_id=str(message.id),
                    )
                )
                for tool_part in pending_calls:
                    clipped = truncate_tool_output(tool_part.output or "")
                    attachments = select_persisted_tool_attachments(
                        _tool_part_attachments(tool_part)
                    )
                    history.append(
                        LLMMessage(
                            role="tool",
                            content=build_tool_message_content(
                                clipped.content, attachments
                            ),
                            tool_call_id=tool_part.call_id,
                        )
                    )
                text = ""
                pending_calls = []

            for part in parts:
                if part.type == "text" and part.output:
                    if pending_calls:
                        flush_segment()
                    text += part.output
                elif part.type == "steer" and part.output:
                    # A message the user sent while the turn was running sits
                    # between the answer segments that surround it.
                    flush_segment()
                    history.append(
                        LLMMessage(
                            role="user",
                            content=part.output,
                            message_id=str(message.id),
                        )
                    )
                elif part.id in tool_ids:
                    pending_calls.append(part)
            flush_segment()
        return history

    @staticmethod
    def _assistant_history_content(
        message: HarnessMessage,
        parts: list[HarnessPart],
        *,
        summary: str = "",
    ) -> str | None:
        """Resolve assistant text for history without the compaction summary."""
        text_output = "".join(
            part.output for part in parts if part.type == "text" and part.output
        )
        if text_output:
            return text_output
        content = (message.content or "").strip()
        if not content:
            return None
        if summary and content == summary.strip():
            return None
        return message.content
