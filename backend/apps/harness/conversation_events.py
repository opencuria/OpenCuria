"""Publish conversation-feed invalidations without live run orchestration."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from asgiref.sync import async_to_sync

from apps.runners.services.infra import realtime

FRONTEND_EVENT_CONVERSATIONS_CHANGED = "harness.conversations_changed"

EmitFn = Callable[[str, dict[str, Any], str], Awaitable[None]]

log = structlog.get_logger(__name__)


class ConversationEvents:
    """Notify workspace subscribers when saved conversation state changes."""

    def __init__(self, *, emit: EmitFn | None = None) -> None:
        """Use frontend fan-out by default, or an injected event publisher."""
        self._emit = emit

    async def emit(self, workspace_id: uuid.UUID | str) -> None:
        """Publish one best-effort invalidation to the affected workspace."""
        workspace = str(workspace_id)
        data = {"workspace_id": workspace}
        try:
            if self._emit is not None:
                await self._emit(FRONTEND_EVENT_CONVERSATIONS_CHANGED, data, workspace)
            else:
                await realtime.emit_to_frontend(
                    FRONTEND_EVENT_CONVERSATIONS_CHANGED, data, workspace
                )
        except Exception:
            log.warning(
                "harness_frontend_emit_failed",
                event_name=FRONTEND_EVENT_CONVERSATIONS_CHANGED,
            )

    def emit_sync(self, workspace_id: uuid.UUID | str) -> None:
        """Bridge sync views/worker threads, or schedule on the caller's loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            async_to_sync(self.emit)(workspace_id)
        else:
            loop.create_task(self.emit(workspace_id))
