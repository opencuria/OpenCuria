"""Run-tree ownership, cancellation and joining."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from .conversation_service import ConversationService
from .interaction_service import InteractionService
from .models import (
    HarnessSession,
    HarnessSessionStatus,
)
from .repositories import HarnessSessionRepository
from .run_state import RunRegistry

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RunCancellationPorts:
    """Explicit domain collaborators; no facade or sibling private state."""

    close_steering: Callable[..., Any]
    emit_empty_steer_queue: Callable[..., Any]
    ensure_external_attempt_reconciled: Callable[..., Any]
    run_service_for_engine: Callable[..., Any]
    spawn_background: Callable[..., Any]
    conversations: ConversationService
    interactions: InteractionService
    run_registry: RunRegistry
    sessions: type[HarnessSessionRepository]


class RunCancellation:
    """RunCancellation domain service."""

    def __init__(self, ports: RunCancellationPorts) -> None:
        self.ports = ports

    async def shutdown(self) -> None:
        """Cancel and join every core owner without recording a user abort.

        Finalization is a cancellation fence. A pre-entry cancellation can
        install a replacement cleanup task, so repeat until callbacks and
        admissions have relinquished every live owner.
        """
        current = asyncio.current_task()
        while True:
            pending: dict[asyncio.Task, str] = {}
            for key, run in tuple(self.ports.run_registry.records.items()):
                self._cancel_owned_task(key)
                for task in (run.task, run.admission):
                    if task is not None and task is not current and not task.done():
                        pending[task] = key
            if not pending:
                await asyncio.sleep(0)
                if any(
                    task is not None and task is not current and not task.done()
                    for run in self.ports.run_registry.records.values()
                    for task in (run.task, run.admission)
                ):
                    continue
                return
            for task, key in pending.items():
                await self._join_owned_task(key, task)

    def reconcile_recovered_run(
        self, session_id: uuid.UUID | str, assistant_message_id: uuid.UUID | str
    ) -> None:
        """Release only bookkeeping owned by the durably recovered attempt.

        A recovery callback can race a newer admission. Assistant identity and
        the absence of task/admission references fence every local mutation;
        ancestry alone never authorizes dropping an independent child run.
        """
        root_key, message_id = str(session_id), str(assistant_message_id)
        for key, run in tuple(self.ports.run_registry.records.items()):
            context = run.context or {}
            root_match = key == root_key and run.assistant_message_id == message_id
            projection_match = (
                context.get("external_projection") is True
                and str(context.get("external_parent_session_id", "")) == root_key
                and str(context.get("external_parent_message_id", "")) == message_id
            )
            if not (root_match or projection_match):
                continue
            # Retained references also protect the pre-entry cancellation gap:
            # a done callback may still install its replacement cleanup owner.
            if run.task is not None or run.admission is not None:
                continue
            self.ports.run_registry.clear_abort(key)
            self._drop_run_tracking(key)

    def _remember_run(self, session: HarnessSession) -> None:
        """Index a live session under its parent before the next await."""
        key = str(session.id)
        parent = str(session.parent_id) if session.parent_id else None
        self.ports.run_registry.link_parent(key, parent)

    def _forget_run(self, key: str) -> None:
        """Drop a finished session once nothing still points at it."""
        registry = self.ports.run_registry
        parent = registry.get_parent(key)
        if any(
            run.indexed and run.parent_id == key for run in registry.records.values()
        ):
            return
        registry.unlink_parent(key)
        parent_run = registry.get(parent) if parent else None
        if parent_run is not None and not parent_run.is_running:
            self._forget_run(parent)

    def _drop_run_tracking(self, key: str) -> None:
        """Release local task bookkeeping without publishing a terminal status."""
        workspace_id = str(
            self.ports.run_registry.get_context(key).get("workspace_id") or ""
        )
        dropped = self.ports.close_steering(key)
        if dropped and workspace_id:
            # No follow-up here (the run's cleanup is unconfirmed), but clients
            # must not keep showing messages that will never be delivered.
            log.warning("harness_steers_discarded", session_id=key, count=len(dropped))
            try:
                self.ports.spawn_background(
                    self.ports.emit_empty_steer_queue(key, workspace_id)
                )
            except RuntimeError:  # pragma: no cover - no running loop
                pass
        self.ports.run_registry.release_context(key)
        self.ports.run_registry.release_event_lock(key)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if current is not None and self.ports.run_registry.get_task(key) is current:
            self.ports.run_registry.release_task(key)
        self._forget_run(key)

    def _descendant_keys(self, root: str) -> set[str]:
        """Return *root* and in-memory descendant session ids."""
        keys = {root}
        changed = True
        while changed:
            changed = False
            for key, run in self.ports.run_registry.records.items():
                if run.indexed and run.parent_id in keys and key not in keys:
                    keys.add(key)
                    changed = True
        return keys

    def _owned_run_task(self, key: str) -> asyncio.Task | None:
        """Return the run owner for *key*.

        Child admissions are the parent tool task (``harness-tool-*``).
        Cancelling those would abort the parent, so only a dedicated run
        task or a non-tool admission (root startup) is owned here.
        """
        task = self.ports.run_registry.get_task(key)
        if task is not None:
            return task
        admission = self.ports.run_registry.get_admission(key)
        if admission is None or admission.done():
            return None
        if admission.get_name().startswith("harness-tool-"):
            return None
        return admission

    def _startup_admission(self, key: str) -> asyncio.Task | None:
        """Return an in-flight ``start_run`` when no background owner exists yet."""
        task = self.ports.run_registry.get_task(key)
        if task is not None and not task.done():
            return None
        admission = self.ports.run_registry.get_admission(key)
        if admission is None or admission.done() or admission is asyncio.current_task():
            return None
        return admission

    def _cancel_owned_task(self, key: str) -> None:
        """Cancel *key*'s run unless it is already stopping or finalizing."""
        task = self._owned_run_task(key)
        if task is None or task.done():
            task = None
        elif not task.cancelling() and not self.ports.run_registry.finalizing(key):
            task.cancel()
        # A start_run blocked before its background task exists is the
        # admission (often the parent tool task). Cancel that waiter too
        # when this session has no live owner yet.
        admission = self._startup_admission(key)
        if (
            admission is None
            or admission is task
            or admission.cancelling()
            or self.ports.run_registry.finalizing(key)
        ):
            return
        admission.cancel()

    def _arm_abort_keys(self, keys: set[str]) -> None:
        """Mark user-stop intent and cancel every owned run in *keys*."""
        for key in keys:
            self.ports.run_registry.request_abort(key)
            self._cancel_owned_task(key)

    async def _join_owned_task(self, key: str, task: asyncio.Task) -> None:
        """Wait for *task* without abandoning it if this stop is cancelled."""
        if task.done() or task is asyncio.current_task():
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                raise
        except Exception:  # pragma: no cover - abort must not raise
            log.exception("harness_abort_run_error", session_id=key)
        # The done callback may have replaced a pre-entry task. Join that
        # owner too, without cancelling it: it is already settling.
        finalizer = self.ports.run_registry.get_task(key)
        if finalizer is not None and finalizer is not task and not finalizer.done():
            await self._join_owned_task(key, finalizer)

    async def _join_abort_tree(self, root: str, closure: set[str]) -> None:
        """Cancel and join every live owner in *closure* until it is stable."""
        while True:
            closure.update(self._descendant_keys(root))
            self._arm_abort_keys(closure)
            pending: list[tuple[str, asyncio.Task]] = []
            seen: set[int] = set()
            for key in closure:
                candidates = (
                    self._owned_run_task(key),
                    self._startup_admission(key),
                )
                for task in candidates:
                    if task is None or task.done() or task is asyncio.current_task():
                        continue
                    if id(task) in seen:
                        continue
                    seen.add(id(task))
                    pending.append((key, task))
            if not pending:
                return
            key, task = pending[0]
            await self._join_owned_task(key, task)

    async def _settle_abort_tree(
        self, session: HarnessSession, closure: set[str]
    ) -> HarnessSession:
        """Mark leftover busy rows idle and drop stop intent for *closure*."""
        ids: list[uuid.UUID] = []
        for key in closure:
            try:
                ids.append(uuid.UUID(key))
            except ValueError:
                continue
        rows = await sync_to_async(self.ports.sessions.list_by_ids)(ids)
        fresh = session
        unresolved = (
            await sync_to_async(
                self.ports.run_service_for_engine().repository.list_open_for_sessions,
                thread_sensitive=True,
            )(ids)
            if ids
            else []
        )
        unresolved_ids = {str(item.session_id) for item in unresolved}
        for row in rows:
            key = str(row.id)
            task = self._owned_run_task(key)
            if key in unresolved_ids or (task is not None and not task.done()):
                continue
            if row.status != HarnessSessionStatus.IDLE:
                await sync_to_async(self.ports.sessions.mark_status)(
                    row, HarnessSessionStatus.IDLE
                )
                row.status = HarnessSessionStatus.IDLE
            self.ports.run_registry.clear_abort(key)
            if row.id == session.id:
                fresh = row
        return fresh

    async def _abort_run_tree(self, session: HarnessSession) -> HarnessSession:
        """Cancel and join an authorized stop tree, including descendants.

        Intent is published and every registered run is cancelled before
        any join. ``asyncio.shield`` around a child therefore cannot keep
        that child alive until the root happens to unwind.
        """
        root = str(session.id)
        # Read descendants before publishing intent. A leaf admitted during
        # this query is then in the memory index and is armed with the rest,
        # instead of being cancelled before it can be registered.
        descendant_ids = await sync_to_async(self.ports.sessions.list_descendant_ids)(
            session.id
        )
        closure = {str(child_id) for child_id in descendant_ids}
        closure.update(self._descendant_keys(root))
        durable_attempts = await sync_to_async(
            self.ports.run_service_for_engine().repository.list_open_for_sessions,
            thread_sensitive=True,
        )(list(uuid.UUID(key) for key in closure))
        for attempt in durable_attempts:
            attempt_key = str(attempt.session_id)
            if (
                self.ports.run_registry.get_task(attempt_key) is None
                and self.ports.run_registry.get_admission(attempt_key) is None
            ):
                # Retry detached cleanup; recovery still fences fresh owners
                # and requires the runner to confirm process termination.
                detached_session = await sync_to_async(
                    self.ports.conversations.get_session
                )(attempt.session_id)
                await self.ports.ensure_external_attempt_reconciled(
                    detached_session, force=True
                )
        # Cancel and join before any further database work. Cleanup writes
        # the terminal message; overlapping reads deadlock SQLite.
        self._arm_abort_keys(closure)
        await self._join_abort_tree(root, closure)
        descendant_ids = await sync_to_async(self.ports.sessions.list_descendant_ids)(
            session.id
        )
        closure.update(str(child_id) for child_id in descendant_ids)
        closure.update(self._descendant_keys(root))
        self._arm_abort_keys(closure)
        await self._join_abort_tree(root, closure)
        await self.ports.interactions.reject_sessions(closure)
        settled = await self._settle_abort_tree(session, closure)
        remaining = await sync_to_async(
            self.ports.run_service_for_engine().repository.list_open_for_sessions,
            thread_sensitive=True,
        )(list(uuid.UUID(key) for key in closure))
        if remaining:
            raise ConflictError(
                "External engine cleanup is pending; the session remains busy"
            )
        return settled

    def _on_run_task_done(self, task: asyncio.Task[None], key: str) -> None:
        """Drop the run task and retrieve any leftover exception."""
        if self.ports.run_registry.get_task(key) is task:
            self.ports.run_registry.release_task(key)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "harness_background_task_failed",
                session_id=key,
                error=str(exc),
            )

    def _user_abort_requested(self, session: HarnessSession) -> bool:
        """Inherit explicit stop intent from any ancestor still in the tree."""
        key = str(session.id)
        cursor = str(session.parent_id) if session.parent_id else None
        chain = [key]
        seen = {key}
        while cursor and cursor not in seen:
            chain.append(cursor)
            seen.add(cursor)
            if self.ports.run_registry.is_aborted(cursor):
                for ancestor in chain:
                    self.ports.run_registry.request_abort(ancestor)
                return True
            cursor = self.ports.run_registry.get_parent(cursor)
        return self.ports.run_registry.is_aborted(key)

    def _parent_stop_in_progress(self, session: HarnessSession) -> bool:
        """True when user stop must end *session* instead of returning a tool error."""
        if self._user_abort_requested(session):
            return True
        parent_task = self.ports.run_registry.get_task(str(session.id))
        return parent_task is not None and bool(parent_task.cancelling())
