"""Bounded, session-scoped runner background work coordination."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ...models import Runner

logger = structlog.get_logger(__name__)


class RunnerSupervisor:
    """Keep at most one drain/reconciliation worker per runner."""

    def __init__(self, service) -> None:
        self.service = service
        self._workers: dict[str, asyncio.Task] = {}
        self._desired_sid: dict[str, str] = {}
        self._drain_requested: set[tuple[str, str]] = set()
        self._draining: set[tuple[str, str]] = set()
        self._drained: set[tuple[str, str]] = set()
        self._snapshots: dict[str, tuple[str, list[dict]]] = {}
        self._online_notified: set[tuple[str, str]] = set()
        self._online_reservations: set[tuple[str, str]] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Reconciliation touches shared process-verification state.
        self._reconcile_lock = asyncio.Lock()

    def session_lock(self, runner_id: str) -> asyncio.Lock:
        """Return the lock serializing local register and disconnect."""
        return self._session_locks.setdefault(runner_id, asyncio.Lock())

    def schedule_drain(self, runner: Runner, sid: str) -> None:
        """Request the initial drain without delaying the registration ACK."""
        runner_id = str(runner.id)
        key = (runner_id, sid)
        self._desired_sid[runner_id] = sid
        if key not in self._drained and key not in self._draining:
            self._drain_requested.add(key)
        self._ensure_worker(runner_id)

    def enqueue_snapshot(
        self, runner: Runner, sid: str, workspaces: list[dict]
    ) -> None:
        """Coalesce to the latest snapshot; status also signals a drain retry."""
        runner_id = str(runner.id)
        key = (runner_id, sid)
        self._desired_sid[runner_id] = sid
        self._snapshots[runner_id] = (sid, list(workspaces))
        if key not in self._drained:
            # If a drain is in flight, retain one retry signal. Successful drain
            # consumes it; failed drain retries only because this status arrived.
            self._drain_requested.add(key)
        self._ensure_worker(runner_id)

    def reserve_online_notification(self, runner_id: str, sid: str) -> bool:
        """Reserve one online event for this session."""
        key = (runner_id, sid)
        if key in self._online_notified or key in self._online_reservations:
            return False
        self._online_reservations.add(key)
        return True

    def release_online_notification(self, runner_id: str, sid: str) -> None:
        """Release an unsuccessful online-event reservation."""
        self._online_reservations.discard((runner_id, sid))

    def mark_online_notified(self, runner_id: str, sid: str) -> None:
        """Record a successfully queued online event."""
        key = (runner_id, sid)
        self._online_reservations.discard(key)
        self._online_notified.add(key)

    def cancel_session(self, runner_id: str, sid: str) -> None:
        """Drop queued state without cancelling in-flight ORM/RPC work."""
        key = (runner_id, sid)
        if self._desired_sid.get(runner_id) == sid:
            self._desired_sid.pop(runner_id, None)
        self._drain_requested.discard(key)
        self._draining.discard(key)
        self._drained.discard(key)
        snapshot = self._snapshots.get(runner_id)
        if snapshot is not None and snapshot[0] == sid:
            self._snapshots.pop(runner_id, None)
        self._online_notified.discard(key)
        self._online_reservations.discard(key)

    def _ensure_worker(self, runner_id: str) -> None:
        """Start a worker only when no unfinished worker owns this runner."""
        worker = self._workers.get(runner_id)
        if worker is None or worker.done():
            worker = asyncio.create_task(self._run(runner_id))
            self._workers[runner_id] = worker

    async def _run(self, runner_id: str) -> None:
        """Drain and reconcile serially, consuming only explicitly queued work."""
        worker = asyncio.current_task()
        try:
            while True:
                sid = self._desired_sid.get(runner_id)
                if sid is None:
                    return
                key = (runner_id, sid)
                try:
                    if not await self.service.is_active_runner_session(runner_id, sid):
                        self._discard_session_work(runner_id, sid)
                        return
                    runner = await self.service.get_runner_async(runner_id)
                    if runner is None:
                        self._discard_session_work(runner_id, sid)
                        return

                    if key in self._drain_requested and key not in self._draining:
                        self._drain_requested.discard(key)
                        self._draining.add(key)
                        try:
                            await self._drain_pending(runner_id, sid, runner)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # The drain stays incomplete. Retry only if a later
                            # status event has queued another request.
                            logger.exception(
                                "runner_pending_drain_failed",
                                runner_id=runner_id,
                                sid=sid,
                            )
                        finally:
                            self._draining.discard(key)
                    if self._desired_sid.get(runner_id) != sid:
                        continue

                    snapshot = self._snapshots.get(runner_id)
                    if snapshot is None or snapshot[0] != sid:
                        self._desired_sid.pop(runner_id, None)
                        return
                    self._snapshots.pop(runner_id, None)
                    async with self._reconcile_lock:
                        if (
                            self._desired_sid.get(runner_id) == sid
                            and await self.service.is_active_runner_session(
                                runner_id, sid
                            )
                        ):
                            await self.service.reconcile_runner_snapshot(
                                runner, snapshot[1]
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "runner_supervisor_work_failed",
                        runner_id=runner_id,
                        sid=sid,
                    )
                    self._discard_session_work(runner_id, sid)
                    return
        finally:
            if self._workers.get(runner_id) is worker:
                self._workers.pop(runner_id, None)
                sid = self._desired_sid.get(runner_id)
                if sid is not None and self._has_work(runner_id, sid):
                    self._ensure_worker(runner_id)

    async def _drain_pending(
        self, runner_id: str, sid: str, runner: Runner
    ) -> None:
        """Mark drained only after all four pending dispatchers succeed."""
        for dispatch in (
            self.service.dispatch_pending_image_builds,
            self.service.dispatch_pending_image_deletions,
            self.service.dispatch_pending_workspace_deletions,
            self.service.dispatch_pending_build_job_deletions,
        ):
            if self._desired_sid.get(runner_id) != sid:
                return
            if not await self.service.is_active_runner_session(runner_id, sid):
                return
            await dispatch(runner)
        if self._desired_sid.get(runner_id) == sid:
            key = (runner_id, sid)
            self._drained.add(key)
            # A status received while dispatch was running is satisfied by a
            # successful drain; don't immediately send every pending task twice.
            self._drain_requested.discard(key)

    def _discard_session_work(self, runner_id: str, sid: str) -> None:
        """Remove queued state owned by this SID, never its successor."""
        if self._desired_sid.get(runner_id) == sid:
            self._desired_sid.pop(runner_id, None)
        key = (runner_id, sid)
        self._drain_requested.discard(key)
        self._draining.discard(key)
        self._drained.discard(key)
        snapshot = self._snapshots.get(runner_id)
        if snapshot is not None and snapshot[0] == sid:
            self._snapshots.pop(runner_id, None)

    def _has_work(self, runner_id: str, sid: str) -> bool:
        """Return whether the desired SID still has queued work."""
        snapshot = self._snapshots.get(runner_id)
        return (runner_id, sid) in self._drain_requested or (
            snapshot is not None and snapshot[0] == sid
        )


class RunnerSupervisorMixin:
    """Expose coordinator operations through RunnerService."""

    def _init_runner_supervisor(self) -> None:
        """Create per-service runner coordination state."""
        self._runner_supervisor = RunnerSupervisor(self)

    def runner_session_lock(self, runner_id: str) -> asyncio.Lock:
        """Return the per-runner register/disconnect lock."""
        return self._runner_supervisor.session_lock(runner_id)

    def schedule_runner_drain(self, runner: Runner, sid: str) -> None:
        """Queue pending work after registration without blocking its ACK."""
        self._runner_supervisor.schedule_drain(runner, sid)

    def reserve_runner_online_notification(self, runner_id: str, sid: str) -> bool:
        """Reserve one online notification per session."""
        return self._runner_supervisor.reserve_online_notification(runner_id, sid)

    def mark_runner_online_notified(self, runner_id: str, sid: str) -> None:
        """Record an online notification as queued for fanout."""
        self._runner_supervisor.mark_online_notified(runner_id, sid)

    def release_runner_online_notification(self, runner_id: str, sid: str) -> None:
        """Release a failed online-notification reservation."""
        self._runner_supervisor.release_online_notification(runner_id, sid)

    def enqueue_runner_snapshot(
        self, runner: Runner, sid: str, workspaces: list[dict]
    ) -> None:
        """Queue the latest runner status for asynchronous reconciliation."""
        self._runner_supervisor.enqueue_snapshot(runner, sid, workspaces)

    def cancel_runner_session_work(self, runner_id: str, sid: str) -> None:
        """Discard queued session work without awaiting in-flight operations."""
        self._runner_supervisor.cancel_session(runner_id, sid)
