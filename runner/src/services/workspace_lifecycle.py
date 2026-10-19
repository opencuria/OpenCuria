"""Workspace lifecycle orchestration across runtime and session managers.

The lifecycle owns workspace transitions and coordinates the injected
runtime, credential, exec, background, stream, and desktop managers.
Checkpoint and scrub-proof hooks connect lifecycle transitions to the
websocket's durable workspace state.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from functools import wraps
from typing import Any

import structlog

from ..models import WorkspaceInfo
from ..runtime.base import RuntimeBackend, WorkspaceConfig
from .capture_fence import CaptureFence, Method
from .sessions.desktop import WorkspaceEndedEvidence
from .workspace_registry import WorkspaceRegistry

logger = structlog.get_logger(__name__)


def coordinated(method: Method) -> Method:
    """Fence awaited lifecycle effects against stale registry observations."""
    signature = inspect.signature(method)

    @wraps(method)
    async def wrapped(self: WorkspaceLifecycle, *args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(self, *args, **kwargs)
        workspace_id = bound.arguments.get("workspace_id")
        if workspace_id is None:
            workspace_id = uuid.uuid4()
            bound.arguments["workspace_id"] = workspace_id
        fence = self.capture_fence
        if fence is not None:
            fence.check_available(workspace_id)
        async with self._registry.lifecycle(workspace_id):
            if fence is None:
                return await method(*bound.args, **bound.kwargs)
            async with fence.interaction(workspace_id, coordinated=True):
                return await method(*bound.args, **bound.kwargs)

    return wrapped  # type: ignore[return-value]


class WorkspaceLifecycle:
    """Sole orchestrator of workspace lifecycle transitions.

    Dependencies are the registry, runtime mapping, and canonical managers
    used to carry out each lifecycle transition.
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
        self.capture_fence: CaptureFence | None = None
        self.checkpoint_hook = None
        self.scrub_proof_hook = None
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

    async def _confirm_workspace_ended(
        self, info: WorkspaceInfo, operation: str
    ) -> None:
        """Publish successful physical disposal before registry metadata disappears."""
        evidence = WorkspaceEndedEvidence(
            info.workspace_id, info.instance_id, info.runtime_type, operation
        )
        confirm = getattr(self._streams, "confirm_workspace_ended", None)
        if confirm is None:
            raise RuntimeError(
                "Managed stream lifecycle confirmation is not configured"
            )
        result = await confirm(info.workspace_id, info.instance_id)
        if result is not True:
            raise RuntimeError("Managed stream physical disposal confirmation failed")
        await self._desktop.workspace_ended(
            info.workspace_id, info.instance_id, info.runtime_type, evidence
        )

    async def _close_managed_before_end(
        self, workspace_id: uuid.UUID, reason: str
    ) -> None:
        """Attempt graceful owner closure; physical disposal is the final proof."""
        for operation in (
            lambda: self._streams.close_workspace_streams(workspace_id, reason=reason),
            lambda: self._desktop.release_workspace(workspace_id),
        ):
            try:
                await operation()
            except Exception:
                logger.exception(
                    "workspace_owner_preclose_unverified",
                    workspace_id=str(workspace_id),
                    reason=reason,
                )

    async def _remove_managed_workspace(
        self, workspace_id: uuid.UUID, reason: str
    ) -> bool:
        """Keep lookup metadata and durable ownership until actual removal succeeds."""
        info = self._registry._cache.get(workspace_id)
        if info is None:
            raise RuntimeError(
                "Managed workspace removal requires physical registry identity"
            )
        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None or not info.instance_id:
            raise RuntimeError("Managed workspace removal requires runtime identity")
        await self._background._kill_all_background_processes(
            workspace_id, reason=reason
        )
        await self._close_managed_before_end(workspace_id, reason)
        # Lifecycle's coordinated workspace lock fences replacement/start. Never
        # publish successful death on exceptions, or remove cache before confirmation.
        await runtime.remove_workspace(info.instance_id)
        await self._confirm_workspace_ended(info, "remove")
        self._registry._cache.pop(workspace_id, None)
        self._unreachable_since.pop(workspace_id, None)
        return True

    async def _inject_with_checkpoint(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env_vars: dict[str, str] | None,
        files: list[dict[str, Any]] | None,
        ssh_keys: list[str] | None,
        log: Any,
    ) -> bool:
        """Checkpoint credential injection, then use the credential manager."""
        if self.checkpoint_hook is not None:
            workspace_id = next(
                (
                    key
                    for key, info in self._registry._cache.items()
                    if info.instance_id == instance_id
                ),
                None,
            )
            if workspace_id is not None:
                await self.checkpoint_hook(workspace_id, None, "injecting")
        return await self._credentials.inject_workspace_credentials(
            runtime, instance_id, env_vars, files, ssh_keys, log
        )

    # -- lifecycle ---------------------------------------------------------

    @coordinated
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
            # A thrown error need not mean runtime creation had no effects.
            # Keep the incomplete entry for reconciliation/operator recovery.
            self._registry.get_cached(workspace_id).credentials_present = None
            raise

        # Update cache with the real instance_id now that the runtime has assigned it.
        self._registry.get_cached(workspace_id).instance_id = instance_id

        credentials_present = await self._inject_with_checkpoint(
            runtime,
            instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )

        for repo_url in repos:
            log.info("cloning_repo", repo=repo_url)
            exit_code, output = await self._exec_kernel.exec_command(
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
        self._registry._cache[workspace_id].credentials_present = credentials_present

        if self.checkpoint_hook is not None:
            await self.checkpoint_hook(
                workspace_id, credentials_present, "workspace:created"
            )
        log.info("workspace_ready", credentials_present=credentials_present)
        return workspace_id, credentials_present

    @coordinated
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
        # Security decisions require a live observation, not heartbeat cache.
        status = await runtime.get_workspace_status(info.instance_id)
        info.status = status.status
        if info.status in {"exited", "stopped"}:
            if self.scrub_proof_hook is not None and await self.scrub_proof_hook(
                workspace_id
            ):
                info.credentials_present = False
                return False
            raise RuntimeError(
                "Stopped guest has no exact scrub proof; manual intervention required"
            )
        if info.status != "running":
            raise RuntimeError("Workspace live status is unknown or not running")

        info.credentials_present = None
        await self._credentials.remove_workspace_credentials(
            runtime, info.instance_id, log
        )
        await self._background._kill_all_background_processes(
            workspace_id, reason="stop"
        )
        await self._close_managed_before_end(workspace_id, "stop")
        await runtime.stop_workspace(info.instance_id)
        await self._confirm_workspace_ended(info, "stop")
        info.status = "exited"
        info.credentials_present = False
        if self.checkpoint_hook is not None:
            await self.checkpoint_hook(workspace_id, False, "scrubbed")
        log.info("workspace_stopped")
        return False

    @coordinated
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
        info.credentials_present = None
        runtime = self._registry.get_runtime(workspace_id)

        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        if self.checkpoint_hook is not None:
            await self.checkpoint_hook(workspace_id, None, "injecting")

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
        info.credentials_present = None
        credentials_present = await self._inject_with_checkpoint(
            runtime,
            info.instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )
        log.info("workspace_resumed", credentials_present=credentials_present)
        info.credentials_present = credentials_present
        return credentials_present

    @coordinated
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
        return await self._inject_with_checkpoint(
            runtime,
            info.instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )

    @coordinated
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
        await self._streams.close_workspace_streams(
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
        await self._background._drop_background_tracking(
            workspace_id, reason="reconfigure_resources"
        )
        info.status = "running"

    @coordinated
    async def remove_workspace(self, workspace_id: uuid.UUID) -> None:
        """Remove the exact guest before finalizing durable owners and cache state."""
        await self._remove_managed_workspace(workspace_id, "remove")

    @coordinated
    async def cleanup_unknown_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Remove a backend-unknown guest while retaining identity until confirmed."""
        return await self._remove_managed_workspace(workspace_id, "cleanup_unknown")

    # -- self-healing SSH health check -------------------------------------

    async def _check_workspace_reachable(
        self, workspace_id: uuid.UUID, info: WorkspaceInfo
    ) -> bool:
        """Return True if the runtime's lightweight reachability probe succeeds.

        Uses a short timeout so the loop does not block for a long time.
        """
        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None or not info.instance_id:
            return True  # cannot check — assume reachable to avoid false restarts

        try:
            return await asyncio.wait_for(
                runtime.check_reachable(info.instance_id),
                timeout=15,
            )
        except Exception:
            return False

    async def run_health_check_loop(self) -> None:
        """Report unreachable guests without altering their runtime state."""
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

                    # Unreachable SSH is health evidence, never reset authority.
                    if unreachable_for >= timeout:
                        log.error(
                            "workspace_unhealthy_requires_intervention",
                            workspace_id=str(ws_id),
                        )

            except asyncio.CancelledError:
                log.info("health_check_loop_stopped")
                break
            except Exception:
                log.exception("health_check_loop_error")
