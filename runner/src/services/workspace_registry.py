"""Workspace cache ownership, runtime resolution, and status reporting.

The registry merges runtime inventory while fencing lifecycle-owned cache
entries. Optional hooks provide desktop and background-process heartbeat data.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog

from ..models import DesktopSession, WorkspaceInfo
from ..runtime.base import RuntimeBackend

logger = structlog.get_logger(__name__)


class WorkspaceRegistry:
    """Owns the in-memory workspace cache and runtime resolution.

    The runner composition root injects the shared runtime. Optional
    hooks provide desktop and background-process heartbeat data.
    """

    def __init__(
        self,
        runtime: RuntimeBackend | None = None,
        settings: Any | None = None,
    ) -> None:
        self._runtime = runtime
        self._settings = settings
        self._cache: dict[uuid.UUID, WorkspaceInfo] = {}
        self._epochs: dict[uuid.UUID, int] = {}
        self._active: set[uuid.UUID] = set()
        self._lifecycle_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._refresh_lock = asyncio.Lock()
        # Optional cross-cluster hooks, set by the runner composition root.
        # All default to ``None`` so
        # the registry stays unit-testable in isolation.
        self.desktop_manager: Any | None = None
        self.desktop_sessions: dict[uuid.UUID, DesktopSession] | None = None
        self.background_entries: dict[uuid.UUID, dict[str, Any]] | None = None
        self.background_statuses: (
            Callable[[RuntimeBackend, str, list[Any]], Awaitable[list[dict[str, Any]]]]
            | None
        ) = None
        self.desktop_live: Callable[[uuid.UUID], Awaitable[bool]] | None = None

    # -- cache ownership -------------------------------------------------

    @asynccontextmanager
    async def lifecycle(self, workspace_id: uuid.UUID) -> AsyncIterator[None]:
        """Serialize one resource, not heartbeats or unrelated workspaces.

        Epochs fence inventory which started before/during an awaited operation.
        Even failures advance the fence: their pre-operation snapshot is unsafe.
        """
        lock = self._lifecycle_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            self._epochs[workspace_id] = self._epochs.get(workspace_id, 0) + 1
            self._active.add(workspace_id)
            try:
                yield
            finally:
                self._active.remove(workspace_id)
                self._epochs[workspace_id] += 1

    @asynccontextmanager
    async def live_boundary(self, workspace_id: uuid.UUID) -> AsyncIterator[None]:
        """Admit live work after pending lifecycle effects, rejecting stale waits.

        Share the lifecycle mutex only for admission, not the runtime operation:
        stop must remain able to close processes whose reads are in flight.
        """
        epoch = self._epochs.get(workspace_id, 0)
        lock = self._lifecycle_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            if epoch != self._epochs.get(workspace_id, 0):
                # Preserve the established missing-workspace error after remove.
                self.get_cached(workspace_id)
                raise RuntimeError("Workspace lifecycle changed while waiting")
            yield

    async def sync_from_runtime(self) -> None:
        """Merge observations without detaching lifecycle-owned cache objects."""
        async with self._refresh_lock:
            epochs = self._epochs.copy()
            active = self._active.copy()
            observed: dict[uuid.UUID, WorkspaceInfo] = {}
            # Collect first: a failed/incomplete enumeration must not delete cache.
            runtime = self._runtime
            for info in await runtime.list_workspaces():
                try:
                    ws_id = uuid.UUID(info.workspace_id)
                except ValueError:
                    logger.warning(
                        "skipping_invalid_workspace_id", raw_id=info.workspace_id
                    )
                    continue
                observed[ws_id] = WorkspaceInfo(
                    workspace_id=ws_id,
                    instance_id=info.instance_id,
                    status=info.status,
                )

            for ws_id in self._cache.keys() | observed.keys():
                if (
                    ws_id in active
                    or ws_id in self._active
                    or epochs.get(ws_id, 0) != self._epochs.get(ws_id, 0)
                ):
                    continue
                existing = self._cache.get(ws_id)
                incoming = observed.get(ws_id)
                if incoming is None:
                    if existing is not None and existing.status != "creating":
                        self._cache.pop(ws_id, None)
                elif (
                    existing is not None
                    and existing.instance_id == incoming.instance_id
                ):
                    # Credentials/progress are lifecycle-owned, never inventory-derived.
                    existing.status = incoming.status
                else:
                    # Different incarnation: no old credentials/proof can be inherited.
                    self._cache[ws_id] = incoming
            logger.info("cache_synced_from_runtime", workspace_count=len(self._cache))

    def get_cached(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Look up a workspace in the cache or raise."""
        info = self._cache.get(workspace_id)
        if info is None:
            raise ValueError(f"Workspace {workspace_id} not found")
        return info

    def get_runtime(self, workspace_id: uuid.UUID) -> RuntimeBackend:
        """Return the runtime backend for a workspace."""
        self.get_cached(workspace_id)
        if self._runtime is None:
            raise RuntimeError("Workspace runtime is unavailable")
        return self._runtime

    def workspace_exists(self, workspace_id: uuid.UUID) -> bool:
        """Return True when *workspace_id* is present in the cache."""
        return workspace_id in self._cache

    async def get_workspace_heartbeat_statuses(self) -> list[dict]:
        """Return workspace heartbeat payload including live desktop sessions."""
        sessions = self.desktop_sessions if self.desktop_sessions is not None else {}
        entries = self.background_entries if self.background_entries is not None else {}
        payload: list[dict] = []
        for info in list(self._cache.values()):
            workspace_id = info.workspace_id
            item = {
                "workspace_id": str(workspace_id),
                "status": info.status,
            }
            session = sessions.get(workspace_id)
            if self.desktop_manager is not None:
                # DesktopManager serializes lifecycle probes and owns pruning.
                item["desktop_epoch"] = self.desktop_manager.epoch
                (
                    desktop_payload,
                    tracked,
                    generation,
                ) = await self.desktop_manager.reconcile_heartbeat_session(
                    workspace_id,
                    session,
                    workspace_running=info.status == "running",
                    liveness_probe=self.desktop_live,
                )
                item["desktop_generation"] = generation
                item["desktop_session_tracked"] = tracked
                item["desktop"] = desktop_payload
            else:
                item["desktop_session_tracked"] = session is not None
                item["desktop"] = None

            processes: list[dict[str, Any]] = []
            if info.status == "running":
                runtime = self._runtime
                tracked = list(entries.get(workspace_id, {}).values())
                if tracked:
                    try:
                        if runtime is None or not info.instance_id:
                            raise RuntimeError("runtime unavailable")
                        if self.background_statuses is None:
                            raise RuntimeError("runtime unavailable")
                        processes = await self.background_statuses(
                            runtime, info.instance_id, tracked
                        )
                    except Exception:
                        logger.exception(
                            "background_heartbeat_failed",
                            workspace_id=str(workspace_id),
                        )
                        processes = [
                            {
                                "process_id": entry.process_id,
                                "status": "unknown",
                                "exit_code": None,
                                "pid": entry.pid,
                            }
                            for entry in tracked
                        ]
            item["processes"] = processes

            payload.append(item)

        return payload

    async def get_vm_metrics(self) -> dict[str, dict[str, Any]]:
        """Collect host-observed metrics for QEMU workspaces."""
        qemu_runtime = self._runtime
        if qemu_runtime is None:
            return {}

        get_workspace_usage = getattr(qemu_runtime, "get_workspace_usage", None)
        if not callable(get_workspace_usage):
            return {}

        async def collect(workspace_id, info) -> dict[str, Any] | None:
            try:
                return await get_workspace_usage(info.instance_id)
            except Exception:
                logger.exception(
                    "vm_metrics_collect_failed",
                    workspace_id=str(workspace_id),
                )
                return None

        # Guest probes are bounded per VM; collect concurrently so one slow
        # guest does not delay the report of all others.
        entries = list(self._cache.items())
        results = await asyncio.gather(
            *(collect(workspace_id, info) for workspace_id, info in entries)
        )
        return {
            str(workspace_id): usage
            for (workspace_id, _), usage in zip(entries, results, strict=True)
            if usage is not None
        }
