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

import asyncio
import io
import tarfile
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
            try:
                import docker  # type: ignore[import-not-found]
            except Exception as exc:
                raise RuntimeError("docker SDK is not available") from exc

            context_stream = io.BytesIO()
            with tarfile.open(fileobj=context_stream, mode="w") as tar:
                df_bytes = dockerfile_content.encode("utf-8")
                df_info = tarfile.TarInfo(name="Dockerfile")
                df_info.size = len(df_bytes)
                tar.addfile(df_info, io.BytesIO(df_bytes))

            context_stream.seek(0)
            client = docker.from_env()
            image, logs = await asyncio.to_thread(
                client.images.build,
                fileobj=context_stream,
                custom_context=True,
                rm=True,
                tag=image_tag,
                pull=False,
                forcerm=True,
            )
            for entry in logs:
                if progress_callback is None:
                    continue
                line = ""
                if isinstance(entry, dict):
                    line = str(entry.get("stream") or entry.get("status") or "").strip()
                else:
                    line = str(entry).strip()
                if line:
                    await progress_callback(line)
            return {"image_tag": image_tag}

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
                progress_callback=progress_callback,
            )

        raise RuntimeError(f"Unsupported runtime_type for image build: {runtime_type}")

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str,
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
        artifact = await runtime.create_image_artifact(info.instance_id, name)
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
            try:
                import docker  # type: ignore[import-not-found]
                from docker.errors import ImageNotFound  # type: ignore[import-not-found]
            except Exception as exc:
                raise RuntimeError("docker SDK is not available") from exc

            client = docker.from_env()
            try:
                await asyncio.to_thread(
                    client.images.remove, image=image_ref, force=True
                )
                logger.info("docker_image_deleted", image_ref=image_ref)
                return "deleted"
            except ImageNotFound:
                logger.info("docker_image_already_absent", image_ref=image_ref)
                return "already_absent"

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

        instance_id = await runtime.create_workspace_from_image_artifact(
            image_artifact_id,
            str(new_workspace_id),
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
        )

        if self._store_workspace is None:
            raise RuntimeError("ImageManager has no workspace store configured")
        self._store_workspace(
            WorkspaceInfo(
                workspace_id=new_workspace_id,
                instance_id=instance_id,
                status="running",
                runtime_type=runtime_type,
            )
        )

        log = logger.bind(
            workspace_id=str(new_workspace_id),
            image_artifact_id=image_artifact_id,
            runtime_type=runtime_type,
        )
        log.info("workspace_created_from_image_artifact")

        if self._credentials_injector is None:
            raise RuntimeError("ImageManager has no credentials injector configured")
        credentials_present = await self._credentials_injector(
            runtime,
            instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )
        return new_workspace_id, credentials_present
