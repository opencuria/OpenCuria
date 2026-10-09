"""REST lifecycle contracts for Claude-backed harness sessions."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import AsyncClient

import apps.harness.api as harness_api
from apps.accounts.models import APIKey, APIKeyPermission
from apps.credentials.models import CredentialService
from apps.credentials.repositories import OrgCredentialServiceActivationRepository
from apps.harness.engines.connections import EngineConnectionService
from apps.harness.engines.repositories import HarnessEngineRepository
from apps.harness.engines.run_repository import HarnessRunRepository
from apps.harness.engines.runs import HarnessRunService, RunOwnership
from apps.harness.harness_service import HarnessService
from apps.harness.models import (
    HarnessMessage,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
)
from apps.harness.providers.base import Usage
from apps.harness.repositories import HarnessMessageRepository
from apps.harness.tests.conftest import FakeAccessor
from common.utils import generate_api_token, hash_token


class _FakeClaudeEngine:
    """Exercise the engine callbacks without invoking Claude or a remote API."""

    def __init__(
        self, *, kwargs: dict[str, Any], registry: _FakeEngineRegistry
    ) -> None:
        self.kwargs = kwargs
        self.registry = registry

    async def run(self, prompt, agent, model, mode, opts):  # type: ignore[no-untyped-def]
        self.registry.prompts.append(prompt)
        lease = {"lease_id": str(uuid.uuid4()), "epoch": "api-test-epoch"}
        external_id = str(uuid.uuid4())
        answer = f"fake Claude answer: {prompt}"
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
                usage=Usage(3, 2, 5, 0.01),
                cost=0.01,
                finish_reason="stop",
                metadata={"harness_id": "claude", "external_session_id": external_id},
            )
        finally:
            await self.kwargs["on_owner"](lease, "released")


class _CompletedRunHarnessService(HarnessService):
    """Await injected engine tasks so Django's per-request loop stays alive."""

    async def start_run(self, session, prompt, **kwargs):  # type: ignore[no-untyped-def]
        assistant = await super().start_run(session, prompt, **kwargs)
        task = self._tasks.get(str(session.id))
        if task is not None:
            await task
        return assistant


class _FakeEngineRegistry:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.prompts: list[str] = []

    def create(self, harness_id: str, **kwargs: Any) -> _FakeClaudeEngine:
        self.created.append((harness_id, kwargs))
        return _FakeClaudeEngine(kwargs=kwargs, registry=self)


async def _drop_emit(_event: str, _data: dict[str, Any]) -> None:
    return None


async def _fake_accessor(workspace_id: str) -> FakeAccessor:
    return FakeAccessor(workspace_id)


def _configure_claude_services(organization_id: uuid.UUID) -> None:
    """Ensure the two personal Claude credential services are active for tests."""
    services = [
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
        organization_id, [service.id for service in services]
    )


def _api_key(
    user, organization_id: uuid.UUID, permissions=None
) -> tuple[AsyncClient, str]:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"claude-contract-{uuid.uuid4().hex[:8]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions
        or [
            APIKeyPermission.HARNESS_RUN.value,
            APIKeyPermission.HARNESS_READ.value,
            APIKeyPermission.HARNESS_PERMISSIONS.value,
        ],
    )
    client = AsyncClient()
    client._claude_headers = {  # type: ignore[attr-defined]
        "X-API-Key": token,
        "X-Organization-Id": str(organization_id),
    }
    return client, token


@pytest.fixture
def claude_api_setup(db, monkeypatch):
    """Install real personal credentials and an injected, deterministic engine."""

    from apps.organizations.models import Membership, MembershipRole, Organization
    from apps.runners.enums import RunnerStatus, WorkspaceStatus
    from apps.runners.models import Runner, Workspace

    org = Organization.objects.create(
        name=f"Claude Harness API {uuid.uuid4().hex[:8]}",
        slug=f"claude-harness-api-{uuid.uuid4().hex[:10]}",
    )
    owner = get_user_model().objects.create_user(
        email=f"claude-harness-api-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    stranger = get_user_model().objects.create_user(
        email=f"claude-harness-api-other-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="claude-harness-api-runner",
        api_token_hash=hash_token(f"claude-harness-api-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"claude-harness-api-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    owned = Workspace.objects.create(
        runner=runner,
        name="Claude API Owned",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    from apps.harness.engines.connections import EngineConnectionService

    data = {"org": org, "owner": owner, "stranger": stranger, "owned": owned}
    _configure_claude_services(data["org"].id)
    connections = EngineConnectionService()
    owner_connection = connections.save_connection(
        organization_id=data["org"].id,
        user=data["owner"],
        auth_type="api_token",
        token="rest-personal-claude-secret-do-not-return",
        label="REST test Claude",
    )
    registry = _FakeEngineRegistry()
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
    monkeypatch.setattr(
        HarnessEngineRepository,
        "update_message_engine_meta",
        staticmethod(lambda _message_id, _engine_meta: None),
        raising=False,
    )
    service = _CompletedRunHarnessService(
        emit=_drop_emit,
        accessor_factory=_fake_accessor,
        engine_registry=registry,
        engine_connections=connections,
        run_service=HarnessRunService(),
    )
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    client, _token = _api_key(data["owner"], data["org"].id)
    return {
        **data,
        "client": client,
        "connection": owner_connection,
        "service": service,
        "registry": registry,
    }


async def _wait_for_completed_messages(
    service: HarnessService, session_id: uuid.UUID
) -> list[HarnessMessage]:
    """Wait for background Claude runs to persist their completed assistant rows."""

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
    pytest.fail("Injected Claude run did not finish within 10 seconds")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rest_create_followup_and_read_projections_keep_claude_engine(
    claude_api_setup,
):
    data = claude_api_setup
    client = data["client"]
    service = data["service"]
    secret = "rest-personal-claude-secret-do-not-return"
    create = await client.post(
        f"/api/v1/workspaces/{data['owned'].id}/harness/sessions/",
        data=json.dumps(
            {
                "prompt": "first API turn",
                "harness_id": "claude",
                "connection_id": str(data["connection"].id),
            }
        ),
        content_type="application/json",
        headers=client._claude_headers,
    )
    assert create.status_code == 201, create.content[:500]
    created = create.json()
    session_id = uuid.UUID(created["id"])
    assert created["harness_id"] == "claude"
    assert "connection_id" not in created
    assert secret not in create.content.decode()

    await _wait_for_completed_messages(service, session_id)
    session = await sync_to_async(HarnessSession.objects.get)(id=session_id)
    assert session.harness_id == "claude"
    assert session.connection_id == data["connection"].id
    assert session.created_by_id == data["owner"].id

    listed = await client.get(
        f"/api/v1/workspaces/{data['owned'].id}/harness/sessions/",
        headers=client._claude_headers,
    )
    assert listed.status_code == 200, listed.content[:500]
    assert (
        next(row for row in listed.json() if row["id"] == str(session_id))["harness_id"]
        == "claude"
    )

    conversations = await client.get(
        "/api/v1/harness/conversations/", headers=client._claude_headers
    )
    assert conversations.status_code == 200, conversations.content[:500]
    conversation = next(
        row for row in conversations.json() if row["session_id"] == str(session_id)
    )
    assert conversation["harness_id"] == "claude"

    timeline = await client.get(
        f"/api/v1/harness/sessions/{session_id}/timeline",
        headers=client._claude_headers,
    )
    assert timeline.status_code == 200, timeline.content[:500]
    first_messages = timeline.json()["messages"]
    assert [message["role"] for message in first_messages] == ["user", "assistant"]
    assert all(message["harness_id"] == "claude" for message in first_messages)
    assert all(message["id"] for message in first_messages)
    assert first_messages[1]["completed_at"] is not None

    # No harness selector is sent on follow-up; the persisted session engine wins.
    followup = await client.post(
        f"/api/v1/harness/sessions/{session_id}/message",
        data=json.dumps({"prompt": "second API turn"}),
        content_type="application/json",
        headers=client._claude_headers,
    )
    assert followup.status_code == 202, followup.content[:500]
    assert followup.json()["harness_id"] == "claude"
    messages = await _wait_for_completed_messages(service, session_id)
    assert [message.content for message in messages if message.role == "user"] == [
        "first API turn",
        "second API turn",
    ]
    assert [engine for engine, _kwargs in data["registry"].created] == [
        "claude",
        "claude",
    ]
    assert data["registry"].prompts == ["first API turn", "second API turn"]
    assert all(
        kwargs["auth_env"] == {"ANTHROPIC_API_KEY": secret}
        for _engine, kwargs in data["registry"].created
    )
    # The in-memory fake receives the secret only in its server-side auth env;
    # it must never appear in any public API projection.
    for response in (listed, conversations, timeline, followup):
        assert secret not in response.content.decode()
        assert "connection_id" not in response.content.decode()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rest_claude_connection_failures_are_scoped_and_do_not_orphan_sessions(
    claude_api_setup,
):
    data = claude_api_setup
    client = data["client"]
    url = f"/api/v1/workspaces/{data['owned'].id}/harness/sessions/"

    connections = EngineConnectionService()
    connections.delete_connection(
        data["org"].id, data["owner"].id, data["connection"].id
    )
    count_before = await sync_to_async(HarnessSession.objects.count)()
    missing = await client.post(
        url,
        data=json.dumps({"prompt": "no auth", "harness_id": "claude"}),
        content_type="application/json",
        headers=client._claude_headers,
    )
    assert missing.status_code == 404, missing.content[:500]
    assert await sync_to_async(HarnessSession.objects.count)() == count_before

    foreign_connection = EngineConnectionService().save_connection(
        organization_id=data["org"].id,
        user=data["stranger"],
        auth_type="api_token",
        token="foreign-claude-connection-secret",
        label="Other user's Claude",
    )
    foreign = await client.post(
        url,
        data=json.dumps(
            {
                "prompt": "foreign connection",
                "harness_id": "claude",
                "connection_id": str(foreign_connection.id),
            }
        ),
        content_type="application/json",
        headers=client._claude_headers,
    )
    assert foreign.status_code == 404, foreign.content[:500]
    assert await sync_to_async(HarnessSession.objects.count)() == count_before
    assert "foreign-claude-connection-secret" not in foreign.content.decode()

    invalid_model = await client.post(
        url,
        data=json.dumps(
            {
                "prompt": "invalid model",
                "harness_id": "claude",
                "connection_id": str(data["connection"].id),
                "model": "not-a-claude-model",
            }
        ),
        content_type="application/json",
        headers=client._claude_headers,
    )
    assert invalid_model.status_code == 400, invalid_model.content[:500]
    assert await sync_to_async(HarnessSession.objects.count)() == count_before
    assert (
        "rest-personal-claude-secret-do-not-return"
        not in invalid_model.content.decode()
    )

    # Even an organization member who owns the workspace cannot reuse another
    # user's personal connection through a session created for that user.
    stranger_connection = foreign_connection
    stranger_session = await sync_to_async(data["service"].create_session)(
        workspace_id=data["owned"].id,
        organization_id=data["org"].id,
        prompt="owned by the other account",
        harness_id="claude",
        connection_id=stranger_connection.id,
        user_id=data["stranger"].id,
    )
    denied = await client.post(
        f"/api/v1/harness/sessions/{stranger_session.id}/message",
        data=json.dumps({"prompt": "should not run"}),
        content_type="application/json",
        headers=client._claude_headers,
    )
    assert denied.status_code == 403, denied.content[:500]
    assert (
        await sync_to_async(
            HarnessMessage.objects.filter(session_id=stranger_session.id).count
        )()
        == 0
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate, route_suffix, payload, resolver_name",
    [
        ("permission", "permissions", {"response": "once"}, "resolve_permission"),
        (
            "question",
            "questions",
            {"answers": [["continue"]], "reject": False},
            "resolve_question",
        ),
    ],
)
async def test_rest_claude_gate_resolution_is_restricted_to_session_owner(
    claude_api_setup, monkeypatch, gate, route_suffix, payload, resolver_name
):
    data = claude_api_setup
    stranger_connection = EngineConnectionService().save_connection(
        organization_id=data["org"].id,
        user=data["stranger"],
        auth_type="api_token",
        token="stranger-personal-claude-secret",
        label="Stranger Claude",
    )
    stranger_session = await sync_to_async(data["service"].create_session)(
        workspace_id=data["owned"].id,
        organization_id=data["org"].id,
        prompt=f"owner-only {gate}",
        harness_id="claude",
        connection_id=stranger_connection.id,
        user_id=data["stranger"].id,
    )
    resolver = AsyncMock()
    monkeypatch.setattr(data["service"], resolver_name, resolver)
    monkeypatch.setattr(
        harness_api,
        "_owned_workspace",
        lambda _request, _org_id, _workspace_id: data["owned"],
    )
    gate_id = uuid.uuid4()
    response = await data["client"].post(
        f"/api/v1/harness/sessions/{stranger_session.id}/{route_suffix}/{gate_id}",
        data=json.dumps(payload),
        content_type="application/json",
        headers=data["client"]._claude_headers,
    )
    assert response.status_code == 403, response.content[:500]
    resolver.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "abort"])
async def test_rest_claude_conflicts_preserve_closing_run_and_session(
    claude_api_setup, action: str
):
    """Closing engine attempts surface as HTTP 409 without erasing evidence."""
    data = claude_api_setup
    service = data["service"]
    client = data["client"]
    session = await sync_to_async(service.create_session)(
        workspace_id=data["owned"].id,
        organization_id=data["org"].id,
        prompt="active session must survive",
        harness_id="claude",
        connection_id=data["connection"].id,
        user_id=data["owner"].id,
    )
    await sync_to_async(HarnessSession.objects.filter(id=session.id).update)(
        status=HarnessSessionStatus.BUSY
    )
    assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id,
        role="assistant",
        model="sonnet",
    )
    assistant.harness_id = "claude"
    await sync_to_async(assistant.save)(update_fields=["harness_id"])
    run = await sync_to_async(HarnessRunRepository.create_attempt)(
        session_id=session.id,
        assistant_message_id=assistant.id,
        user_id=data["owner"].id,
        harness_id="claude",
    )
    await sync_to_async(HarnessRunRepository.model.objects.filter(id=run.id).update)(
        status=HarnessRunStatus.CLOSING,
        lease_id=uuid.uuid4(),
        lease_epoch="api-closing-epoch",
    )
    # A CLOSING attempt with a runner lease must not be erased unless release
    # has been confirmed. Remove the fake accessor to exercise this fail-closed
    # recovery path rather than letting the fixture silently release the lease.
    service._accessor_factory = None
    persisted_session = await sync_to_async(HarnessSession.objects.get)(id=session.id)
    persisted_run = await sync_to_async(HarnessRunRepository.get_by_id)(run.id)
    assert persisted_session.harness_id == "claude"
    assert persisted_run is not None
    assert persisted_run.status == HarnessRunStatus.CLOSING
    assert persisted_run.lease_id is not None

    url = f"/api/v1/harness/sessions/{session.id}"
    if action == "delete":
        response = await client.delete(url, headers=client._claude_headers)
    else:
        response = await client.post(f"{url}/abort", headers=client._claude_headers)

    assert response.status_code == 409, response.content[:500]
    assert response.json()["code"] == "conflict"
    saved_session = await sync_to_async(HarnessSession.objects.get)(id=session.id)
    saved_run = await sync_to_async(HarnessRunRepository.get_by_id)(run.id)
    assert saved_session.status == HarnessSessionStatus.BUSY
    assert saved_run is not None
    assert saved_run.status == HarnessRunStatus.CLOSING


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rest_claude_create_rejects_invalid_engine_without_session(
    claude_api_setup,
):
    data = claude_api_setup
    count_before = await sync_to_async(HarnessSession.objects.count)()
    response = await data["client"].post(
        f"/api/v1/workspaces/{data['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "unknown engine", "harness_id": "not-an-engine"}),
        content_type="application/json",
        headers=data["client"]._claude_headers,
    )
    assert response.status_code == 400, response.content[:500]
    assert await sync_to_async(HarnessSession.objects.count)() == count_before
    assert "rest-personal-claude-secret-do-not-return" not in response.content.decode()
