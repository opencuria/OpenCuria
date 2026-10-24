"""MCP harness create/send must not call Django ORM from an async context."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core.exceptions import SynchronousOnlyOperation

from apps.harness.harness_service import HarnessService
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
from apps.mcp_app.server import (
    _call_create_harness_session,
    _call_get_harness_timeline,
    _call_send_harness_message,
    _call_steer_harness_session,
    _call_withdraw_harness_steer,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import hash_token


class _FakeProvider(ProviderAdapter):
    """Immediate text answer, no network."""

    name = "fake"

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Yield one text delta with usage."""
        yield Delta(text="mcp-answer", usage=Usage(1, 1, 2))


def _setup_owned_workspace():
    """Create an org, member, and owned running workspace."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"MCP {uuid.uuid4().hex[:6]}",
        slug=f"mcp-{uuid.uuid4().hex[:10]}",
    )
    user = user_model.objects.create_user(
        email=f"mcp-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.MEMBER)
    runner = Runner.objects.create(
        name="mcp-runner",
        api_token_hash=hash_token(f"mcp-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"mcp-sid-{uuid.uuid4().hex[:8]}",
        organization=org,
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="MCP Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    return org, user, workspace


async def _drop_emit(event: str, data: dict[str, Any]) -> None:
    """Ignore frontend events in MCP handler tests."""
    return None


def _service() -> HarnessService:
    """HarnessService with a scripted provider (no network)."""
    return HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
        provider_factory=lambda _org: _FakeProvider(),
    )


def _parse(result: list) -> dict[str, Any]:
    """Decode an MCP text payload as JSON."""
    assert result, "handler returned no content"
    text = result[0].text
    assert not text.startswith("Error:"), text
    return json.loads(text)


def test_create_and_send_handlers_are_coroutines() -> None:
    """call_tool must await these handlers instead of wrapping them."""
    import inspect

    assert inspect.iscoroutinefunction(_call_create_harness_session)
    assert inspect.iscoroutinefunction(_call_send_harness_message)


@pytest.mark.django_db(transaction=True)
async def test_mcp_create_harness_session_without_sync_orm(monkeypatch) -> None:
    """Create handler must wrap ORM and await start_run on the ASGI loop."""
    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    service = _service()
    monkeypatch.setattr(
        "apps.mcp_app.server._get_harness_service", lambda: service
    )
    api_key = SimpleNamespace(user=user)
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    try:
        result = await _call_create_harness_session(
            api_key,
            org.id,
            {"workspace_id": str(workspace.id), "prompt": "hello from mcp"},
        )
        body = _parse(result)
        task = service._tasks.get(str(body["id"]))
        if task is not None:
            await task
    except SynchronousOnlyOperation:
        pytest.fail("MCP create_harness_session called ORM from an async context")
    finally:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    assert body["workspace_id"] == str(workspace.id)
    assert body["title"]


@pytest.mark.django_db(transaction=True)
async def test_mcp_send_harness_message_without_sync_orm(monkeypatch) -> None:
    """Send handler must wrap ORM and await start_run on the ASGI loop."""
    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    service = _service()
    session = await sync_to_async(service.create_session)(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="seed",
        agent_name="build",
        mode="build",
        model="fake-model",
        user_id=user.id,
    )
    monkeypatch.setattr(
        "apps.mcp_app.server._get_harness_service", lambda: service
    )
    api_key = SimpleNamespace(user=user)
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    try:
        result = await _call_send_harness_message(
            api_key,
            org.id,
            {
                "session_id": str(session.id),
                "prompt": "follow-up",
                "mode": "plan",
                "model": "other-model",
            },
        )
        body = _parse(result)
        task = service._tasks.get(str(body["id"]))
        if task is not None:
            await task
    except SynchronousOnlyOperation:
        pytest.fail("MCP send_harness_message called ORM from an async context")
    finally:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    assert body["id"] == str(session.id)
    assert body["mode"] == "plan"
    assert body["model"] == "other-model"


@pytest.mark.django_db(transaction=True)
async def test_mcp_steer_and_withdraw_without_sync_orm(monkeypatch) -> None:
    """Steer/withdraw handlers queue into the live run and stay ORM-safe."""
    import asyncio

    from apps.harness.steering import SteeringChannel

    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    service = _service()
    session = await sync_to_async(service.create_session)(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="seed",
        agent_name="build",
        mode="build",
        model="fake-model",
        user_id=user.id,
    )
    monkeypatch.setattr(
        "apps.mcp_app.server._get_harness_service", lambda: service
    )
    api_key = SimpleNamespace(user=user)
    args = {"session_id": str(session.id), "prompt": "also add tests"}

    idle = await _call_steer_harness_session(api_key, org.id, args)
    assert idle[0].text.startswith("Error:") and "no active run" in idle[0].text

    async def _deliver(_entries: list[Any]) -> None:
        return None

    key = str(session.id)
    service._tasks[key] = asyncio.ensure_future(asyncio.Event().wait())
    service._runs[key] = {"steering": SteeringChannel(deliver=_deliver)}
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    try:
        queued = _parse(await _call_steer_harness_session(api_key, org.id, args))
        listed = _parse(
            await sync_to_async(_call_get_harness_timeline)(
                api_key, org.id, {"session_id": key}
            )
        )
        withdrawn = _parse(
            await _call_withdraw_harness_steer(
                api_key,
                org.id,
                {"session_id": key, "steer_id": queued["id"]},
            )
        )
    except SynchronousOnlyOperation:
        pytest.fail("MCP steer handlers called ORM from an async context")
    finally:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        service._tasks.pop(key).cancel()
    assert queued["content"] == "also add tests"
    assert [item["id"] for item in listed["steers"]] == [queued["id"]]
    assert withdrawn["id"] == queued["id"]
    assert service.list_pending_steers(session.id) == []
    gone = await _call_withdraw_harness_steer(
        api_key, org.id, {"session_id": key, "steer_id": queued["id"]}
    )
    assert gone[0].text.startswith("Error:")


def test_mcp_steer_tools_are_registered_with_run_permission() -> None:
    from apps.accounts.models import APIKeyPermission
    from apps.mcp_app.server import _TOOL_HANDLERS, _TOOL_PERMISSIONS, _TOOLS

    names = {tool.name for tool in _TOOLS}
    for name in ("steer_harness_session", "withdraw_harness_steer"):
        assert name in names
        assert _TOOL_PERMISSIONS[name] == APIKeyPermission.HARNESS_RUN
        assert name in _TOOL_HANDLERS
