"""Image definitions, builds and artifacts (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.image_definitions``,
``self.build_jobs``, ``self.image_instances``, ``self.workspaces``,
``self.tasks`` plus sibling helpers (``_emit_to_runner`` via
``RunnerTransportMixin``, ``_forward_*`` via ``FrontendBusMixin``,
``_dispatch_workspace_task`` via ``TaskDispatchMixin``). Lazy
``from ...models`` / ``django`` imports keep their semantics; the
``Path(__file__)`` lookup in ``_desktop_session_init_script_block`` uses
one extra ``.parent`` for the deeper ``domains/`` location (same
``runners/scripts/`` target).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from ...exceptions import WorkspaceNotFoundError

if TYPE_CHECKING:
    from ...models import Task, Workspace

logger = logging.getLogger(__name__)


class ImageLifecycleMixin:
    """Image definition/build/artifact flows shared by RunnerService."""


    @staticmethod
    def _validate_qemu_base_distro(base_distro: str) -> None:
        """Ensure QEMU image definitions use a supported distro source."""
        distro = (base_distro or "").strip().lower()
        if distro.startswith("ubuntu:"):
            return
        raise ConflictError(
            "QEMU image definitions currently require an ubuntu:<version> base distro"
        )


    @staticmethod
    def _desktop_session_init_script_block() -> str:
        """Return shell script lines that install the QEMU KasmVNC desktop."""
        script_path = (
            Path(__file__).resolve().parent.parent.parent
            / "scripts"
            / "qemu_desktop_session.sh"
        )
        return "\n" + script_path.read_text(encoding="utf-8").strip() + "\n"

    @classmethod
    def _build_qemu_init_script_content(cls, definition) -> str:
        """Build a shell init script for QEMU image definitions."""
        lines = [
            "#!/bin/bash",
            "set -euo pipefail",
            "",
        ]

        packages = [p.strip() for p in list(definition.packages or []) if p.strip()]
        if packages:
            lines += [
                "export DEBIAN_FRONTEND=noninteractive",
                "apt-get update",
                f"apt-get install -y {' '.join(packages)}",
                "rm -rf /var/lib/apt/lists/*",
                "",
            ]

        env_vars = dict(definition.env_vars or {})
        if env_vars:
            lines += [
                "cat >/etc/profile.d/opencuria-image-env.sh <<'EOF'",
                "#!/bin/sh",
            ]
            for key, value in env_vars.items():
                if key:
                    escaped = str(value).replace('"', '\\"')
                    lines.append(f'export {key}="{escaped}"')
            lines += [
                "EOF",
                "chmod 644 /etc/profile.d/opencuria-image-env.sh",
                "",
            ]

        custom_script = (definition.custom_init_script or "").strip()
        if custom_script:
            lines += [
                "# Custom image definition steps",
                custom_script,
                "",
            ]

        lines += [cls._desktop_session_init_script_block(), ""]

        return "\n".join(lines).strip() + "\n"


    def _ensure_definition_mutable(self, definition) -> None:
        """Reject runner mutations while a definition is deleted or being removed."""
        from ...models import ImageDefinition

        if definition.status in {
            ImageDefinition.Status.PENDING_DELETION,
            ImageDefinition.Status.DELETING,
            ImageDefinition.Status.DELETED,
            ImageDefinition.Status.DELETE_FAILED,
        }:
            raise ConflictError(
                f"Cannot modify image definition in state '{definition.status}'"
            )

    def deactivate_runner_image(self, build):
        """Disable assignment selection without mutating any generation."""
        self._ensure_definition_mutable(build.image_definition)
        return self.build_jobs.ensure_inactive(build.image_definition, build.runner)

    async def activate_build_job(self, build, *, created_by=None, message: str = ""):
        """Make an existing runner image selectable, or build it if none exists."""
        from ...models import ImageInstance

        self._ensure_definition_mutable(build.image_definition)
        instance = await sync_to_async(self.image_instances.get_by_build_job_id)(
            build.id
        )
        has_ready_image = (
            build.built_at is not None
            and instance is not None
            and instance.status
            in {ImageInstance.Status.READY, ImageInstance.Status.RETIRED}
            and bool(instance.runner_ref)
        )
        if not has_ready_image:
            return await self.trigger_build_job(
                image_definition=build.image_definition,
                runner=build.runner,
                activate=True,
                created_by=created_by,
                message=message,
            )

        build = await sync_to_async(self.build_jobs.activate)(build.id)
        if instance.status == ImageInstance.Status.RETIRED:
            await sync_to_async(self.image_instances.mark_ready_from_retired)(
                instance.id
            )
        return build

    async def trigger_build_job(
        self,
        *,
        image_definition,
        runner,
        activate: bool = True,
        created_by=None,
        message: str = "",
    ):
        """Create/update runner build record and dispatch task:build_image.

        Every build is a new standalone version; ``message`` describes it.
        """

        self._ensure_definition_mutable(image_definition)

        from ...repositories import ImageGenerationRepository

        if not activate:
            return await sync_to_async(self.build_jobs.ensure_inactive)(
                image_definition, runner
            )
        self._validate_qemu_base_distro(image_definition.base_distro)
        rendered = {
            "base_distro": image_definition.base_distro,
            "init_script": self._build_qemu_init_script_content(image_definition),
        }
        build, image, task = await sync_to_async(ImageGenerationRepository.request)(
            definition=image_definition,
            runner=runner,
            rendered_input=rendered,
            created_by=created_by,
            message=message,
        )
        payload = {
            **rendered,
            "task_id": str(task.id),
            "build_job_id": str(build.id),
            "image_instance_id": str(image.id),
        }
        payload["image_path"] = image.runner_ref
        await self._emit_to_runner(runner, "task:build_image", payload)
        if runner.sid:
            await sync_to_async(self.tasks.mark_in_progress)(task)
        return build

    #: Maximum build_log size kept per runner image build (chars). The log is
    #: a rolling tail: new lines are appended atomically in the DB and the
    #: stored value is trimmed to the last BUILD_LOG_MAX_CHARS characters.
    #: This bounds the per-line SQL size and the per-poll response size so
    #: long image builds (10k+ lines) cannot grow process memory without
    #: bound. The list endpoint additionally omits build_log entirely (see
    #: ImageBuildJobListOut and the dedicated /log/ endpoint).
    BUILD_LOG_MAX_CHARS = 200_000

    def handle_image_build_progress(
        self,
        build_job_id: str,
        line: str,
        runner_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Append one build log line for a runner image build.

        Uses a single atomic UPDATE (Concat + Right in the database) so the
        full log is never read into Python and re-written. Per-query memory
        therefore stays proportional to the line, not to the total log.
        Unknown build jobs are ignored; runner_id mismatches are rejected.
        """
        from ...repositories import ImageGenerationRepository

        ImageGenerationRepository.progress(
            build_job_id=build_job_id,
            task_id=task_id,
            runner_id=runner_id,
            line=line,
            max_chars=self.BUILD_LOG_MAX_CHARS,
        )

    def handle_image_built(
        self,
        *,
        task_id: str,
        build_job_id: str,
        image_path: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Mark a runner image build as active and complete its task."""

        from ...repositories import ImageGenerationRepository

        ImageGenerationRepository.finish(
            task_id=task_id,
            build_job_id=build_job_id,
            runner_id=runner_id,
            runner_ref=image_path,
        )

    def handle_image_build_failed(
        self,
        *,
        task_id: str,
        build_job_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Fail only the correlated generation; retain the current image."""
        from ...repositories import ImageGenerationRepository

        ImageGenerationRepository.finish(
            task_id=task_id,
            build_job_id=build_job_id,
            runner_id=runner_id,
            error=error,
        )

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str = "",
        organization_id: uuid.UUID | None = None,
        captured_image_id: uuid.UUID | None = None,
        message: str = "",
    ) -> tuple["Workspace", "Task"]:
        """Reserve an automatic QEMU capture; recovery owns child progression.

        Without ``captured_image_id`` a new image (v1) is created; otherwise the
        capture becomes the next version of that image.
        """
        from ...capture_repository import CaptureRepository

        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None or (
            organization_id and workspace.runner.organization_id != organization_id
        ):
            raise WorkspaceNotFoundError(str(workspace_id))
        workspace, task = await sync_to_async(CaptureRepository.allocate)(
            workspace_id,
            name,
            has_busy_agent=self._harness().has_busy_session,
            captured_image_id=captured_image_id,
            message=message,
        )
        await sync_to_async(self._forward_workspace_operation)(
            str(workspace_id), "capturing_image"
        )
        return workspace, task

    def handle_image_artifact_created(
        self,
        task_id: str,
        workspace_id: str,
        artifact_id: str,
        name: str,
        size_bytes: int = 0,
        runner_id: str | None = None,
    ) -> None:
        """Handle image_artifact:created from runner and mark the image ready."""
        from ...repositories import ImageGenerationRepository

        if not ImageGenerationRepository.capture_result(
            task_id=task_id,
            workspace_id=workspace_id,
            runner_id=runner_id,
            runner_ref=artifact_id,
            size_bytes=size_bytes,
        ):
            return

        logger.info(
            "Image artifact created: workspace=%s, artifact=%s",
            workspace_id,
            artifact_id,
        )

        self._forward_to_frontend(
            "image_artifact:created",
            {
                "workspace_id": workspace_id,
                "image_artifact_id": artifact_id,
                "name": name,
                "size_bytes": size_bytes,
            },
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def handle_image_artifact_failed(
        self,
        task_id: str,
        workspace_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Handle image_artifact:failed by marking the pending image as failed."""
        from ...repositories import ImageGenerationRepository

        if not ImageGenerationRepository.capture_result(
            task_id=task_id,
            workspace_id=workspace_id,
            runner_id=runner_id,
            error=error,
        ):
            return

        logger.warning(
            "Image artifact creation failed: workspace=%s, task=%s, error=%s",
            workspace_id,
            task_id,
            error,
        )

        self._forward_to_frontend(
            "image_artifact:failed",
            {"workspace_id": workspace_id, "task_id": task_id, "error": error},
            workspace_id,
        )
        self._forward_workspace_operation(workspace_id, None)

    def handle_image_artifact_deleted(
        self,
        task_id: str,
        image_instance_id: str = "",
        runner_ref: str = "",
        result: str = "deleted",
        runner_id: str | None = None,
    ) -> None:
        """Mark an image instance deleted after runner cleanup confirms it."""
        from ...repositories import ImageGenerationRepository

        if result not in {"deleted", "already_absent"}:
            self.handle_image_artifact_delete_failed(
                task_id=task_id,
                error=f"Delete was not confirmed: {result}",
                runner_id=runner_id,
            )
            return
        ImageGenerationRepository.delete_result(
            task_id=task_id,
            runner_id=runner_id,
            image_id=image_instance_id,
            runner_ref=runner_ref,
        )
        # Coordinator finalizes assignments after all generation confirmations.

    def handle_image_artifact_delete_failed(
        self,
        task_id: str,
        error: str = "",
        runner_id: str | None = None,
    ) -> None:
        """Fail only the exact live deletion attempt."""
        from ...repositories import ImageGenerationRepository

        image = ImageGenerationRepository.delete_result(
            task_id=task_id,
            runner_id=runner_id,
            error=error,
        )
        if image and image.build_job_id:
            self.build_jobs.mark_delete_failed(image.build_job_id, error=error)
            self._mark_definition_delete_failed(image.origin_definition_id, error=error)

    async def delete_build_job(self, build_job_id: uuid.UUID) -> None:
        """Delete a runner image build and its associated artifact.

        Checks workspace dependencies before allowing deletion.
        If runner is offline, queues as pending_deletion.
        """
        from ...deletion_repository import DeletionRepository
        obj = await sync_to_async(self.build_jobs.get_by_id)(build_job_id)
        if obj is None:
            raise ValueError('Deletion target not found')
        if not obj.runner.organization_id:
            raise ConflictError('Global recipes are read-only')
        return await sync_to_async(DeletionRepository.request)(
            obj.runner.organization_id, None, 'assignment', obj.id)

    def _mark_definition_delete_failed(
        self,
        definition_id: uuid.UUID | None,
        *,
        error: str,
    ) -> None:
        """Move a definition delete flow into DELETE_FAILED when a child cleanup fails."""
        if definition_id is None:
            return
        definition = self.image_definitions.get_by_id(definition_id)
        if definition is None:
            return
        if definition.status not in {
            definition.Status.PENDING_DELETION,
            definition.Status.DELETING,
            definition.Status.DELETE_FAILED,
        }:
            return
        self.image_definitions.mark_delete_failed(definition_id, error=error)

    async def deactivate_image_definition(self, definition_id: uuid.UUID) -> None:
        """Deactivate a definition — immediately not selectable for new workspaces."""
        definition = await sync_to_async(self.image_definitions.get_by_id)(
            definition_id
        )
        if definition is None:
            raise ValueError(f"Image definition '{definition_id}' not found")
        await sync_to_async(self.image_definitions.deactivate)(definition_id)
        logger.info("Image definition deactivated: %s", definition_id)

    async def activate_image_definition(self, definition_id: uuid.UUID) -> None:
        """Re-activate a deactivated definition."""
        from ...models import ImageDefinition

        definition = await sync_to_async(self.image_definitions.get_by_id)(
            definition_id
        )
        if definition is None:
            raise ValueError(f"Image definition '{definition_id}' not found")
        if definition.status not in (
            ImageDefinition.Status.DEACTIVATED,
            ImageDefinition.Status.ACTIVE,
            ImageDefinition.Status.DELETE_FAILED,
        ):
            raise ConflictError(
                f"Cannot activate definition in state '{definition.status}'"
            )
        if definition.status == ImageDefinition.Status.DELETE_FAILED:
            in_progress = await sync_to_async(
                lambda: self.build_jobs.list_in_progress_deletes_for_definition(
                    definition_id
                ).exists()
            )()
            if in_progress:
                raise ConflictError(
                    "Cannot restore while runner image removal is still in progress"
                )
        await sync_to_async(self.image_definitions.activate)(definition_id)
        logger.info("Image definition activated: %s", definition_id)

    async def delete_image_definition(self, definition_id: uuid.UUID) -> None:
        """Orchestrated two-step definition delete.

        Step 1: Immediately deactivate.
        Step 2: Initiate deletion of all runner builds.
        Definition itself is only marked deleted when all build deletes are confirmed.
        """
        from ...deletion_repository import DeletionRepository
        obj = await sync_to_async(self.image_definitions.get_by_id)(definition_id)
        if obj is None:
            raise ValueError('Deletion target not found')
        if not obj.organization_id:
            raise ConflictError('Global recipes are read-only')
        return await sync_to_async(DeletionRepository.request)(
            obj.organization_id, None, 'definition', obj.id)
