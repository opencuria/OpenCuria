"""Ownership heartbeat and conservative recovery for external engine runs."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from asgiref.sync import sync_to_async
from django.utils import timezone

from common.exceptions import ConflictError

from ..models import HarnessRun, HarnessRunStatus
from .run_repository import HarnessRunRepository

RUN_HEARTBEAT_SECONDS = 30
RUN_HEARTBEAT_GRACE_SECONDS = 240


class RunOwnership:
    """One worker's fenced authority to publish an external engine attempt."""

    def __init__(
        self,
        run: HarnessRun,
        *,
        repository: type[HarnessRunRepository] | None = None,
    ) -> None:
        self.run_id = run.id
        self.owner_token = run.owner_token
        self.repository = repository or HarnessRunRepository
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def start(self) -> bool:
        """Claim the new attempt's starting row as running."""
        return await sync_to_async(
            self.repository.start_if_owner, thread_sensitive=True
        )(self.run_id, self.owner_token)

    async def is_owner_for_finalization(self) -> bool:
        """Retain settlement authority after this owner's closing transition."""
        return await sync_to_async(
            self.repository.is_owner_for_finalization, thread_sensitive=True
        )(self.run_id, self.owner_token)

    async def settle_shell(
        self,
        *,
        finish: str,
        error: str,
        assistant_content: str | None = None,
        engine_meta: dict | None = None,
    ) -> bool:
        """Persist a fenced terminal-looking shell without releasing admission."""
        from .repositories import HarnessEngineRepository

        return await sync_to_async(
            HarnessEngineRepository.settle_owned_shell,
            thread_sensitive=True,
        )(
            self.run_id,
            self.owner_token,
            finish=finish,
            error=error,
            assistant_content=assistant_content,
            engine_meta=engine_meta,
        )

    async def finalize_turn(
        self,
        *,
        status: str,
        finish: str,
        error: str = "",
        assistant_content: str | None = None,
        engine_meta: dict | None = None,
        session_usage: dict | None = None,
        session_cost: float = 0.0,
        tail_remainder: str = "",
    ) -> bool:
        """Atomically commit this attempt and its assistant/session state."""
        from .repositories import HarnessEngineRepository

        return await sync_to_async(
            HarnessEngineRepository.finalize_owned_turn,
            thread_sensitive=True,
        )(
            self.run_id,
            self.owner_token,
            status=status,
            finish=finish,
            error=error,
            assistant_content=assistant_content,
            engine_meta=engine_meta,
            session_usage=session_usage,
            session_cost=session_cost,
            tail_remainder=tail_remainder,
        )

    async def touch(self) -> bool:
        """Refresh ownership only while this attempt remains writable."""
        return await sync_to_async(
            self.repository.touch_if_owner, thread_sensitive=True
        )(self.run_id, self.owner_token)

    async def is_current_owner(self) -> bool:
        """Fence engine callbacks after takeover or transition into closing."""
        return await sync_to_async(
            self.repository.is_current_owner, thread_sensitive=True
        )(self.run_id, self.owner_token)

    async def bind_runner_lease(self, lease_id: str, epoch: str) -> bool:
        """Persist runner lease identity before spawning the external process."""
        return await sync_to_async(
            self.repository.set_identity_if_owner, thread_sensitive=True
        )(
            self.run_id,
            self.owner_token,
            lease_id=lease_id,
            lease_epoch=epoch,
        )

    async def begin_close(self) -> bool:
        """Fence further writes before external-resource cleanup starts."""
        return await sync_to_async(
            self.repository.begin_close_if_owner, thread_sensitive=True
        )(self.run_id, self.owner_token)

    async def set_external_session(self, external_session_id: str) -> bool:
        """Attach only a bounded, non-secret upstream session identifier."""
        cleaned = (external_session_id or "").strip()
        if not cleaned or len(cleaned) > 64:
            raise ValueError("external_session_id must be 1..64 characters")
        return await sync_to_async(
            self.repository.set_external_session_if_owner, thread_sensitive=True
        )(self.run_id, self.owner_token, external_session_id=cleaned)

    async def finish(
        self,
        *,
        status: str = HarnessRunStatus.COMPLETED,
        error: str = "",
        cleanup_confirmed: bool = True,
    ) -> bool:
        """Finish if still owner; retain CLOSING if runner cleanup is unknown."""
        return await sync_to_async(
            self.repository.complete_if_owner, thread_sensitive=True
        )(
            self.run_id,
            self.owner_token,
            status=status,
            error=error,
            cleanup_confirmed=cleanup_confirmed,
        )

    def start_heartbeat(
        self,
        *,
        on_lost: Callable[[], Any] | None = None,
        interval: float = RUN_HEARTBEAT_SECONDS,
    ) -> asyncio.Task[None]:
        """Keep a live attempt durable; optional callback should cancel worker."""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return self._heartbeat_task
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(on_lost=on_lost, interval=interval),
            name=f"harness-run-heartbeat-{self.run_id}",
        )
        return self._heartbeat_task

    async def _heartbeat_loop(
        self, *, on_lost: Callable[[], Any] | None, interval: float
    ) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                if await self.touch():
                    continue
                if on_lost is not None:
                    result = on_lost()
                    if asyncio.iscoroutine(result):
                        await result
                return
        except asyncio.CancelledError:
            raise

    async def stop_heartbeat(self) -> None:
        """Stop and join this attempt's heartbeat task."""
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class HarnessRunService:
    """Create attempts and recover stale workers without assuming they died."""

    def __init__(
        self,
        *,
        repository: type[HarnessRunRepository] | None = None,
        heartbeat_grace: timedelta = timedelta(seconds=RUN_HEARTBEAT_GRACE_SECONDS),
    ) -> None:
        self.repository = repository or HarnessRunRepository
        self.heartbeat_grace = heartbeat_grace

    def create_attempt(
        self,
        *,
        session_id: uuid.UUID,
        assistant_message_id: uuid.UUID,
        user_id: object | None = None,
        harness_id: str = "claude",
    ) -> RunOwnership:
        """Create a durable attempt after session/message admission succeeds."""
        row = self.repository.create_attempt(
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            user_id=user_id,
            harness_id=harness_id,
        )
        return RunOwnership(row, repository=self.repository)

    async def prepare_session(
        self, session_id: uuid.UUID, accessor: Any = None
    ) -> bool:
        """Recover a stale attempt or raise while a process may still be live.

        An in-memory task map is deliberately not consulted: its absence says
        nothing about a remote worker. A persisted runner lease must be released
        and confirmed before its assistant turn can be marked interrupted.
        """
        row = await sync_to_async(
            self.repository.get_open_for_session, thread_sensitive=True
        )(session_id)
        if row is None:
            return True

        cutoff = timezone.now() - self.heartbeat_grace
        stale = row.status == HarnessRunStatus.CLOSING or row.heartbeat_at <= cutoff
        if not stale:
            raise ConflictError("Harness session already has an active run")

        if row.status != HarnessRunStatus.CLOSING:
            fenced = await sync_to_async(
                self.repository.begin_close_if_stale, thread_sensitive=True
            )(row.id, cutoff=cutoff, owner_token=row.owner_token)
            if not fenced:
                raise ConflictError("Harness session run ownership changed")
        else:
            await sync_to_async(self.repository.ensure_closing, thread_sensitive=True)(
                row.id
            )
        # Stale recovery rotates the token; use it only for the recovery
        # transaction, never for the original worker.
        row = await sync_to_async(self.repository.get_by_id, thread_sensitive=True)(
            row.id
        )
        if row is None or row.status != HarnessRunStatus.CLOSING:
            raise ConflictError("Harness session run ownership changed")

        if row.lease_id is not None:
            if accessor is None:
                raise ConflictError("Previous engine process cleanup is pending")
            try:
                result = await accessor.desktop_action(
                    "release",
                    {
                        "lease_id": str(row.lease_id),
                        "epoch": row.lease_epoch,
                        "kind": "agent",
                        "owner_id": str(session_id),
                        "revision": 1,
                    },
                )
            except Exception:
                # Keep the durable closing row so a later admission retries.
                raise ConflictError(
                    "Previous engine process cleanup is pending"
                ) from None
            if (
                not isinstance(result, dict)
                or not result.get("ok")
                or result.get("lease_state") not in {"released", "expired"}
            ):
                raise ConflictError("Previous engine process cleanup is pending")
        # No lease identity means the owner died before the pre-spawn binding
        # callback; grace exceeds the runner's lease TTL, so no engine child can
        # still be authorized. Do not invent a local task-death signal.

        finalized = await sync_to_async(
            self.repository.finish_interrupted_after_cleanup, thread_sensitive=True
        )(row.id, owner_token=row.owner_token)
        if not finalized:
            raise ConflictError("Harness session run ownership changed")
        return True

    async def sweep_stale(
        self, accessor_for_session: Callable[[uuid.UUID], Any]
    ) -> int:
        """Best-effort bounded recovery entrypoint usable by a maintenance job."""
        rows = await sync_to_async(self.repository.list_open, thread_sensitive=True)()
        recovered = 0
        for row in rows:
            if row.status != HarnessRunStatus.CLOSING and row.heartbeat_at > (
                timezone.now() - self.heartbeat_grace
            ):
                continue
            try:
                accessor = accessor_for_session(row.session_id)
                if asyncio.iscoroutine(accessor):
                    accessor = await accessor
                await self.prepare_session(row.session_id, accessor)
            except ConflictError:
                continue
            recovered += 1
        return recovered
