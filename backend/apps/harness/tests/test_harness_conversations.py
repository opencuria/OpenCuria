"""Tests for harness conversations feed and unread tracking (Paket 5)."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

import apps.harness.api as harness_api
from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage, HarnessMessageRole, HarnessSessionStatus
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.repositories import HarnessMessageRepository, HarnessSessionRepository
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


class FakeProvider(ProviderAdapter):
    """Immediate text answer, no network."""

    name = "fake"

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Yield one text delta with usage."""
        yield Delta(text="done", usage=Usage(1, 1, 2))


def _client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"conv-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


READ = [APIKeyPermission.HARNESS_READ.value]
RUN = [APIKeyPermission.HARNESS_RUN.value]


@pytest.fixture
def conv_setup(db):
    """Org, users, workspaces, and harness service for conversation tests."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Conv {uuid.uuid4().hex[:6]}",
        slug=f"conv-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"conv-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    stranger = user_model.objects.create_user(
        email=f"conv-stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="conv-runner",
        api_token_hash=hash_token(f"conv-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"conv-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    owned = Workspace.objects.create(
        runner=runner,
        name="Owned Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    foreign = Workspace.objects.create(
        runner=runner,
        name="Foreign Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=stranger,
    )
    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=lambda *_args, **_kwargs: None,
        provider_factory=lambda _org: FakeProvider(),
    )
    return {
        "org": org,
        "owner": owner,
        "stranger": stranger,
        "owned": owned,
        "foreign": foreign,
        "service": service,
    }


@pytest.fixture
def fake_harness_service(conv_setup, monkeypatch):
    """Route API calls through the conversation test service."""
    monkeypatch.setattr(
        harness_api, "_resolve_harness_service", lambda: conv_setup["service"]
    )
    return conv_setup["service"]


def _complete_assistant(session_id: uuid.UUID, *, when=None) -> None:
    """Create a completed assistant message for unread calculations."""
    message = HarnessMessageRepository.create(
        session_id=session_id,
        role=HarnessMessageRole.ASSISTANT,
        content="assistant reply",
    )
    HarnessMessageRepository.complete(message)
    if when is not None:
        HarnessMessage.objects.filter(id=message.id).update(completed_at=when)


@pytest.mark.django_db
def test_list_conversations_returns_enriched_fields(conv_setup, fake_harness_service):
    """GET /harness/conversations/ returns session + workspace metadata."""
    session = fake_harness_service.create_session(
        workspace_id=conv_setup["owned"].id,
        organization_id=conv_setup["org"].id,
        prompt="hello harness",
    )
    HarnessSessionRepository.mark_status(session, HarnessSessionStatus.IDLE)
    client = _client(user=conv_setup["owner"], org=conv_setup["org"], permissions=READ)
    response = client.get("/api/v1/harness/conversations/")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    row = body[0]
    assert row["session_id"] == str(session.id)
    assert row["workspace_id"] == str(conv_setup["owned"].id)
    assert row["workspace_name"] == "Owned Workspace"
    assert row["title"]
    assert row["status"] == HarnessSessionStatus.IDLE
    assert row["mode"] == "build"
    assert row["agent_name"] == "build"
    assert row["unread"] is False
    assert "updated_at" in row


@pytest.mark.django_db
def test_list_conversations_scoped_to_owned_workspaces(conv_setup, fake_harness_service):
    """Foreign-owner sessions are excluded from the caller's feed."""
    fake_harness_service.create_session(
        workspace_id=conv_setup["owned"].id,
        organization_id=conv_setup["org"].id,
        prompt="mine",
    )
    fake_harness_service.create_session(
        workspace_id=conv_setup["foreign"].id,
        organization_id=conv_setup["org"].id,
        prompt="theirs",
    )
    client = _client(user=conv_setup["owner"], org=conv_setup["org"], permissions=READ)
    response = client.get("/api/v1/harness/conversations/")
    assert response.status_code == 200
    workspace_ids = {row["workspace_id"] for row in response.json()}
    assert workspace_ids == {str(conv_setup["owned"].id)}


@pytest.mark.django_db
def test_list_conversations_requires_harness_read(conv_setup, fake_harness_service):
    """Missing harness:read returns 403."""
    fake_harness_service.create_session(
        workspace_id=conv_setup["owned"].id,
        organization_id=conv_setup["org"].id,
        prompt="perm",
    )
    client = _client(user=conv_setup["owner"], org=conv_setup["org"], permissions=RUN)
    response = client.get("/api/v1/harness/conversations/")
    assert response.status_code == 403


@pytest.mark.django_db
def test_unread_when_idle_with_completed_assistant(conv_setup, fake_harness_service):
    """Idle sessions with assistant work and no last_read_at are unread."""
    session = fake_harness_service.create_session(
        workspace_id=conv_setup["owned"].id,
        organization_id=conv_setup["org"].id,
        prompt="work done",
    )
    HarnessSessionRepository.mark_status(session, HarnessSessionStatus.IDLE)
    _complete_assistant(session.id)
    client = _client(user=conv_setup["owner"], org=conv_setup["org"], permissions=READ)
    response = client.get("/api/v1/harness/conversations/")
    assert response.status_code == 200
    assert response.json()[0]["unread"] is True


@pytest.mark.django_db
def test_unread_false_when_busy(conv_setup, fake_harness_service):
    """Busy sessions are never unread in the dashboard feed."""
    session = fake_harness_service.create_session(
        workspace_id=conv_setup["owned"].id,
        organization_id=conv_setup["org"].id,
        prompt="running",
    )
    HarnessSessionRepository.mark_status(session, HarnessSessionStatus.BUSY)
    _complete_assistant(session.id)
    client = _client(user=conv_setup["owner"], org=conv_setup["org"], permissions=READ)
    response = client.get("/api/v1/harness/conversations/")
    assert response.json()[0]["unread"] is False


@pytest.mark.django_db
def test_unread_false_after_mark_read(conv_setup, fake_harness_service):
    """Mark-read clears unread until new assistant work arrives."""
    session = fake_harness_service.create_session(
        workspace_id=conv_setup["owned"].id,
        organization_id=conv_setup["org"].id,
        prompt="read me",
    )
    HarnessSessionRepository.mark_status(session, HarnessSessionStatus.IDLE)
    completed_at = timezone.now()
    _complete_assistant(session.id, when=completed_at)
    client = _client(user=conv_setup["owner"], org=conv_setup["org"], permissions=READ)
    assert client.get("/api/v1/harness/conversations/").json()[0]["unread"] is True

    mark = client.post(f"/api/v1/harness/sessions/{session.id}/read")
    assert mark.status_code == 204
    assert client.get("/api/v1/harness/conversations/").json()[0]["unread"] is False

    later = completed_at + timedelta(seconds=5)
    _complete_assistant(session.id, when=later)
    assert client.get("/api/v1/harness/conversations/").json()[0]["unread"] is True


@pytest.mark.django_db
def test_mark_read_requires_harness_read(conv_setup, fake_harness_service):
    """Run-only keys cannot mark sessions read."""
    session = fake_harness_service.create_session(
        workspace_id=conv_setup["owned"].id,
        organization_id=conv_setup["org"].id,
        prompt="perm",
    )
    client = _client(user=conv_setup["owner"], org=conv_setup["org"], permissions=RUN)
    response = client.post(f"/api/v1/harness/sessions/{session.id}/read")
    assert response.status_code == 403


@pytest.mark.django_db
def test_mark_read_foreign_session_is_404(conv_setup, fake_harness_service):
    """Cannot mark another owner's session as read."""
    session = fake_harness_service.create_session(
        workspace_id=conv_setup["foreign"].id,
        organization_id=conv_setup["org"].id,
        prompt="foreign",
    )
    client = _client(user=conv_setup["owner"], org=conv_setup["org"], permissions=READ)
    response = client.post(f"/api/v1/harness/sessions/{session.id}/read")
    assert response.status_code == 404
