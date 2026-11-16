"""The scheduler delivers due chat messages and records every outcome."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.harness.models import HarnessScheduledMessage, HarnessSession
from apps.harness.scheduled_messages import ScheduledMessageService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.scheduled_tasks.messages import MISSED_ERROR, ScheduledMessageDispatcher
from apps.scheduled_tasks.services import ScheduledTaskScheduler, ScheduledTaskService
from common.utils import hash_token


class FakeRunner:
    """Workspace lifecycle double that resumes a stopped workspace."""

    def __init__(self, status: str = WorkspaceStatus.RUNNING, owner: bool = True):
        self.status = status
        self.owner = owner
        self.resumed: list[uuid.UUID] = []
        self.touched: list[uuid.UUID] = []
        self.workspace_lifecycle = self.workspace_access = self

    def get_workspace_launch_state(self, workspace_id):  # type: ignore[no-untyped-def]
        return self.status, None, "online", True, None

    def is_active_workspace_owner(self, user_id, workspace_id, *, organization_id):  # type: ignore[no-untyped-def]
        return self.owner

    async def resume_workspace(self, workspace_id):  # type: ignore[no-untyped-def]
        self.resumed.append(workspace_id)
        self.status = WorkspaceStatus.RUNNING

    async def touch_workspace_activity(self, workspace_id):  # type: ignore[no-untyped-def]
        self.touched.append(workspace_id)


class FakeDelivery:
    """Harness facade double recording new turns."""

    def __init__(self, start_error: Exception | None = None) -> None:
        self.start_error = start_error
        self.starts: list[str] = []

    def is_running(self, session_id):  # type: ignore[no-untyped-def]
        return False

    async def steer_run(self, session, prompt, *, user_id=None):  # type: ignore[no-untyped-def]
        raise AssertionError("idle chats are not steered")

    async def start_run(self, session, prompt, **kwargs):  # type: ignore[no-untyped-def]
        if self.start_error is not None:
            raise self.start_error
        self.starts.append(prompt)


async def _drop(event: str, data: dict, workspace_id: str) -> None:
    return None


@pytest.fixture
def chat(db):
    """A running workspace owned by an active member, with one chat."""
    org = Organization.objects.create(
        name=f"Dispatch {uuid.uuid4().hex[:6]}",
        slug=f"dispatch-{uuid.uuid4().hex[:10]}",
    )
    owner = get_user_model().objects.create_user(
        email=f"dispatch-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    runner = Runner.objects.create(
        name="dispatch-runner",
        api_token_hash=hash_token(f"dispatch-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"dispatch-{uuid.uuid4().hex[:8]}",
        organization=org,
    )
    workspace = Workspace.objects.create(
        runner=runner, name="W", status=WorkspaceStatus.RUNNING, created_by=owner
    )
    session = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, created_by=owner
    )

    def schedule(prompt: str, send_at) -> HarnessScheduledMessage:  # type: ignore[no-untyped-def]
        return HarnessScheduledMessage.objects.create(
            session=session, created_by=owner, prompt=prompt, send_at=send_at
        )

    return SimpleNamespace(workspace=workspace, session=session, schedule=schedule)


def _dispatcher(
    runner: FakeRunner, delivery: FakeDelivery
) -> ScheduledMessageDispatcher:
    tasks = ScheduledTaskService(runner=runner, harness=delivery)
    return ScheduledMessageDispatcher(
        tasks, messages=ScheduledMessageService(emit=_drop)
    )


def _rows() -> list[tuple[str, str, str]]:
    return [
        (row.prompt, row.status, row.error)
        for row in HarnessScheduledMessage.objects.order_by("send_at")
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_due_message_resumes_a_stopped_workspace_and_is_sent(chat):
    """A due message wakes its workspace, starts a turn and disappears."""
    now = timezone.now()
    await sync_to_async(chat.schedule)("Deploy", now - timedelta(seconds=5))
    await sync_to_async(chat.schedule)("Later", now + timedelta(hours=1))
    runner = FakeRunner(status=WorkspaceStatus.STOPPED)
    delivery = FakeDelivery()

    await _dispatcher(runner, delivery).dispatch_due(now=now)

    assert runner.resumed == [chat.workspace.id]
    assert delivery.starts == ["Deploy"]
    assert runner.touched == [chat.workspace.id]
    assert await sync_to_async(_rows)() == [("Later", "pending", "")]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_missed_unavailable_and_refused_messages_stay_with_a_reason(chat):
    """Nothing is sent late, into a lost workspace, or silently dropped."""
    now = timezone.now()
    await sync_to_async(chat.schedule)("Stale", now - timedelta(hours=2))
    dispatcher = _dispatcher(FakeRunner(), FakeDelivery())
    await dispatcher.dispatch_due(now=now)
    assert await sync_to_async(_rows)() == [("Stale", "failed", MISSED_ERROR)]
    await sync_to_async(HarnessScheduledMessage.objects.all().delete)()

    await sync_to_async(chat.schedule)("Orphan", now - timedelta(seconds=1))
    await _dispatcher(FakeRunner(owner=False), FakeDelivery()).dispatch_due(now=now)
    assert await sync_to_async(_rows)() == [
        ("Orphan", "failed", "Workspace is no longer available")
    ]
    await sync_to_async(HarnessScheduledMessage.objects.all().delete)()

    await sync_to_async(chat.schedule)("Refused", now - timedelta(seconds=1))
    refused = FakeDelivery(start_error=ValueError("Model is not available"))
    await _dispatcher(FakeRunner(), refused).dispatch_due(now=now)
    assert await sync_to_async(_rows)() == [
        ("Refused", "failed", "Model is not available")
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_scheduler_delivers_claimed_messages_in_supervised_tasks(chat):
    """The scheduler loop hands deliveries to background tasks it supervises."""
    await sync_to_async(chat.schedule)(
        "Background", timezone.now() - timedelta(seconds=1)
    )
    delivery = FakeDelivery()
    dispatcher = _dispatcher(FakeRunner(), delivery)
    scheduler = ScheduledTaskScheduler(service=dispatcher.tasks, messages=dispatcher)

    await dispatcher.dispatch_due(on_claim=scheduler._supervise_message)
    assert scheduler._launch_tasks
    await asyncio.gather(*scheduler._launch_tasks)

    assert delivery.starts == ["Background"]
    assert await sync_to_async(_rows)() == []
