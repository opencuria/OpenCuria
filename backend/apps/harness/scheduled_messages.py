"""Messages a user schedules for a conversation, and their delivery.

A scheduled message is sent exactly like a message typed at its send time: it
starts a new turn when the conversation is idle and steers the running turn
otherwise. The execution worker's scheduler delivers due messages; "send now"
delivers from the API process through the same harness facade, so both paths
share :meth:`ScheduledMessageService.deliver`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any, NoReturn, Protocol

import structlog
from asgiref.sync import sync_to_async
from django.utils import timezone

from common import realtime
from common.exceptions import ConflictError, NotFoundError, ServiceError

from .conversation_service import ConversationService
from .models import HarnessSession
from .scheduled_message_models import HarnessScheduledMessage
from .scheduled_message_repository import ScheduledMessageRepository
from .session_access_service import SessionAccessService
from .session_settings_service import SessionSettingsService
from .steering import MAX_STEER_CHARS, NO_ACTIVE_RUN_CODE

log = structlog.get_logger(__name__)

FRONTEND_EVENT_SCHEDULED_MESSAGES = "harness.scheduled_messages"

#: Upper bound for messages waiting in one conversation.
MAX_SCHEDULED_PER_SESSION = 20

#: How far ahead a message may be scheduled.
MAX_SCHEDULE_AHEAD = timedelta(days=366)

#: A delivery fenced longer than this lost its owner (process restart).
STALE_DELIVERY_AFTER = timedelta(minutes=15)

INTERRUPTED_ERROR = "Sending was interrupted. Check the chat before sending it again."

EmitFn = Callable[[str, dict[str, Any], str], Awaitable[None]]


class HarnessDelivery(Protocol):
    """The harness operations a delivery needs (local or remote facade)."""

    def is_running(self, session_id: uuid.UUID) -> bool: ...

    async def steer_run(
        self, session: Any, prompt: str, *, user_id: int | None = None
    ) -> Any: ...

    async def start_run(self, session: Any, prompt: str, **kwargs: Any) -> Any: ...


def scheduled_message_payload(row: HarnessScheduledMessage) -> dict[str, Any]:
    """Return the wire shape shared by REST, timeline and Socket.IO."""
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "content": row.prompt,
        "send_at": row.send_at.isoformat(),
        "status": row.status,
        "error": row.error or "",
        "skill_ids": [str(skill_id) for skill_id in (row.skill_ids or [])],
        "created_at": row.created_at.isoformat(),
    }


def _consume_outcome(task: asyncio.Future) -> None:
    """Mark a detached delivery's failure as observed (it is already recorded)."""
    if not task.cancelled():
        task.exception()


def delivery_error(exc: BaseException) -> str:
    """Return a short, user-facing reason for a failed delivery."""
    if isinstance(exc, ServiceError):
        text = exc.message
    elif isinstance(exc, ValueError):
        text = str(exc)
    else:
        text = ""
    return (text or "The message could not be sent.")[:500]


class ScheduledMessageService:
    """Validate, store and deliver scheduled conversation messages."""

    #: Reason recorded when a delivery could not finish (process stop).
    INTERRUPTED_ERROR = INTERRUPTED_ERROR

    def __init__(
        self,
        *,
        repository: type[ScheduledMessageRepository] | None = None,
        conversations: ConversationService | None = None,
        session_settings: SessionSettingsService | None = None,
        emit: EmitFn | None = None,
    ) -> None:
        self.repository = repository or ScheduledMessageRepository
        self.conversations = conversations or ConversationService()
        self.session_settings = session_settings or SessionSettingsService()
        self._emit = emit

    # -- reads ---------------------------------------------------------------

    def snapshot(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return the session's scheduled messages in wire shape."""
        return [
            scheduled_message_payload(row)
            for row in self.repository.list_for_session(session_id)
        ]

    # -- user actions --------------------------------------------------------

    def create(
        self,
        session: HarnessSession,
        *,
        user_id: int,
        prompt: str,
        send_at: datetime,
        mode: str = "",
        model: str = "",
        reasoning_effort: str = "",
        skill_ids: list[str] | None = None,
        now: datetime | None = None,
    ) -> HarnessScheduledMessage:
        """Schedule *prompt* for *session*.

        Raises:
            ValueError: Invalid text, time, mode or skills, or a subagent chat.
            AuthenticationError: Another user's external-engine chat.
            ConflictError: ``scheduled_messages_full`` at the per-chat limit.
        """
        self._require_promptable(session, user_id)
        text = (prompt or "").strip()
        if not text:
            raise ValueError("prompt must not be empty")
        if len(text) > MAX_STEER_CHARS:
            raise ValueError(f"prompt exceeds {MAX_STEER_CHARS} characters")
        normalized_mode = (mode or "").strip().lower()
        if normalized_mode not in {"", "plan", "build"}:
            raise ValueError("mode must be plan or build")
        moment = self._validate_send_at(send_at, now=now)
        skills = self._validate_skill_ids(skill_ids)
        if self.repository.count_for_session(session.id) >= MAX_SCHEDULED_PER_SESSION:
            raise ConflictError(
                f"A chat can hold at most {MAX_SCHEDULED_PER_SESSION} scheduled "
                "messages",
                code="scheduled_messages_full",
            )
        row = self.repository.create(
            session_id=session.id,
            created_by_id=user_id,
            prompt=text,
            mode=normalized_mode,
            model=(model or "").strip(),
            reasoning_effort=(reasoning_effort or "").strip(),
            skill_ids=skills,
            send_at=moment,
        )
        log.info(
            "harness_message_scheduled",
            session_id=str(session.id),
            scheduled_message_id=str(row.id),
        )
        return row

    def reschedule(
        self,
        session: HarnessSession,
        message_id: uuid.UUID,
        *,
        send_at: datetime,
        now: datetime | None = None,
    ) -> HarnessScheduledMessage:
        """Move a pending or failed message to *send_at*."""
        moment = self._validate_send_at(send_at, now=now)
        row = self.repository.reschedule(session.id, message_id, moment)
        if row is None:
            self._raise_unavailable(session.id, message_id)
        return row

    def withdraw(
        self, session: HarnessSession, message_id: uuid.UUID
    ) -> HarnessScheduledMessage:
        """Remove a pending or failed message and return it (e.g. to edit it)."""
        row = self.repository.withdraw(session.id, message_id)
        if row is None:
            self._raise_unavailable(session.id, message_id)
        return row

    async def send_now(
        self,
        session: HarnessSession,
        message_id: uuid.UUID,
        *,
        harness: HarnessDelivery,
    ) -> None:
        """Deliver a pending or failed message immediately.

        The delivery is shielded from request cancellation: once fenced, it
        always ends delivered (row removed) or failed (row kept with reason).
        """
        row = await sync_to_async(self.repository.claim)(
            session.id, message_id, timezone.now()
        )
        if row is None:
            await sync_to_async(self._raise_unavailable)(session.id, message_id)
        await self.publish(session.id)
        settle = asyncio.ensure_future(self._settle(row, harness))
        # A cancelled request leaves the outcome to the shielded task alone.
        settle.add_done_callback(_consume_outcome)
        await asyncio.shield(settle)

    # -- delivery ------------------------------------------------------------

    def claim_due(
        self, now: datetime | None = None, *, limit: int = 20
    ) -> list[HarnessScheduledMessage]:
        """Fence messages whose send time has come."""
        return self.repository.claim_due(now or timezone.now(), limit)

    async def deliver(
        self, message: HarnessScheduledMessage, *, harness: HarnessDelivery
    ) -> None:
        """Send *message* as if its author typed it now.

        A running turn is steered; an idle conversation starts a turn with the
        message's run options. When a run starts or ends in between, the other
        path is taken once.
        """
        session = await sync_to_async(self.conversations.get_session)(
            message.session_id
        )
        self._require_promptable(session, message.created_by_id)
        for attempt in range(2):
            if await sync_to_async(harness.is_running)(session.id):
                try:
                    await harness.steer_run(
                        session, message.prompt, user_id=message.created_by_id
                    )
                    return
                except ConflictError as exc:
                    if exc.code != NO_ACTIVE_RUN_CODE:
                        raise
            session = await sync_to_async(self._apply_run_options)(message)
            try:
                await harness.start_run(
                    session,
                    message.prompt,
                    organization_id=session.organization_id,
                    workspace_id=str(session.workspace_id),
                    user_id=message.created_by_id,
                    skill_ids=list(message.skill_ids or []) or None,
                )
                return
            except ConflictError:
                if attempt or not await sync_to_async(harness.is_running)(session.id):
                    raise

    async def complete(self, message: HarnessScheduledMessage) -> None:
        """Forget a delivered message and publish the session's list."""
        await sync_to_async(self.repository.delete)(message.id)
        log.info(
            "harness_scheduled_message_sent",
            session_id=str(message.session_id),
            scheduled_message_id=str(message.id),
        )
        await self.publish(message.session_id)

    async def fail(self, message: HarnessScheduledMessage, error: str) -> None:
        """Keep an undelivered message with its reason and publish the list."""
        await sync_to_async(self.repository.fail)(message.id, error[:500])
        log.warning(
            "harness_scheduled_message_failed",
            session_id=str(message.session_id),
            scheduled_message_id=str(message.id),
        )
        await self.publish(message.session_id)

    @staticmethod
    def delivery_error(exc: BaseException) -> str:
        """Return the user-facing reason recorded for a failed delivery."""
        return delivery_error(exc)

    async def recover_stale(self, now: datetime | None = None) -> int:
        """Fail deliveries whose owner stopped before settling them."""
        before = (now or timezone.now()) - STALE_DELIVERY_AFTER
        session_ids = await sync_to_async(self.repository.fail_stale)(
            before, INTERRUPTED_ERROR
        )
        for session_id in session_ids:
            await self.publish(session_id)
        return len(session_ids)

    # -- events --------------------------------------------------------------

    async def publish(self, session_id: uuid.UUID) -> None:
        """Send the session's current list to its workspace (never raises)."""
        try:
            session, messages = await sync_to_async(self._publication)(session_id)
        except NotFoundError:
            return  # Deleted together with its messages.
        workspace_id = str(session.workspace_id)
        data = {
            "workspace_id": workspace_id,
            "session_id": str(session.id),
            "messages": messages,
        }
        try:
            if self._emit is not None:
                await self._emit(FRONTEND_EVENT_SCHEDULED_MESSAGES, data, workspace_id)
            else:
                await realtime.emit_to_frontend(
                    FRONTEND_EVENT_SCHEDULED_MESSAGES, data, workspace_id
                )
        except Exception:
            log.warning(
                "harness_frontend_emit_failed",
                event_name=FRONTEND_EVENT_SCHEDULED_MESSAGES,
            )

    # -- internals -----------------------------------------------------------

    def _publication(
        self, session_id: uuid.UUID
    ) -> tuple[HarnessSession, list[dict[str, Any]]]:
        return self.conversations.get_session(session_id), self.snapshot(session_id)

    async def _settle(
        self, message: HarnessScheduledMessage, harness: HarnessDelivery
    ) -> None:
        try:
            await self.deliver(message, harness=harness)
        except BaseException as exc:
            await self.fail(message, delivery_error(exc))
            raise
        await self.complete(message)

    def _apply_run_options(self, message: HarnessScheduledMessage) -> HarnessSession:
        """Apply the message's run options like a regular follow-up does."""
        session = self.conversations.get_session(message.session_id)
        if message.mode:
            session = self.session_settings.set_mode(session.id, message.mode)
        if message.model:
            session = self.session_settings.set_model(session.id, message.model)
        if message.reasoning_effort:
            session = self.session_settings.set_reasoning_effort(
                session.id, message.reasoning_effort
            )
        return session

    @staticmethod
    def _require_promptable(session: HarnessSession, user_id: int | None) -> None:
        SessionAccessService.require_user_promptable(session.parent_id)
        SessionAccessService.require_external_owner(
            engine_id=getattr(session, "harness_id", "native") or "native",
            owner_id=getattr(session, "created_by_id", None),
            user_id=user_id,
        )

    @staticmethod
    def _validate_send_at(value: datetime, *, now: datetime | None) -> datetime:
        if not isinstance(value, datetime):
            raise ValueError("send_at must be a date and time")
        moment = (
            value
            if timezone.is_aware(value)
            else timezone.make_aware(value, dt_timezone.utc)
        )
        current = now or timezone.now()
        if moment <= current:
            raise ValueError("send_at must be in the future")
        if moment > current + MAX_SCHEDULE_AHEAD:
            raise ValueError("send_at must be within a year")
        return moment.replace(microsecond=0)

    @staticmethod
    def _validate_skill_ids(value: list[str] | None) -> list[str]:
        skills: list[str] = []
        for skill_id in value or []:
            try:
                normalized = str(uuid.UUID(str(skill_id)))
            except ValueError as exc:
                raise ValueError(f"Invalid skill_id: {skill_id}") from exc
            if normalized not in skills:
                skills.append(normalized)
        return skills

    def _raise_unavailable(
        self, session_id: uuid.UUID, message_id: uuid.UUID
    ) -> NoReturn:
        """Explain why an open-state transition did not apply."""
        if self.repository.get_for_session(session_id, message_id) is None:
            raise NotFoundError("ScheduledMessage", str(message_id))
        raise ConflictError(
            "The message is being sent", code="scheduled_message_sending"
        )
