"""Image build / artifact operations (Step 2 leaf cluster).

Canonical home for the image build / artifact helpers previously living
on ``WorkspaceService`` in :mod:`src.service`:

- :meth:`ImageManager.build_image`,
- :meth:`ImageManager.create_image_artifact`,
- :meth:`ImageManager.list_image_artifacts`,
- :meth:`ImageManager.delete_image_artifact`,
- :meth:`ImageManager.delete_image_reference`,
- :meth:`ImageManager.create_workspace_from_image_artifact`.

``WorkspaceService`` keeps thin delegates (same names/signatures/messages)
plus an ``images`` property exposing the manager, so existing callers and
websocket handlers keep working while new code can call the manager
directly.

Workspace resolution / mutation is injected so this module never imports
``src.service`` (no dependency cycle). ``store_workspace`` always writes
through to the live service cache (a closure over the service, not a
captured dict object) so :meth:`src.service.WorkspaceService.sync_from_runtime`
reassignments stay correct.

Extraction owner: Step 2 (leaf cluster: terminals + images).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from ..models import WorkspaceInfo
from ..runtime.base import ImageArtifactInfo, RuntimeBackend

logger = structlog.get_logger(__name__)


class ImageManager:
    """Owns image build / artifact operations.

    Injected dependencies (all mirror ``WorkspaceService`` helpers):

    - ``runtimes``: runtime backends by type (kept for introspection;
      resolution itself goes through the callables below).
    - ``get_cached``: ``(workspace_id) -> WorkspaceInfo``; raises
      ``ValueError("... not found")`` for unknown ids.
    - ``get_runtime``: ``(workspace_id) -> RuntimeBackend``; raises
      ``RuntimeError`` for unknown runtimes.
    - ``get_runtime_by_type``: ``(runtime_type) -> RuntimeBackend``;
      raises ``RuntimeError("... is not enabled")``.
    - ``cache``: legacy direct dict reference (accepted for
      compatibility; prefer ``store_workspace`` which stays correct
      across ``sync_from_runtime`` reassignments).
    - ``store_workspace``: ``(info) -> None``; persists a new
      ``WorkspaceInfo`` into the live service cache.
    - ``credentials_injector``: async
      ``(runtime, instance_id, env_vars, files, ssh_keys, log) -> bool``;
      ``WorkspaceService.inject_workspace_credentials`` bound method.
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        get_runtime_by_type: Callable[[str], RuntimeBackend] | None = None,
        cache: dict[uuid.UUID, WorkspaceInfo] | None = None,
        store_workspace: Callable[[WorkspaceInfo], None] | None = None,
        credentials_injector: Callable[
            [RuntimeBackend, str, Any, Any, Any, Any], Awaitable[bool]
        ]
        | None = None,
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {}
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._get_runtime_by_type = get_runtime_by_type
        self._cache = cache
        if store_workspace is not None:
            self._store_workspace = store_workspace
        elif cache is not None:
            self._store_workspace = lambda info: cache.__setitem__(  # noqa: E731
                info.workspace_id, info
            )
        else:
            self._store_workspace = None
        self.lifecycle_context = None
        self.scrub_proof_hook = None
        self.checkpoint_hook = None
        self._credentials_injector = credentials_injector

    async def build_image(
        self,
        *,
        runtime_type: str,
        build_job_id: str,
        dockerfile_content: str = "",
        image_tag: str = "",
        base_distro: str = "",
        init_script: str = "",
        image_path: str = "",
        progress_callback=None,
        operation_id: str | None = None,
        image_instance_id: str | None = None,
    ) -> dict[str, str]:
        """Build runtime image from definition payload.

        Returns a dict containing ``image_tag`` and/or ``image_path``.
        """
        if runtime_type == "docker":
            if not dockerfile_content.strip():
                raise RuntimeError(
                    "dockerfile_content is required for docker image builds"
                )
            if not image_tag.strip():
                raise RuntimeError("image_tag is required for docker image builds")
            runtime = self._get_runtime_by_type("docker")
            return await runtime.build_image(
                dockerfile_content=dockerfile_content,
                image_tag=image_tag,
                operation_id=operation_id,
                image_instance_id=image_instance_id,
                progress_callback=progress_callback,
            )

        if runtime_type == "qemu":
            if not image_path.strip():
                raise RuntimeError("image_path is required for qemu image builds")
            if self._get_runtime_by_type is None:
                raise RuntimeError("Runtime 'qemu' is not enabled")
            runtime = self._get_runtime_by_type("qemu")
            build_image = getattr(runtime, "build_image", None)
            if build_image is None:
                raise RuntimeError("QEMU runtime does not support image builds")
            return await build_image(
                base_distro=base_distro,
                init_script=init_script,
                image_path=image_path,
                operation_id=operation_id,
                image_instance_id=image_instance_id,
                progress_callback=progress_callback,
            )

        raise RuntimeError(f"Unsupported runtime_type for image build: {runtime_type}")

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str,
        *,
        artifact_id: str | None = None,
        operation_id: str | None = None,
        credential_clean: bool = False,
    ) -> "ImageArtifactInfo":
        """Create an image artifact from a workspace.

        The runtime must support artifact capture.
        """
        if self._get_cached is None or self._get_runtime is None:
            raise RuntimeError("ImageManager has no workspace lookup configured")
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not runtime.supports_image_artifacts:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' does not support image artifact capture"
            )
        proven_clean = False
        if self.scrub_proof_hook is not None:
            proven_clean = await self.scrub_proof_hook(workspace_id)
        artifact = await runtime.create_image_artifact(
            info.instance_id,
            name,
            artifact_id=artifact_id,
            operation_id=operation_id,
            credential_clean=proven_clean,
        )
        logger.info(
            "image_artifact_created",
            workspace_id=str(workspace_id),
            image_artifact_id=artifact.artifact_id,
            name=name,
        )
        return artifact

    async def list_image_artifacts(
        self,
        workspace_id: uuid.UUID,
    ) -> list["ImageArtifactInfo"]:
        """List all captured image artifacts for a workspace."""
        if self._get_cached is None or self._get_runtime is None:
            raise RuntimeError("ImageManager has no workspace lookup configured")
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not runtime.supports_image_artifacts:
            return []
        return await runtime.list_image_artifacts(info.instance_id)

    async def delete_image_artifact(
        self,
        workspace_id: uuid.UUID,
        image_artifact_id: str,
    ) -> None:
        """Delete a captured image artifact."""
        if self._get_cached is None or self._get_runtime is None:
            raise RuntimeError("ImageManager has no workspace lookup configured")
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not runtime.supports_image_artifacts:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' does not support image artifact deletion"
            )
        await runtime.delete_image_artifact(image_artifact_id)
        logger.info(
            "image_artifact_deleted",
            workspace_id=str(workspace_id),
            image_artifact_id=image_artifact_id,
        )

    async def delete_image_reference(
        self,
        *,
        runtime_type: str,
        image_ref: str,
    ) -> str:
        """Delete a concrete runtime image reference without requiring a workspace.

        Returns 'deleted' or 'already_absent' to indicate the result.
        """
        if runtime_type == "docker":
            if not image_ref.strip():
                raise RuntimeError("image_ref is required for docker image deletion")
            runtime = self._get_runtime_by_type("docker")
            return await runtime.delete_image_reference(image_ref)

        if runtime_type == "qemu":
            if not image_ref.strip():
                raise RuntimeError("image_ref is required for qemu image deletion")
            if self._get_runtime_by_type is None:
                raise RuntimeError("Runtime 'qemu' is not enabled")
            runtime = self._get_runtime_by_type("qemu")
            try:
                await runtime.delete_image_artifact(image_ref)
                logger.info("qemu_image_deleted", image_ref=image_ref)
                return "deleted"
            except FileNotFoundError:
                logger.info("qemu_image_already_absent", image_ref=image_ref)
                return "already_absent"

        raise RuntimeError(
            f"Unsupported runtime_type for image deletion: {runtime_type}"
        )

    async def create_workspace_from_image_artifact(
        self,
        image_artifact_id: str,
        new_workspace_id: uuid.UUID,
        runtime_type: str,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Fence clone initialization against inventory while retaining leaf API."""
        kwargs = dict(
            image_artifact_id=image_artifact_id, new_workspace_id=new_workspace_id,
            runtime_type=runtime_type, qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb, qemu_disk_size_gb=qemu_disk_size_gb,
            env_vars=env_vars, files=files, ssh_keys=ssh_keys,
        )
        if self.lifecycle_context is None:
            return await self._create_workspace_from_image_artifact(**kwargs)
        async with self.lifecycle_context(new_workspace_id):
            return await self._create_workspace_from_image_artifact(**kwargs)

    async def _create_workspace_from_image_artifact(
        self,
        image_artifact_id: str,
        new_workspace_id: uuid.UUID,
        runtime_type: str,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Create a workspace from an image artifact and inject credentials.

        Credentials remain on disk until a controlled stop.
        """
        if self._get_runtime_by_type is None:
            raise RuntimeError(f"Runtime '{runtime_type}' is not enabled")
        runtime = self._get_runtime_by_type(runtime_type)
        if not runtime.supports_image_artifacts:
            raise RuntimeError(
                f"Runtime '{runtime_type}' does not support image artifact cloning"
            )

        info = WorkspaceInfo(
            workspace_id=new_workspace_id, instance_id="", status="creating",
            runtime_type=runtime_type,
        )
        if self._store_workspace is None:
            raise RuntimeError("ImageManager has no workspace store configured")
        self._store_workspace(info)
        instance_id = await runtime.create_workspace_from_image_artifact(
            image_artifact_id,
            str(new_workspace_id),
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
        )

        info.instance_id = instance_id

        log = logger.bind(
            workspace_id=str(new_workspace_id),
            image_artifact_id=image_artifact_id,
            runtime_type=runtime_type,
        )
        log.info("workspace_created_from_image_artifact")

        if self._credentials_injector is None:
            raise RuntimeError("ImageManager has no credentials injector configured")
        if self.checkpoint_hook is not None:
            await self.checkpoint_hook(new_workspace_id, None, "injecting")
        credentials_present = await self._credentials_injector(
            runtime,
            instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )
        if self.checkpoint_hook is not None:
            await self.checkpoint_hook(
                new_workspace_id, credentials_present, "workspace:created"
            )
        info.status = "running"
        info.credentials_present = credentials_present
        return new_workspace_id, credentials_present
