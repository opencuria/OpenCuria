"""MCP lifecycle contracts for Claude-backed harness sessions."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.accounts.models import APIKeyPermission
from apps.credentials.models import CredentialService
from apps.credentials.repositories import OrgCredentialServiceActivationRepository
from apps.harness.engines.connections import EngineConnectionService
from apps.harness.engines.repositories import HarnessEngineRepository
from apps.harness.engines.run_repository import HarnessRunRepository
from apps.harness.engines.runs import HarnessRunService, RunOwnership
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage, HarnessSession
from apps.harness.providers.base import Usage
from apps.harness.repositories import HarnessMessageRepository
from apps.harness.tests.conftest import FakeAccessor
from apps.mcp_app import server as mcp_server
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_create_harness_session,
    _call_get_harness_timeline,
    _call_list_harness_conversations,
    _call_list_harness_sessions,
    _call_resolve_harness_permission,
    _call_resolve_harness_question,
    _call_resolve_harness_permission,
    _call_resolve_harness_question,
    _call_send_harness_message,
    _error,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import hash_token


class _FakeClaudeEngine:
    def __init__(self, kwargs: dict[str, Any], registry: _FakeRegistry) -> None:
        self.kwargs = kwargs
        self.registry = registry

    async def run(self, prompt, agent, model, mode, opts):  # type: ignore[no-untyped-def]
        self.registry.prompts.append(prompt)
        lease = {"lease_id": str(uuid.uuid4()), "epoch": "mcp-test-epoch"}
        external_id = str(uuid.uuid4())
        answer = f"fake MCP Claude answer: {prompt}"
        try:
            await self.kwargs["on_owner"](lease, "reserved")
            await self.kwargs["on_binding"](
                external_id,
                {"harness_id": "claude", "model": model, "mode": mode},
            )
            await self.kwargs["emit"](
                {"type": "part_updated", "step": 1, "delta": {"text": answer}}
            )
            from apps.harness.runner import RunResult

            return RunResult(
                output=answer,
                steps=1,
                usage=Usage(2, 2, 4, 0.01),
                cost=0.01,
                finish_reason="stop",
                metadata={"harness_id": "claude", "external_session_id": external_id},
            )
        finally:
            await self.kwargs["on_owner"](lease, "released")


class _CompletedRunHarnessService(HarnessService):
    """Await injected engine tasks so create/send contracts are deterministic."""

    async def start_run(self, session, prompt, **kwargs):  # type: ignore[no-untyped-def]
        assistant = await super().start_run(session, prompt, **kwargs)
        task = self._tasks.get(str(session.id))
        if task is not None:
            await task
        return assistant


class _FakeRegistry:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.prompts: list[str] = []

    def create(self, harness_id: str, **kwargs: Any) -> _FakeClaudeEngine:
        self.created.append((harness_id, kwargs))
        return _FakeClaudeEngine(kwargs, self)


async def _noop_emit(_event: str, _data: dict[str, Any]) -> None:
    return None


async def _accessor(workspace_id: str) -> FakeAccessor:
    return FakeAccessor(workspace_id)


def _json(result):
    assert result and not result[0].text.startswith("Error:"), (
        result[0].text if result else ""
    )
    return json.loads(result[0].text)


@pytest.fixture
def claude_mcp_setup(db, monkeypatch):
    org = Organization.objects.create(
        name=f"MCP Claude Session {uuid.uuid4().hex[:8]}",
        slug=f"mcp-claude-session-{uuid.uuid4().hex[:10]}",
    )
    owner = get_user_model().objects.create_user(
        email=f"mcp-claude-session-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    other = get_user_model().objects.create_user(
        email=f"mcp-claude-session-other-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(user=other, organization=org, role=MembershipRole.MEMBER)
    runner = Runner.objects.create(
        name="mcp-claude-session-runner",
        api_token_hash=hash_token(f"mcp-claude-session-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"mcp-claude-session-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="MCP Claude Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    credential_services = [
        CredentialService.objects.get_or_create(
            slug="claude-agent-api-token",
            organization=None,
            defaults={
                "name": "Claude Agent API Token",
                "credential_type": "env",
                "env_var_name": "ANTHROPIC_API_KEY",
                "label": "Claude API Token",
            },
        )[0],
        CredentialService.objects.get_or_create(
            slug="claude-agent-subscription-token",
            organization=None,
            defaults={
                "name": "Claude Subscription Token",
                "credential_type": "env",
                "env_var_name": "CLAUDE_CODE_OAUTH_TOKEN",
                "label": "Claude Subscription Token",
            },
        )[0],
    ]
    OrgCredentialServiceActivationRepository.ensure_activated(
        org.id, [service.id for service in credential_services]
    )
    connections = EngineConnectionService()
    connection = connections.save_connection(
        organization_id=org.id,
        user=owner,
        auth_type="api_token",
        token="mcp-claude-secret-never-return",
        label="MCP Claude",
    )
    foreign_connection = connections.save_connection(
        organization_id=org.id,
        user=other,
        auth_type="api_token",
        token="another-users-mcp-secret",
        label="Other Claude",
    )
    registry = _FakeRegistry()

    def bind_fake_session(
        run_id, owner_token, session_id, *, external_session_id, engine_state
    ):
        attempt = HarnessRunRepository.get_by_id(run_id)
        if (
            attempt is None
            or attempt.owner_token != owner_token
            or attempt.session_id != session_id
            or not HarnessRunRepository.set_external_session_if_owner(
                run_id, owner_token, external_session_id=external_session_id
            )
        ):
            return False
        HarnessEngineRepository.set_session_engine_state(
            session_id,
            external_session_id=external_session_id,
            engine_state=engine_state,
        )
        return True

    monkeypatch.setattr(
        HarnessEngineRepository,
        "bind_external_session_if_owner",
        staticmethod(bind_fake_session),
        raising=False,
    )
    def finalize_fake_turn(
        run_id,
        owner_token,
        *,
        status,
        finish,
        error="",
        assistant_content=None,
        engine_meta=None,
        session_usage=None,
        session_cost=0.0,
        tail_remainder="",
    ):
        attempt = HarnessRunRepository.get_by_id(run_id)
        if attempt is None or attempt.owner_token != owner_token:
            return False
        completed = HarnessRunRepository.complete_if_owner(
            run_id,
            owner_token,
            status=status,
            error=error,
            cleanup_confirmed=True,
        )
        if not completed:
            return False
        assistant = HarnessMessageRepository.model.objects.get(
            id=attempt.assistant_message_id
        )
        updates = {"finish": finish, "error": error}
        if assistant_content is not None:
            updates["content"] = assistant_content
        if engine_meta is not None:
            updates["engine_meta"] = engine_meta
        for field, value in updates.items():
            setattr(assistant, field, value)
        assistant.save(update_fields=list(updates))
        if tail_remainder:
            HarnessMessageRepository.append_content(assistant, tail_remainder)
        return True

    async def finalize_fake_ownership(
        self,
        *,
        status,
        finish,
        error="",
        assistant_content=None,
        engine_meta=None,
        session_usage=None,
        session_cost=0.0,
        tail_remainder="",
    ):
        return finalize_fake_turn(
            self.run_id,
            self.owner_token,
            status=status,
            finish=finish,
            error=error,
            assistant_content=assistant_content,
            engine_meta=engine_meta,
            session_usage=session_usage,
            session_cost=session_cost,
            tail_remainder=tail_remainder,
        )

    monkeypatch.setattr(RunOwnership, "finalize_turn", finalize_fake_ownership)
    monkeypatch.setattr(
        RunOwnership,
        "finalize_error",
        lambda self, **kwargs: None,
        raising=False,
    )
    service = _CompletedRunHarnessService(
        emit=_noop_emit,
        accessor_factory=_accessor,
        engine_registry=registry,
        engine_connections=connections,
        run_service=HarnessRunService(),
    )
    api_key = SimpleNamespace(
        user=owner,
        has_permission=lambda permission: (
            permission in (APIKeyPermission.HARNESS_RUN, APIKeyPermission.HARNESS_READ)
        ),
    )
    monkeypatch.setattr(mcp_server, "_get_harness_service", lambda: service)
    monkeypatch.setattr(
        mcp_server,
        "_runner_service",
        lambda: SimpleNamespace(
            list_workspaces=lambda *, organization_id, user: [workspace]
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "_get_owned_workspace_or_error",
        lambda _key, _org, workspace_id: (
            (workspace, None)
            if str(workspace_id) == str(workspace.id)
            else (None, _error("Workspace not found"))
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "_owned_harness_session_or_error",
        lambda _key, _org, session_id: _resolve_owned_session(service, session_id),
    )
    return {
        "org": org,
        "owner": owner,
        "other": other,
        "workspace": workspace,
        "connection": connection,
        "foreign_connection": foreign_connection,
        "service": service,
        "registry": registry,
        "api_key": api_key,
    }


def _resolve_owned_session(service, session_id):
    try:
        return service.get_session(uuid.UUID(str(session_id))), None
    except Exception:
        return None, _error("Harness session not found")


async def _wait_for_messages(
    service: HarnessService, session_id: uuid.UUID
) -> list[HarnessMessage]:
    async def read_messages():
        return await sync_to_async(HarnessMessageRepository.list_for_session)(
            session_id
        )

    for _ in range(1000):
        messages = await read_messages()
        assistants = [message for message in messages if message.role == "assistant"]
        if (
            assistants
            and len(assistants) == len([m for m in messages if m.role == "user"])
            and all(message.completed_at is not None for message in assistants)
        ):
            task = service._tasks.get(str(session_id))
            if task is not None and not task.done():
                await task
            return await read_messages()
        task = service._tasks.get(str(session_id))
        if task is not None and task.done():
            await task
        await asyncio.sleep(0.01)
    pytest.fail("Injected MCP Claude run did not finish within 10 seconds")


def test_mcp_session_tools_publish_engine_and_connection_selector():
    tools = {tool.name: tool for tool in _TOOLS}
    create_schema = tools["create_harness_session"].inputSchema
    assert create_schema["properties"]["harness_id"]["enum"] == ["native", "claude"]
    assert create_schema["properties"]["connection_id"]["format"] == "uuid"
    assert _TOOL_PERMISSIONS["create_harness_session"] == APIKeyPermission.HARNESS_RUN
    assert _TOOL_PERMISSIONS["send_harness_message"] == APIKeyPermission.HARNESS_RUN
    assert _TOOL_HANDLERS["create_harness_session"] is _call_create_harness_session
    assert _TOOL_HANDLERS["send_harness_message"] is _call_send_harness_message


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_mcp_claude_create_followup_list_and_timeline(claude_mcp_setup):
    data = claude_mcp_setup
    service = data["service"]
    secret = "mcp-claude-secret-never-return"
    created = _json(
        await _call_create_harness_session(
            data["api_key"],
            data["org"].id,
            {
                "workspace_id": str(data["workspace"].id),
                "prompt": "first MCP turn",
                "harness_id": "claude",
                "connection_id": str(data["connection"].id),
            },
        )
    )
    session_id = uuid.UUID(created["id"])
    assert created["harness_id"] == "claude"
    assert "connection_id" not in created
    assert secret not in json.dumps(created)
    await _wait_for_messages(service, session_id)

    listed = _json(
        _call_list_harness_sessions(
            data["api_key"],
            data["org"].id,
            {"workspace_id": str(data["workspace"].id)},
        )
    )
    assert (
        next(row for row in listed if row["id"] == str(session_id))["harness_id"]
        == "claude"
    )

    timeline = _json(
        _call_get_harness_timeline(
            data["api_key"], data["org"].id, {"session_id": str(session_id)}
        )
    )
    conversations = _json(
        _call_list_harness_conversations(
            data["api_key"], data["org"].id, {}
        )
    )
    conversation = next(
        row for row in conversations if row["session_id"] == str(session_id)
    )
    assert conversation["harness_id"] == "claude"

    assert timeline["session"]["harness_id"] == "claude"
    assert [message["role"] for message in timeline["messages"]] == [
        "user",
        "assistant",
    ]
    assert all(message["harness_id"] == "claude" for message in timeline["messages"])
    assert all(message["id"] for message in timeline["messages"])
    assert timeline["messages"][1]["completed_at"] is not None

    followed = _json(
        await _call_send_harness_message(
            data["api_key"],
            data["org"].id,
            {"session_id": str(session_id), "prompt": "second MCP turn"},
        )
    )
    assert followed["harness_id"] == "claude"
    messages = await _wait_for_messages(service, session_id)
    assert [message.content for message in messages if message.role == "user"] == [
        "first MCP turn",
        "second MCP turn",
    ]
    assert [name for name, _kwargs in data["registry"].created] == ["claude", "claude"]
    assert data["registry"].prompts == ["first MCP turn", "second MCP turn"]
    assert secret not in json.dumps(listed)
    assert secret not in json.dumps(timeline)
    assert secret not in json.dumps(followed)
    assert all(
        "connection_id" not in json.dumps(payload)
        for payload in (listed, timeline, followed)
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_mcp_claude_create_rejects_missing_foreign_and_invalid_engine(
    claude_mcp_setup,
):
    data = claude_mcp_setup
    service = data["service"]
    base_args = {
        "workspace_id": str(data["workspace"].id),
        "prompt": "not created",
        "harness_id": "claude",
    }
    await sync_to_async(data["service"]._engine_connections.delete_connection)(
        data["org"].id, data["owner"].id, data["connection"].id
    )
    before = await sync_to_async(HarnessSession.objects.count)()

    missing = await _call_create_harness_session(
        data["api_key"], data["org"].id, base_args
    )
    assert missing[0].text.startswith("Error:")
    assert await sync_to_async(HarnessSession.objects.count)() == before

    foreign = await _call_create_harness_session(
        data["api_key"],
        data["org"].id,
        {**base_args, "connection_id": str(data["foreign_connection"].id)},
    )
    assert foreign[0].text.startswith("Error:")
    assert "another-users-mcp-secret" not in foreign[0].text
    assert await sync_to_async(HarnessSession.objects.count)() == before

    invalid = await _call_create_harness_session(
        data["api_key"],
        data["org"].id,
        {
            "workspace_id": str(data["workspace"].id),
            "prompt": "not created",
            "harness_id": "nope",
        },
    )
    assert invalid[0].text.startswith("Error:")
    assert await sync_to_async(HarnessSession.objects.count)() == before

    # Existing Claude sessions stay bound to their creator; a workspace
    # collaborator cannot use its personal token to continue another user's run.
    stranger_session = await sync_to_async(service.create_session)(
        workspace_id=data["workspace"].id,
        organization_id=data["org"].id,
        prompt="other owner",
        harness_id="claude",
        connection_id=data["foreign_connection"].id,
        user_id=data["other"].id,
    )
    denied = await _call_send_harness_message(
        data["api_key"],
        data["org"].id,
        {"session_id": str(stranger_session.id), "prompt": "must be forbidden"},
    )
    assert denied[0].text.startswith("Error:")
    assert "only the session owner" in denied[0].text.lower()
    assert (
        await sync_to_async(
            HarnessMessage.objects.filter(session_id=stranger_session.id).count
        )()
        == 0
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("gate", ["permission", "question"])
def test_mcp_claude_gate_resolution_is_restricted_to_session_owner(
    claude_mcp_setup, monkeypatch, gate
):
    data = claude_mcp_setup
    service = data["service"]
    stranger_session = service.create_session(
        workspace_id=data["workspace"].id,
        organization_id=data["org"].id,
        prompt=f"owner-only {gate}",
        harness_id="claude",
        connection_id=data["foreign_connection"].id,
        user_id=data["other"].id,
    )
    called = []

    async def denied_if_called(**_kwargs):
        called.append(True)
        raise AssertionError("foreign Claude gate must not be resolved")

    if gate == "permission":
        monkeypatch.setattr(service, "resolve_permission", denied_if_called)
        result = _call_resolve_harness_permission(
            data["api_key"],
            data["org"].id,
            {
                "session_id": str(stranger_session.id),
                "request_id": str(uuid.uuid4()),
                "response": "once",
            },
        )
    else:
        monkeypatch.setattr(service, "resolve_question", denied_if_called)
        result = _call_resolve_harness_question(
            data["api_key"],
            data["org"].id,
            {
                "session_id": str(stranger_session.id),
                "question_id": str(uuid.uuid4()),
                "answers": [],
            },
        )
    assert result[0].text.startswith("Error:")
    assert "only the session owner" in result[0].text.lower()
    assert called == []
