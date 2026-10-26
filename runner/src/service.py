"""Compose runner managers and expose workspace lifecycle operations.

The backend owns agent logic; runner managers execute its lifecycle,
terminal, file, desktop, image, git, and harness RPCs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import structlog

from .config import RunnerSettings
from .runtime.artifacts import RuntimeArtifactManager
from .runtime.base import RuntimeBackend
from .services.capture_fence import CaptureFence
from .services.credentials import WORKSPACE_CREDENTIAL_ENV_FILE, CredentialManager
from .services.files import FileManager
from .services.git_service import GitService
from .services.harness_exec import HarnessExecService
from .services.images import ImageManager
from .services.sessions.background import BackgroundProcessManager
from .services.sessions.desktop import DesktopManager
from .services.sessions.streams import StreamManager
from .services.sessions.terminals import TerminalManager
from .services.workspace_helpers import sanitize_exec_workdir
from .services.workspace_lifecycle import WorkspaceLifecycle
from .services.workspace_registry import WorkspaceRegistry

logger = structlog.get_logger(__name__)


class WorkspaceService:
    """Wire canonical managers to one registry and shared runtime."""

    def __init__(
        self,
        runtime: RuntimeBackend,
        settings: RunnerSettings,
    ) -> None:
        self._runtime = runtime
        self._settings = settings
        self._registry = WorkspaceRegistry(runtime, settings)
        get_cached = self._registry.get_cached
        get_runtime = self._registry.get_runtime
        self._credentials_manager = CredentialManager()
        self._terminals_manager = TerminalManager(
            runtime,
            get_cached,
            get_runtime,
            lambda workspace_id: self._registry._cache.pop(workspace_id, None),
            WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        self._images = ImageManager(
            runtime,
            get_cached,
            get_runtime,
            store_workspace=lambda info: self._registry._cache.__setitem__(
                info.workspace_id, info
            ),
            credentials_injector=self._credentials_manager.inject_workspace_credentials,
        )
        self._images.lifecycle_context = self._registry.lifecycle
        self._streams_manager = StreamManager(
            runtime, get_cached, get_runtime, sanitize_exec_workdir
        )
        self._background = BackgroundProcessManager(
            runtime,
            get_cached,
            get_runtime,
            sanitize_exec_workdir,
            WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        self._files_manager = FileManager(runtime, get_cached, get_runtime)
        self._harness = HarnessExecService(
            runtime,
            get_cached,
            get_runtime,
            sanitize_exec_workdir,
            WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        self._git = GitService(get_cached, get_runtime)
        self._desktop = DesktopManager(
            runtime, get_cached, get_runtime, self._harness.exec_harness_command
        )
        self._lifecycle = WorkspaceLifecycle(
            self._registry,
            settings,
            runtime,
            self._credentials_manager,
            self._harness.exec_harness_command,
            self._background,
            self._streams_manager,
            self._desktop,
        )
        self._registry.desktop_sessions = self._desktop._desktop_sessions
        self._registry.background_entries = self._background._background_processes
        self._registry.background_statuses = partial(
            self._background._background_statuses, tolerate_errors=True
        )
        self._registry.desktop_live = self._desktop._is_desktop_session_live
        self._registry.desktop_manager = self._desktop
        self.capture_fence = CaptureFence(self._registry)
        self._runtime_artifacts = RuntimeArtifactManager(
            runtime, get_cached, get_runtime, settings.state_dir
        )
        for manager in (
            self._images,
            self._terminals_manager,
            self._streams_manager,
            self._background,
            self._files_manager,
            self._harness,
            self._git,
            self._desktop,
            self._lifecycle,
            self._runtime_artifacts,
        ):
            manager.capture_fence = self.capture_fence

    @property
    def registry(self) -> WorkspaceRegistry:
        """Return the workspace registry (cache ownership)."""
        return self._registry

    @property
    def lifecycle(self) -> WorkspaceLifecycle:
        """Return the workspace lifecycle orchestrator."""
        return self._lifecycle

    @property
    def streams(self) -> StreamManager:
        """Return the generic stream session manager."""
        return self._streams_manager

    @property
    def background(self) -> BackgroundProcessManager:
        """Return the background process manager."""
        return self._background

    @property
    def terminal_manager(self) -> TerminalManager:
        """Return the terminal session manager."""
        return self._terminals_manager

    @property
    def images(self) -> ImageManager:
        """Return the image build/artifact manager."""
        return self._images

    @property
    def files(self) -> FileManager:
        """Return the file operation manager."""
        return self._files_manager

    @property
    def harness(self) -> HarnessExecService:
        """Return the harness command execution manager."""
        return self._harness

    @property
    def runtime_artifacts(self) -> RuntimeArtifactManager:
        """Return the pinned runtime artifact manager."""
        return self._runtime_artifacts

    async def ensure_runtime_artifact(
        self,
        workspace_id: uuid.UUID,
        artifact_id: str,
        version: str,
    ) -> dict[str, Any]:
        """Ensure one approved native runtime tool in a workspace."""
        return await self._runtime_artifacts.ensure_runtime_artifact(
            workspace_id, artifact_id, version
        )

    @property
    def credentials(self) -> CredentialManager:
        """Return the credential inject/remove manager."""
        return self._credentials_manager

    @property
    def desktop(self) -> DesktopManager:
        """Return the desktop session manager."""
        return self._desktop

    @property
    def git(self) -> GitService:
        """Return the git orchestration manager."""
        return self._git

    async def sync_from_runtime(self) -> None:
        """Rebuild the in-memory cache from live runtime state."""
        await self._registry.sync_from_runtime()

    async def inventory_for_runner(self) -> dict:
        """Collect the QEMU storage inventory with explicit completeness."""
        from dataclasses import asdict

        from .runtime.inventory import RuntimeInventory

        snapshots = []
        runtime = self._runtime
        try:
            scan = await runtime.inventory()
        except Exception:
            scan = RuntimeInventory(errors=["Runtime unavailable"])
        if getattr(self, "_journal", None):
            for resource in scan.resources:
                workspace_id = resource.metadata.get("workspace_id")
                if resource.kind == "workspace" and workspace_id:
                    self._journal.observe_workspace(workspace_id, resource.state)
        snapshots.append(asdict(scan))
        return {
            "schema_version": 1,
            "runtimes": snapshots,
            "complete": all(s["complete"] for s in snapshots),
        }

    async def workspace_incarnation(self, workspace_id: str) -> tuple[dict, str] | None:
        """Read exact current resource identity for durable checkpoint validation."""
        from dataclasses import asdict

        matches = []
        runtime = self._runtime
        inspect_source = getattr(runtime, "workspace_incarnation", None)
        if inspect_source is not None:
            result = await inspect_source(workspace_id)
            if result and getattr(self, "_journal", None):
                self._journal.observe_workspace(workspace_id, result[1])
            return result
        scan = asdict(await runtime.inventory())
        if not scan["complete"]:
            return None
        for resource in scan["resources"]:
            if (
                resource["kind"] == "workspace"
                and resource["metadata"].get("workspace_id") == workspace_id
            ):
                matches.append(
                    (
                        {
                            "runtime": "qemu",
                            "resource_id": resource["resource_id"],
                            "dependencies": sorted(resource["dependencies"]),
                            "metadata": resource["metadata"],
                        },
                        resource["state"],
                    )
                )
        result = matches[0] if len(matches) == 1 else None
        if result and getattr(self, "_journal", None):
            self._journal.observe_workspace(workspace_id, result[1])
        return result

    async def publication_evidence(self, identity: dict) -> tuple[str, dict] | None:
        """Prove publication or exact incarnation-bound initialization/scrub checkpoint."""
        if identity.get("workspace_id") and getattr(self, "_journal", None):
            observed = await self.workspace_incarnation(identity["workspace_id"])
            proof = (
                self._journal.proof(identity["workspace_id"], observed[0])
                if observed
                else None
            )
            if proof and proof.get("operation_id") == identity.get("operation_id"):
                if (
                    proof.get("initialized")
                    and observed[1] == "running"
                    and identity.get("failure_event") == "workspace:error"
                ):
                    return proof.get("event", "workspace:created"), {
                        **identity,
                        "credentials_present": proof.get("credentials_present"),
                        "execution_finished": True,
                        "outcome_known": True,
                    }
                if proof.get("event") in {"scrubbed", "workspace:stopped"} and observed[
                    1
                ] in {"exited", "stopped"}:
                    return "workspace:stopped", {
                        **identity,
                        "credentials_present": False,
                        "execution_finished": True,
                        "outcome_known": True,
                    }
        snapshot = await self.inventory_for_runner()
        for scan in snapshot["runtimes"]:
            if not scan["complete"]:
                continue
            for resource in scan["resources"]:
                meta = resource["metadata"]
                op = meta.get("operation_id")
                image = meta.get("artifact_id")
                if (
                    op != identity.get("operation_id")
                    or image != identity.get("image_instance_id")
                    or image != identity.get("target")
                ):
                    continue
                if (identity.get("image_path")) and (
                    meta.get("image_path") != identity["image_path"]
                ):
                    continue
                failure = identity.get("failure_event")
                if failure == "image:build_failed":
                    return "image:built", {
                        **identity,
                        "image_path": meta.get("image_path", ""),
                    }
                if (
                    failure == "image_artifact:failed"
                    and meta.get("credential_clean") is True
                ):
                    return "image_artifact:created", {
                        **identity,
                        "image_artifact_id": image,
                        "name": meta["name"],
                        "created_at": meta["created_at"],
                        "size_bytes": meta["size_bytes"],
                    }
        return None


    def workspace_exists(self, workspace_id: uuid.UUID) -> bool:
        """Return True when *workspace_id* is present in the cache."""
        return self._registry.workspace_exists(workspace_id)

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
        """Create a new workspace, inject credentials, and clone repos."""
        return await self._lifecycle.create_workspace(
            repos,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            env_vars=env_vars,
            files=files,
            ssh_keys=ssh_keys,
            workspace_id=workspace_id,
            base_image_path=base_image_path,
        )

    async def stop_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Remove credentials then stop a running workspace."""
        return await self._lifecycle.stop_workspace(workspace_id)

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
        return await self._lifecycle.resume_workspace(
            workspace_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            env_vars=env_vars,
            files=files,
            ssh_keys=ssh_keys,
        )

    async def inject_credentials(
        self,
        workspace_id: uuid.UUID,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> bool:
        """Replace persistent credentials on a running workspace."""
        return await self._lifecycle.inject_credentials(
            workspace_id, env_vars=env_vars, files=files, ssh_keys=ssh_keys
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
        await self._lifecycle.update_workspace_resources(
            workspace_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
        )

    async def remove_workspace(self, workspace_id: uuid.UUID) -> None:
        """Remove a workspace and clean up resources."""
        await self._lifecycle.remove_workspace(workspace_id)

    async def run_health_check_loop(self) -> None:
        """Periodically probe running workspaces and restart unreachable ones."""
        await self._lifecycle.run_health_check_loop()

    async def get_workspace_heartbeat_statuses(self) -> list[dict]:
        """Return workspace heartbeat payload including live desktop sessions."""
        return await self._registry.get_workspace_heartbeat_statuses()

    async def recover_desktop_sessions_from_runtime(self) -> None:
        """Reconcile desktops from live runtime state with bounded workers."""
        await self.run_desktop_workers(
            list(self._registry._cache), self._desktop.recover_workspace
        )

    async def run_desktop_workers(
        self,
        workspace_ids: list[uuid.UUID],
        operation: Callable[[uuid.UUID], Awaitable[None]],
    ) -> None:
        """Run bounded, owned workers; cancel and join every worker on exit."""
        pending = iter(workspace_ids)

        async def worker() -> None:
            for workspace_id in pending:
                try:
                    await asyncio.wait_for(
                        operation(workspace_id), self._settings.desktop_lease_timeout
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "desktop_maintenance_unconfirmed",
                        workspace_id=str(workspace_id),
                        exc_info=True,
                    )

        workers = [
            asyncio.create_task(worker())
            for _ in range(
                min(len(workspace_ids), max(1, self._settings.desktop_lease_workers))
            )
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def get_vm_metrics(self) -> dict[str, dict[str, Any]]:
        """Collect host-observed metrics for QEMU workspaces."""
        return await self._registry.get_vm_metrics()

    def configure_desktop_leases(self, state_dir: str, epoch: str) -> None:
        """Wire durable ownership after the transport acquires its writer lock."""
        from .services.sessions.desktop_leases import DesktopLeaseStore
        from .services.sessions.stream_intents import StreamIntentStore

        store = DesktopLeaseStore(state_dir)
        self._streams_manager.intent_store = StreamIntentStore(state_dir)
        self._streams_manager.lease_store = store
        self._streams_manager.epoch = epoch
        self._desktop.configure_leases(
            store, epoch, self._streams_manager.close_owner_streams
        )
        self._registry.desktop_manager = self._desktop
