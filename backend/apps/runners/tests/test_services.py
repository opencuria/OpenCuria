"""
Tests for RunnerService business logic.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.credentials.models import CredentialService
from apps.credentials.services import CredentialSvc
from apps.runners.enums import (
    AgentCommandPhase,
    RunnerStatus,
    TaskStatus,
    TaskType,
    WorkspaceOperation,
    WorkspaceStatus,
)
from apps.runners.exceptions import (
    RunnerNotFoundError,
    RunnerOfflineError,
    WorkspaceNotFoundError,
    WorkspaceStateError,
)
from common.exceptions import ConflictError, NotFoundError

from apps.runners.models import (
    AgentCommand,
    AgentCredentialRelationCommand,
    AgentDefinition,
    AgentDefinitionCredentialRelation,
    Chat,
    ImageDefinition,
    ImageInstance,
    Runner,
    ImageBuildJob,
    Session,
    Task,
    Workspace,
)
from apps.organizations.models import Organization
from apps.runners.services import RunnerService


@pytest.fixture
def sio_mock() -> AsyncMock:
    """Mock Socket.IO server."""
    return AsyncMock()


@pytest.fixture
def service(sio_mock: AsyncMock) -> RunnerService:
    """RunnerService with a mocked Socket.IO server."""
    return RunnerService(sio_server=sio_mock)


@pytest.mark.django_db
class TestRegisterRunner:
    def test_register_sets_online(self, service, runner):
        """Registering a runner should set it online."""
        result = service.register_runner(runner, sid="new-sid")
        assert result.status == RunnerStatus.ONLINE
        assert result.sid == "new-sid"

    def test_unregister_sets_offline(self, service, runner):
        """Disconnecting a runner should set it offline."""
        service.unregister_runner(runner.sid)
        runner.refresh_from_db()
        assert runner.status == RunnerStatus.OFFLINE


@pytest.mark.django_db(transaction=True)
class TestCreateWorkspace:
    @pytest.mark.asyncio
    async def test_dispatches_to_runner(self, service, sio_mock, runner, user):
        """Creating a workspace should emit task:create_workspace."""
        definition = ImageDefinition.objects.create(
            organization=runner.organization,
            created_by=user,
            name="Base Workspace",
            runtime_type="docker",
            base_distro="ubuntu:24.04",
        )
        build = ImageBuildJob.objects.create(
            image_definition=definition,
            runner=runner,
            status=ImageBuildJob.Status.ACTIVE,
        )
        runner_ref = "opencuria/custom/base:1"
        artifact = ImageInstance.objects.create(
            runner=runner,
            runtime_type="docker",
            origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
            origin_definition=definition,
            created_by=user,
            build_job=build,
            runner_ref=runner_ref,
            name="Base Workspace Artifact",
            status=ImageInstance.Status.READY,
        )
        workspace, task = await service.create_workspace(
            name="Test Workspace",
            repos=["https://github.com/test/repo"],
            image_artifact_id=artifact.id,
            env_vars={"GITHUB_TOKEN": "test-token"},
            ssh_keys=["-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----"],
            user=user,
            organization_id=runner.organization_id,
        )

        assert workspace.status == WorkspaceStatus.CREATING
        assert task.type == TaskType.CREATE_WORKSPACE
        assert task.status == TaskStatus.IN_PROGRESS
        sio_mock.emit.assert_called_once()
        _, payload = sio_mock.emit.await_args.args[:2]
        assert payload["env_vars"] == {"GITHUB_TOKEN": "test-token"}
        assert len(payload["ssh_keys"]) == 1
        workspace.refresh_from_db()
        assert workspace.base_image_instance_id == artifact.id

    @pytest.mark.asyncio
    async def test_requires_image_selection(self, service, runner, user):
        """Creating a workspace without an image should be rejected."""
        with pytest.raises(ConflictError):
            await service.create_workspace(
                name="No Image Workspace",
                repos=[],
                user=user,
                organization_id=runner.organization_id,
            )

    @pytest.mark.asyncio
    async def test_unknown_image_raises_not_found(self, service):
        """Selecting an unknown image should be rejected."""
        with pytest.raises(NotFoundError):
            await service.create_workspace(
                name="No Runner Workspace",
                repos=[],
                image_artifact_id=uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_rejects_runner_build_from_other_organization(self, service):
        """Selecting a foreign-org runner build should be rejected."""
        user_model = get_user_model()
        owner = user_model.objects.create_user(
            email=f"owner-{uuid.uuid4().hex[:6]}@example.com",
            password="secret",
        )
        local_org = Organization.objects.create(
            name=f"Local Org {uuid.uuid4().hex[:6]}",
            slug=f"local-org-{uuid.uuid4().hex[:8]}",
        )
        local_runner = Runner.objects.create(
            name="local-runner",
            api_token_hash=uuid.uuid4().hex,
            status=RunnerStatus.ONLINE,
            sid="local-sid",
            organization=local_org,
            available_runtimes=["docker"],
        )
        foreign_org = Organization.objects.create(
            name=f"Foreign Org {uuid.uuid4().hex[:6]}",
            slug=f"foreign-org-{uuid.uuid4().hex[:8]}",
        )
        foreign_runner = Runner.objects.create(
            name="foreign-runner",
            api_token_hash=uuid.uuid4().hex,
            status=RunnerStatus.ONLINE,
            sid="foreign-sid",
            organization=foreign_org,
            available_runtimes=["docker"],
        )
        definition = ImageDefinition.objects.create(
            organization=foreign_org,
            created_by=owner,
            name="Foreign Definition",
            runtime_type="docker",
            base_distro="ubuntu:24.04",
        )
        build = ImageBuildJob.objects.create(
            image_definition=definition,
            runner=foreign_runner,
            status=ImageBuildJob.Status.ACTIVE,
        )
        runner_ref = "opencuria/custom/foreign:1"
        artifact = ImageInstance.objects.create(
            runner=foreign_runner,
            runtime_type="docker",
            origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
            origin_definition=definition,
            created_by=owner,
            build_job=build,
            runner_ref=runner_ref,
            name="Foreign Artifact",
            status=ImageInstance.Status.READY,
        )

        with pytest.raises(NotFoundError):
            await service.create_workspace(
                name="cross-org",
                repos=[],
                image_artifact_id=artifact.id,
                organization_id=local_runner.organization_id,
            )

    @pytest.mark.asyncio
    async def test_rejects_runner_without_selected_definition_image(self, service, runner, user):
        """A manually selected runner must match the chosen definition image build."""
        other_runner = Runner.objects.create(
            name="other-runner",
            api_token_hash=uuid.uuid4().hex,
            status=RunnerStatus.ONLINE,
            sid="other-sid",
            organization=runner.organization,
            available_runtimes=["docker"],
        )
        definition = ImageDefinition.objects.create(
            organization=runner.organization,
            created_by=user,
            name="Definition Image",
            runtime_type="docker",
            base_distro="ubuntu:24.04",
        )
        build = ImageBuildJob.objects.create(
            image_definition=definition,
            runner=runner,
            status=ImageBuildJob.Status.ACTIVE,
        )
        runner_ref = "opencuria/custom/base:definition"
        artifact = ImageInstance.objects.create(
            runner=runner,
            runtime_type="docker",
            origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
            origin_definition=definition,
            created_by=user,
            build_job=build,
            runner_ref=runner_ref,
            name="Definition Artifact",
            status=ImageInstance.Status.READY,
        )

        with pytest.raises(
            ConflictError, match="Selected runner does not have the selected image artifact"
        ):
            await service.create_workspace(
                name="mismatch-runner",
                repos=[],
                image_artifact_id=artifact.id,
                runner_id=other_runner.id,
                user=user,
                organization_id=runner.organization_id,
            )

    @pytest.mark.asyncio
    async def test_rejects_captured_image_from_other_organization(self, service):
        """Selecting a foreign-org captured image should be rejected."""
        user_model = get_user_model()
        owner = user_model.objects.create_user(
            email=f"owner-image-{uuid.uuid4().hex[:6]}@example.com",
            password="secret",
        )
        local_org = Organization.objects.create(
            name=f"Image Local Org {uuid.uuid4().hex[:6]}",
            slug=f"image-local-org-{uuid.uuid4().hex[:8]}",
        )
        local_runner = Runner.objects.create(
            name="local-runner-image",
            api_token_hash=uuid.uuid4().hex,
            status=RunnerStatus.ONLINE,
            sid="local-image-sid",
            organization=local_org,
            available_runtimes=["docker"],
        )
        foreign_org = Organization.objects.create(
            name=f"Image Foreign Org {uuid.uuid4().hex[:6]}",
            slug=f"image-foreign-org-{uuid.uuid4().hex[:8]}",
        )
        foreign_runner = Runner.objects.create(
            name="foreign-runner-image",
            api_token_hash=uuid.uuid4().hex,
            status=RunnerStatus.ONLINE,
            sid="foreign-image-sid",
            organization=foreign_org,
            available_runtimes=["docker"],
        )
        foreign_workspace = Workspace.objects.create(
            runner=foreign_runner,
            name="Foreign Workspace",
            status=WorkspaceStatus.RUNNING,
            created_by=owner,
            runtime_type="docker",
        )
        image = ImageInstance.objects.create(
            runner=foreign_runner,
            runtime_type="docker",
            origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
            origin_workspace=foreign_workspace,
            created_by=owner,
            runner_ref="snapshot-1",
            name="Foreign Image",
            status=ImageInstance.Status.READY,
        )

        with pytest.raises(NotFoundError):
            await service.create_workspace(
                name="cross-org-image",
                repos=[],
                image_artifact_id=image.id,
                organization_id=local_runner.organization_id,
            )


@pytest.mark.django_db(transaction=True)
class TestCreateImageArtifactSecurity:
    @pytest.mark.asyncio
    async def test_rejects_image_artifact_for_workspace_in_other_organization(self, service, user):
        local_org = Organization.objects.create(
            name=f"Snapshot Local Org {uuid.uuid4().hex[:6]}",
            slug=f"snapshot-local-org-{uuid.uuid4().hex[:8]}",
        )
        foreign_org = Organization.objects.create(
            name=f"Snapshot Foreign Org {uuid.uuid4().hex[:6]}",
            slug=f"snapshot-foreign-org-{uuid.uuid4().hex[:8]}",
        )
        foreign_runner = Runner.objects.create(
            name="snapshot-foreign-runner",
            api_token_hash=uuid.uuid4().hex,
            status=RunnerStatus.ONLINE,
            sid="snapshot-foreign-sid",
            organization=foreign_org,
            available_runtimes=["docker"],
        )
        foreign_workspace = Workspace.objects.create(
            runner=foreign_runner,
            name="Foreign Snapshot Workspace",
            status=WorkspaceStatus.RUNNING,
            created_by=user,
            runtime_type="docker",
        )

        with pytest.raises(WorkspaceNotFoundError):
            await service.create_image_artifact(
                workspace_id=foreign_workspace.id,
                name="forbidden-image-artifact",
                organization_id=local_org.id,
            )


@pytest.mark.django_db(transaction=True)
class TestImageDeletionLifecycle:
    @pytest.mark.asyncio
    async def test_delete_retires_image_when_workspace_still_depends_on_it(
        self, service, runner, user
    ):
        image = ImageInstance.objects.create(
            runner=runner,
            runtime_type="docker",
            origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
            name="Reusable Base",
            runner_ref="opencuria/custom/reusable:1",
            status=ImageInstance.Status.READY,
            created_by=user,
        )
        Workspace.objects.create(
            runner=runner,
            name="Dependent Workspace",
            status=WorkspaceStatus.STOPPED,
            runtime_type="docker",
            created_by=user,
            base_image_instance=image,
        )

        with pytest.raises(ConflictError, match="was retired instead"):
            await service.delete_image_artifact(image.id)

        image.refresh_from_db()
        assert image.status == ImageInstance.Status.RETIRED

    @pytest.mark.asyncio
    async def test_delete_marks_image_deleting_when_runner_offline(
        self, service, runner, user
    ):
        runner.status = RunnerStatus.OFFLINE
        runner.save(update_fields=["status"])
        image = ImageInstance.objects.create(
            runner=runner,
            runtime_type="docker",
            origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
            name="Offline Cleanup",
            runner_ref="opencuria/custom/offline-cleanup:1",
            status=ImageInstance.Status.READY,
            created_by=user,
        )

        await service.delete_image_artifact(image.id)

        image.refresh_from_db()
        assert image.status == ImageInstance.Status.DELETING
        assert image.deleting_task_id is not None

    @pytest.mark.asyncio
    async def test_dispatch_pending_image_deletions_marks_task_in_progress(
        self, service, runner, user
    ):
        image = ImageInstance.objects.create(
            runner=runner,
            runtime_type="docker",
            origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
            name="Pending Delete",
            runner_ref="opencuria/custom/pending-delete:1",
            status=ImageInstance.Status.DELETING,
            created_by=user,
            deleting_task_id=str(
                Task.objects.create(
                    runner=runner,
                    type=TaskType.DELETE_IMAGE,
                    status=TaskStatus.PENDING,
                ).id
            ),
        )

        dispatched = await service.dispatch_pending_image_deletions(runner)

        assert [item.id for item in dispatched] == [image.id]
        task = Task.objects.get(id=image.deleting_task_id)
        assert task.status == TaskStatus.IN_PROGRESS

    def test_handle_image_artifact_deleted_marks_image_deleted(
        self, service, runner, user
    ):
        task = Task.objects.create(
            runner=runner,
            type=TaskType.DELETE_IMAGE,
            status=TaskStatus.IN_PROGRESS,
        )
        image = ImageInstance.objects.create(
            runner=runner,
            runtime_type="docker",
            origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
            name="Delete Confirmed",
            runner_ref="opencuria/custom/delete-confirmed:1",
            status=ImageInstance.Status.DELETING,
            created_by=user,
            deleting_task_id=str(task.id),
        )

        service.handle_image_artifact_deleted(
            task_id=str(task.id),
            image_instance_id=str(image.id),
            runner_id=str(runner.id),
        )

        image.refresh_from_db()
        task.refresh_from_db()
        assert image.status == ImageInstance.Status.DELETED
        assert task.status == TaskStatus.COMPLETED


@pytest.mark.django_db(transaction=True)
class TestRuntimeCompatibilityGuards:
    @pytest.mark.asyncio
    async def test_resume_workspace_rejects_runner_without_workspace_runtime(
        self, service, stopped_workspace
    ):
        """Resume should fail when the runner no longer supports the workspace runtime."""
        stopped_workspace.runtime_type = "qemu"
        stopped_workspace.save(update_fields=["runtime_type"])
        runner = stopped_workspace.runner
        runner.available_runtimes = ["docker"]
        runner.save(update_fields=["available_runtimes"])

        with pytest.raises(ConflictError, match="Runner does not support runtime 'qemu'"):
            await service.resume_workspace(stopped_workspace.id)

    def test_update_runner_qemu_settings_rejects_docker_only_runner(self, service, runner):
        """QEMU runner settings cannot be updated on a runner without QEMU support."""
        runner.available_runtimes = ["docker"]
        runner.save(update_fields=["available_runtimes"])

        with pytest.raises(ConflictError, match="Runner does not support runtime 'qemu'"):
            service.update_runner_qemu_settings(runner.id, qemu_default_vcpus=4)

    @pytest.mark.asyncio
    async def test_trigger_build_job_rejects_unsupported_runtime(
        self, service, runner, user
    ):
        """Image definition builds must match a runner-supported runtime."""
        runner.available_runtimes = ["docker"]
        runner.save(update_fields=["available_runtimes"])
        definition = ImageDefinition.objects.create(
            organization=runner.organization,
            created_by=user,
            name="QEMU Base",
            runtime_type="qemu",
            base_distro="ubuntu:24.04",
        )

        with pytest.raises(ConflictError, match="Runner does not support runtime 'qemu'"):
            await service.trigger_build_job(
                image_definition=definition,
                runner=runner,
                activate=True,
                created_by=user,
            )

    @pytest.mark.asyncio
    async def test_clone_workspace_from_image_artifact_rejects_unsupported_runtime(
        self, service, runner, user
    ):
        """Cloning from an artifact should fail if the runner no longer supports its runtime."""
        source_workspace = Workspace.objects.create(
            runner=runner,
            name="QEMU Source",
            status=WorkspaceStatus.RUNNING,
            created_by=user,
            runtime_type="qemu",
            qemu_vcpus=2,
            qemu_memory_mb=4096,
            qemu_disk_size_gb=50,
        )
        artifact = ImageInstance.objects.create(
            runner=runner,
            runtime_type="qemu",
            origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
            origin_workspace=source_workspace,
            created_by=user,
            name="QEMU Snapshot",
            runner_ref="artifact-qemu-1",
            status=ImageInstance.Status.READY,
        )
        runner.available_runtimes = ["docker"]
        runner.save(update_fields=["available_runtimes"])

        with pytest.raises(ConflictError, match="Runner does not support runtime 'qemu'"):
            await service.create_workspace_from_image_artifact(
                image_artifact_id=artifact.id,
                name="clone-qemu",
                user=user,
                organization_id=runner.organization_id,
            )


@pytest.mark.django_db
class TestHandleWorkspaceCreated:
    def test_marks_workspace_running(self, service, runner, workspace):
        """workspace:created should mark workspace as RUNNING."""
        # Create a task for this workspace
        task = Task.objects.create(
            runner=runner,
            workspace=workspace,
            type=TaskType.CREATE_WORKSPACE,
            status=TaskStatus.IN_PROGRESS,
        )
        workspace.status = WorkspaceStatus.CREATING
        workspace.save()

        service.handle_workspace_created(
            task_id=str(task.id),
            workspace_id=str(workspace.id),
            status="created",
        )

        workspace.refresh_from_db()
        task.refresh_from_db()
        assert workspace.status == WorkspaceStatus.RUNNING
        assert workspace.active_operation is None
        assert task.status == TaskStatus.COMPLETED


@pytest.mark.django_db
class TestHandleWorkspaceError:
    def test_marks_workspace_failed_for_clone_task(self, service, runner, workspace):
        """workspace:error should mark clone target workspaces as FAILED."""
        task = Task.objects.create(
            runner=runner,
            workspace=workspace,
            type=TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT,
            status=TaskStatus.IN_PROGRESS,
        )
        workspace.status = WorkspaceStatus.CREATING
        workspace.save()

        service.handle_workspace_error(
            task_id=str(task.id),
            error="clone failed",
        )

        workspace.refresh_from_db()
        task.refresh_from_db()
        assert workspace.status == WorkspaceStatus.FAILED
        assert workspace.active_operation is None
        assert task.status == TaskStatus.FAILED
        assert task.error == "clone failed"


@pytest.mark.django_db(transaction=True)
class TestWorkspaceOperationState:
    @pytest.mark.asyncio
    async def test_running_qemu_update_sets_restart_operation(
        self, service, workspace
    ):
        """Running QEMU resource updates should mark the workspace as restarting."""
        workspace.runtime_type = "qemu"
        workspace.qemu_vcpus = 2
        workspace.qemu_memory_mb = 4096
        workspace.qemu_disk_size_gb = 50
        workspace.save(
            update_fields=[
                "runtime_type",
                "qemu_vcpus",
                "qemu_memory_mb",
                "qemu_disk_size_gb",
                "updated_at",
            ]
        )

        updated = await service.update_workspace(
            workspace.id,
            qemu_vcpus=4,
            qemu_memory_mb=8192,
            qemu_disk_size_gb=60,
        )

        assert updated is not None
        assert updated.active_operation == WorkspaceOperation.RESTARTING

    @pytest.mark.asyncio
    async def test_busy_workspace_rejects_prompt(self, service, workspace):
        """Prompts must be blocked while a blocking workspace operation is active."""
        workspace.active_operation = WorkspaceOperation.RESTARTING
        workspace.save(update_fields=["active_operation", "updated_at"])

        with pytest.raises(ConflictError, match="currently restarting"):
            await service.run_prompt(workspace.id, "Fix the bug")


@pytest.mark.django_db
class TestHeartbeatReconciliation:
    @staticmethod
    def _create_runner() -> Runner:
        org = Organization.objects.create(
            name=f"Test Org {uuid.uuid4().hex[:6]}",
            slug=f"test-org-{uuid.uuid4().hex[:10]}",
        )
        return Runner.objects.create(
            name="test-runner",
            api_token_hash=uuid.uuid4().hex,
            status=RunnerStatus.ONLINE,
            sid="test-sid-heartbeat",
            organization=org,
        )

    @staticmethod
    def _create_workspace(runner: Runner, status: str) -> Workspace:
        user_model = get_user_model()
        user = user_model.objects.create_user(
            email=f"heartbeat-{uuid.uuid4().hex[:8]}@example.com",
            password="admin",
        )
        return Workspace.objects.create(
            runner=runner,
            status=status,
            created_by=user,
            name="Heartbeat Workspace",
        )

    def test_promotes_stopped_workspace_to_running(
        self, service
    ):
        """Heartbeat should promote STOPPED to RUNNING when runtime is active."""
        runner = self._create_runner()
        stopped_workspace = self._create_workspace(runner, WorkspaceStatus.STOPPED)
        service.handle_heartbeat(
            runner=runner,
            workspaces=[
                {
                    "workspace_id": str(stopped_workspace.id),
                    "status": "running",
                }
            ],
        )

        stopped_workspace.refresh_from_db()
        assert stopped_workspace.status == WorkspaceStatus.RUNNING

    def test_creating_workspace_not_promoted_by_heartbeat(
        self, service
    ):
        """Heartbeat must not bypass explicit workspace:created transition."""
        runner = self._create_runner()
        workspace = self._create_workspace(runner, WorkspaceStatus.CREATING)

        service.handle_heartbeat(
            runner=runner,
            workspaces=[
                {
                    "workspace_id": str(workspace.id),
                    "status": "running",
                }
            ],
        )

        workspace.refresh_from_db()
        assert workspace.status == WorkspaceStatus.CREATING

    def test_missing_workspace_marked_failed(self, service):
        """Heartbeat should mark backend workspace FAILED when runtime misses it."""
        runner = self._create_runner()
        workspace = self._create_workspace(runner, WorkspaceStatus.RUNNING)

        service.handle_heartbeat(runner=runner, workspaces=[])

        workspace.refresh_from_db()
        assert workspace.status == WorkspaceStatus.FAILED

    def test_unknown_runtime_workspace_triggers_cleanup_task(self, service, sio_mock):
        """Heartbeat should request runner cleanup for unknown runtime workspaces."""
        runner = self._create_runner()
        unknown_workspace_id = str(uuid.uuid4())

        service.handle_heartbeat(
            runner=runner,
            workspaces=[
                {
                    "workspace_id": unknown_workspace_id,
                    "status": "running",
                }
            ],
        )

        sio_mock.emit.assert_awaited_once_with(
            "task:cleanup_unknown_workspace",
            {"workspace_id": unknown_workspace_id},
            to=runner.sid,
        )

    def test_unknown_cleanup_request_is_deduplicated_while_pending(
        self, service, sio_mock
    ):
        """Repeated heartbeats should not spam duplicate cleanup requests."""
        runner = self._create_runner()
        unknown_workspace_id = str(uuid.uuid4())
        payload = [
            {
                "workspace_id": unknown_workspace_id,
                "status": "running",
            }
        ]

        service.handle_heartbeat(runner=runner, workspaces=payload)
        service.handle_heartbeat(runner=runner, workspaces=payload)

        sio_mock.emit.assert_awaited_once_with(
            "task:cleanup_unknown_workspace",
            {"workspace_id": unknown_workspace_id},
            to=runner.sid,
        )

    @pytest.mark.parametrize(
        "terminal_state",
        [WorkspaceStatus.FAILED, WorkspaceStatus.REMOVED],
    )
    def test_terminal_backend_state_triggers_cleanup(
        self, service, sio_mock, terminal_state
    ):
        """Heartbeat should clean runner instances for FAILED/REMOVED workspaces."""
        runner = self._create_runner()
        workspace = self._create_workspace(runner, terminal_state)

        service.handle_heartbeat(
            runner=runner,
            workspaces=[
                {
                    "workspace_id": str(workspace.id),
                    "status": "running",
                }
            ],
        )

        sio_mock.emit.assert_awaited_once_with(
            "task:cleanup_unknown_workspace",
            {"workspace_id": str(workspace.id)},
            to=runner.sid,
        )


@pytest.mark.django_db(transaction=True)
class TestAutoStopInactiveWorkspaces:
    @pytest.mark.asyncio
    async def test_dispatches_stop_for_inactive_workspace(self, service, sio_mock, workspace):
        workspace.runner.organization.workspace_auto_stop_timeout_minutes = 5
        workspace.runner.organization.save(update_fields=["workspace_auto_stop_timeout_minutes"])
        workspace.last_activity_at = timezone.now() - timedelta(minutes=10)
        workspace.save(update_fields=["last_activity_at", "updated_at"])

        tasks = await service.auto_stop_inactive_workspaces(runner_id=workspace.runner_id)

        assert len(tasks) == 1
        workspace.refresh_from_db()
        assert workspace.active_operation == WorkspaceOperation.STOPPING
        sio_mock.emit.assert_awaited_once()
        event, payload = sio_mock.emit.await_args.args[:2]
        assert event == "task:stop_workspace"
        assert payload["workspace_id"] == str(workspace.id)

    @pytest.mark.asyncio
    async def test_skips_workspace_with_active_session(self, service, sio_mock, workspace):
        workspace.runner.organization.workspace_auto_stop_timeout_minutes = 5
        workspace.runner.organization.save(update_fields=["workspace_auto_stop_timeout_minutes"])
        workspace.last_activity_at = timezone.now() - timedelta(minutes=10)
        workspace.save(update_fields=["last_activity_at", "updated_at"])
        chat = Chat.objects.create(workspace=workspace, name="Busy Chat")
        Session.objects.create(
            chat=chat,
            prompt="Still running",
            status="running",
        )

        tasks = await service.auto_stop_inactive_workspaces(runner_id=workspace.runner_id)

        assert tasks == []
        sio_mock.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_terminal_and_file_events_touch_workspace_activity(self, service, workspace):
        before = workspace.last_activity_at
        await service.forward_terminal_input(
            workspace_id=str(workspace.id),
            terminal_id="term-1",
            data="ls\n",
        )
        workspace.refresh_from_db()
        after_terminal = workspace.last_activity_at
        assert after_terminal >= before

        await service.forward_files_event(
            workspace_id=str(workspace.id),
            event="files:list",
            data={"workspace_id": str(workspace.id), "request_id": "req-1", "path": "/workspace"},
        )
        workspace.refresh_from_db()
        assert workspace.last_activity_at >= after_terminal

    def test_terminal_state_cleanup_is_deduplicated_while_pending(
        self, service, sio_mock
    ):
        """Repeated heartbeats should not repeat terminal-state cleanup requests."""
        org = Organization.objects.create(
            name=f"Test Org {uuid.uuid4().hex[:6]}",
            slug=f"test-org-{uuid.uuid4().hex[:10]}",
        )
        runner = Runner.objects.create(
            name="test-runner",
            api_token_hash=uuid.uuid4().hex,
            status=RunnerStatus.ONLINE,
            sid="test-sid-heartbeat",
            organization=org,
        )
        user = get_user_model().objects.create_user(
            email=f"heartbeat-{uuid.uuid4().hex[:8]}@example.com",
            password="admin",
        )
        workspace = Workspace.objects.create(
            runner=runner,
            status=WorkspaceStatus.REMOVED,
            created_by=user,
            name="Heartbeat Workspace",
        )
        payload = [
            {
                "workspace_id": str(workspace.id),
                "status": "running",
            }
        ]

        service.handle_heartbeat(runner=runner, workspaces=payload)
        service.handle_heartbeat(runner=runner, workspaces=payload)

        sio_mock.emit.assert_awaited_once_with(
            "task:cleanup_unknown_workspace",
            {"workspace_id": str(workspace.id)},
            to=runner.sid,
        )


@pytest.mark.django_db(transaction=True)
class TestRunPrompt:
    @pytest.mark.asyncio
    async def test_dispatches_prompt(self, service, sio_mock, workspace):
        """Running a prompt should create session + task and emit."""
        AgentDefinition.objects.create(
            name="copilot",
            description="copilot",
            supports_multi_chat=True,
        )
        agent = AgentDefinition.objects.get(name="copilot")
        AgentCommand.objects.create(
            agent=agent,
            phase="run",
            args=["copilot", "run", "{prompt}"],
            workdir="/workspace",
            order=0,
        )
        chat = Chat.objects.create(
            workspace=workspace,
            name="Default",
            agent_definition=agent,
            agent_type="copilot",
        )
        session, task, _ = await service.run_prompt(
            workspace.id,
            "Fix the bug",
            chat_id=str(chat.id),
        )

        assert session.prompt == "Fix the bug"
        assert task.type == TaskType.RUN_PROMPT
        sio_mock.emit.assert_called_once()

    @pytest.mark.asyncio
    async def test_stopped_workspace_raises(self, service, stopped_workspace):
        """Should raise when workspace is not RUNNING."""
        with pytest.raises(WorkspaceStateError):
            await service.run_prompt(
                stopped_workspace.id, "Fix the bug"
            )

    @pytest.mark.asyncio
    async def test_dispatches_credential_relation_preflight_commands(
        self, service, sio_mock, workspace, user
    ):
        """run_prompt should include relation-bound preflight commands."""
        credential_service = CredentialService.objects.create(
            name="GitHub Token",
            slug=f"github-token-{uuid.uuid4().hex[:6]}",
            credential_type="env",
            env_var_name="GITHUB_TOKEN",
            label="GitHub PAT",
        )
        credential = CredentialSvc().create_org_credential(
            organization_id=workspace.runner.organization_id,
            service_id=credential_service.id,
            name="GitHub PAT",
            value="secret-token",
            user=user,
        )
        workspace.credentials.add(credential)

        agent = AgentDefinition.objects.create(
            name=f"copilot-{uuid.uuid4().hex[:6]}",
            description="copilot",
            supports_multi_chat=True,
            default_env={"AGENT_DEFAULT": "1"},
        )
        AgentCommand.objects.create(
            agent=agent,
            phase="run",
            args=["copilot", "run", "{prompt}"],
            workdir="/workspace",
            order=0,
        )
        relation = AgentDefinitionCredentialRelation.objects.create(
            agent_definition=agent,
            credential_service=credential_service,
            default_env={"RELATION_DEFAULT": "yes"},
        )
        AgentCredentialRelationCommand.objects.create(
            relation=relation,
            phase=AgentCommandPhase.CONFIGURE,
            args=["gh", "auth", "setup-git"],
            workdir="/workspace",
            env={"REL_CMD": "1"},
            description="Bind GitHub auth",
            order=0,
        )
        chat = Chat.objects.create(
            workspace=workspace,
            name="Default",
            agent_definition=agent,
            agent_type=agent.name,
        )

        await service.run_prompt(
            workspace.id,
            "Fix the bug",
            chat_id=str(chat.id),
        )

        event, payload = sio_mock.emit.await_args.args[:2]
        assert event == "task:run_prompt"
        assert payload["env_vars"]["GITHUB_TOKEN"] == "secret-token"
        assert payload["configure_commands"] == [
            {
                "args": ["gh", "auth", "setup-git"],
                "workdir": "/workspace",
                "env": {
                    "GITHUB_TOKEN": "secret-token",
                    "RELATION_DEFAULT": "yes",
                    "AGENT_DEFAULT": "1",
                    "REL_CMD": "1",
                },
                "description": "Bind GitHub auth",
            }
        ]
        assert payload["command"]["env"] == {
            "GITHUB_TOKEN": "secret-token",
            "RELATION_DEFAULT": "yes",
            "AGENT_DEFAULT": "1",
        }

    @pytest.mark.asyncio
    async def test_single_chat_old_chat_locked(self, service, workspace):
        """Single-chat agents only allow writes to their latest chat."""
        agent = AgentDefinition.objects.create(
            name="single-agent",
            description="single",
            supports_multi_chat=False,
        )
        old_chat = Chat.objects.create(
            workspace=workspace,
            name="Old",
            agent_definition=agent,
            agent_type="single-agent",
        )
        Chat.objects.create(
            workspace=workspace,
            name="Latest",
            agent_definition=agent,
            agent_type="single-agent",
        )

        with pytest.raises(ConflictError):
            await service.run_prompt(
                workspace.id,
                "Try old chat",
                chat_id=str(old_chat.id),
            )


@pytest.mark.django_db(transaction=True)
class TestStartTerminal:
    @pytest.mark.asyncio
    async def test_dispatches_terminal_with_credential_relation_preflights(
        self, service, sio_mock, workspace, user
    ):
        """start_terminal should include relation-bound preflight commands."""
        credential_service = CredentialService.objects.create(
            name="GitHub Token",
            slug=f"github-token-{uuid.uuid4().hex[:6]}",
            credential_type="env",
            env_var_name="GITHUB_TOKEN",
            label="GitHub PAT",
        )
        credential = CredentialSvc().create_org_credential(
            organization_id=workspace.runner.organization_id,
            service_id=credential_service.id,
            name="GitHub PAT",
            value="terminal-token",
            user=user,
        )
        workspace.credentials.add(credential)

        agent = AgentDefinition.objects.create(
            name=f"terminal-agent-{uuid.uuid4().hex[:6]}",
            description="terminal",
            supports_multi_chat=True,
        )
        relation = AgentDefinitionCredentialRelation.objects.create(
            agent_definition=agent,
            credential_service=credential_service,
            default_env={"RELATION_DEFAULT": "yes"},
        )
        AgentCredentialRelationCommand.objects.create(
            relation=relation,
            phase=AgentCommandPhase.CONFIGURE,
            args=["gh", "auth", "status"],
            workdir="/workspace",
            env={"CHECK_AUTH": "1"},
            description="Verify GitHub auth",
            order=0,
        )
        Chat.objects.create(
            workspace=workspace,
            name="Default",
            agent_definition=agent,
            agent_type=agent.name,
        )

        task = await service.start_terminal(workspace.id)

        assert task.type == TaskType.START_TERMINAL
        event, payload = sio_mock.emit.await_args.args[:2]
        assert event == "task:start_terminal"
        assert payload["env_vars"]["GITHUB_TOKEN"] == "terminal-token"
        assert payload["configure_commands"] == [
            {
                "args": ["gh", "auth", "status"],
                "workdir": "/workspace",
                "env": {
                    "GITHUB_TOKEN": "terminal-token",
                    "RELATION_DEFAULT": "yes",
                    "CHECK_AUTH": "1",
                },
                "description": "Verify GitHub auth",
            }
        ]


@pytest.mark.django_db
class TestHandleOutputError:
    def test_user_cancelled_keeps_failed_session_without_persistent_error(
        self, service, workspace, runner
    ):
        """User-cancelled prompts should fail session but not set error_message."""
        chat = Chat.objects.create(workspace=workspace, name="Test")
        session = Session.objects.create(
            chat=chat,
            prompt="Cancelled prompt",
            output="partial output",
            status="running",
        )
        task = Task.objects.create(
            runner=runner,
            workspace=workspace,
            session=session,
            type=TaskType.RUN_PROMPT,
            status=TaskStatus.IN_PROGRESS,
        )

        service.handle_output_error(
            str(task.id),
            str(workspace.id),
            "Prompt execution cancelled by user",
            runner_id=str(runner.id),
        )

        session.refresh_from_db()
        task.refresh_from_db()
        assert session.status == "failed"
        assert session.error_message is None
        assert session.output == "partial output\n[Error] Prompt execution cancelled by user"
        assert task.status == TaskStatus.FAILED
        assert task.error == "Prompt execution cancelled by user"

    def test_regular_error_remains_persistent(self, service, workspace, runner):
        """Non-cancellation errors should still persist in session.error_message."""
        chat = Chat.objects.create(workspace=workspace, name="Test")
        session = Session.objects.create(
            chat=chat,
            prompt="Broken prompt",
            output="work in progress",
            status="running",
        )
        task = Task.objects.create(
            runner=runner,
            workspace=workspace,
            session=session,
            type=TaskType.RUN_PROMPT,
            status=TaskStatus.IN_PROGRESS,
        )

        service.handle_output_error(
            str(task.id),
            str(workspace.id),
            "Tool crashed",
            runner_id=str(runner.id),
        )

        session.refresh_from_db()
        task.refresh_from_db()
        assert session.status == "failed"
        assert session.error_message == "Tool crashed"
        assert session.output == "work in progress\n[Error] Tool crashed"
        assert task.status == TaskStatus.FAILED
        assert task.error == "Tool crashed"


@pytest.mark.django_db(transaction=True)
class TestDispatchPendingImageBuilds:
    """Tests for dispatching pending image builds when a runner comes online."""

    @pytest.mark.asyncio
    async def test_dispatches_pending_builds(self, service, sio_mock, runner, user):
        """Pending builds without a task should be dispatched."""
        definition = ImageDefinition.objects.create(
            organization=runner.organization,
            created_by=user,
            name="Pending Build Test",
            runtime_type="docker",
            base_distro="ubuntu:22.04",
        )
        build = ImageBuildJob.objects.create(
            image_definition=definition,
            runner=runner,
            status=ImageBuildJob.Status.PENDING,
            build_task=None,
        )

        dispatched = await service.dispatch_pending_image_builds(runner)

        assert len(dispatched) == 1
        assert dispatched[0].id == build.id

        # Build should now have a task and the SIO mock should have been called
        build.refresh_from_db()
        assert build.build_task is not None
        assert build.status == ImageBuildJob.Status.PENDING
        sio_mock.emit.assert_called()

        # An artifact should have been created (in CREATING status)
        artifact = ImageInstance.objects.filter(build_job=build).first()
        assert artifact is not None
        assert artifact.status == ImageInstance.Status.BUILDING

    @pytest.mark.asyncio
    async def test_skips_builds_with_task(self, service, sio_mock, runner, user):
        """Builds that already have a task should not be re-dispatched."""
        definition = ImageDefinition.objects.create(
            organization=runner.organization,
            created_by=user,
            name="Has Task Test",
            runtime_type="docker",
            base_distro="ubuntu:22.04",
        )
        task = Task.objects.create(
            runner=runner,
            type=TaskType.BUILD_IMAGE,
            status=TaskStatus.IN_PROGRESS,
        )
        ImageBuildJob.objects.create(
            image_definition=definition,
            runner=runner,
            status=ImageBuildJob.Status.PENDING,
            build_task=task,
        )

        dispatched = await service.dispatch_pending_image_builds(runner)
        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_skips_active_builds(self, service, sio_mock, runner, user):
        """Already active builds should not be dispatched."""
        definition = ImageDefinition.objects.create(
            organization=runner.organization,
            created_by=user,
            name="Active Build Test",
            runtime_type="docker",
            base_distro="ubuntu:22.04",
        )
        ImageBuildJob.objects.create(
            image_definition=definition,
            runner=runner,
            status=ImageBuildJob.Status.ACTIVE,
        )

        dispatched = await service.dispatch_pending_image_builds(runner)
        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_no_pending_builds_returns_empty(self, service, runner):
        """When there are no pending builds, return an empty list."""
        dispatched = await service.dispatch_pending_image_builds(runner)
        assert dispatched == []
