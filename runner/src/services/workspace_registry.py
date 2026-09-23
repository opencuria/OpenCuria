"""Workspace registry: ownership of the in-memory workspace cache.

Step 8 (final): canonical home of the cache-ownership logic previously
living on ``WorkspaceService`` in :mod:`src.service`:

- :meth:`WorkspaceRegistry.sync_from_runtime`,
- :meth:`WorkspaceRegistry._get_cached` / :meth:`get_cached`,
- :meth:`WorkspaceRegistry._get_runtime` / :meth:`get_runtime`,
- :meth:`WorkspaceRegistry._get_runtime_by_type` /
  :meth:`get_runtime_by_type`,
- :meth:`WorkspaceRegistry.supported_runtimes` (property),
- :meth:`WorkspaceRegistry.list_workspaces`,
- :meth:`WorkspaceRegistry.get_workspace`,
- :meth:`WorkspaceRegistry.get_workspace_statuses`,
- :meth:`WorkspaceRegistry.get_workspace_heartbeat_statuses`,
- :meth:`WorkspaceRegistry.recover_desktop_sessions_from_runtime`,
- :meth:`WorkspaceRegistry.get_vm_metrics`,
- :meth:`WorkspaceRegistry.workspace_exists`.

Bodies are verbatim moves of the ``WorkspaceService`` implementations
(all read/write ``self._cache`` / ``self._runtimes`` semantics
preserved, including creating-preservation, tracking-drop and the
heartbeat payload shape). Cross-cluster reads (desktop sessions,
background statuses, desktop liveness) go through optional hooks set
post-construction by the composer (``WorkspaceService``), so the
registry stays unit-testable without importing sibling managers:

- ``desktop_sessions``: live ``dict[uuid.UUID, DesktopSession]`` used
  for heartbeat/recover reads and session pruning.
- ``background_statuses``: async
  ``(runtime, instance_id, entry) -> dict`` hook (normally
  ``BackgroundProcessManager._background_status_locked``).
- ``background_entries``: live
  ``dict[uuid.UUID, dict[str, BackgroundProcess]]`` tracking dict
  (normally ``BackgroundProcessManager._background_processes``).
- ``desktop_live``: async ``(workspace_id) -> bool`` hook (normally
  ``DesktopManager._is_desktop_session_live``).
- ``desktop_heartbeat_payload``: ``(workspace_id, session) -> dict``
  hook (normally ``DesktopManager._desktop_heartbeat_payload``).

Hooks are only called when set; without them the registry degrades to
plain cache ownership (heartbeat omits desktop/process enrichment
gracefully, recover is a no-op).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import structlog

from ..models import DesktopSession, WorkspaceInfo
from ..runtime.base import RuntimeBackend

logger = structlog.get_logger(__name__)


class WorkspaceRegistry:
    """Owns the in-memory workspace cache and runtime resolution.

    Injected dependencies (all mirror ``WorkspaceService`` helpers):

    - ``runtimes``: runtime backends by type (kept for introspection;
      resolution itself goes through the methods below so a plain
      dict reassignment stays visible — the composer passes the same
      dict object it hands to every manager).
    - ``settings``: runner settings (kept for forward compatibility;
      unused by the registry itself).
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        settings: Any | None = None,
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {}
        self._settings = settings
        self._cache: dict[uuid.UUID, WorkspaceInfo] = {}
        # Optional cross-cluster hooks, set post-construction by the
        # composer (``WorkspaceService``). All default to ``None`` so
        # the registry stays unit-testable in isolation.
        self.desktop_sessions: dict[uuid.UUID, DesktopSession] | None = None
        self.background_entries: dict[uuid.UUID, dict[str, Any]] | None = None
        self.background_status: (
            Callable[[RuntimeBackend, str, Any], Awaitable[dict[str, Any]]] | None
        ) = None
        self.desktop_live: (
            Callable[[uuid.UUID], Awaitable[bool]] | None
        ) = None
        self.desktop_heartbeat_payload: (
            Callable[[uuid.UUID, DesktopSession], dict[str, Any]] | None
        ) = None

    # -- cache ownership -------------------------------------------------

    async def sync_from_runtime(self) -> None:
        """Rebuild the in-memory cache from live runtime state.

        Called at startup and can be called periodically to reconcile
        the cache with actual runtime state (e.g. workspaces killed
        externally).  Queries all registered runtime backends.
        """
        new_cache: dict[uuid.UUID, WorkspaceInfo] = {}

        for runtime_type, runtime in self._runtimes.items():
            infos = await runtime.list_workspaces()
            for info in infos:
                try:
                    ws_id = uuid.UUID(info.workspace_id)
                except ValueError:
                    logger.warning(
                        "skipping_invalid_workspace_id",
                        raw_id=info.workspace_id,
                    )
                    continue

                existing = self._cache.get(ws_id)

                new_cache[ws_id] = WorkspaceInfo(
                    workspace_id=ws_id,
                    instance_id=info.instance_id,
                    status=info.status,
                    runtime_type=runtime_type,
                    created_at=(
                        existing.created_at if existing else datetime.now(timezone.utc)
                    ),
                )

        # Preserve "creating" entries that are not yet visible to the runtime.
        # A workspace in the "creating" state has been registered by the service
        # layer but runtime.create_workspace() is still in progress (e.g. the
        # QEMU VM is booting).  Dropping it from the cache would cause the
        # next heartbeat to omit it and the backend to mark it as failed.
        for ws_id, existing in self._cache.items():
            if existing.status == "creating" and ws_id not in new_cache:
                new_cache[ws_id] = existing

        self._cache = new_cache
        logger.info(
            "cache_synced_from_runtime",
            workspace_count=len(self._cache),
        )

    def _get_cached(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Look up a workspace in the cache or raise."""
        return self.get_cached(workspace_id)

    def get_cached(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Look up a workspace in the cache or raise."""
        info = self._cache.get(workspace_id)
        if info is None:
            raise ValueError(f"Workspace {workspace_id} not found")
        return info

    def _get_runtime(self, workspace_id: uuid.UUID) -> RuntimeBackend:
        """Return the runtime backend for a workspace."""
        return self.get_runtime(workspace_id)

    def get_runtime(self, workspace_id: uuid.UUID) -> RuntimeBackend:
        """Return the runtime backend for a workspace."""
        info = self.get_cached(workspace_id)
        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' not available for "
                f"workspace {workspace_id}"
            )
        return runtime

    def _get_runtime_by_type(self, runtime_type: str) -> RuntimeBackend:
        """Return the runtime backend by type name."""
        return self.get_runtime_by_type(runtime_type)

    def get_runtime_by_type(self, runtime_type: str) -> RuntimeBackend:
        """Return the runtime backend by type name."""
        runtime = self._runtimes.get(runtime_type)
        if runtime is None:
            raise RuntimeError(f"Runtime '{runtime_type}' is not enabled")
        return runtime

    @property
    def supported_runtimes(self) -> list[str]:
        """Return the list of enabled runtime type names."""
        return list(self._runtimes.keys())

    def workspace_exists(self, workspace_id: uuid.UUID) -> bool:
        """Return True when *workspace_id* is present in the cache."""
        return workspace_id in self._cache

    # -- read surface ----------------------------------------------------

    async def list_workspaces(self) -> list[WorkspaceInfo]:
        """Return all known workspaces, refreshing from the runtime."""
        await self.sync_from_runtime()
        return list(self._cache.values())

    async def get_workspace(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Return a single workspace by ID, checking live status."""
        info = self.get_cached(workspace_id)
        runtime = self.get_runtime(workspace_id)

        # Refresh status from runtime
        if info.instance_id:
            try:
                status = await runtime.get_workspace_status(info.instance_id)
                info.status = status.status
            except Exception:
                info.status = "unknown"

        return info

    def get_workspace_statuses(self) -> list[dict]:
        """Return lightweight status list for heartbeat reporting.

        Reads from the in-memory cache without hitting the runtime,
        so it's fast enough for periodic heartbeats.
        """
        return [
            {
                "workspace_id": str(info.workspace_id),
                "status": info.status,
                "runtime_type": info.runtime_type,
            }
            for info in self._cache.values()
        ]

    async def get_workspace_heartbeat_statuses(self) -> list[dict]:
        """Return workspace heartbeat payload including live desktop sessions."""
        sessions = self.desktop_sessions if self.desktop_sessions is not None else {}
        entries = self.background_entries if self.background_entries is not None else {}
        payload: list[dict] = []
        for info in self._cache.values():
            workspace_id = info.workspace_id
            item = {
                "workspace_id": str(workspace_id),
                "status": info.status,
                "runtime_type": info.runtime_type,
            }

            session = sessions.get(workspace_id)
            if session is not None:
                try:
                    live = (
                        await self.desktop_live(workspace_id)
                        if self.desktop_live is not None
                        else True
                    )
                    if live:
                        if self.desktop_heartbeat_payload is not None:
                            item["desktop"] = self.desktop_heartbeat_payload(
                                workspace_id, session
                            )
                    else:
                        sessions.pop(workspace_id, None)
                        item["desktop"] = None
                        logger.warning(
                            "desktop_session_pruned_from_cache",
                            workspace_id=str(workspace_id),
                        )
                except Exception:
                    sessions.pop(workspace_id, None)
                    item["desktop"] = None
                    logger.exception(
                        "desktop_session_health_check_failed",
                        workspace_id=str(workspace_id),
                    )

            processes: list[dict[str, Any]] = []
            for entry in entries.get(workspace_id, {}).values():
                try:
                    info_for_proc = self._cache.get(workspace_id)
                    runtime_for_proc = (
                        self._runtimes.get(info_for_proc.runtime_type)
                        if info_for_proc is not None
                        else None
                    )
                    if info_for_proc is None or runtime_for_proc is None:
                        raise RuntimeError("runtime unavailable")
                    if not info_for_proc.instance_id:
                        raise RuntimeError("no instance assigned")
                    if self.background_status is None:
                        raise RuntimeError("runtime unavailable")
                    processes.append(
                        await self.background_status(
                            runtime_for_proc,
                            info_for_proc.instance_id,
                            entry,
                        )
                    )
                except Exception:
                    logger.exception(
                        "background_heartbeat_failed",
                        workspace_id=str(workspace_id),
                        process_id=entry.process_id,
                    )
                    processes.append(
                        {
                            "process_id": entry.process_id,
                            "status": "unknown",
                            "exit_code": None,
                            "pid": entry.pid,
                        }
                    )
            item["processes"] = processes

            payload.append(item)

        return payload

    async def recover_desktop_sessions_from_runtime(self) -> None:
        """Rebuild in-memory desktop sessions from live runtime state."""
        sessions = self.desktop_sessions if self.desktop_sessions is not None else {}
        for workspace_id, info in self._cache.items():
            if info.status != "running" or workspace_id in sessions:
                continue

            try:
                live = (
                    await self.desktop_live(workspace_id)
                    if self.desktop_live is not None
                    else False
                )
                if not live:
                    continue
            except Exception:
                logger.exception(
                    "desktop_session_recovery_failed",
                    workspace_id=str(workspace_id),
                )
                continue

            sessions[workspace_id] = DesktopSession(
                workspace_id=workspace_id,
                instance_id=info.instance_id,
            )
            logger.info(
                "desktop_session_recovered",
                workspace_id=str(workspace_id),
            )

    async def get_vm_metrics(self) -> dict[str, dict[str, Any]]:
        """Collect host-observed metrics for QEMU workspaces."""
        qemu_runtime = self._runtimes.get("qemu")
        if qemu_runtime is None:
            return {}

        get_workspace_usage = getattr(qemu_runtime, "get_workspace_usage", None)
        if not callable(get_workspace_usage):
            return {}

        metrics: dict[str, dict[str, Any]] = {}
        for workspace_id, info in self._cache.items():
            if info.runtime_type != "qemu":
                continue
            try:
                usage = await get_workspace_usage(info.instance_id)
            except Exception:
                logger.exception(
                    "vm_metrics_collect_failed",
                    workspace_id=str(workspace_id),
                )
                continue

            if usage is None:
                continue

            metrics[str(workspace_id)] = usage

        return metrics
