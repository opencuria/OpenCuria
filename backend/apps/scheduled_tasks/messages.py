"""Delivery of scheduled chat messages by the scheduled-work scheduler.

The harness owns what a scheduled message is and how it is sent; this module
owns *when*: it fences due messages, resumes their workspace like a scheduled
task does, and records every outcome on the message.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog
from asgiref.sync import sync_to_async
from django.utils import timezone

from common.exceptions import ConflictError, ServiceError

from .services import ScheduledTaskService

log = structlog.get_logger(__name__)

#: A message found later than this after its time was missed (e.g. the
#: execution worker was down); sending stale instructions could do harm.
MISSED_AFTER = timedelta(minutes=15)

MISSED_ERROR = "Not sent at the scheduled time because the server was unavailable."

ClaimFn = Callable[[Any], None]


class ScheduledMessages(Protocol):
    """The harness's scheduled-message service, as reached via its facade."""

    INTERRUPTED_ERROR: str

    def claim_due(self, now: datetime | None = None) -> list[Any]: ...

    async def deliver(self, message: Any, *, harness: Any) -> None: ...

    async def complete(self, message: Any) -> None: ...

    async def fail(self, message: Any, error: str) -> None: ...

    async def recover_stale(self, now: datetime | None = None) -> int: ...

    async def publish(self, session_id: Any) -> None: ...

    @staticmethod
    def delivery_error(exc: BaseException) -> str: ...


class ScheduledMessageDispatcher:
    """Send due chat messages, resuming their workspace first."""

    def __init__(
        self,
        tasks: ScheduledTaskService,
        *,
        messages: ScheduledMessages | None = None,
        missed_after: timedelta = MISSED_AFTER,
    ) -> None:
        self.tasks = tasks
        self._messages = messages
        self.missed_after = missed_after

    @property
    def messages(self) -> ScheduledMessages:
        """The harness service, resolved lazily like the harness itself."""
        return self._messages or self.tasks._harness().scheduled_messages

    async def dispatch_due(
        self, *, now: datetime | None = None, on_claim: ClaimFn | None = None
    ) -> None:
        """Fence due messages and deliver them (or hand them to *on_claim*)."""
        current = now or timezone.now()
        await self.messages.recover_stale(now=current)
        due = await sync_to_async(self.messages.claim_due)(current)
        for message in due:
            await self.messages.publish(message.session_id)
            if on_claim is not None:
                on_claim(message)
            else:
                await self.deliver(message, now=current)

    async def deliver(self, message: Any, *, now: datetime | None = None) -> None:
        """Deliver one fenced message; a failure stays visible with its reason."""
        current = now or timezone.now()
        try:
            if current - message.send_at > self.missed_after:
                await self.messages.fail(message, MISSED_ERROR)
                return
            session = message.session
            try:
                skip = await self.tasks.prepare_workspace(
                    workspace_id=session.workspace_id,
                    owner_id=message.created_by_id,
                    organization_id=session.organization_id,
                )
            except ConflictError as exc:
                skip = await self.tasks.classify_workspace_conflict(
                    session.workspace_id, exc
                )
            if skip is not None:
                await self.messages.fail(message, skip.error)
                return
            await self.messages.deliver(message, harness=self.tasks._harness())
        except asyncio.CancelledError:
            await asyncio.shield(
                self.messages.fail(message, self.messages.INTERRUPTED_ERROR)
            )
            raise
        except Exception as exc:
            # Refusals (no provider, workspace busy, …) are recorded on the
            # message; only unexpected failures need a traceback.
            if not isinstance(exc, (ServiceError, ValueError)):
                log.exception(
                    "scheduled_message_delivery_failed",
                    scheduled_message_id=str(message.id),
                    session_id=str(message.session_id),
                )
            await self.messages.fail(message, self.messages.delivery_error(exc))
            return
        await self.messages.complete(message)
        try:
            await self.tasks._runner().workspace_lifecycle.touch_workspace_activity(
                message.session.workspace_id
            )
        except Exception:
            log.exception(
                "scheduled_message_workspace_activity_touch_failed",
                scheduled_message_id=str(message.id),
            )
