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
from apps.runners.enums import (
    RunnerStatus,
    SessionStatus,
    TaskStatus,
    TaskType,
    WorkspaceStatus,
)
from apps.runners.models import Chat, Runner, Session, Task, Workspace
from apps.runners.repositories import (
    ConversationRepository,
    RunnerRepository,
    SessionRepository,
    TaskRepository,
    WorkspaceRepository,
)


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


@pytest.mark.django_db
class TestSessionRepository:
    def test_create_and_complete(self, workspace):
        chat = Chat.objects.create(workspace=workspace, name="Test Chat")
        session = SessionRepository.create(
            session_id=uuid.uuid4(),
            chat=chat,
            prompt="test prompt",
        )
        assert session.status == SessionStatus.RUNNING

        SessionRepository.append_output(session, "line 1")
        SessionRepository.append_output(session, "line 2")
        session.refresh_from_db()
        assert "line 1\nline 2" in session.output

        SessionRepository.complete(session)
        session.refresh_from_db()
        assert session.status == SessionStatus.COMPLETED

    def test_has_any_successful_for_chat_counts_read_from_completed(self):
        org = Organization.objects.create(name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        user = User.objects.create_user(email=f"u-{uuid.uuid4().hex[:8]}@example.com", password="x")
        runner = Runner.objects.create(
            name="runner",
            api_token_hash=hash_token("token1"),
            status=RunnerStatus.ONLINE,
            organization=org,
        )
        workspace = WorkspaceRepository.create(
            workspace_id=uuid.uuid4(),
            runner=runner,
            name="ws",
            created_by=user,
        )
        chat = Chat.objects.create(workspace=workspace, name="Chat")
        session = Session.objects.create(
            chat=chat,
            prompt="hello",
            status=SessionStatus.COMPLETED,
            read_at=timezone.now(),
        )

        assert SessionRepository.has_any_successful_for_chat(chat.id) is True

    def test_has_any_successful_for_chat_ignores_failed_read_sessions(self):
        org = Organization.objects.create(name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        user = User.objects.create_user(email=f"u-{uuid.uuid4().hex[:8]}@example.com", password="x")
        runner = Runner.objects.create(
            name="runner",
            api_token_hash=hash_token("token2"),
            status=RunnerStatus.ONLINE,
            organization=org,
        )
        workspace = WorkspaceRepository.create(
            workspace_id=uuid.uuid4(),
            runner=runner,
            name="ws",
            created_by=user,
        )
        chat = Chat.objects.create(workspace=workspace, name="Chat")
        session = Session.objects.create(
            chat=chat,
            prompt="hello",
            status=SessionStatus.FAILED,
            read_at=timezone.now(),
        )

        assert SessionRepository.has_any_successful_for_chat(chat.id) is False

    def test_mark_read_ignores_running_sessions(self, workspace):
        chat = Chat.objects.create(workspace=workspace, name="Chat")
        session = Session.objects.create(
            chat=chat,
            prompt="hello",
            status=SessionStatus.RUNNING,
        )

        SessionRepository.mark_read(session)
        session.refresh_from_db()

        assert session.read_at is None

    def test_mark_unread_clears_completed_session_read_at(self, workspace):
        chat = Chat.objects.create(workspace=workspace, name="Chat")
        session = Session.objects.create(
            chat=chat,
            prompt="hello",
            status=SessionStatus.COMPLETED,
            read_at=timezone.now(),
        )

        SessionRepository.mark_unread(session)
        session.refresh_from_db()

        assert session.read_at is None

    def test_mark_unread_ignores_running_sessions(self, workspace):
        chat = Chat.objects.create(workspace=workspace, name="Chat")
        session = Session.objects.create(
            chat=chat,
            prompt="hello",
            status=SessionStatus.RUNNING,
            read_at=timezone.now(),
        )

        SessionRepository.mark_unread(session)
        session.refresh_from_db()

        assert session.read_at is not None


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
            task_type=TaskType.RUN_PROMPT,
        )
        TaskRepository.fail(task, "Something went wrong")
        assert task.status == TaskStatus.FAILED
        assert task.error == "Something went wrong"


@pytest.mark.django_db
class TestConversationRepository:
    def test_uses_last_session_activity_as_updated_at(self):
        org = Organization.objects.create(name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        user = User.objects.create_user(email=f"u-{uuid.uuid4().hex[:8]}@example.com", password="x")
        runner = Runner.objects.create(
            name="runner",
            api_token_hash=hash_token("token-conv"),
            status=RunnerStatus.ONLINE,
            organization=org,
        )
        workspace = WorkspaceRepository.create(
            workspace_id=uuid.uuid4(),
            runner=runner,
            name="ws",
            created_by=user,
        )
        chat = Chat.objects.create(workspace=workspace, name="Chat")

        old_time = timezone.now() - timedelta(days=2)
        Chat.objects.filter(id=chat.id).update(updated_at=old_time)

        session = Session.objects.create(
            chat=chat,
            prompt="hello",
            status=SessionStatus.COMPLETED,
        )
        completed_at = timezone.now() - timedelta(minutes=5)
        Session.objects.filter(id=session.id).update(
            created_at=timezone.now() - timedelta(hours=1),
            completed_at=completed_at,
        )

        rows = ConversationRepository.list_for_user(org.id, user.id, is_admin=True)
        row = next(r for r in rows if r["chat_id"] == chat.id)
        assert row["updated_at"] == completed_at

    def test_append_output_updates_workspace_and_chat_activity_timestamps(self):
        org = Organization.objects.create(name="Org", slug=f"org-{uuid.uuid4().hex[:8]}")
        user = User.objects.create_user(email=f"u-{uuid.uuid4().hex[:8]}@example.com", password="x")
        runner = Runner.objects.create(
            name="runner",
            api_token_hash=hash_token("token-touch"),
            status=RunnerStatus.ONLINE,
            organization=org,
        )
        workspace = WorkspaceRepository.create(
            workspace_id=uuid.uuid4(),
            runner=runner,
            name="ws",
            created_by=user,
        )
        chat = Chat.objects.create(workspace=workspace, name="Chat")
        session = SessionRepository.create(
            session_id=uuid.uuid4(),
            chat=chat,
            prompt="hello",
        )

        old_time = timezone.now() - timedelta(days=1)
        Workspace.objects.filter(id=workspace.id).update(updated_at=old_time)
        Chat.objects.filter(id=chat.id).update(updated_at=old_time)

        SessionRepository.append_output(session, "line")

        workspace.refresh_from_db()
        chat.refresh_from_db()
        assert workspace.updated_at > old_time
        assert chat.updated_at > old_time
