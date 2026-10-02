"""
Tests for repository layer.
"""

from __future__ import annotations

import uuid

import pytest

from apps.organizations.models import Organization
from apps.runners.enums import RunnerStatus, TaskStatus, TaskType, WorkspaceStatus
from apps.runners.repositories import (
    RunnerRepository,
    TaskRepository,
    WorkspaceRepository,
)
from common.utils import hash_token


@pytest.mark.django_db
class TestRunnerRepository:
    def test_create_and_get(self, db):
        org = Organization.objects.create(name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        token_hash = hash_token("test-token")
        runner = RunnerRepository.create(
            name="test", api_token_hash=token_hash, organization=org
        )
        found = RunnerRepository.get_by_id(runner.id)
        assert found is not None
        assert found.name == "test"

    def test_get_by_token_hash(self, db):
        org = Organization.objects.create(name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        token_hash = hash_token("lookup-token")
        RunnerRepository.create(api_token_hash=token_hash, organization=org)
        found = RunnerRepository.get_by_token_hash(token_hash)
        assert found is not None

    def test_register_and_conditionally_set_offline(self, runner):
        registered, previous_sid, changed = RunnerRepository.register_session(
            runner.id,
            sid="sid-1",
            available_runtimes=["docker"],
        )
        assert previous_sid == "test-sid-123"
        assert changed is True
        assert registered.sid == "sid-1"
        assert registered.status == RunnerStatus.ONLINE
        assert registered.available_runtimes == ["docker"]
        assert RunnerRepository.set_offline_for_sid("old-sid") is None
        offline = RunnerRepository.set_offline_for_sid("sid-1")
        assert offline.sid == ""
        assert offline.status == RunnerStatus.OFFLINE
        assert offline.connected_at == registered.connected_at
        runner.refresh_from_db()
        assert runner.status == RunnerStatus.OFFLINE
        assert runner.sid == ""


@pytest.mark.django_db
class TestWorkspaceRepository:
    def test_create_and_list(self, runner, user):
        ws = WorkspaceRepository.create(
            workspace_id=uuid.uuid4(),
            runner=runner,
            name="Workspace",
            created_by=user,
        )
        assert ws.status == WorkspaceStatus.CREATING

        all_ws = list(WorkspaceRepository.list_by_runner(runner.id))
        assert len(all_ws) >= 1

    def test_update_status(self, workspace):
        WorkspaceRepository.update_status(workspace, WorkspaceStatus.STOPPED)
        workspace.refresh_from_db()
        assert workspace.status == WorkspaceStatus.STOPPED

    def test_get_runner_id(self, workspace, runner):
        """Ownership lookup returns the runner UUID, or None if missing."""
        assert WorkspaceRepository.get_runner_id(workspace.id) == runner.id
        assert WorkspaceRepository.get_runner_id(uuid.uuid4()) is None


@pytest.mark.django_db
class TestTaskRepository:
    def test_create_and_complete(self, runner):
        task = TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=runner,
            task_type=TaskType.CREATE_WORKSPACE,
        )
        assert task.status == TaskStatus.PENDING

        TaskRepository.mark_in_progress(task)
        assert task.status == TaskStatus.IN_PROGRESS

        TaskRepository.complete(task)
        assert task.status == TaskStatus.COMPLETED

    def test_fail_with_error(self, runner):
        task = TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=runner,
            task_type=TaskType.CREATE_WORKSPACE,
        )
        TaskRepository.fail(task, "Something went wrong")
        assert task.status == TaskStatus.FAILED
        assert task.error == "Something went wrong"


