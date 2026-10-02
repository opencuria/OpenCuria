"""Runner presence & queries (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.runners``,
``self.workspaces``, ``self.tasks``, ``self.image_instances``,
``self.build_jobs`` plus sibling helpers (``trigger_build_job`` /
``timeout_stale_image_operations`` via :class:`ImageLifecycleMixin`,
``fail_streams_for_runner`` via :class:`StreamTransportMixin`,
``_forward_runner_status_to_frontend`` via ``FrontendBusMixin``,
``_ensure_runner_supports_runtime`` / ``_validate_runner_qemu_limits``).
Lazy ``from ...models import ...`` imports keep their relative depth.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from common.exceptions import AuthenticationError
from common.utils import generate_uuid, hash_token

from ...enums import RuntimeType, TaskStatus, TaskType
from ...exceptions import (
    PendingDispatchError,
    RunnerNotFoundError,
    WorkspaceNotFoundError,
)
from ..infra.runner_presence import run_presence_query

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids runtime cycles
    from ...models import ImageBuildJob, ImageInstance, Runner, Workspace
else:
    from ...models import ImageBuildJob

logger = logging.getLogger(__name__)


class RunnerLifecycleMixin:
    """Runner lifecycle and read queries shared by RunnerService."""

    def authenticate_runner(self, token: str) -> Runner:
        """
        Authenticate a runner by its API token.

        Returns the Runner instance if valid, raises AuthenticationError otherwise.
        """
        token_hash = hash_token(token)
        runner = self.runners.get_by_token_hash(token_hash)
        if runner is None:
            raise AuthenticationError("Invalid runner API token")
        return runner

    async def authenticate_runner_async(self, token: str) -> Runner:
        """Authenticate on the bounded presence database lane."""
        return await run_presence_query(self.authenticate_runner, token)

    def register_runner(
        self,
        runner: Runner,
        *,
        sid: str,
        available_runtimes: list[str] | None = None,
    ) -> Runner:
        """Synchronously register a runner for internal service callers."""
        runner_id = runner.id
        runner, old_sid, changed = self.runners.register_session(
            runner_id,
            sid=sid,
            available_runtimes=(
                ["docker"] if available_runtimes is None else available_runtimes
            ),
        )
        if runner is None:
            raise RunnerNotFoundError(str(runner_id))
        runner._registration_changed = changed
        runner._superseded_sid = old_sid if old_sid != sid else None
        logger.info("Runner registered: %s (sid=%s)", runner.id, sid)
        return runner

    def notify_runner_online(self, runner: Runner) -> None:
        """Synchronously notify frontend for internal service callers."""
        self._forward_runner_status_to_frontend(runner, "online")

    def unregister_runner(self, sid: str) -> str | None:
        """Synchronously unregister a runner for internal service callers."""
        runner = self.runners.set_offline_for_sid(sid)
        if runner is None:
            logger.info("Ignoring stale runner disconnect (sid=%s)", sid)
            return None
        self._disconnect_streams_for_id(str(runner.id))
        self._forward_runner_status_to_frontend(runner, "offline")
        return str(runner.id)

    async def unregister_runner_async(self, sid: str) -> str | None:
        """Commit an SID-conditional offline transition and clean up its streams."""
        runner = await run_presence_query(self.runners.set_offline_for_sid, sid)
        if runner is None:
            logger.info("Ignoring stale runner disconnect (sid=%s)", sid)
            return None
        runner_id = str(runner.id)
        await run_presence_query(self._disconnect_streams_for_id, runner_id)
        await self.notify_runner_status_async(runner, "offline")
        logger.info("Runner unregistered: %s", runner_id)
        return runner_id

    async def notify_runner_online_async(self, runner: Runner) -> None:
        """Queue an online event after the registration SID is verified."""
        await self.notify_runner_status_async(runner, "online")

    async def notify_runner_status_async(self, runner: Runner, status: str) -> None:
        """Forward runner status using the bounded database lane for fanout lookup."""
        workspace_ids = await run_presence_query(
            self.workspaces.list_status_workspace_ids, runner.id
        )
        event = "runner:offline" if status == "offline" else "runner:online"
        for workspace_id in workspace_ids:
            workspace_id = str(workspace_id)
            self._forward_to_frontend(
                event,
                {"workspace_id": workspace_id, "runner_id": str(runner.id)},
                workspace_id,
            )

    async def register_runner_async(
        self,
        runner_id: str,
        *,
        sid: str,
        available_runtimes: list[str],
    ) -> Runner | None:
        """Commit registration on the bounded presence DB lane."""
        runner, old_sid, changed = await run_presence_query(
            self.runners.register_session,
            uuid.UUID(runner_id),
            sid=sid,
            available_runtimes=available_runtimes,
        )
        if runner is None:
            return None
        runner._registration_changed = changed
        runner._superseded_sid = old_sid if old_sid != sid else None
        logger.info("Runner registered: %s (sid=%s)", runner.id, sid)
        return runner

    async def is_active_runner_session(self, runner_id: str, sid: str) -> bool:
        """Return whether *sid* is the active, persisted runner session."""
        return await run_presence_query(
            self.runners.is_active_session,
            uuid.UUID(runner_id),
            sid,
        )

    async def get_runner_async(self, runner_id: str) -> Runner | None:
        """Load a runner on the bounded presence database lane."""
        return await run_presence_query(self.runners.get_by_id, uuid.UUID(runner_id))

    async def get_active_runner_for_sid(
        self, runner_id: str, sid: str
    ) -> Runner | None:
        """Load a runner only when its persisted online SID still matches."""
        return await run_presence_query(
            self.runners.get_active_session,
            uuid.UUID(runner_id),
            sid,
        )

    async def record_runner_heartbeat(self, runner_id: str, sid: str) -> bool:
        """Persist liveness only while *sid* remains the active session."""
        return await run_presence_query(
            self.runners.record_heartbeat,
            uuid.UUID(runner_id),
            sid,
        )

    async def dispatch_pending_image_builds(
        self, runner: Runner
    ) -> list[ImageBuildJob]:
        """Dispatch offline-created builds, raising if any item fails.

        Successful items remain dispatched; :class:`PendingDispatchError`
        reports failures so the coordinator retries on the next runner status.
        """
        await sync_to_async(self.timeout_stale_image_operations)()

        pending_builds = await sync_to_async(
            lambda: list(
                ImageBuildJob.objects.filter(
                    runner=runner,
                    status=ImageBuildJob.Status.PENDING,
                    build_task__isnull=True,
                ).select_related("image_definition", "runner")
            )
        )()

        dispatched = []
        failures: list[str] = []
        for build in pending_builds:
            try:
                await self.trigger_build_job(
                    image_definition=build.image_definition,
                    runner=runner,
                    activate=True,
                )
                dispatched.append(build)
                logger.info(
                    "Dispatched pending image build %s for runner %s",
                    build.id,
                    runner.id,
                )
            except Exception as exc:
                failures.append(f"{build.id}: {exc}")
                logger.exception(
                    "Failed to dispatch pending image build %s for runner %s",
                    build.id,
                    runner.id,
                )
        if failures:
            raise PendingDispatchError("pending image build dispatch", failures)
        return dispatched

    async def dispatch_pending_image_deletions(
        self, runner: Runner
    ) -> list[ImageInstance]:
        """Dispatch offline image deletions, raising if any item fails."""
        from ...models import ImageInstance

        pending_images = await sync_to_async(
            lambda: list(self.image_instances.list_pending_delete_for_runner(runner.id))
        )()

        dispatched = []
        failures: list[str] = []
        for image in pending_images:
            try:
                reused_active_task = False
                if image.deleting_task_id:
                    existing_task = await sync_to_async(self.tasks.get_by_id)(
                        uuid.UUID(image.deleting_task_id)
                    )
                    if existing_task and existing_task.status in {
                        TaskStatus.PENDING,
                        TaskStatus.IN_PROGRESS,
                    }:
                        task = existing_task
                        reused_active_task = (
                            image.status == ImageInstance.Status.DELETING
                        )
                    else:
                        task = None
                else:
                    task = None
                if task is None:
                    task_id = generate_uuid()
                    task = await sync_to_async(self.tasks.create)(
                        task_id=task_id,
                        runner=runner,
                        task_type=TaskType.DELETE_IMAGE,
                    )
                if not reused_active_task:
                    await sync_to_async(self.image_instances.mark_deleting)(
                        image.id,
                        deleting_task_id=str(task.id),
                    )
                await self._emit_to_runner(
                    runner,
                    "task:delete_image_artifact",
                    {
                        "task_id": str(task.id),
                        "image_instance_id": str(image.id),
                        "runtime_type": image.runtime_type,
                        "image_artifact_id": image.runner_ref,
                    },
                )
                await sync_to_async(self.tasks.mark_in_progress)(task)
                dispatched.append(image)
            except Exception as exc:
                failures.append(f"{image.id}: {exc}")
                logger.exception(
                    "Failed to dispatch pending image deletion %s for runner %s",
                    image.id,
                    runner.id,
                )
        if failures:
            raise PendingDispatchError("pending image deletion dispatch", failures)
        return dispatched

    def _disconnect_streams(self, runner: Runner) -> None:
        """Fail byte streams owned by a runner after its session ends."""
        self._disconnect_streams_for_id(str(runner.id))

    def _disconnect_streams_for_id(self, runner_id: str) -> None:
        """Fail byte streams using repository-only ownership lookups."""
        try:
            self.fail_streams_for_runner(runner_id)
        except Exception:
            logger.exception("Failed failing streams for runner %s", runner_id)

    def update_runner_qemu_settings(
        self,
        runner_id: uuid.UUID,
        **fields,
    ) -> Runner:
        """Update per-runner QEMU resource limits/defaults."""
        runner = self.get_runner(runner_id)
        updated_fields = dict(fields)
        if not updated_fields:
            return runner
        self._ensure_runner_supports_runtime(
            runner=runner,
            runtime_type=RuntimeType.QEMU,
        )
        for key, value in updated_fields.items():
            setattr(runner, key, value)
        self._validate_runner_qemu_limits(runner)
        return self.runners.update_qemu_settings(runner, **updated_fields)

    def list_runners(self, organization_id: uuid.UUID | None = None) -> list[Runner]:
        """Return all registered runners, optionally filtered by organization."""
        if organization_id:
            return list(self.runners.list_by_organization(organization_id))
        return list(self.runners.list_all())

    def get_runner(self, runner_id: uuid.UUID) -> Runner:
        """Return a runner by ID or raise RunnerNotFoundError."""
        runner = self.runners.get_by_id(runner_id)
        if runner is None:
            raise RunnerNotFoundError(str(runner_id))
        return runner

    def list_workspaces(
        self,
        runner_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        user=None,
    ) -> list[Workspace]:
        """Return workspaces filtered by org and owner."""
        if runner_id:
            qs = self.workspaces.list_by_runner(runner_id)
        elif organization_id:
            qs = self.workspaces.list_by_organization(organization_id)
        else:
            qs = self.workspaces.list_all()

        if user is not None:
            qs = qs.filter(created_by=user)

        return list(qs)

    def get_workspace(self, workspace_id: uuid.UUID) -> Workspace:
        """Return a workspace by ID or raise WorkspaceNotFoundError."""
        workspace = self.workspaces.get_by_id(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        return workspace

    def get_workspace_or_none(self, workspace_id: uuid.UUID) -> Workspace | None:
        """Return a workspace by ID or None (no raise).

        Phase 4 (leak closure): public read so external callers no longer
        reach into ``service.workspaces.get_by_id`` directly. Thin
        delegation, no logic change. Callers SHOULD prefer this over the
        repository attribute; ``apps/harness/access/runner_accessor.py``
        keeps a ``service.workspaces.get_by_id`` fallback for foreign
        service doubles (see its ``_service_workspace_lookup`` helper).
        """
        return self.workspaces.get_by_id(workspace_id)

    def get_workspace_for_user(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
    ) -> Workspace:
        """Return a workspace only when it belongs to the active org and owner."""
        workspace = self.get_workspace(workspace_id)
        if workspace.runner.organization_id != organization_id:
            raise WorkspaceNotFoundError(str(workspace_id))
        if workspace.created_by_id != user.id:
            raise WorkspaceNotFoundError(str(workspace_id))
        return workspace
