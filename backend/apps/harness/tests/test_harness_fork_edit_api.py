"""REST contract tests for fork and edit (201/202 + 400/403/404/409)."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from django.test import Client

import apps.harness.api as harness_api
from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.harness_service import HarnessService
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


@pytest.fixture
def fork_edit_setup(db):
    """Org + owner/stranger + runner + owned/foreign workspaces."""
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Fork API {uuid.uuid4().hex[:6]}",
        slug=f"fork-api-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"f-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    stranger = user_model.objects.create_user(
        email=f"f-stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="fork-api-runner",
        api_token_hash=hash_token(f"fork-api-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"fork-api-{uuid.uuid4().hex[:8]}",
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


async def _drop_emit(event: str, data: dict) -> None:
    """Drop frontend emits in API tests."""
    return None


@pytest.fixture
def fake_fork_service(monkeypatch):
    """Route fork/edit API calls through a fake-provider service."""
    fake = FakeProvider()
    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
        provider_factory=lambda _org: fake,
    )
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    return service


def _client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"fork-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


RUN = [APIKeyPermission.HARNESS_RUN.value]
READ = [APIKeyPermission.HARNESS_READ.value]


def _seed_two_turns(service, setup) -> object:  # type: ignore[no-untyped-def]
    """Create one session with two persisted user/assistant turns (sync)."""
    session = service.create_session(
        workspace_id=setup["owned"].id,
        organization_id=setup["org"].id,
        prompt="first",
    )
    first_user = HarnessMessageRepository.create(
        session_id=session.id, role="user", content="first"
    )
    HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="answer one"
    )
    second_user = HarnessMessageRepository.create(
        session_id=session.id, role="user", content="second"
    )
    HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="answer two"
    )
    session.first_user = first_user  # type: ignore[attr-defined]
    session.second_user = second_user  # type: ignore[attr-defined]
    return session


def _seed_one_turn(service, setup) -> object:  # type: ignore[no-untyped-def]
    """Create one session with a single persisted user turn (sync)."""
    session = service.create_session(
        workspace_id=setup["owned"].id,
        organization_id=setup["org"].id,
        prompt="first",
    )
    user_msg = HarnessMessageRepository.create(
        session_id=session.id, role="user", content="first"
    )
    session.only_user = user_msg  # type: ignore[attr-defined]
    return session


# -- fork ------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_fork_returns_201_and_prefix_only(fork_edit_setup, fake_fork_service):
    """POST fork copies messages before the cutoff (201, no run started)."""
    session = _seed_two_turns(fake_fork_service, fork_edit_setup)
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/fork",
        data=json.dumps({"message_id": str(session.second_user.id)}),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:500]
    body = response.json()
    assert body["title"] == f"{session.title} (fork #1)"
    assert body["parent_id"] is None

    read_client = _client(
        user=fork_edit_setup["owner"],
        org=fork_edit_setup["org"],
        permissions=READ,
    )
    parts = read_client.get(f"/api/v1/harness/sessions/{body['id']}/parts")
    assert parts.status_code == 200, parts.content[:500]
    contents = [m["content"] for m in parts.json()["messages"]]
    assert contents == ["first", "answer one"]
    roles = [m["role"] for m in parts.json()["messages"]]
    assert roles == ["user", "assistant"]


@pytest.mark.django_db(transaction=True)
def test_fork_title_increments(fork_edit_setup, fake_fork_service):
    """Forking a fork bumps the ``(fork #N)`` suffix."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    first = client.post(
        f"/api/v1/harness/sessions/{session.id}/fork",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert first.status_code == 201, first.content[:500]
    assert first.json()["title"].endswith("(fork #1)")
    second = client.post(
        f"/api/v1/harness/sessions/{first.json()['id']}/fork",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert second.status_code == 201, second.content[:500]
    assert second.json()["title"].endswith("(fork #2)")


@pytest.mark.django_db(transaction=True)
def test_fork_full_copy_without_message_id(fork_edit_setup, fake_fork_service):
    """Omitting message_id copies the full history."""
    session = _seed_two_turns(fake_fork_service, fork_edit_setup)
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/fork",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:500]
    read_client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=READ
    )
    parts = read_client.get(f"/api/v1/harness/sessions/{response.json()['id']}/parts")
    assert parts.status_code == 200
    assert len(parts.json()["messages"]) == 4


@pytest.mark.django_db(transaction=True)
def test_fork_works_while_busy(fork_edit_setup, fake_fork_service, monkeypatch):
    """Fork has no busy guard: 201 even with an active run (OpenCode parity)."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    loop = asyncio.new_event_loop()

    async def _never() -> None:
        await asyncio.Event().wait()

    task = loop.create_task(_never())
    fake_fork_service._tasks[str(session.id)] = task
    try:
        client = _client(
            user=fork_edit_setup["owner"],
            org=fork_edit_setup["org"],
            permissions=RUN,
        )
        response = client.post(
            f"/api/v1/harness/sessions/{session.id}/fork",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert response.status_code == 201, response.content[:500]
    finally:
        task.cancel()
        loop.close()


@pytest.mark.django_db(transaction=True)
def test_fork_child_session_is_400(fork_edit_setup, fake_fork_service):
    """Forking a subagent child session is a validation error."""
    parent = _seed_one_turn(fake_fork_service, fork_edit_setup)
    child = fake_fork_service.create_session(
        workspace_id=fork_edit_setup["owned"].id,
        organization_id=fork_edit_setup["org"].id,
        prompt="child",
        parent_id=parent.id,
    )
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{child.id}/fork",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "subagent" in response.json()["detail"].lower()


@pytest.mark.django_db(transaction=True)
def test_fork_unknown_message_id_is_400(fork_edit_setup, fake_fork_service):
    """A cutoff message from another session is a validation error."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    other = _seed_one_turn(fake_fork_service, fork_edit_setup)
    orphan = HarnessMessageRepository.create(
        session_id=other.id, role="user", content="elsewhere"
    )
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/fork",
        data=json.dumps({"message_id": str(orphan.id)}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_fork_foreign_workspace_is_404(fork_edit_setup, fake_fork_service):
    """Owner scoping: forking another user's session reads as not found."""
    foreign_session = fake_fork_service.create_session(
        workspace_id=fork_edit_setup["foreign"].id,
        organization_id=fork_edit_setup["org"].id,
        prompt="foreign",
    )
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{foreign_session.id}/fork",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_fork_needs_run_permission(fork_edit_setup, fake_fork_service):
    """A read-only key cannot fork (403)."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=READ
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/fork",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


# -- edit ------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_edit_returns_202_and_reruns(fork_edit_setup, fake_fork_service):
    """POST edit truncates the suffix and returns the busy session (202)."""
    session = _seed_two_turns(fake_fork_service, fork_edit_setup)
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/messages/{session.first_user.id}/edit",
        data=json.dumps({"prompt": "first edited"}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content[:500]
    body = response.json()
    assert body["id"] == str(session.id)
    assert body["status"] in ("busy", "idle")

    stored = HarnessMessageRepository.list_for_session(session.id)
    user_contents = [m.content for m in stored if m.role == "user"]
    assert user_contents[0] == "first edited"
    assert "second" not in user_contents


@pytest.mark.django_db(transaction=True)
def test_edit_while_busy_is_409(fork_edit_setup, monkeypatch):
    """Editing a session with an active run is rejected with 409."""
    service = HarnessService(emit=_drop_emit)
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    session = service.create_session(
        workspace_id=fork_edit_setup["owned"].id,
        organization_id=fork_edit_setup["org"].id,
        prompt="first",
    )
    user_msg = HarnessMessageRepository.create(
        session_id=session.id, role="user", content="first"
    )
    loop = asyncio.new_event_loop()

    async def _never() -> None:
        await asyncio.Event().wait()

    task = loop.create_task(_never())
    service._tasks[str(session.id)] = task
    try:
        client = _client(
            user=fork_edit_setup["owner"],
            org=fork_edit_setup["org"],
            permissions=RUN,
        )
        response = client.post(
            f"/api/v1/harness/sessions/{session.id}/messages/{user_msg.id}/edit",
            data=json.dumps({"prompt": "edited"}),
            content_type="application/json",
        )
        assert response.status_code == 409
    finally:
        task.cancel()
        loop.close()


@pytest.mark.django_db(transaction=True)
def test_edit_assistant_message_is_400(fork_edit_setup, fake_fork_service):
    """Assistant message ids cannot be edited (400)."""
    session = _seed_two_turns(fake_fork_service, fork_edit_setup)
    assistant = next(
        m
        for m in HarnessMessageRepository.list_for_session(session.id)
        if m.role == "assistant"
    )
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/messages/{assistant.id}/edit",
        data=json.dumps({"prompt": "edited"}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_edit_empty_prompt_is_400(fork_edit_setup, fake_fork_service):
    """Blank edit prompts are a validation error (nothing truncated)."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/messages/{session.only_user.id}/edit",
        data=json.dumps({"prompt": "   "}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert len(HarnessMessageRepository.list_for_session(session.id)) == 1


@pytest.mark.django_db(transaction=True)
def test_edit_unknown_message_is_404(fork_edit_setup, fake_fork_service):
    """A message id outside the session reads as not found."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/messages/{uuid.uuid4()}/edit",
        data=json.dumps({"prompt": "edited"}),
        content_type="application/json",
    )
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_edit_child_session_is_400(fork_edit_setup, fake_fork_service):
    """Users cannot edit messages inside a subagent child session."""
    parent = _seed_one_turn(fake_fork_service, fork_edit_setup)
    child = fake_fork_service.create_session(
        workspace_id=fork_edit_setup["owned"].id,
        organization_id=fork_edit_setup["org"].id,
        prompt="child",
        parent_id=parent.id,
    )
    child_msg = HarnessMessageRepository.create(
        session_id=child.id, role="user", content="child prompt"
    )
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{child.id}/messages/{child_msg.id}/edit",
        data=json.dumps({"prompt": "edited"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "subagent" in response.json()["detail"].lower()


@pytest.mark.django_db(transaction=True)
def test_edit_foreign_workspace_is_404(fork_edit_setup, fake_fork_service):
    """Owner scoping: editing another user's session reads as not found."""
    foreign_session = fake_fork_service.create_session(
        workspace_id=fork_edit_setup["foreign"].id,
        organization_id=fork_edit_setup["org"].id,
        prompt="foreign",
    )
    foreign_msg = HarnessMessageRepository.create(
        session_id=foreign_session.id, role="user", content="foreign prompt"
    )
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{foreign_session.id}/messages/{foreign_msg.id}/edit",
        data=json.dumps({"prompt": "edited"}),
        content_type="application/json",
    )
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_edit_needs_run_permission(fork_edit_setup, fake_fork_service):
    """A read-only key cannot edit messages (403)."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=READ
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/messages/{session.only_user.id}/edit",
        data=json.dumps({"prompt": "edited"}),
        content_type="application/json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db(transaction=True)
def test_edit_invalid_mode_is_400(fork_edit_setup, fake_fork_service):
    """Unknown mode overrides are a validation error, not a 500."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/messages/{session.only_user.id}/edit",
        data=json.dumps({"prompt": "edited", "mode": "turbo"}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_edit_applies_mode_override(fork_edit_setup, fake_fork_service):
    """Explicit mode overrides persist before the rerun (plan->agent plan)."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    assert HarnessSessionRepository.get_by_id(session.id).mode == "build"
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/messages/{session.only_user.id}/edit",
        data=json.dumps({"prompt": "edited", "mode": "plan"}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content[:500]
    assert response.json()["mode"] == "plan"
    assert response.json()["agent_name"] == "plan"


@pytest.mark.django_db(transaction=True)
def test_edit_preserves_parts_before_cutoff(fork_edit_setup, fake_fork_service):
    """Parts of the surviving prefix remain attached after an edit."""
    session = _seed_one_turn(fake_fork_service, fork_edit_setup)
    first_user = session.only_user
    assistant = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="answer"
    )
    part = HarnessPartRepository.create(
        message_id=assistant.id,
        type="text",
        state="completed",
        output="answer",
    )
    HarnessPartRepository.mark_state(part, "completed", output="answer")
    second_user = HarnessMessageRepository.create(
        session_id=session.id, role="user", content="second"
    )
    client = _client(
        user=fork_edit_setup["owner"], org=fork_edit_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/messages/{second_user.id}/edit",
        data=json.dumps({"prompt": "second edited"}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content[:500]
    remaining_parts = HarnessPartRepository.list_for_session(session.id)
    assert any(p.output == "answer" for p in remaining_parts)
    assert first_user.id is not None
