"""Compose runner managers and shared lifecycle coordination.

The backend owns agent logic; runner managers execute its lifecycle,
terminal, file, desktop, image, git, and harness RPCs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from functools import partial

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
        self.registry = WorkspaceRegistry(runtime, settings)
        get_cached = self.registry.get_cached
        get_runtime = self.registry.get_runtime
        self.credentials = CredentialManager()
        self.terminal_manager = TerminalManager(
            runtime,
            get_cached,
            get_runtime,
            lambda workspace_id: self.registry._cache.pop(workspace_id, None),
            WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        self.images = ImageManager(
            runtime,
            get_cached,
            get_runtime,
            store_workspace=lambda info: self.registry._cache.__setitem__(
                info.workspace_id, info
            ),
            credentials_injector=self.credentials.inject_workspace_credentials,
        )
        self.images.lifecycle_context = self.registry.lifecycle
        self.streams = StreamManager(
            runtime, get_cached, get_runtime, sanitize_exec_workdir
        )
        self.background = BackgroundProcessManager(
            runtime,
            get_cached,
            get_runtime,
            sanitize_exec_workdir,
            WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        self.files = FileManager(runtime, get_cached, get_runtime)
        self.harness = HarnessExecService(
            runtime,
            get_cached,
            get_runtime,
            sanitize_exec_workdir,
            WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        self.git = GitService(get_cached, get_runtime)
        self.desktop = DesktopManager(
            runtime, get_cached, get_runtime, self.harness.exec_harness_command
        )
        self.lifecycle = WorkspaceLifecycle(
            self.registry,
            settings,
            runtime,
            self.credentials,
            self.harness.exec_harness_command,
            self.background,
            self.streams,
            self.desktop,
        )
        self.registry.desktop_sessions = self.desktop._desktop_sessions
        self.registry.background_entries = self.background._background_processes
        self.registry.background_statuses = partial(
            self.background._background_statuses, tolerate_errors=True
        )
        self.registry.desktop_live = self.desktop._is_desktop_session_live
        self.registry.desktop_manager = self.desktop
        self.capture_fence = CaptureFence(self.registry)
        self.runtime_artifacts = RuntimeArtifactManager(
            runtime, get_cached, get_runtime, settings.state_dir
        )
        for manager in (
            self.images,
            self.terminal_manager,
            self.streams,
            self.background,
            self.files,
            self.harness,
            self.git,
            self.desktop,
            self.lifecycle,
            self.runtime_artifacts,
        ):
            manager.capture_fence = self.capture_fence

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

    async def recover_desktop_sessions_from_runtime(self) -> None:
        """Reconcile desktops from live runtime state with bounded workers."""
        await self.run_desktop_workers(
            list(self.registry._cache), self.desktop.maintain_workspace
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

    def configure_desktop_leases(self, state_dir: str, epoch: str) -> None:
        """Wire durable ownership after the transport acquires its writer lock."""
        from .services.sessions.desktop_leases import DesktopLeaseStore
        from .services.sessions.stream_intents import StreamIntentStore

        store = DesktopLeaseStore(state_dir)
        self.streams.intent_store = StreamIntentStore(state_dir)
        self.streams.lease_store = store
        self.streams.epoch = epoch
        self.desktop.configure_leases(
            store, epoch, self.streams.close_owner_streams
        )
        self.registry.desktop_manager = self.desktop
