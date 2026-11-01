"""Transport-neutral lifecycle commands with explicit terminal outcomes."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import structlog

from .service import WorkspaceService
from .workspace_evidence import WorkspaceEvidence

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """One terminal outcome and optional request acknowledgement."""

    event: str
    data: dict
    response: dict | None = None


class CommandHandler(Protocol):
    """Application port used by persistent execution, independent of Socket.IO."""

    async def execute(self, event: str, data: dict) -> CommandResult:
        """Perform the requested effect and return its terminal outcome."""
        ...


class LifecycleCommands:
    """Adapt lifecycle and image managers to the runner command protocol."""

    def __init__(
        self,
        service: WorkspaceService,
        evidence: WorkspaceEvidence,
        progress: Callable[[str, dict], Awaitable[None]],
    ) -> None:
        self._service = service
        self._evidence = evidence
        self._publish_progress = progress
        self._handlers = {
            "task:create_workspace": self.on_create_workspace,
            "task:build_image": self.on_build_image,
            "task:stop_workspace": self.on_stop_workspace,
            "task:resume_workspace": self.on_resume_workspace,
            "task:update_workspace": self.on_update_workspace,
            "task:inject_credentials": self.on_inject_credentials,
            "task:remove_workspace": self.on_remove_workspace,
            "task:create_image_artifact": self.on_create_image_artifact,
            "task:delete_image_artifact": self.on_delete_image_artifact,
            "task:create_workspace_from_image_artifact": self.on_create_workspace_from_image_artifact,
        }

    async def execute(self, event: str, data: dict) -> CommandResult:
        """Execute one supported command without depending on a transport adapter."""
        return await self._handlers[event](data)

    async def _progress(self, event: str, data: dict) -> None:
        """Progress delivery is best effort; transport loss cannot cancel effects."""
        try:
            await self._publish_progress(event, data)
        except Exception:  # noqa: BLE001 - inspection/transport is not proof
            logger.debug("command_progress_offline", command_event=event)

    async def _workspace_failure_evidence(
        self, workspace_id: uuid.UUID
    ) -> dict[str, object]:
        """Report inspected runtime state and incarnation-bound credential proof."""
        evidence: dict[str, object] = {"outcome_known": False}
        try:
            observed = await self._service.workspace_incarnation(str(workspace_id))
            if not observed or observed[1] not in {"running", "exited", "stopped"}:
                return evidence
            incarnation, state = observed
            evidence["observed_status"] = state
            proof = await self._evidence.journal.call(
                "proof", str(workspace_id), incarnation
            )
            # Lifecycle marks credentials unknown before a potentially partial scrub.
            # An older running checkpoint cannot prove what remains after that write.
            registry = getattr(self._service, "registry", None)
            if registry is not None:
                info = registry.get_cached(workspace_id)
                if info.credentials_present is None:
                    return evidence
            if proof and proof.get("state") == state:
                credentials_present = proof.get("credentials_present")
                if isinstance(credentials_present, bool):
                    evidence.update(
                        credentials_present=credentials_present, outcome_known=True
                    )
        except Exception:  # noqa: BLE001 - inspection/transport is not proof
            # Neither stale cache entries nor a failed inspection prove an outcome.
            logger.debug(
                "workspace_evidence_unavailable", workspace_id=str(workspace_id)
            )
        return evidence

    async def on_create_workspace(self, data: dict) -> CommandResult:
        """Create workspace and return one terminal outcome."""
        task_id = data.get("task_id", str(uuid.uuid4()))
        raw_ws_id = data.get("workspace_id")
        workspace_id = uuid.UUID(raw_ws_id) if raw_ws_id else None
        log = logger.bind(task_id=task_id)
        log.info("task_received", task="create_workspace")
        try:
            ws_id, credentials_present = await self._service.lifecycle.create_workspace(
                repos=data.get("repos", []),
                qemu_vcpus=data.get("qemu_vcpus"),
                qemu_memory_mb=data.get("qemu_memory_mb"),
                qemu_disk_size_gb=data.get("qemu_disk_size_gb"),
                env_vars=data.get("env_vars", {}),
                files=data.get("files", []),
                ssh_keys=data.get("ssh_keys", []),
                workspace_id=workspace_id,
                base_image_path=data.get("base_image_path") or None,
            )
            result = CommandResult(
                "workspace:created",
                {
                    "task_id": task_id,
                    "workspace_id": str(ws_id),
                    "status": "created",
                    "credentials_present": credentials_present,
                },
            )
            log.info("task_completed", workspace_id=str(ws_id))
        except Exception as exc:
            result = CommandResult(
                "workspace:error",
                {"task_id": task_id, "error": str(exc), "outcome_known": False},
            )
            log.exception("task_failed")
        return result

    async def on_build_image(self, data: dict) -> CommandResult:
        """Build image and return one terminal outcome."""
        task_id = data.get("task_id", str(uuid.uuid4()))
        build_job_id = data.get("build_job_id", "")
        log = logger.bind(task_id=task_id, build_job_id=build_job_id)
        log.info("task_received", task="build_image")
        try:

            async def _progress(line: str) -> None:
                await self._progress(
                    "image:build_progress",
                    {"task_id": task_id, "build_job_id": build_job_id, "line": line},
                )

            result = await self._service.images.build_image(
                build_job_id=build_job_id,
                base_distro=data.get("base_distro", ""),
                init_script=data.get("init_script", ""),
                image_path=data.get("image_path", ""),
                operation_id=data.get("operation_id"),
                image_instance_id=data.get("image_instance_id"),
                progress_callback=_progress,
            )
            result = CommandResult(
                "image:built",
                {
                    "task_id": task_id,
                    "build_job_id": build_job_id,
                    "image_path": result.get("image_path", ""),
                    **{
                        key: result[key]
                        for key in ("min_disk_size_gb", "virtual_size_bytes")
                        if key in result
                    },
                },
            )
        except Exception as exc:
            result = CommandResult(
                "image:build_failed",
                {"task_id": task_id, "build_job_id": build_job_id, "error": str(exc)},
            )
            log.exception("image_build_failed")
        return result

    async def on_stop_workspace(self, data: dict) -> CommandResult:
        """Stop workspace and return one terminal outcome."""
        task_id = data.get("task_id", str(uuid.uuid4()))
        workspace_id = uuid.UUID(data["workspace_id"])
        log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
        try:
            credentials_present = await self._service.lifecycle.stop_workspace(
                workspace_id
            )
            result = CommandResult(
                "workspace:stopped",
                {
                    "task_id": task_id,
                    "workspace_id": str(workspace_id),
                    "credentials_present": credentials_present,
                },
            )
            log.info("workspace_stopped")
        except Exception as exc:
            result = CommandResult(
                "workspace:error",
                {
                    "task_id": task_id,
                    "error": str(exc),
                    **await self._workspace_failure_evidence(workspace_id),
                },
            )
            log.exception("stop_failed")
        return result

    async def on_resume_workspace(self, data: dict) -> CommandResult:
        """Resume workspace and return one terminal outcome."""
        task_id = data.get("task_id", str(uuid.uuid4()))
        workspace_id = uuid.UUID(data["workspace_id"])
        log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
        try:
            credentials_present = await self._service.lifecycle.resume_workspace(
                workspace_id,
                qemu_vcpus=data.get("qemu_vcpus"),
                qemu_memory_mb=data.get("qemu_memory_mb"),
                qemu_disk_size_gb=data.get("qemu_disk_size_gb"),
                env_vars=data.get("env_vars", {}),
                files=data.get("files", []),
                ssh_keys=data.get("ssh_keys", []),
            )
            result = CommandResult(
                "workspace:resumed",
                {
                    "task_id": task_id,
                    "workspace_id": str(workspace_id),
                    "credentials_present": credentials_present,
                },
            )
            log.info("workspace_resumed")
        except Exception as exc:
            result = CommandResult(
                "workspace:error",
                {
                    "task_id": task_id,
                    "error": str(exc),
                    **await self._workspace_failure_evidence(workspace_id),
                },
            )
            log.exception("resume_failed")
        return result

    async def on_update_workspace(self, data: dict) -> CommandResult:
        """Update workspace and return one terminal outcome."""
        task_id = data.get("task_id", str(uuid.uuid4()))
        workspace_id = uuid.UUID(data["workspace_id"])
        log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
        try:
            await self._service.lifecycle.update_workspace_resources(
                workspace_id,
                qemu_vcpus=int(data["qemu_vcpus"]),
                qemu_memory_mb=int(data["qemu_memory_mb"]),
                qemu_disk_size_gb=int(data["qemu_disk_size_gb"]),
            )
            result = CommandResult(
                "workspace:updated",
                {"task_id": task_id, "workspace_id": str(workspace_id)},
            )
            log.info("workspace_updated")
        except Exception as exc:
            result = CommandResult(
                "workspace:error",
                {"task_id": task_id, "error": str(exc), "outcome_known": False},
            )
            log.exception("update_workspace_failed")
        return result

    async def on_inject_credentials(self, data: dict) -> CommandResult:
        """Inject credentials and return one terminal outcome."""
        task_id = data.get("task_id", str(uuid.uuid4()))
        workspace_id = uuid.UUID(data["workspace_id"])
        log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
        try:
            credentials_present = await self._service.lifecycle.inject_credentials(
                workspace_id,
                env_vars=data.get("env_vars", {}),
                files=data.get("files", []),
                ssh_keys=data.get("ssh_keys", []),
            )
            result = CommandResult(
                "workspace:credentials_injected",
                {
                    "task_id": task_id,
                    "workspace_id": str(workspace_id),
                    "credentials_present": credentials_present,
                },
            )
            log.info(
                "workspace_credentials_injected",
                credentials_present=credentials_present,
            )
            return CommandResult(
                result.event,
                result.data,
                {"ok": True, "credentials_present": credentials_present},
            )
        except Exception as exc:
            evidence = await self._workspace_failure_evidence(workspace_id)
            result = CommandResult(
                "workspace:error", {"task_id": task_id, "error": str(exc), **evidence}
            )
            log.exception("inject_credentials_failed")
            return CommandResult(
                result.event, result.data, {"ok": False, "error": str(exc), **evidence}
            )
        return result

    async def on_remove_workspace(self, data: dict) -> CommandResult:
        """Remove workspace and return one terminal outcome."""
        task_id = data.get("task_id", str(uuid.uuid4()))
        workspace_id = uuid.UUID(data["workspace_id"])
        log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
        try:
            if not self._service.registry.workspace_exists(workspace_id):
                await self._service.registry.sync_from_runtime()
            already_absent = not self._service.registry.workspace_exists(workspace_id)
            await self._service.lifecycle.remove_workspace(workspace_id)
            await self._service.registry.sync_from_runtime()
            if self._service.registry.workspace_exists(workspace_id):
                raise RuntimeError("Runtime removal could not be verified")
            result = CommandResult(
                "workspace:removed",
                {
                    "task_id": task_id,
                    "workspace_id": str(workspace_id),
                    "result": "already_absent" if already_absent else "deleted",
                    "already_absent": already_absent,
                },
            )
            log.info("workspace_removed", already_absent=already_absent)
        except Exception as exc:
            result = CommandResult(
                "workspace:error",
                {"task_id": task_id, "error": str(exc), "outcome_known": False},
            )
            log.exception("remove_failed")
        return result

    async def on_create_image_artifact(self, data: dict) -> CommandResult:
        """Create image artifact and return one terminal outcome."""
        task_id = data.get("task_id", str(uuid.uuid4()))
        workspace_id = uuid.UUID(data["workspace_id"])
        name = data["name"]
        log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
        log.info("task_received", task="create_image_artifact")
        try:
            artifact = await self._service.images.create_image_artifact(
                workspace_id,
                name,
                artifact_id=data.get("image_instance_id"),
                operation_id=data.get("operation_id"),
            )
            result = CommandResult(
                "image_artifact:created",
                {
                    "task_id": task_id,
                    "workspace_id": str(workspace_id),
                    "image_artifact_id": artifact.artifact_id,
                    "name": artifact.name,
                    "created_at": artifact.created_at.isoformat()
                    if isinstance(artifact.created_at, datetime)
                    else str(artifact.created_at),
                    "size_bytes": artifact.size_bytes,
                    **{
                        key: getattr(artifact, key)
                        for key in ("min_disk_size_gb", "virtual_size_bytes")
                        if getattr(artifact, key, None) is not None
                    },
                },
            )
            log.info("image_artifact_created", image_artifact_id=artifact.artifact_id)
        except Exception as exc:
            result = CommandResult(
                "image_artifact:failed",
                {
                    "task_id": task_id,
                    "workspace_id": str(workspace_id),
                    "error": str(exc),
                },
            )
            log.exception("create_image_artifact_failed")
        return result

    async def on_delete_image_artifact(self, data: dict) -> CommandResult:
        """Delete image artifact and return one terminal outcome."""
        task_id = data.get("task_id", str(uuid.uuid4()))
        image_artifact_id = data["image_artifact_id"]
        image_instance_id = data.get("image_instance_id", "")
        log = logger.bind(task_id=task_id, image_artifact_id=image_artifact_id)
        try:
            result = await self._service.images.delete_image_reference(
                image_ref=image_artifact_id
            )
            result = CommandResult(
                "image_artifact:deleted",
                {
                    "task_id": task_id,
                    "image_instance_id": image_instance_id,
                    "image_artifact_id": image_artifact_id,
                    "result": result,
                    "already_absent": result == "already_absent",
                },
            )
            log.info(
                "image_artifact_deleted",
                image_artifact_id=image_artifact_id,
                result=result,
            )
        except Exception as exc:
            result = CommandResult(
                "image_artifact:delete_failed", {"task_id": task_id, "error": str(exc)}
            )
            log.exception("delete_image_artifact_failed")
        return result

    async def on_create_workspace_from_image_artifact(
        self, data: dict
    ) -> CommandResult:
        task_id = data.get("task_id", str(uuid.uuid4()))
        image_artifact_id = data["image_artifact_id"]
        raw_ws_id = data.get("workspace_id")
        new_workspace_id = uuid.UUID(raw_ws_id) if raw_ws_id else uuid.uuid4()
        log = logger.bind(task_id=task_id, image_artifact_id=image_artifact_id)
        log.info("task_received", task="create_workspace_from_image_artifact")
        try:
            (
                ws_id,
                credentials_present,
            ) = await self._service.images.create_workspace_from_image_artifact(
                image_artifact_id=image_artifact_id,
                new_workspace_id=new_workspace_id,
                qemu_vcpus=data.get("qemu_vcpus"),
                qemu_memory_mb=data.get("qemu_memory_mb"),
                qemu_disk_size_gb=data.get("qemu_disk_size_gb"),
                env_vars=data.get("env_vars", {}),
                files=data.get("files", []),
                ssh_keys=data.get("ssh_keys", []),
            )
            result = CommandResult(
                "workspace:created",
                {
                    "task_id": task_id,
                    "workspace_id": str(ws_id),
                    "status": "created",
                    "credentials_present": credentials_present,
                },
            )
            log.info("workspace_created_from_image_artifact", workspace_id=str(ws_id))
        except Exception as exc:
            result = CommandResult(
                "workspace:error",
                {
                    "task_id": task_id,
                    "workspace_id": str(new_workspace_id),
                    "error": str(exc),
                    "outcome_known": False,
                },
            )
            log.exception("clone_failed")
        return result
