"""
Tests for repository layer.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from common.utils import hash_token

from apps.accounts.models import User
from apps.organizations.models import Organization
from apps.runners.enums import RunnerStatus, TaskStatus, TaskType, WorkspaceStatus
from apps.runners.models import Runner, Task, Workspace
from apps.runners.repositories import RunnerRepository, TaskRepository, WorkspaceRepository


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

    def test_set_online_offline(self, runner):
        RunnerRepository.set_online(runner, sid="sid-1")
        assert runner.status == RunnerStatus.ONLINE

        RunnerRepository.set_offline(runner)
        assert runner.status == RunnerStatus.OFFLINE


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


