"""Workspace lifecycle orchestration across runtime and session managers.

The lifecycle owns workspace transitions and coordinates the injected
runtime, credential, background, stream, and desktop managers, plus harness
execution. Checkpoint and scrub-proof hooks connect lifecycle transitions to the
websocket's durable workspace state.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
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

    Dependencies are the registry, runtime, and canonical managers
    used to carry out each lifecycle transition.
    """

    def __init__(
        self,
        registry: WorkspaceRegistry,
        runtime: RuntimeBackend | None = None,
        credentials: Any | None = None,
        exec_harness_command: (
            Callable[[uuid.UUID, list[str]], Awaitable[tuple[int, str, str]]] | None
        ) = None,
        background: Any | None = None,
        streams: Any | None = None,
        desktop: Any | None = None,
        *,
        repo_clone_timeout: float = 1800,
    ) -> None:
        self.capture_fence: CaptureFence | None = None
        self.checkpoint_hook = None
        self.scrub_proof_hook = None
        self.partial_workspace_identity: (
            Callable[[uuid.UUID], Awaitable[str | None]] | None
        ) = None
        self._registry = registry
        self._runtime = runtime
        self._credentials = credentials
        self._exec_harness_command = exec_harness_command
        self._background = background
        self._streams = streams
        self._desktop = desktop
        self._repo_clone_timeout = repo_clone_timeout

    async def _confirm_workspace_ended(
        self, info: WorkspaceInfo, operation: str
    ) -> None:
        """Publish successful physical disposal before registry metadata disappears."""
        evidence = WorkspaceEndedEvidence(
            info.workspace_id, info.instance_id, operation
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
            info.workspace_id, info.instance_id, evidence
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

        Returns the workspace UUID and whether credentials were injected.
        """
        if workspace_id is None:
            workspace_id = uuid.uuid4()

        runtime = self._runtime

        log = logger.bind(
            workspace_id=str(workspace_id),
        )
        log.info("creating_workspace", repos=repos)

        if not base_image_path:
            raise RuntimeError("QEMU workspace creation requires a base image path")
        config = WorkspaceConfig(
            workspace_id=str(workspace_id),
            image=base_image_path,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
        )

        # Register a "creating" cache entry *before* calling
        # runtime.create_workspace() so that heartbeat syncs during VM boot
        # (which can take 60 s+ for QEMU) do not drop this workspace and
        # cause the backend to mark it as failed.  instance_id is unknown at
        # this point — it will be updated once create_workspace() returns.
        self._registry.register_new_workspace(
            WorkspaceInfo(
                workspace_id=workspace_id,
                instance_id="",
                status="creating",
            )
        )

        try:
            instance_id = await runtime.create_workspace(config)
        except Exception:
            log.exception("workspace_creation_failed")
            # A thrown error need not mean runtime creation had no effects.
            # Keep the incomplete entry for reconciliation and safe retry/removal.
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
            # Bound the guest process itself: cancelling SSH only abandons the
            # wait and cannot prove that git stopped writing to the disk.
            try:
                exit_code, stdout, stderr = await asyncio.wait_for(
                    self._exec_harness_command(
                        workspace_id,
                        [
                            "timeout",
                            "--kill-after=10s",
                            f"{self._repo_clone_timeout:g}s",
                            "env",
                            "GIT_TERMINAL_PROMPT=0",
                            "git",
                            "clone",
                            repo_url,
                        ],
                    ),
                    timeout=self._repo_clone_timeout + 15,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "Repository clone did not return after its execution limit"
                ) from exc
            if exit_code != 0:
                log.warning("repo_clone_failed", repo=repo_url, output=stdout + stderr)
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
                "Stopped guest has no exact scrub proof; "
                "resume and stop to verify credential removal"
            )
        if info.status != "running":
            raise RuntimeError("Workspace live status is unknown or not running")

        info.credentials_present = None
        if self.checkpoint_hook is not None:
            await self.checkpoint_hook(workspace_id, None, "scrubbing")
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

        if qemu_vcpus is None or qemu_memory_mb is None or qemu_disk_size_gb is None:
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
        if self.checkpoint_hook is not None:
            await self.checkpoint_hook(
                workspace_id, credentials_present, "workspace:resumed"
            )
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
        info.credentials_present = None
        credentials_present = await self._inject_with_checkpoint(
            runtime,
            info.instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )
        info.credentials_present = credentials_present
        if self.checkpoint_hook is not None:
            await self.checkpoint_hook(
                workspace_id, credentials_present, "workspace:credentials_injected"
            )
        return credentials_present

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
        info = self._registry._cache.get(workspace_id)
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("Workspace runtime is unavailable")
        if (info is None or not info.instance_id) and self.partial_workspace_identity:
            instance_id = await self.partial_workspace_identity(workspace_id)
            if instance_id is not None:
                if info is None:
                    info = WorkspaceInfo(workspace_id, instance_id, "creating")
                    self._registry.register_new_workspace(info)
                else:
                    info.instance_id = instance_id
        if info is None or not info.instance_id:
            # QEMU can prove the exact canonical allocation is fully absent,
            # including files and network. Domain absence alone is insufficient.
            verify_removed = getattr(runtime, "workspace_removed", None)
            instance_id = str(workspace_id)
            if verify_removed is None or not await verify_removed(instance_id):
                raise RuntimeError(
                    "Managed workspace removal requires physical registry identity"
                )
            if info is None:
                info = WorkspaceInfo(workspace_id, instance_id, "removed")
                self._registry.register_new_workspace(info)
            else:
                info.instance_id = instance_id
                info.status = "removed"
            await self._confirm_workspace_ended(info, "remove")
            self._registry._cache.pop(workspace_id, None)
            return
        await self._background._kill_all_background_processes(
            workspace_id, reason="remove"
        )
        await self._close_managed_before_end(workspace_id, "remove")
        # Lifecycle's coordinated workspace lock fences replacement/start. Never
        # publish successful death on exceptions, or remove cache before confirmation.
        await runtime.remove_workspace(info.instance_id)
        await self._confirm_workspace_ended(info, "remove")
        self._registry._cache.pop(workspace_id, None)
