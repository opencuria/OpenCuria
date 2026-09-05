"""Tests for Paket 3: skills, session CRUD, title agent, child sessions."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

import apps.harness.api as harness_api
from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.harness_service import HarnessService, resolve_skill_bodies
from apps.harness.models import HarnessSession
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.repositories import HarnessSessionRepository
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.skills.models import Skill
from common.utils import generate_api_token, hash_token


class FakeProvider(ProviderAdapter):
    """Immediate text answer, no network."""

    name = "fake"

    def __init__(self, text: str = "api-answer") -> None:
        self._text = text

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Yield one text delta with usage."""
        yield Delta(text=self._text, usage=Usage(1, 1, 2))


def _client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"p3-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


async def _drop_emit(event: str, data: dict) -> None:
    return None


READ = [APIKeyPermission.HARNESS_READ.value]
RUN = [APIKeyPermission.HARNESS_RUN.value]


@pytest.fixture
def paket3_setup(db):
    """Org, users, workspace, and skills for Paket 3 tests."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"P3 {uuid.uuid4().hex[:6]}",
        slug=f"p3-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"p3-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    stranger = user_model.objects.create_user(
        email=f"p3-stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="p3-runner",
        api_token_hash=hash_token(f"p3-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"p3-{uuid.uuid4().hex[:8]}",
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
    org_skill = Skill.objects.create(
        name="Org Skill",
        body="Always use type hints.",
        organization=org,
        created_by=owner,
    )
    personal_skill = Skill.objects.create(
        name="Personal",
        body="Prefer pytest.",
        user=owner,
        created_by=owner,
    )
    foreign_skill = Skill.objects.create(
        name="Foreign",
        body="secret",
        user=stranger,
        created_by=stranger,
    )
    return {
        "org": org,
        "owner": owner,
        "stranger": stranger,
        "owned": owned,
        "foreign": foreign,
        "org_skill": org_skill,
        "personal_skill": personal_skill,
        "foreign_skill": foreign_skill,
    }


@pytest.fixture
def fake_harness_service(monkeypatch):
    """Harness service with fake provider for API tests."""
    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
        provider_factory=lambda _org: FakeProvider(),
    )
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    return service


@pytest.mark.django_db
def test_resolve_skill_bodies_rejects_foreign_skill(paket3_setup):
    """Foreign skill IDs raise ValueError (surfaced as HTTP 400)."""
    with pytest.raises(ValueError, match="not accessible"):
        resolve_skill_bodies(
            [str(paket3_setup["foreign_skill"].id)],
            user_id=paket3_setup["owner"].id,
            organization_id=paket3_setup["org"].id,
        )


@pytest.mark.django_db(transaction=True)
def test_create_session_invalid_skill_is_400(paket3_setup, fake_harness_service):
    """POST with an inaccessible skill_id returns 400."""
    client = _client(user=paket3_setup["owner"], org=paket3_setup["org"], permissions=RUN)
    response = client.post(
        f"/api/v1/workspaces/{paket3_setup['owned'].id}/harness/sessions/",
        data=json.dumps(
            {
                "prompt": "hello",
                "skill_ids": [str(paket3_setup["foreign_skill"].id)],
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_create_session_returns_skill_ids_in_body(paket3_setup, fake_harness_service):
    """POST with skill_ids echoes them in the 201 response."""
    skill_id = str(paket3_setup["org_skill"].id)
    client = _client(user=paket3_setup["owner"], org=paket3_setup["org"], permissions=RUN)
    response = client.post(
        f"/api/v1/workspaces/{paket3_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hello", "skill_ids": [skill_id]}),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:500]
    body = response.json()
    assert body["skill_ids"] == [skill_id]
    assert body["parent_id"] is None


@pytest.mark.django_db(transaction=True)
def test_patch_session_title_happy(paket3_setup, fake_harness_service):
    """PATCH session title succeeds with harness:run."""
    session = fake_harness_service.create_session(
        workspace_id=paket3_setup["owned"].id,
        organization_id=paket3_setup["org"].id,
        prompt="rename me",
    )
    client = _client(user=paket3_setup["owner"], org=paket3_setup["org"], permissions=RUN)
    response = client.patch(
        f"/api/v1/harness/sessions/{session.id}",
        data=json.dumps({"title": "Renamed chat"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Renamed chat"


@pytest.mark.django_db(transaction=True)
def test_patch_session_foreign_workspace_is_404(paket3_setup, fake_harness_service):
    """Cannot rename another owner's session."""
    session = fake_harness_service.create_session(
        workspace_id=paket3_setup["foreign"].id,
        organization_id=paket3_setup["org"].id,
        prompt="foreign",
    )
    client = _client(user=paket3_setup["owner"], org=paket3_setup["org"], permissions=RUN)
    response = client.patch(
        f"/api/v1/harness/sessions/{session.id}",
        data=json.dumps({"title": "Nope"}),
        content_type="application/json",
    )
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_patch_session_needs_run_permission(paket3_setup, fake_harness_service):
    """Read-only API keys cannot rename sessions."""
    session = fake_harness_service.create_session(
        workspace_id=paket3_setup["owned"].id,
        organization_id=paket3_setup["org"].id,
        prompt="perm",
    )
    client = _client(user=paket3_setup["owner"], org=paket3_setup["org"], permissions=READ)
    response = client.patch(
        f"/api/v1/harness/sessions/{session.id}",
        data=json.dumps({"title": "Nope"}),
        content_type="application/json",
    )
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_delete_session_returns_204(paket3_setup, fake_harness_service):
    """DELETE removes the session after aborting any active run."""
    session = fake_harness_service.create_session(
        workspace_id=paket3_setup["owned"].id,
        organization_id=paket3_setup["org"].id,
        prompt="delete me",
    )
    client = _client(user=paket3_setup["owner"], org=paket3_setup["org"], permissions=RUN)
    response = client.delete(f"/api/v1/harness/sessions/{session.id}")
    assert response.status_code == 204
    assert not HarnessSession.objects.filter(id=session.id).exists()


@pytest.mark.django_db(transaction=True)
def test_delete_session_needs_run_not_read(paket3_setup, fake_harness_service):
    """harness:read alone cannot delete sessions."""
    session = fake_harness_service.create_session(
        workspace_id=paket3_setup["owned"].id,
        organization_id=paket3_setup["org"].id,
        prompt="keep",
    )
    client = _client(user=paket3_setup["owner"], org=paket3_setup["org"], permissions=READ)
    response = client.delete(f"/api/v1/harness/sessions/{session.id}")
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
async def test_title_agent_updates_session_title(harness_workspace, monkeypatch) -> None:
    """Root session title is updated asynchronously by the title agent."""
    title_provider = FakeProvider("Short Generated Title Here")
    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
        provider_factory=lambda _org: title_provider,
    )
    from apps.harness.services import ProviderConfigService

    ProviderConfigService().save_config(
        organization_id=harness_workspace.runner.organization_id,
        api_key="test-key",
        base_url="https://example.com",
        default_model="big-model",
        small_model="small-model",
    )
    prompt = "Please help me build a feature"
    from apps.harness.harness_service import _title_from_prompt

    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title=_title_from_prompt(prompt),
        agent_name="build",
        mode="build",
        model="big-model",
    )
    await service.start_run(
        session,
        prompt,
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    for _ in range(20):
        await asyncio.sleep(0.05)
        refreshed = HarnessSessionRepository.get_by_id(session.id)
        if refreshed and refreshed.title == "Short Generated Title Here":
            break
    else:
        refreshed = HarnessSessionRepository.get_by_id(session.id)
    assert refreshed is not None
    assert refreshed.title == "Short Generated Title Here"


@pytest.mark.django_db(transaction=True)
async def test_title_agent_does_not_overwrite_user_rename(harness_workspace) -> None:
    """Title generation skips when the user already renamed the session."""
    title_provider = FakeProvider("Generated Title Should Not Win")
    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
        provider_factory=lambda _org: title_provider,
    )
    from apps.harness.services import ProviderConfigService

    ProviderConfigService().save_config(
        organization_id=harness_workspace.runner.organization_id,
        api_key="test-key",
        base_url="https://example.com",
        default_model="big-model",
        small_model="small-model",
    )
    prompt = "Please help me build a feature"
    session = service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt=prompt,
    )
    service.update_title(session.id, "User Renamed Early")
    await service._generate_title(
        session_id=session.id,
        prompt=prompt,
        organization_id=harness_workspace.runner.organization_id,
    )
    refreshed = HarnessSessionRepository.get_by_id(session.id)
    assert refreshed is not None
    assert refreshed.title == "User Renamed Early"


@pytest.mark.django_db(transaction=True)
async def test_child_session_created_for_task_tool(harness_workspace) -> None:
    """Task subagent runner creates a child session with parent_id set."""
    emitted: list[dict] = []

    async def _emit(event: str, data: dict) -> None:
        emitted.append({"event": event, **data})

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_emit,
        provider_factory=lambda _org: FakeProvider("child output"),
    )
    from apps.harness.services import ProviderConfigService
    from apps.harness.tools.subagents import TaskArgs

    ProviderConfigService().save_config(
        organization_id=harness_workspace.runner.organization_id,
        api_key="test-key",
        base_url="https://example.com",
        default_model="big-model",
        small_model="small-model",
    )
    parent = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
        agent_name="build",
        mode="build",
        model="big-model",
    )
    parent_assistant = service.messages.create(
        session_id=parent.id, role="assistant", content=""
    )
    service._runs[str(parent.id)] = {
        "session_id": str(parent.id),
        "message_id": str(parent_assistant.id),
        "tool_parts": {},
        "subtask_parts": {},
    }
    ctx = type(
        "Ctx",
        (),
        {
            "session_id": str(parent.id),
            "workspace_id": str(harness_workspace.id),
            "model": "big-model",
            "depth": 0,
            "max_depth": 1,
        },
    )()
    args = TaskArgs(description="research", prompt="look around", subagent_type="general")
    result = await service._run_subagent_tool(
        parent=parent,
        args=args,
        ctx=ctx,
        subtask_id="sub-abc",
        organization_id=harness_workspace.runner.organization_id,
        small_model="small-model",
    )
    assert "child output" in result.output
    children = list(HarnessSession.objects.filter(parent_id=parent.id))
    assert len(children) == 1
    child = children[0]
    assert child.parent_id == parent.id
    started = [item for item in emitted if item.get("event") == "harness.subtask_started"]
    assert started
    assert started[0]["child_session_id"] == str(child.id)
