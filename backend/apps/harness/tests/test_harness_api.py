"""Tests for M6 harness REST contracts: auth, scope, key permissions."""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

import apps.harness.api as harness_api
from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessSession
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


@pytest.fixture
def harness_setup(db):
    """Org + users + runner + workspaces for harness API tests."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Harness API {uuid.uuid4().hex[:6]}",
        slug=f"harness-api-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"h-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    stranger = user_model.objects.create_user(
        email=f"h-stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="harness-api-runner",
        api_token_hash=hash_token(f"harness-api-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"harness-api-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    owned = Workspace.objects.create(
        runner=runner,
        name="Owned",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    foreign = Workspace.objects.create(
        runner=runner,
        name="Foreign",
        status=WorkspaceStatus.RUNNING,
        created_by=stranger,
    )
    return {
        "org": org,
        "owner": owner,
        "stranger": stranger,
        "runner": runner,
        "owned": owned,
        "foreign": foreign,
    }


class FakeProvider(ProviderAdapter):
    """Immediate text answer, no network."""

    name = "fake"

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Yield one text delta with usage."""
        yield Delta(text="api-answer", usage=Usage(1, 1, 2))


def _client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"harness-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


@pytest.fixture
def fake_harness_service(monkeypatch):
    """Route API calls through a service with a fake provider."""
    fake = FakeProvider()
    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
        provider_factory=lambda _org: fake,
    )
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    # Keep the runners owner-scoping real (no monkeypatch of _get_service).
    return service


async def _drop_emit(event: str, data: dict) -> None:
    """Drop frontend emits in API tests."""
    return None


READ = [APIKeyPermission.HARNESS_READ.value]
RUN = [APIKeyPermission.HARNESS_RUN.value]
PERMS = [APIKeyPermission.HARNESS_PERMISSIONS.value]


@pytest.mark.django_db(transaction=True)
def test_create_session_starts_run(harness_setup, fake_harness_service):
    """POST sessions creates a session and runs the fake provider."""
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hello", "agent_name": "build", "mode": "build"}),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:500]
    body = response.json()
    assert body["status"] in ("busy", "idle")
    assert HarnessSession.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_create_session_invalid_mode_is_400(harness_setup, fake_harness_service):
    """Unknown mode/agent is a validation error, not a 500."""
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hi", "mode": "turbo"}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_foreign_workspace_is_404(harness_setup, fake_harness_service):
    """Owner scoping: another user's workspace reads as not found."""
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['foreign'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hi"}),
        content_type="application/json",
    )
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_missing_key_permission_is_403(harness_setup, fake_harness_service):
    """A key without harness:run cannot create sessions."""
    client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=[APIKeyPermission.WORKSPACES_READ.value],
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hi"}),
        content_type="application/json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db(transaction=True)
def test_unauthenticated_is_401(harness_setup, fake_harness_service):
    """No credentials yields 401 on harness endpoints."""
    client = Client(HTTP_X_ORGANIZATION_ID=str(harness_setup["org"].id))
    response = client.get(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/"
    )
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_parts_todos_abort_flow(harness_setup, fake_harness_service):
    """Messages/parts, todos and abort endpoints honor read/run perms."""
    run_client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    created = run_client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "flow"}),
        content_type="application/json",
    )
    assert created.status_code == 201
    session_id = created.json()["id"]

    read_client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=READ
    )
    parts = read_client.get(f"/api/v1/harness/sessions/{session_id}/parts")
    assert parts.status_code == 200
    assert "messages" in parts.json()

    todos = read_client.get(f"/api/v1/harness/sessions/{session_id}/todos")
    assert todos.status_code == 200
    assert todos.json() == []

    abort = run_client.post(f"/api/v1/harness/sessions/{session_id}/abort")
    assert abort.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_double_message_is_409_while_busy(harness_setup, monkeypatch):
    """A second prompt while busy is rejected with 409 (no provider)."""
    service = HarnessService(emit=_drop_emit)
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    session = service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="first",
    )
    # Fake a busy task without running the loop.
    import asyncio as _asyncio

    async def _never() -> None:
        await _asyncio.Event().wait()

    loop = _asyncio.new_event_loop()
    task = loop.create_task(_never())
    service._tasks[str(session.id)] = task
    try:
        client = _client(
            user=harness_setup["owner"],
            org=harness_setup["org"],
            permissions=RUN,
        )
        response = client.post(
            f"/api/v1/harness/sessions/{session.id}/message",
            data=json.dumps({"prompt": "second"}),
            content_type="application/json",
        )
        assert response.status_code == 409
    finally:
        task.cancel()
        loop.close()


@pytest.mark.django_db(transaction=True)
def test_permission_resolve_via_api(harness_setup, fake_harness_service):
    """POST .../permissions/{pid} resolves once via the M3 service."""
    run_client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=RUN + PERMS + READ,
    )
    created = run_client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "perm-flow"}),
        content_type="application/json",
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    session = HarnessSession.objects.get(id=session_id)
    request = fake_harness_service.permissions.requests.create(
        organization_id=harness_setup["org"].id,
        session_id=session.id,
        workspace_id=harness_setup["owned"].id,
        tool="bash",
        pattern="rm -rf /tmp/x",
        title="$ rm -rf /tmp/x",
    )
    response = run_client.post(
        f"/api/v1/harness/sessions/{session_id}/permissions/{request.id}",
        data=json.dumps({"response": "once"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content[:500]
    assert response.json()["decision"] == "allow"


@pytest.mark.django_db(transaction=True)
def test_permission_resolve_needs_permission_key(harness_setup, fake_harness_service):
    """Resolving without harness:permissions is 403."""
    run_client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    created = run_client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "perm-403"}),
        content_type="application/json",
    )
    session_id = created.json()["id"]
    session = HarnessSession.objects.get(id=session_id)
    request = fake_harness_service.permissions.requests.create(
        organization_id=harness_setup["org"].id,
        session_id=session.id,
        workspace_id=harness_setup["owned"].id,
        tool="bash",
        pattern="ls",
    )
    response = run_client.post(
        f"/api/v1/harness/sessions/{session_id}/permissions/{request.id}",
        data=json.dumps({"response": "once"}),
        content_type="application/json",
    )
    assert response.status_code == 403
