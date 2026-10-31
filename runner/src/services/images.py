"""Image build, artifact, and image-backed workspace operations.

:class:`ImageManager` uses injected runtime, workspace lookup/store, and
credential dependencies; image-backed workspace updates go through the
provided ``store_workspace`` callback.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog

from ..models import WorkspaceInfo
from ..runtime.base import ImageArtifactInfo, RuntimeBackend
from .capture_fence import CaptureFence

logger = structlog.get_logger(__name__)


class ImageManager:
    """Owns image build and artifact operations.

    Runtime lookup, workspace storage, and credential injection are provided by
    the runner composition root. ``store_workspace`` writes new workspace
    metadata to the active registry.
    """

    def __init__(
        self,
        runtime: RuntimeBackend | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        store_workspace: Callable[[WorkspaceInfo], None] | None = None,
        credentials_injector: Callable[
            [RuntimeBackend, str, Any, Any, Any, Any], Awaitable[bool]
        ]
        | None = None,
    ) -> None:
        self.capture_fence: CaptureFence | None = None
        self._runtime = runtime
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._store_workspace = store_workspace
        self.lifecycle_context = None
        self.scrub_proof_hook = None
        self.checkpoint_hook = None
        self._credentials_injector = credentials_injector

    async def build_image(
        self,
        *,
        build_job_id: str,
        base_distro: str = "",
        init_script: str = "",
        image_path: str = "",
        progress_callback=None,
        operation_id: str | None = None,
        image_instance_id: str | None = None,
    ) -> dict[str, str | int]:
        """Build runtime image from definition payload.

        Returns a dict containing ``image_path``.
        """

        if not image_path.strip():
            raise RuntimeError("image_path is required for qemu image builds")
        if self._runtime is None:
            raise RuntimeError("Workspace runtime is unavailable")
        runtime = self._runtime
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

    @asynccontextmanager
    async def _capture_context(self, workspace_id: uuid.UUID) -> AsyncIterator[None]:
        fence = self.capture_fence
        if fence is None:
            if self.lifecycle_context is None:
                yield
            else:
                async with self.lifecycle_context(workspace_id):
                    yield
        else:
            async with fence.capture(workspace_id):
                if self.lifecycle_context is None:
                    await fence.drain(workspace_id)
                    yield
                else:
                    async with self.lifecycle_context(workspace_id):
                        await fence.drain(workspace_id)
                        yield

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
        async with self._capture_context(workspace_id):
            if self._get_cached is None or self._get_runtime is None:
                raise RuntimeError("ImageManager has no workspace lookup configured")
            info = self._get_cached(workspace_id)
            runtime = self._get_runtime(workspace_id)
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

    async def delete_image_reference(
        self,
        *,
        image_ref: str,
    ) -> str:
        """Delete a concrete runtime image reference without requiring a workspace.

        Returns 'deleted' or 'already_absent' to indicate the result.
        """

        if not image_ref.strip():
            raise RuntimeError("image_ref is required for qemu image deletion")
        if self._runtime is None:
            raise RuntimeError("Workspace runtime is unavailable")
        runtime = self._runtime
        try:
            await runtime.delete_image_artifact(image_ref)
            logger.info("qemu_image_deleted", image_ref=image_ref)
            return "deleted"
        except FileNotFoundError:
            logger.info("qemu_image_already_absent", image_ref=image_ref)
            return "already_absent"

    async def create_workspace_from_image_artifact(
        self,
        image_artifact_id: str,
        new_workspace_id: uuid.UUID,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Fence clone initialization against inventory while retaining leaf API."""
        kwargs = dict(
            image_artifact_id=image_artifact_id,
            new_workspace_id=new_workspace_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            env_vars=env_vars,
            files=files,
            ssh_keys=ssh_keys,
        )
        if self.lifecycle_context is None:
            return await self._create_workspace_from_image_artifact(**kwargs)
        async with self.lifecycle_context(new_workspace_id):
            return await self._create_workspace_from_image_artifact(**kwargs)

    async def _create_workspace_from_image_artifact(
        self,
        image_artifact_id: str,
        new_workspace_id: uuid.UUID,
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
        if self._runtime is None:
            raise RuntimeError("Workspace runtime is unavailable")
        runtime = self._runtime

        info = WorkspaceInfo(
            workspace_id=new_workspace_id, instance_id="", status="creating",
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
