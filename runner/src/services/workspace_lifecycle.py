"""Workspace lifecycle: sole cross-cluster orchestrator.

Step 8 (final): canonical home of the lifecycle orchestration previously
living on ``WorkspaceService`` in :mod:`src.service`:

- :meth:`WorkspaceLifecycle.create_workspace`,
- :meth:`WorkspaceLifecycle.stop_workspace`,
- :meth:`WorkspaceLifecycle.resume_workspace`,
- :meth:`WorkspaceLifecycle.inject_credentials` (workspace-level, thin),
- :meth:`WorkspaceLifecycle.inject_workspace_credentials`
  (runtime-level, thin),
- :meth:`WorkspaceLifecycle.update_workspace_resources`,
- :meth:`WorkspaceLifecycle.remove_workspace`,
- :meth:`WorkspaceLifecycle.cleanup_unknown_workspace`,
- :meth:`WorkspaceLifecycle._check_workspace_reachable`,
- :meth:`WorkspaceLifecycle.run_health_check_loop`.

Bodies are verbatim moves of the ``WorkspaceService`` implementations.
Cross-cluster calls (credential remove/inject, exec, stream close,
background kill/drop, desktop release/interrupt/lock) go through
optional hooks set post-construction by the composer
(``WorkspaceService``) as late-bound closures over the service facades,
so instance-attribute overrides and ``monkeypatch`` on the service keep
working exactly like the pre-Step-8 ``self.<facade>`` calls. When a
hook is ``None`` the lifecycle falls back to the injected manager
directly, so it stays unit-testable in isolation:

- ``remove_hook``: async ``(runtime, instance_id, log) -> None``
  (normally the service ``remove_workspace_credentials`` facade).
- ``inject_hook``: async
  ``(runtime, instance_id, env_vars, files, ssh_keys, log) -> bool``
  (normally the service ``inject_workspace_credentials`` facade).
- ``exec_hook``: async ``(runtime, instance_id, command) -> (exit, out)``
  (normally the service ``_exec_command`` facade).
- ``close_streams_hook``: async ``(workspace_id, reason) -> int``.
- ``kill_all_hook``: async ``(workspace_id, reason) -> None``.
- ``drop_tracking_hook``: async ``(workspace_id, reason) -> int``.
- ``release_hook``: async
  ``(workspace_id, holder, run_id, force) -> DesktopReleaseResult``.
- ``interrupt_hook``: async ``(workspace_id) -> None``.
- ``desktop_lock_hook``: async ``(workspace_id) -> asyncio.Lock``.

``_unreachable_since`` (self-healing timers) is owned here; the
composer exposes it as a read-through alias.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from ..models import WorkspaceInfo
from ..runtime.base import RuntimeBackend, WorkspaceConfig
from .sessions.desktop import DESKTOP_HOLDER_VIEWER
from .workspace_registry import WorkspaceRegistry

logger = structlog.get_logger(__name__)


class WorkspaceLifecycle:
    """Sole orchestrator of workspace lifecycle transitions.

    Injected dependencies (all mirror ``WorkspaceService`` helpers):

    - ``registry``: the :class:`WorkspaceRegistry` owning ``_cache``.
    - ``settings``: runner settings (docker/qemu network names, SSH
      health-check interval/timeout).
    - ``runtimes``: runtime backends by type (same dict object the
      composer holds; direct ``.get`` lookups mirror the verbatim
      bodies).
    - ``credentials``: ``CredentialManager`` (remainder of inject after
      the leading remove).
    - ``exec_kernel``: ``ExecKernel`` (repo-clone exec fallback).
    - ``background``: ``BackgroundProcessManager`` (kill/drop
      fallbacks).
    - ``streams``: ``StreamManager`` (close fallbacks).
    - ``desktop``: ``DesktopManager`` (release/interrupt/lock
      fallbacks plus session/recording dicts).
    """

    def __init__(
        self,
        registry: WorkspaceRegistry,
        settings: Any | None = None,
        runtimes: dict[str, RuntimeBackend] | None = None,
        credentials: Any | None = None,
        exec_kernel: Any | None = None,
        background: Any | None = None,
        streams: Any | None = None,
        desktop: Any | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._runtimes = runtimes if runtimes is not None else {}
        self._credentials = credentials
        self._exec_kernel = exec_kernel
        self._background = background
        self._streams = streams
        self._desktop = desktop
        # Self-healing: tracks when each workspace was first found
        # unreachable. Cleared once the workspace becomes reachable
        # again.
        self._unreachable_since: dict[uuid.UUID, float] = {}
        # Optional composer hooks (late-bound service-facade closures).
        # All default to ``None`` (direct-manager fallback).
        self.remove_hook: (
            Callable[[RuntimeBackend, str, Any], Awaitable[None]] | None
        ) = None
        self.inject_hook: (
            Callable[
                [RuntimeBackend, str, Any, Any, Any, Any], Awaitable[bool]
            ]
            | None
        ) = None
        self.exec_hook: (
            Callable[[RuntimeBackend, str, dict], Awaitable[tuple[int, str]]] | None
        ) = None
        self.close_streams_hook: (
            Callable[[uuid.UUID, str], Awaitable[int]] | None
        ) = None
        self.kill_all_hook: (
            Callable[[uuid.UUID, str], Awaitable[None]] | None
        ) = None
        self.drop_tracking_hook: (
            Callable[[uuid.UUID, str], Awaitable[int]] | None
        ) = None
        self.release_hook: (
            Callable[[uuid.UUID, str, Any, bool], Awaitable[Any]] | None
        ) = None
        self.interrupt_hook: (
            Callable[[uuid.UUID], Awaitable[None]] | None
        ) = None
        self.desktop_lock_hook: (
            Callable[[uuid.UUID], Awaitable[asyncio.Lock]] | None
        ) = None

    # -- hook-or-manager call sites --------------------------------------

    async def _call_remove(
        self, runtime: RuntimeBackend, instance_id: str, log: Any
    ) -> None:
        """Remove persisted credentials (hook-aware)."""
        if self.remove_hook is not None:
            await self.remove_hook(runtime, instance_id, log)
        else:
            await self._credentials.remove_workspace_credentials(
                runtime, instance_id, log
            )

    async def inject_workspace_credentials(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env_vars: dict[str, str] | None,
        files: list[dict[str, Any]] | None,
        ssh_keys: list[str] | None,
        log: Any,
    ) -> bool:
        """Persist credentials on the workspace disk, replacing any previous set.

        Returns True when credential material was written, False when the
        workspace has no attached secrets after a clean remove.

        Step 8: verbatim move of the ``WorkspaceService`` facade logic —
        the leading remove goes through the hook-aware ``_call_remove``
        first (exactly like the pre-Step-8 overridable/mockable facade
        call), then the manager's ``_inject_after_remove`` materializes
        the remainder verbatim.
        """
        await self._call_remove(runtime, instance_id, log)
        return await self._credentials._inject_after_remove(
            runtime, instance_id, env_vars, files, ssh_keys, log
        )

    async def _call_inject(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env_vars: dict[str, str] | None,
        files: list[dict[str, Any]] | None,
        ssh_keys: list[str] | None,
        log: Any,
    ) -> bool:
        """Inject credentials (hook-aware, so service overrides apply)."""
        if self.inject_hook is not None:
            return await self.inject_hook(
                runtime, instance_id, env_vars, files, ssh_keys, log
            )
        return await self.inject_workspace_credentials(
            runtime, instance_id, env_vars, files, ssh_keys, log
        )

    async def _call_exec(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        command: dict,
    ) -> tuple[int, str]:
        """Execute a structured command dict (hook-aware)."""
        if self.exec_hook is not None:
            return await self.exec_hook(runtime, instance_id, command)
        return await self._exec_kernel.exec_command(runtime, instance_id, command)

    async def _call_close_streams(self, workspace_id: uuid.UUID, reason: str) -> int:
        """Close every stream bound to *workspace_id* (hook-aware)."""
        if self.close_streams_hook is not None:
            return await self.close_streams_hook(workspace_id, reason)
        return await self._streams.close_workspace_streams(
            workspace_id, reason=reason
        )

    async def _call_kill_all(self, workspace_id: uuid.UUID, reason: str) -> None:
        """Kill every tracked background process (hook-aware)."""
        if self.kill_all_hook is not None:
            await self.kill_all_hook(workspace_id, reason)
        else:
            await self._background._kill_all_background_processes(
                workspace_id, reason=reason
            )

    async def _call_drop_tracking(
        self, workspace_id: uuid.UUID, reason: str
    ) -> int:
        """Drop background tracking after a reboot (hook-aware)."""
        if self.drop_tracking_hook is not None:
            return await self.drop_tracking_hook(workspace_id, reason)
        return await self._background._drop_background_tracking(
            workspace_id, reason=reason
        )

    async def _call_release(
        self,
        workspace_id: uuid.UUID,
        holder: str,
        run_id: str | None = None,
        force: bool = False,
    ) -> Any:
        """Drop a desktop lease (hook-aware)."""
        if self.release_hook is not None:
            return await self.release_hook(workspace_id, holder, run_id, force)
        return await self._desktop.release_desktop(
            workspace_id, holder=holder, run_id=run_id, force=force
        )

    async def _call_interrupt(self, workspace_id: uuid.UUID) -> None:
        """Interrupt desktop recordings (hook-aware)."""
        if self.interrupt_hook is not None:
            await self.interrupt_hook(workspace_id)
        else:
            await self._desktop._interrupt_desktop_recordings(workspace_id)

    async def _call_desktop_lock(self, workspace_id: uuid.UUID) -> asyncio.Lock:
        """Return the workspace desktop lifecycle lock (hook-aware)."""
        if self.desktop_lock_hook is not None:
            return await self.desktop_lock_hook(workspace_id)
        return await self._desktop._desktop_lock(workspace_id)

    # -- lifecycle ---------------------------------------------------------

    async def create_workspace(
        self,
        repos: list[str],
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
        workspace_id: uuid.UUID | None = None,
        runtime_type: str = "docker",
        image_tag: str | None = None,
        base_image_path: str | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Create a new workspace, inject credentials, and clone repos.

        Args:
            repos: Git repository URLs to clone into the workspace.
            env_vars: Environment variables persisted in the workspace
                until a controlled stop.
            files: Credential files persisted in the workspace until a
                controlled stop.
            ssh_keys: SSH private keys persisted in the workspace until a
                controlled stop.
            workspace_id: Workspace ID assigned by the backend.
            runtime_type: Which runtime to use (``"docker"`` or ``"qemu"``).

        Returns the workspace UUID and whether credentials were injected.
        """
        if workspace_id is None:
            workspace_id = uuid.uuid4()

        runtime = self._registry.get_runtime_by_type(runtime_type)

        log = logger.bind(
            workspace_id=str(workspace_id),
            runtime=runtime_type,
        )
        log.info("creating_workspace", repos=repos)

        # Build runtime-appropriate config
        if runtime_type == "docker":
            if not image_tag:
                raise RuntimeError("Docker workspace creation requires an image tag")
            volume_name = f"opencuria-workspace-{workspace_id}"
            config = WorkspaceConfig(
                workspace_id=str(workspace_id),
                image=image_tag,
                env_vars={},
                volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
                network=self._settings.docker_network,
                labels={"opencuria.workspace-id": str(workspace_id)},
            )
        else:
            if not base_image_path:
                raise RuntimeError("QEMU workspace creation requires a base image path")
            # QEMU — image is base QCOW2 path, no Docker volumes
            config = WorkspaceConfig(
                workspace_id=str(workspace_id),
                image=base_image_path,
                env_vars={},
                network=self._settings.qemu_network,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
                labels={"opencuria.workspace-id": str(workspace_id)},
            )

        # Register a "creating" cache entry *before* calling
        # runtime.create_workspace() so that heartbeat syncs during VM boot
        # (which can take 60 s+ for QEMU) do not drop this workspace and
        # cause the backend to mark it as failed.  instance_id is unknown at
        # this point — it will be updated once create_workspace() returns.
        self._registry._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id,
            instance_id="",
            status="creating",
            runtime_type=runtime_type,
        )

        try:
            instance_id = await runtime.create_workspace(config)
        except Exception:
            log.exception("workspace_creation_failed")
            self._registry._cache.pop(workspace_id, None)
            raise

        # Update cache with the real instance_id now that the runtime has assigned it.
        self._registry._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id,
            instance_id=instance_id,
            status="creating",
            runtime_type=runtime_type,
        )

        credentials_present = await self._call_inject(
            runtime,
            instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )

        for repo_url in repos:
            log.info("cloning_repo", repo=repo_url)
            exit_code, output = await self._call_exec(
                runtime,
                instance_id,
                {
                    "args": ["git", "clone", repo_url],
                    "workdir": "/workspace",
                    "env": {},
                    "description": f"Clone repository: {repo_url}",
                },
            )
            if exit_code != 0:
                log.warning("repo_clone_failed", repo=repo_url, output=output)
            else:
                log.info("repo_cloned", repo=repo_url)

        self._registry._cache[workspace_id].status = "running"

        log.info("workspace_ready", credentials_present=credentials_present)
        return workspace_id, credentials_present

    async def stop_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Remove credentials then stop a running workspace.

        Returns False because credentials are stripped before the instance
        is stopped. Raises if credential removal fails so the workspace
        stays running with secrets still present.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._registry.get_cached(workspace_id)
        runtime = self._registry.get_runtime(workspace_id)

        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        await self._call_remove(runtime, info.instance_id, log)
        await self._call_kill_all(workspace_id, reason="stop")
        await self._call_close_streams(workspace_id, reason="stop")
        await self._call_release(
            workspace_id, holder=DESKTOP_HOLDER_VIEWER, force=True
        )
        await runtime.stop_workspace(info.instance_id)
        info.status = "exited"
        log.info("workspace_stopped")
        return False

    async def resume_workspace(
        self,
        workspace_id: uuid.UUID,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> bool:
        """Resume a stopped workspace and re-inject persistent credentials."""
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._registry.get_cached(workspace_id)
        runtime = self._registry.get_runtime(workspace_id)

        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        if info.runtime_type == "qemu":
            if (
                qemu_vcpus is None
                or qemu_memory_mb is None
                or qemu_disk_size_gb is None
            ):
                raise RuntimeError("Missing QEMU resource settings for resume")
            await runtime.reconfigure_workspace(
                info.instance_id,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
                restart=False,
            )

        await runtime.start_workspace(info.instance_id)
        info.status = "running"
        credentials_present = await self._call_inject(
            runtime,
            info.instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )
        log.info("workspace_resumed", credentials_present=credentials_present)
        return credentials_present

    async def inject_credentials(
        self,
        workspace_id: uuid.UUID,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> bool:
        """Replace persistent credentials on a running workspace."""
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._registry.get_cached(workspace_id)
        runtime = self._registry.get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        return await self._call_inject(
            runtime,
            info.instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )

    async def update_workspace_resources(
        self,
        workspace_id: uuid.UUID,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
    ) -> None:
        """Reconfigure resources for an existing QEMU workspace."""
        info = self._registry.get_cached(workspace_id)
        if info.runtime_type != "qemu":
            raise RuntimeError(
                "Workspace runtime does not support resource reconfiguration"
            )
        runtime = self._registry.get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        # Reconfigure restarts the VM: all in-workspace stream processes
        # die with it, so close the tracked sessions first.
        await self._call_close_streams(
            workspace_id, reason="reconfigure_resources"
        )
        await runtime.reconfigure_workspace(
            info.instance_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            restart=True,
        )
        # The VM rebooted: every RAM process is dead, so drop tracking.
        # Keeping entries would report stale exited rows and risk
        # signalling a reused foreign PID after the reboot.
        await self._call_drop_tracking(
            workspace_id, reason="reconfigure_resources"
        )
        info.status = "running"

    async def remove_workspace(self, workspace_id: uuid.UUID) -> None:
        """Remove a workspace and clean up resources.

        The per-workspace desktop lock is acquired first and held across
        the entire desktop-state/cache transition (recording interrupt
        while the cache is still available, then cache pop plus
        session/recording clear with no lock release in between). A
        concurrent ensure/start holding the lock therefore completes
        first; remove only pops the cache afterwards. A queued start/hold
        acquiring the same retained lock afterwards finds no cache entry
        and fails cleanly instead of resurrecting a session. The lock
        object itself is kept for the process lifetime (never dropped) so
        queued waiters and later callers always share one serialising
        lock. Runtime removal runs after the lock is released: it must
        never block behind a stuck Xvnc start while holding the desktop
        lock. ``_interrupt_desktop_recordings``/``_exec_desktop_shell``
        take no desktop lock and are awaited while holding it;
        correctness wins over head-of-line blocking (runtime commands
        have their own limits). Recording-interrupt errors still clear
        state and attempt runtime removal.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        await self._call_close_streams(workspace_id, reason="remove")
        await self._call_kill_all(workspace_id, reason="remove")
        lock = await self._call_desktop_lock(workspace_id)
        async with lock:
            # Interrupt while the cache is still available:
            # _exec_desktop_shell needs _get_cached, so popping first
            # would turn every interrupt into a "not found" failure.
            # _interrupt_desktop_recordings already swallows per-command
            # errors and clears the recordings dict; the outer guard only
            # covers unexpected failures so state clear + runtime remove
            # still run.
            try:
                await self._call_interrupt(workspace_id)
            except Exception:
                logger.exception(
                    "desktop_recording_interrupt_failed",
                    workspace_id=str(workspace_id),
                )
            info = self._registry._cache.pop(workspace_id, None)
            self._desktop._desktop_sessions.pop(workspace_id, None)
            # Final sweep for entries added during the interrupt awaits
            # (record_start is lock-free); still under the same hold, so
            # no waiter could publish a session in between.
            self._desktop._desktop_recordings = {
                key: value
                for key, value in self._desktop._desktop_recordings.items()
                if key[0] != workspace_id
            }

        if info and info.instance_id:
            runtime = self._runtimes.get(info.runtime_type)
            if runtime:
                await runtime.remove_workspace(info.instance_id)

        log.info("workspace_removed")

    async def cleanup_unknown_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Best-effort cleanup for a runtime workspace unknown to the backend.

        Returns ``True`` when a cached runtime instance was found and cleanup
        was attempted. Returns ``False`` when the workspace was already absent.

        Same atomicity as :meth:`remove_workspace`: the desktop lock is
        held across recording interrupt (cache still available), cache pop
        (plus unreachable-timer pop), and session/recording clear, with no
        release in between. The lock object is retained afterwards (never
        dropped) so queued waiters keep sharing one lock object.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        await self._call_close_streams(
            workspace_id, reason="cleanup_unknown"
        )
        await self._call_kill_all(
            workspace_id, reason="cleanup_unknown"
        )
        lock = await self._call_desktop_lock(workspace_id)
        async with lock:
            # Same ordering as remove_workspace: interrupt while the cache
            # is still available (per-command errors are swallowed inside
            # the helper; the guard only covers unexpected failures), then
            # pop and sweep with no lock release in between.
            try:
                await self._call_interrupt(workspace_id)
            except Exception:
                logger.exception(
                    "desktop_recording_interrupt_failed",
                    workspace_id=str(workspace_id),
                )
            info = self._registry._cache.pop(workspace_id, None)
            self._unreachable_since.pop(workspace_id, None)
            self._desktop._desktop_sessions.pop(workspace_id, None)
            self._desktop._desktop_recordings = {
                key: value
                for key, value in self._desktop._desktop_recordings.items()
                if key[0] != workspace_id
            }

        if info is None:
            log.info("unknown_workspace_already_absent")
            return False

        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' not available for cleanup"
            )

        if info.instance_id:
            await runtime.remove_workspace(info.instance_id)

        log.warning(
            "unknown_workspace_cleaned",
            runtime_type=info.runtime_type,
            instance_id=info.instance_id,
        )
        return True

    # -- self-healing SSH health check -------------------------------------

    async def _check_workspace_reachable(
        self, workspace_id: uuid.UUID, info: WorkspaceInfo
    ) -> bool:
        """Return True if the workspace responds to a lightweight exec probe.

        Uses a short timeout so the loop does not block for a long time.
        """
        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None or not info.instance_id:
            return True  # cannot check — assume reachable to avoid false restarts

        try:
            exit_code, _ = await asyncio.wait_for(
                runtime.exec_command_wait(
                    info.instance_id,
                    command=["echo", "ok"],
                ),
                timeout=15,
            )
            return exit_code == 0
        except Exception:
            return False

    async def run_health_check_loop(self) -> None:
        """Periodically probe running workspaces and restart unreachable ones.

        Runs indefinitely; cancel the task to stop it.

        A workspace is restarted when it has been continuously unreachable for
        more than ``settings.ssh_unreachable_timeout`` seconds.  After a
        restart, the unreachable timer is cleared so the workspace gets a
        fresh chance to come up.
        """
        interval = self._settings.ssh_health_check_interval
        timeout = self._settings.ssh_unreachable_timeout

        log = logger.bind(loop="health_check")
        log.info(
            "health_check_loop_started",
            check_interval_s=interval,
            unreachable_timeout_s=timeout,
        )

        while True:
            try:
                await asyncio.sleep(interval)

                # Snapshot the cache — do not hold it across awaits.
                candidates = [
                    (ws_id, info)
                    for ws_id, info in self._registry._cache.items()
                    if info.status == "running"
                ]

                for ws_id, info in candidates:
                    reachable = await self._check_workspace_reachable(ws_id, info)

                    if reachable:
                        # Clear any existing failure timer.
                        self._unreachable_since.pop(ws_id, None)
                        continue

                    # Workspace is unreachable.
                    first_failure = self._unreachable_since.setdefault(
                        ws_id, time.monotonic()
                    )
                    unreachable_for = time.monotonic() - first_failure

                    log.warning(
                        "workspace_unreachable",
                        workspace_id=str(ws_id),
                        unreachable_for_s=round(unreachable_for),
                        threshold_s=timeout,
                    )

                    if unreachable_for >= timeout:
                        log.error(
                            "workspace_self_healing_restart",
                            workspace_id=str(ws_id),
                            runtime=info.runtime_type,
                        )
                        try:
                            runtime = self._runtimes.get(info.runtime_type)
                            if runtime and info.instance_id:
                                # Hard reset kills all in-workspace stream
                                # processes: close tracked sessions first.
                                await self._call_close_streams(
                                    ws_id, reason="self_healing_restart"
                                )
                                await runtime.restart_workspace(info.instance_id)
                                # The VM rebooted: drop background tracking
                                # (RAM processes are dead; stale PIDs must
                                # never be signalled after a reboot).
                                await self._call_drop_tracking(
                                    ws_id, reason="self_healing_restart"
                                )
                                # Reset status and clear the failure timer.
                                if ws_id in self._registry._cache:
                                    self._registry._cache[ws_id].status = "running"
                                self._unreachable_since.pop(ws_id, None)
                                log.info(
                                    "workspace_self_healed",
                                    workspace_id=str(ws_id),
                                )
                        except Exception:
                            log.exception(
                                "workspace_self_heal_failed",
                                workspace_id=str(ws_id),
                            )

            except asyncio.CancelledError:
                log.info("health_check_loop_stopped")
                break
            except Exception:
                log.exception("health_check_loop_error")
