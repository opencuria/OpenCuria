"""Scheduled chat messages: REST contract, events and delivery semantics."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

import apps.harness.api as harness_api
from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.models import HarnessScheduledMessage, HarnessSession
from apps.harness.scheduled_messages import (
    FRONTEND_EVENT_SCHEDULED_MESSAGES,
    INTERRUPTED_ERROR,
    ScheduledMessageService,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.exceptions import ConflictError
from common.utils import generate_api_token, hash_token

RUN = [APIKeyPermission.HARNESS_RUN.value]
READ = [APIKeyPermission.HARNESS_READ.value]


@pytest.fixture
def setup(db):
    """Org, owner, stranger and an owned running workspace with one chat."""
    users = get_user_model()
    org = Organization.objects.create(
        name=f"Schedule {uuid.uuid4().hex[:6]}",
        slug=f"schedule-{uuid.uuid4().hex[:10]}",
    )
    owner = users.objects.create_user(
        email=f"s-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    stranger = users.objects.create_user(
        email=f"s-stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    for user in (owner, stranger):
        Membership.objects.create(
            user=user, organization=org, role=MembershipRole.MEMBER
        )
    runner = Runner.objects.create(
        name="schedule-runner",
        api_token_hash=hash_token(f"schedule-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"schedule-{uuid.uuid4().hex[:8]}",
        organization=org,
    )
    workspace = Workspace.objects.create(
        runner=runner, name="Owned", status=WorkspaceStatus.RUNNING, created_by=owner
    )
    session = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, created_by=owner, title="Chat"
    )
    return SimpleNamespace(
        org=org, owner=owner, stranger=stranger, workspace=workspace, session=session
    )


def _client(user: Any, org: Organization, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"schedule-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))


@pytest.fixture
def events(monkeypatch):
    """Record published snapshots instead of fanning out over Socket.IO."""
    published: list[tuple[str, dict, str]] = []

    async def emit(event: str, data: dict, workspace_id: str) -> None:
        published.append((event, data, workspace_id))

    monkeypatch.setattr(
        harness_api,
        "_scheduled_messages",
        lambda: ScheduledMessageService(emit=emit),
    )
    return published


class FakeDelivery:
    """Harness facade double: records steers and starts."""

    def __init__(self, *, running: bool = False, steer_error=None, start_error=None):
        self.running = running
        self.steer_error = steer_error
        self.start_error = start_error
        self.steers: list[tuple[str, int | None]] = []
        self.starts: list[tuple[HarnessSession, str, dict]] = []

    def is_running(self, session_id: uuid.UUID) -> bool:
        return self.running

    async def steer_run(self, session, prompt, *, user_id=None):  # type: ignore[no-untyped-def]
        if self.steer_error is not None:
            error, self.steer_error = self.steer_error, None
            raise error
        self.steers.append((prompt, user_id))

    async def start_run(self, session, prompt, **kwargs):  # type: ignore[no-untyped-def]
        if self.start_error is not None:
            error, self.start_error = self.start_error, None
            raise error
        self.starts.append((session, prompt, kwargs))


def _schedule(client: Client, session: HarnessSession, **payload: Any):  # type: ignore[no-untyped-def]
    body = {
        "prompt": "Check the deploy",
        "send_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        **payload,
    }
    return client.post(
        f"/api/v1/harness/sessions/{session.id}/scheduled-messages",
        data=json.dumps(body),
        content_type="application/json",
    )


@pytest.mark.django_db(transaction=True)
def test_schedule_lists_in_timeline_reschedules_and_withdraws(setup, events):
    """The full lifecycle publishes a snapshot after every change."""
    client = _client(setup.owner, setup.org, RUN + READ)
    created = _schedule(client, setup.session, prompt="  Check the deploy  ")
    assert created.status_code == 201, created.content[:500]
    body = created.json()
    assert body["content"] == "Check the deploy"
    assert body["status"] == "pending"
    assert events[-1][0] == FRONTEND_EVENT_SCHEDULED_MESSAGES
    assert events[-1][2] == str(setup.workspace.id)
    assert [row["id"] for row in events[-1][1]["messages"]] == [body["id"]]

    timeline = client.get(f"/api/v1/harness/sessions/{setup.session.id}/timeline")
    assert timeline.status_code == 200
    assert [row["id"] for row in timeline.json()["scheduled_messages"]] == [body["id"]]

    url = f"/api/v1/harness/sessions/{setup.session.id}/scheduled-messages/{body['id']}"
    later = timezone.now() + timedelta(days=2)
    moved = client.patch(
        url,
        data=json.dumps({"send_at": later.isoformat()}),
        content_type="application/json",
    )
    assert moved.status_code == 200, moved.content[:500]
    assert moved.json()["send_at"].startswith(later.strftime("%Y-%m-%dT%H:%M"))

    withdrawn = client.delete(url)
    assert withdrawn.status_code == 200
    assert withdrawn.json()["content"] == "Check the deploy"
    assert events[-1][1]["messages"] == []
    assert client.delete(url).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_schedule_validates_text_time_target_and_limit(setup, events, monkeypatch):
    """Empty text, past or far times, subagent chats and full chats are refused."""
    client = _client(setup.owner, setup.org, RUN)
    assert _schedule(client, setup.session, prompt="   ").status_code == 400
    past = (timezone.now() - timedelta(minutes=1)).isoformat()
    assert _schedule(client, setup.session, send_at=past).status_code == 400
    far = (timezone.now() + timedelta(days=400)).isoformat()
    assert _schedule(client, setup.session, send_at=far).status_code == 400
    assert _schedule(client, setup.session, mode="ask").status_code == 400
    child = HarnessSession.objects.create(
        workspace=setup.workspace,
        organization_id=setup.org.id,
        created_by=setup.owner,
        parent=setup.session,
    )
    assert _schedule(client, child).status_code == 400

    monkeypatch.setattr("apps.harness.scheduled_messages.MAX_SCHEDULED_PER_SESSION", 1)
    assert _schedule(client, setup.session).status_code == 201
    full = _schedule(client, setup.session)
    assert full.status_code == 409
    assert full.json()["code"] == "scheduled_messages_full"


@pytest.mark.django_db(transaction=True)
def test_schedule_enforces_scope_key_permission_and_engine_owner(setup, events):
    """Strangers see nothing, read-only keys cannot write, engines stay personal."""
    stranger = _client(setup.stranger, setup.org, RUN)
    assert _schedule(stranger, setup.session).status_code == 404
    read_only = _client(setup.owner, setup.org, READ)
    assert _schedule(read_only, setup.session).status_code == 403
    external = HarnessSession.objects.create(
        workspace=setup.workspace,
        organization_id=setup.org.id,
        created_by=setup.stranger,
        harness_id="claude",
    )
    owner = _client(setup.owner, setup.org, RUN)
    assert _schedule(owner, external).status_code == 403
    assert not HarnessScheduledMessage.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("running", [False, True])
def test_send_now_starts_a_turn_or_steers_the_running_one(
    setup, events, monkeypatch, running
):
    """Send now delivers like a typed message and removes the scheduled row."""
    delivery = FakeDelivery(running=running)
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: delivery)
    client = _client(setup.owner, setup.org, RUN)
    created = _schedule(client, setup.session, prompt="Run the tests").json()

    sent = client.post(
        f"/api/v1/harness/sessions/{setup.session.id}"
        f"/scheduled-messages/{created['id']}/send"
    )

    assert sent.status_code == 202, sent.content[:500]
    if running:
        assert delivery.steers == [("Run the tests", setup.owner.id)]
        assert delivery.starts == []
    else:
        assert delivery.steers == []
        assert [
            (prompt, kwargs["user_id"]) for _, prompt, kwargs in delivery.starts
        ] == [("Run the tests", setup.owner.id)]
    assert not HarnessScheduledMessage.objects.exists()
    statuses = [[row["status"] for row in data["messages"]] for _, data, _ in events]
    assert statuses[-2:] == [["sending"], []]


@pytest.mark.django_db(transaction=True)
def test_failed_send_now_keeps_the_message_with_its_reason(setup, events, monkeypatch):
    """A refused delivery answers with the error and leaves a retryable row."""
    delivery = FakeDelivery(start_error=ConflictError("Workspace is not running"))
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: delivery)
    client = _client(setup.owner, setup.org, RUN)
    created = _schedule(client, setup.session).json()
    url = (
        f"/api/v1/harness/sessions/{setup.session.id}"
        f"/scheduled-messages/{created['id']}"
    )

    failed = client.post(f"{url}/send")

    assert failed.status_code == 409
    row = HarnessScheduledMessage.objects.get(pk=created["id"])
    assert row.status == HarnessScheduledMessage.Status.FAILED
    assert row.error == "Workspace is not running"
    assert events[-1][1]["messages"][0]["error"] == "Workspace is not running"

    # Failed messages can be retried, and retried messages are delivered once.
    assert client.post(f"{url}/send").status_code == 202
    assert len(delivery.starts) == 1
    assert client.post(f"{url}/send").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_a_message_being_sent_cannot_change(setup, events):
    """Rescheduling, withdrawing or resending a fenced message is a conflict."""
    client = _client(setup.owner, setup.org, RUN)
    created = _schedule(client, setup.session).json()
    HarnessScheduledMessage.objects.filter(pk=created["id"]).update(
        status=HarnessScheduledMessage.Status.SENDING, claimed_at=timezone.now()
    )
    url = (
        f"/api/v1/harness/sessions/{setup.session.id}"
        f"/scheduled-messages/{created['id']}"
    )
    later = (timezone.now() + timedelta(hours=3)).isoformat()
    moved = client.patch(
        url, data=json.dumps({"send_at": later}), content_type="application/json"
    )
    assert moved.status_code == 409
    assert moved.json()["code"] == "scheduled_message_sending"
    assert client.delete(url).status_code == 409
    assert client.post(f"{url}/send").status_code == 409


class FakeSettings:
    """Records run options applied before a new turn."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _record(self, name: str, session_id: uuid.UUID, value: str) -> HarnessSession:
        self.calls.append((name, value))
        return HarnessSession.objects.get(pk=session_id)

    def set_mode(self, session_id, mode):  # type: ignore[no-untyped-def]
        return self._record("mode", session_id, mode)

    def set_model(self, session_id, model):  # type: ignore[no-untyped-def]
        return self._record("model", session_id, model)

    def set_reasoning_effort(self, session_id, effort):  # type: ignore[no-untyped-def]
        return self._record("effort", session_id, effort)


def _row(setup, **fields: Any) -> HarnessScheduledMessage:  # type: ignore[no-untyped-def]
    return HarnessScheduledMessage.objects.create(
        session=setup.session,
        created_by=setup.owner,
        prompt=fields.pop("prompt", "Ship it"),
        send_at=fields.pop("send_at", timezone.now() + timedelta(hours=1)),
        **fields,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_delivery_applies_run_options_before_a_new_turn(setup):
    """An idle chat receives the message with its mode, model, effort and skills."""
    skill = str(uuid.uuid4())
    settings = FakeSettings()
    service = ScheduledMessageService(session_settings=settings)
    row = _row(
        setup, mode="plan", model="m-1", reasoning_effort="high", skill_ids=[skill]
    )
    delivery = FakeDelivery()

    await service.deliver(row, harness=delivery)

    assert settings.calls == [("mode", "plan"), ("model", "m-1"), ("effort", "high")]
    [(session, prompt, kwargs)] = delivery.starts
    assert session.id == setup.session.id
    assert prompt == "Ship it"
    assert kwargs["skill_ids"] == [skill]
    assert kwargs["organization_id"] == setup.org.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_delivery_switches_path_when_the_run_state_changes(setup):
    """A run that just ended gets a new turn; a run that just started is steered."""
    service = ScheduledMessageService(session_settings=FakeSettings())
    ended = FakeDelivery(
        running=True,
        steer_error=ConflictError("ended", code="no_active_run"),
    )
    await service.deliver(_row(setup), harness=ended)
    assert ended.steers == [] and len(ended.starts) == 1

    class StartsMeanwhile(FakeDelivery):
        async def start_run(self, session, prompt, **kwargs):  # type: ignore[no-untyped-def]
            self.running = True
            raise ConflictError("already has an active run")

    started = StartsMeanwhile()
    await service.deliver(_row(setup), harness=started)
    assert started.steers == [("Ship it", setup.owner.id)]

    refused = FakeDelivery(start_error=ConflictError("Workspace is stopping"))
    with pytest.raises(ConflictError, match="stopping"):
        await service.deliver(_row(setup), harness=refused)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stale_deliveries_fail_and_due_messages_are_claimed_once(setup):
    """Lost deliveries become retryable; a due message is fenced exactly once."""
    published: list[dict] = []

    async def emit(event: str, data: dict, workspace_id: str) -> None:
        published.append(data)

    service = ScheduledMessageService(emit=emit)
    now = timezone.now()
    stale = _row(
        setup,
        status=HarnessScheduledMessage.Status.SENDING,
        claimed_at=now - timedelta(hours=1),
    )
    due = _row(setup, send_at=now - timedelta(seconds=1))
    later = _row(setup, send_at=now + timedelta(minutes=5))

    assert await service.recover_stale(now=now) == 1
    stale.refresh_from_db()
    assert stale.status == HarnessScheduledMessage.Status.FAILED
    assert stale.error == INTERRUPTED_ERROR
    assert published and published[-1]["session_id"] == str(setup.session.id)

    assert [row.id for row in service.claim_due(now)] == [due.id]
    assert service.claim_due(now) == []
    later.refresh_from_db()
    assert later.status == HarnessScheduledMessage.Status.PENDING
