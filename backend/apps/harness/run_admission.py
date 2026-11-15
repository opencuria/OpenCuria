"""Persistent admission, task ownership transfer and startup rollback."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import anyio
import structlog
from asgiref.sync import sync_to_async

from common.exceptions import ConflictError, NotFoundError

from .models import (
    HarnessMessage,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
)
from .provider_account_events import (
    register_revocation_listener,
    unregister_revocation_listener,
)
from .provider_history_service import ProviderHistoryService
from .provider_turns import ProviderTurnService
from .repositories import HarnessMessageRepository, HarnessSessionRepository
from .run_preparation import PreparedRun, RunPreparation
from .run_state import RunRegistry
from .session_metadata_service import SessionMetadataService
from .steering import SteeringChannel

log = structlog.get_logger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]
CommitResult = TypeVar("CommitResult")


FRONTEND_EVENT_STATUS = "harness.session_status"
FRONTEND_EVENT_STEER_QUEUE = "harness.steer_queue"


@dataclass(frozen=True)
class RunAdmissionPorts:
    """Explicit collaborators for admission and its rollback."""

    run_registry: RunRegistry
    cancel_owned_task: Callable[..., Any]
    commit_message_edit: Callable[..., Any]
    deliver_steers: Callable[..., Any]
    emit_frontend: Callable[..., Any]
    execute_run: Callable[..., Any]
    finalize_unstarted_run: Callable[..., Any]
    finish_provider_hold: Callable[..., Any]
    forget_run: Callable[..., Any]
    monitor_provider_turn: Callable[..., Any]
    on_run_task_done: Callable[..., Any]
    provider_turn_roots: dict[str, dict[str, Any]]
    provider_turn_service: ProviderTurnService
    run_service_for_engine: Callable[..., Any]
    session_status_payload: Callable[..., Any]
    spawn_background: Callable[..., Any]
    user_abort_requested: Callable[..., Any]
    watch_provider_followup: Callable[..., Any]
    history: ProviderHistoryService
    messages: type[HarnessMessageRepository]
    metadata: SessionMetadataService
    reserve_run: Callable[..., Any]
    sessions: type[HarnessSessionRepository]


class RunAdmission:
    """Admit a prepared turn, persist its shell, and transfer task ownership."""

    def __init__(self, ports: RunAdmissionPorts, preparation: RunPreparation) -> None:
        self.ports = ports
        self.preparation = preparation

    async def start(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        organization_id: uuid.UUID | None = None,
        workspace_id: str = "",
        user_id: int | None = None,
        skill_ids: list[str] | None = None,
        scheduled: bool = False,
        account_ids: dict[str, str] | None = None,
        max_depth: int | None = None,
        _edit_message_id: uuid.UUID | None = None,
    ) -> HarnessMessage:
        """Prepare a run, then admit it under persistent workspace fencing."""
        key = str(session.id)
        current = self.ports.run_registry.get(key)
        if current is not None and current.is_running:
            raise ConflictError(
                f"Harness session '{session.id}' already has an active run"
            )
        owner = asyncio.current_task()
        active_run = self.ports.run_registry.ensure(key)
        if owner is not None:
            self.ports.run_registry.begin_admission(key, owner)
        try:
            prepared = await self.preparation.prepare(
                session,
                prompt,
                organization_id=organization_id,
                workspace_id=workspace_id,
                user_id=user_id,
                skill_ids=skill_ids,
                scheduled=scheduled,
                account_ids=account_ids,
                max_depth=max_depth,
                _edit_message_id=_edit_message_id,
            )
            return await self._admit(prepared)
        finally:
            # Preparation is owned too: cancellation before message persistence
            # must not leave an invisible admission or orphan ancestry record.
            if active_run.admission is owner:
                self.ports.run_registry.release_admission(key)
            if active_run.task is None and active_run.context is None:
                self.ports.forget_run(key)

    @staticmethod
    async def _commit(
        operation: Awaitable[CommitResult],
        capture: Callable[[CommitResult], None],
    ) -> CommitResult:
        """Join a write and capture its identity before propagating cancellation.

        A cancelled waiter cannot roll back a database thread. The capture runs
        synchronously on this loop once the outcome is known, so admission's
        rollback always sees committed rows/holders/ownership. A simultaneous
        write failure remains the cause of the caller's cancellation.
        """
        task = asyncio.ensure_future(operation)
        cancelled: asyncio.CancelledError | None = None
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    if task.cancelled():
                        raise
                    cancelled = cancelled or exc
                except BaseException:
                    break
            try:
                result = task.result()
                capture(result)
            except BaseException as exc:
                if cancelled is not None:
                    raise cancelled from exc
                raise
        if cancelled is not None:
            raise cancelled
        return result

    async def _reserve(self, prepared: PreparedRun) -> bool:
        """Release only a successful reservation if its caller was cancelled."""
        admitted = False

        def capture(result: bool) -> None:
            nonlocal admitted
            admitted = result

        try:
            return await self._commit(
                sync_to_async(self.ports.reserve_run)(
                    prepared.session, scheduled=prepared.scheduled
                ),
                capture,
            )
        except asyncio.CancelledError as cancelled:
            if admitted:

                def release(_result: Any) -> None:
                    prepared.session.status = HarnessSessionStatus.IDLE

                try:
                    await self._commit(
                        sync_to_async(self.ports.sessions.mark_status)(
                            prepared.session, HarnessSessionStatus.IDLE
                        ),
                        release,
                    )
                except BaseException as exc:
                    raise cancelled from exc
            raise

    async def _admit(self, prepared: PreparedRun) -> HarnessMessage:
        """Own reservation, persistence, task transfer and startup rollback."""
        session = prepared.session
        prompt = prepared.prompt
        org_id = prepared.org_id
        workspace_id = prepared.workspace_id
        run_user_id = prepared.run_user_id
        scheduled = prepared.scheduled
        account_ids = prepared.account_ids
        max_depth = prepared.max_depth
        depth = prepared.depth
        _edit_message_id = prepared.edit_message_id
        key = prepared.key
        engine_id = prepared.engine_id
        inherited_context = prepared.inherited_context
        skill_bodies = prepared.skill_bodies
        edit_model = prepared.edit_model
        edit_effort = prepared.edit_effort
        resolved_model = prepared.resolved_model
        resolved_provider = prepared.resolved_provider
        admitted = await self._reserve(prepared)
        if not admitted:
            detail = (
                "Another chat is already active in this workspace"
                if scheduled
                else f"Harness session '{session.id}' already has an active run"
            )
            raise ConflictError(detail)

        assistant: HarnessMessage | None = None
        user_message: HarnessMessage | None = None
        edit_suffix_ids: list[uuid.UUID] = []
        edit_committed = False
        original_last_message_at = session.last_message_at
        original_updated_at = session.updated_at
        spawned_task: asyncio.Task | None = None
        provider_turn_id: uuid.UUID | None = None
        provider_turn_holder: str | None = None
        title_holder: str | None = None
        inherited_turn_hold = False
        owner = asyncio.current_task()
        if owner is not None:
            self.ports.run_registry.begin_admission(key, owner)
        try:
            if _edit_message_id is not None:
                # Read the suffix under the same reservation as normal sends;
                # never delete the provisional messages used for admission.
                stored = await sync_to_async(self.ports.messages.list_for_session)(
                    session.id
                )
                index = next(
                    (i for i, row in enumerate(stored) if row.id == _edit_message_id),
                    None,
                )
                if index is None:
                    raise NotFoundError("HarnessMessage", str(_edit_message_id))
                edit_suffix_ids = [row.id for row in stored[index:]]
            prior_user_messages = await sync_to_async(
                self.ports.messages.count_for_session
            )(session.id, role="user")

            def capture_user(row: HarnessMessage) -> None:
                nonlocal user_message
                user_message = row

            def capture_assistant(row: HarnessMessage) -> None:
                nonlocal assistant
                assistant = row

            await self._commit(
                sync_to_async(self.ports.messages.create)(
                    session_id=session.id,
                    role="user",
                    content=prompt.strip(),
                    skill_ids=list(session.skill_ids or []),
                    harness_id=engine_id,
                ),
                capture_user,
            )
            await self._commit(
                sync_to_async(self.ports.messages.create)(
                    session_id=session.id,
                    role="assistant",
                    content="",
                    model=resolved_model,
                    reasoning_effort=session.reasoning_effort or "",
                    provider=("claude" if engine_id == "claude" else resolved_provider),
                    harness_id=engine_id,
                ),
                capture_assistant,
            )
            if _edit_message_id is None:
                if session.parent_id is None:
                    await sync_to_async(self.ports.sessions.clear_summary)(session.id)
                # Edits defer changes to the existing timeline until admission.
                await sync_to_async(self.ports.messages.dismiss_prior_notices)(
                    session.id, exclude_ids=[assistant.id]
                )
            await sync_to_async(self.ports.sessions.mark_status)(
                session, HarnessSessionStatus.BUSY
            )
            session.status = HarnessSessionStatus.BUSY
            history = await self.ports.history.build(
                session,
                exclude_message_id=assistant.id,
                exclude_user_message_id=user_message.id,
                exclude_message_ids=edit_suffix_ids,
            )
            run_context = {
                "session_id": key,
                "workspace_id": workspace_id or str(session.workspace_id),
                "organization_id": str(org_id),
                "message_id": str(assistant.id),
                "user_message_id": str(user_message.id),
                "text_part_id": None,
                "reasoning_part_id": None,
                "tool_parts": {},
                "step_parts": {},
                "subtask_parts": {},
                "skill_bodies": skill_bodies,
                "initiated_by_id": run_user_id,
                "account_ids": dict(account_ids),
            }
            if session.parent_id is None:
                run_context["steering"] = SteeringChannel(
                    deliver=lambda entries: self.ports.deliver_steers(
                        session, assistant, entries
                    )
                )
            self.ports.run_registry.attach_context(key, run_context)
            inherited_turn_id = inherited_context.get("provider_turn_id")
            if inherited_turn_id:
                turn = await sync_to_async(self.ports.provider_turn_service.get)(
                    inherited_turn_id
                )
                if turn is None or not turn.active or turn.cancel_requested:
                    raise ValueError(
                        "The provider account for this answer is no longer available"
                    )
                provider_turn_id = inherited_turn_id
                provider_turn_holder = str(uuid.uuid4())
                inherited_turn_hold = await sync_to_async(
                    self.ports.provider_turn_service.add_hold
                )(provider_turn_id, provider_turn_holder)
                if not inherited_turn_hold:
                    raise ValueError(
                        "The provider account for this answer is no longer available"
                    )
                account_ids = dict(inherited_context.get("account_ids") or {})
                run_context["account_ids"] = account_ids
                run_context["provider_turn_id"] = provider_turn_id
                run_context["provider_turn_holder"] = provider_turn_holder
                # Root monitor already watches the complete answer tree.
                run_context["provider_monitor_task"] = None
            elif run_user_id is not None:

                def capture_sidecar(sidecar: Any) -> None:
                    nonlocal provider_turn_id, provider_turn_holder
                    provider_turn_id = sidecar.id
                    provider_turn_holder = str(sidecar.holder_tokens[0])
                    run_context.update(
                        provider_turn_id=provider_turn_id,
                        provider_turn_holder=provider_turn_holder,
                    )

                await self._commit(
                    sync_to_async(self.ports.provider_turn_service.admit)(
                        assistant_message_id=assistant.id,
                        organization_id=org_id,
                        user_id=run_user_id,
                        account_ids=account_ids,
                    ),
                    capture_sidecar,
                )
                revocation_event = asyncio.Event()
                watched_accounts = register_revocation_listener(
                    account_ids.values(),
                    loop=asyncio.get_running_loop(),
                    event=revocation_event,
                )
                run_context.update(
                    provider_turn_id=provider_turn_id,
                    provider_turn_holder=provider_turn_holder,
                    provider_revocation_event=revocation_event,
                    provider_revocation_accounts=watched_accounts,
                    provider_session=session,
                    provider_followup_tasks={},
                )
                self.ports.provider_turn_roots[str(provider_turn_id)] = run_context
                monitor_task = self.ports.spawn_background(
                    self.ports.monitor_provider_turn(
                        session=session,
                        context=run_context,
                        event=revocation_event,
                        watched_accounts=watched_accounts,
                    )
                )
                run_context["provider_monitor_task"] = monitor_task
            if engine_id != "native":
                run_context["initiated_by_id"] = run_user_id
                run_context["engine_id"] = engine_id
                run_context["cleanup_confirmed"] = True

                def capture_attempt(ownership: Any) -> None:
                    run_context["ownership"] = ownership

                await self._commit(
                    sync_to_async(
                        self.ports.run_service_for_engine().create_attempt,
                        thread_sensitive=True,
                    )(
                        session_id=session.id,
                        assistant_message_id=assistant.id,
                        user_id=run_user_id,
                        harness_id=engine_id,
                    ),
                    capture_attempt,
                )
            if self.ports.user_abort_requested(session):
                raise asyncio.CancelledError()
            if _edit_message_id is not None:

                def capture_edit(_result: Any) -> None:
                    nonlocal edit_committed
                    edit_committed = True

                await self._commit(
                    self.ports.commit_message_edit(
                        session,
                        edit_suffix_ids,
                        model=edit_model,
                        reasoning_effort=edit_effort,
                    ),
                    capture_edit,
                )
            if (
                _edit_message_id is None
                and session.parent_id is None
                and prior_user_messages == 0
                and provider_turn_id
                and not scheduled
            ):
                # Reserve before spawning: a fast answer may release its last
                # holder before the title task gets a chance to start.
                title_holder = f"title:{uuid.uuid4()}"
                if not await sync_to_async(self.ports.provider_turn_service.add_hold)(
                    provider_turn_id, title_holder
                ):
                    title_holder = None
            active_run = self.ports.run_registry.ensure(key)
            run_coro = self.ports.execute_run(
                session=session,
                prompt=prompt.strip(),
                history=history,
                assistant=assistant,
                organization_id=org_id,
                depth=depth,
                max_depth=max_depth,
            )
            spawned_task = self.ports.spawn_background(run_coro)
            self.ports.run_registry.start_execution(key, spawned_task)
            self.ports.run_registry.release_admission(key)

            def run_done(task: asyncio.Task) -> None:
                # A cancelled task may never execute its first instruction, so
                # neither the run's except nor finally would finalize its shell.
                if task.cancelled() and not active_run.execution_started:
                    run_coro.close()
                    active_run.begin_finalization()
                    finalizer = self.ports.spawn_background(
                        self.ports.finalize_unstarted_run(session, assistant)
                    )
                    self.ports.run_registry.start_execution(key, finalizer)
                    finalizer.add_done_callback(
                        lambda done: self.ports.on_run_task_done(done, key)
                    )
                self.ports.on_run_task_done(task, key)

            spawned_task.add_done_callback(run_done)
            if title_holder is not None:
                title_task = self.ports.spawn_background(
                    self.ports.metadata.generate_title(
                        session_id=session.id,
                        prompt=prompt.strip(),
                        organization_id=org_id,
                        provider_turn_id=provider_turn_id,
                        account_ids=dict(account_ids),
                        initiated_by_id=run_user_id,
                    )
                )
                self.ports.watch_provider_followup(
                    run_context, title_task, title_holder
                )
                title_task.add_done_callback(
                    lambda _task, turn_id=provider_turn_id, holder=title_holder: (
                        self.ports.spawn_background(
                            self.ports.finish_provider_hold(turn_id, holder)
                        )
                    )
                )
            if self.ports.user_abort_requested(session):
                # Stop arrived while this run was being admitted. Cancel a
                # still-running owner. A task already cancelled before its
                # callback was installed must keep settling and return.
                already_stopping = spawned_task.done() or bool(
                    spawned_task.cancelling()
                )
                self.ports.cancel_owned_task(key)
                if not already_stopping and (
                    spawned_task.cancelling() or spawned_task.done()
                ):
                    raise asyncio.CancelledError()
            await self.ports.emit_frontend(
                FRONTEND_EVENT_STATUS,
                self.ports.session_status_payload(
                    session,
                    "busy",
                    model=resolved_model,
                    assistant=assistant,
                    user_message_id=str(user_message.id),
                ),
                str(session.workspace_id),
            )
            log.info(
                "harness_run_started",
                session_id=key,
                user_message_id=str(user_message.id),
            )
            return assistant
        except BaseException as exc:
            if spawned_task is not None:
                # A dropped HTTP caller must not erase a live background turn.
                # User stop cancels that owner instead of leaving it running.
                if isinstance(exc, asyncio.CancelledError):
                    if self.ports.user_abort_requested(session):
                        self.ports.cancel_owned_task(key)
                    else:
                        log.exception("harness_run_admitted_with_caller_error")
                    raise
                log.exception("harness_run_admitted_with_caller_error")
                return assistant
            user_abort = self.ports.user_abort_requested(session)
            # sync_to_async re-raises CancelledError after a shielded write
            # when this task is already stopping. Retry until the shell is
            # durable, then propagate the original cancellation.
            with anyio.CancelScope(shield=True):
                for _ in range(4):
                    try:
                        await sync_to_async(self.ports.sessions.mark_status)(
                            session, HarnessSessionStatus.IDLE
                        )
                        session.status = HarnessSessionStatus.IDLE
                        if assistant is not None:
                            await sync_to_async(self.ports.messages.complete)(
                                assistant,
                                finish="aborted" if user_abort else "error",
                                error=(
                                    "aborted by user"
                                    if user_abort
                                    else "Run admission failed before the "
                                    "harness started: "
                                    + (str(exc) or type(exc).__name__)
                                ),
                            )
                        break
                    except asyncio.CancelledError:
                        continue
            run_context = self.ports.run_registry.get_context(key)
            if provider_turn_id and provider_turn_holder and spawned_task is None:
                await sync_to_async(self.ports.provider_turn_service.finish_once)(
                    provider_turn_id, provider_turn_holder
                )
            if provider_turn_id and title_holder and spawned_task is None:
                await self.ports.finish_provider_hold(provider_turn_id, title_holder)
            monitor_task = run_context.get("provider_monitor_task")
            if monitor_task is not None and not monitor_task.done():
                monitor_task.cancel()
                watched = run_context.get("provider_revocation_accounts")
                event = run_context.get("provider_revocation_event")
                if watched and event is not None:
                    unregister_revocation_listener(watched, event=event)
            durable_owner = run_context.get("ownership")
            if durable_owner is not None:
                try:
                    await durable_owner.stop_heartbeat()
                    await durable_owner.begin_close()
                    await durable_owner.finalize_turn(
                        status=(
                            HarnessRunStatus.INTERRUPTED
                            if user_abort
                            else HarnessRunStatus.ERROR
                        ),
                        finish="aborted" if user_abort else "error",
                        error=(
                            "aborted by user" if user_abort else "Run admission failed"
                        ),
                    )
                except Exception:
                    log.warning("harness_run_attempt_finalize_failed", session_id=key)
            if _edit_message_id is not None and not edit_committed:
                # Failed admission must not leave a replacement turn or alter
                # the original timeline's activity timestamp.
                with anyio.CancelScope(shield=True):
                    await sync_to_async(self.ports.messages.discard_edit_run)(
                        session.id,
                        message_ids=[
                            row.id
                            for row in (user_message, assistant)
                            if row is not None
                        ],
                        last_message_at=original_last_message_at,
                        updated_at=original_updated_at,
                    )
            self.ports.run_registry.release_context(key)
            self.ports.forget_run(key)
            raise
        finally:
            if self.ports.run_registry.get_admission(key) is owner:
                self.ports.run_registry.release_admission(key)
