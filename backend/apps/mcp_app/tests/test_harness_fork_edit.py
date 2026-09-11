"""MCP fork/edit handler tests (happy path, permissions, UUID errors).

Follows the direct-handler pattern of ``test_provider_connections.py``
and ``test_processes.py``: call the ``_call_*`` functions with a
SimpleNamespace API key, no MCP transport involved.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.accounts.models import APIKeyPermission
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
from apps.harness.repositories import HarnessMessageRepository
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_edit_harness_message,
    _call_fork_harness_session,
    create_mcp_server,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import hash_token

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


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


def _setup_owned_workspace():
    """Create an org, member, and owned running workspace."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"MCP Fork {uuid.uuid4().hex[:6]}",
        slug=f"mcp-fork-{uuid.uuid4().hex[:10]}",
    )
    user = user_model.objects.create_user(
        email=f"mcp-fork-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.MEMBER)
    runner = Runner.objects.create(
        name="mcp-fork-runner",
        api_token_hash=hash_token(f"mcp-fork-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"mcp-fork-sid-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="MCP Fork Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    return org, user, workspace


def _parse(result: list) -> dict[str, Any]:
    """Decode an MCP text payload as JSON."""
    assert result, "handler returned no content"
    text = result[0].text
    assert not text.startswith("Error:"), text
    return json.loads(text)


def _parse_error(result: list) -> str:
    """Return the raw MCP error text."""
    assert result, "handler returned no content"
    return result[0].text


def _key(user, permissions: list[APIKeyPermission]) -> SimpleNamespace:
    """Build an API key namespace with an explicit permission allowlist."""

    class _Key(SimpleNamespace):
        def has_permission(self, permission) -> bool:  # type: ignore[no-untyped-def]
            value = (
                permission.value
                if isinstance(permission, APIKeyPermission)
                else permission
            )
            return value in [p.value for p in permissions]

    return _Key(user=user)


def test_fork_edit_tools_registered_with_run_permission() -> None:
    """Both tools are exposed and require ``harness:run``."""
    names = {tool.name for tool in _TOOLS}
    assert {"fork_harness_session", "edit_harness_message"} <= names
    assert _TOOL_PERMISSIONS["fork_harness_session"] == APIKeyPermission.HARNESS_RUN
    assert _TOOL_PERMISSIONS["edit_harness_message"] == APIKeyPermission.HARNESS_RUN
    assert _TOOL_HANDLERS["fork_harness_session"] is _call_fork_harness_session
    assert _TOOL_HANDLERS["edit_harness_message"] is _call_edit_harness_message
    assert inspect.iscoroutinefunction(_call_fork_harness_session)
    assert inspect.iscoroutinefunction(_call_edit_harness_message)


@pytest.mark.django_db(transaction=True)
def test_fork_edit_tools_hidden_without_run_permission() -> None:
    """A read-only key lists no fork/edit tools via create_mcp_server."""
    import mcp.types as _types

    org, user, _workspace = _setup_owned_workspace()
    read_key = _key(user, [APIKeyPermission.HARNESS_READ])
    server = create_mcp_server(read_key)
    handler = server.request_handlers[_types.ListToolsRequest]
    loop = asyncio.new_event_loop()
    try:
        listed = loop.run_until_complete(
            handler(_types.ListToolsRequest(method="tools/list", params=None))
        )
        names = {tool.name for tool in listed.root.tools}
        assert "fork_harness_session" not in names
        assert "edit_harness_message" not in names

        run_key = _key(
            user, [APIKeyPermission.HARNESS_READ, APIKeyPermission.HARNESS_RUN]
        )
        server = create_mcp_server(run_key)
        handler = server.request_handlers[_types.ListToolsRequest]
        listed = loop.run_until_complete(
            handler(_types.ListToolsRequest(method="tools/list", params=None))
        )
        names = {tool.name for tool in listed.root.tools}
        assert {"fork_harness_session", "edit_harness_message"} <= names
    finally:
        loop.close()


@pytest.mark.django_db(transaction=True)
def test_mcp_fork_edit_permission_denied_at_call_time(monkeypatch) -> None:
    """call_tool rejects fork/edit without ``harness:run`` (defense in depth)."""
    import mcp.types as _types

    org, user, workspace = _setup_owned_workspace()
    service = _service()
    session = service.create_session(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="seed",
        user_id=user.id,
    )
    monkeypatch.setattr("apps.mcp_app.server._get_harness_service", lambda: service)
    read_key = _key(user, [APIKeyPermission.HARNESS_READ])
    server = create_mcp_server(read_key)
    handler = server.request_handlers[_types.CallToolRequest]
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            handler(
                _types.CallToolRequest(
                    method="tools/call",
                    params=_types.CallToolRequestParams(
                        name="fork_harness_session",
                        arguments={"session_id": str(session.id)},
                    ),
                )
            )
        )
        assert "Permission denied" in result.root.content[0].text
        assert APIKeyPermission.HARNESS_RUN.value in result.root.content[0].text
        result = loop.run_until_complete(
            handler(
                _types.CallToolRequest(
                    method="tools/call",
                    params=_types.CallToolRequestParams(
                        name="edit_harness_message",
                        arguments={
                            "session_id": str(session.id),
                            "message_id": str(uuid.uuid4()),
                            "prompt": "edited",
                        },
                    ),
                )
            )
        )
        assert "Permission denied" in result.root.content[0].text
    finally:
        loop.close()


@pytest.mark.django_db(transaction=True)
async def test_mcp_fork_happy_path(monkeypatch) -> None:
    """fork_harness_session copies the prefix and returns the new session."""
    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    service = _service()
    session = await sync_to_async(service.create_session)(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="first",
        user_id=user.id,
    )
    first_user = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="first"
    )
    await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="assistant", content="answer one"
    )
    second_user = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="second"
    )
    monkeypatch.setattr("apps.mcp_app.server._get_harness_service", lambda: service)
    api_key = SimpleNamespace(user=user)
    result = await _call_fork_harness_session(
        api_key,
        org.id,
        {"session_id": str(session.id), "message_id": str(second_user.id)},
    )
    body = _parse(result)
    assert body["id"] != str(session.id)
    assert body["title"] == f"{session.title} (fork #1)"
    forked_msgs = await sync_to_async(HarnessMessageRepository.list_for_session)(
        body["id"]
    )
    assert [m.content for m in forked_msgs] == ["first", "answer one"]
    assert first_user.id is not None


@pytest.mark.django_db(transaction=True)
async def test_mcp_fork_full_copy_without_message_id(monkeypatch) -> None:
    """Omitting message_id copies the full history (no busy guard)."""
    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    service = _service()
    session = await sync_to_async(service.create_session)(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="first",
        user_id=user.id,
    )
    await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="first"
    )
    monkeypatch.setattr("apps.mcp_app.server._get_harness_service", lambda: service)
    api_key = SimpleNamespace(user=user)
    result = await _call_fork_harness_session(
        api_key, org.id, {"session_id": str(session.id)}
    )
    body = _parse(result)
    assert body["id"] != str(session.id)
    forked_msgs = await sync_to_async(HarnessMessageRepository.list_for_session)(
        body["id"]
    )
    assert len(forked_msgs) == 1


@pytest.mark.django_db(transaction=True)
async def test_mcp_fork_invalid_uuid() -> None:
    """Invalid session/message UUIDs fail fast with an error text."""
    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    api_key = SimpleNamespace(user=user)
    result = await _call_fork_harness_session(
        api_key, org.id, {"session_id": "not-a-uuid"}
    )
    assert "Invalid session_id UUID" in _parse_error(result)
    assert _parse_error(
        await _call_fork_harness_session(api_key, org.id, {})
    ).startswith("Error: session_id is required")
    # Valid session id + invalid message id fails before the owner check.
    service = _service()
    session = await sync_to_async(service.create_session)(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="seed",
        user_id=user.id,
    )
    result = await _call_fork_harness_session(
        api_key,
        org.id,
        {"session_id": str(session.id), "message_id": "nope"},
    )
    assert "Invalid message_id UUID" in _parse_error(result)


@pytest.mark.django_db(transaction=True)
async def test_mcp_fork_rejects_foreign_session(monkeypatch) -> None:
    """A session in a workspace the caller does not own reads as not found."""
    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    stranger = await sync_to_async(get_user_model().objects.create_user)(
        email=f"mcp-fork-stranger-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    await sync_to_async(Membership.objects.create)(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    service = _service()
    session = await sync_to_async(service.create_session)(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="seed",
        user_id=user.id,
    )
    monkeypatch.setattr("apps.mcp_app.server._get_harness_service", lambda: service)
    api_key = SimpleNamespace(user=stranger)
    result = await _call_fork_harness_session(
        api_key, org.id, {"session_id": str(session.id)}
    )
    assert "not found" in _parse_error(result).lower()


@pytest.mark.django_db(transaction=True)
async def test_mcp_edit_happy_path(monkeypatch) -> None:
    """edit_harness_message truncates the suffix and reruns the session."""
    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    service = _service()
    session = await sync_to_async(service.create_session)(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="first",
        user_id=user.id,
    )
    first_user = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="first"
    )
    await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="assistant", content="answer one"
    )
    await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="second"
    )
    monkeypatch.setattr("apps.mcp_app.server._get_harness_service", lambda: service)
    api_key = SimpleNamespace(user=user)
    result = await _call_edit_harness_message(
        api_key,
        org.id,
        {
            "session_id": str(session.id),
            "message_id": str(first_user.id),
            "prompt": "first edited",
        },
    )
    body = _parse(result)
    assert body["id"] == str(session.id)
    task = service._tasks.get(str(session.id))
    if task is not None:
        await task
    stored = await sync_to_async(HarnessMessageRepository.list_for_session)(session.id)
    assert [m.content for m in stored if m.role == "user"][0] == "first edited"
    assert "second" not in [m.content for m in stored]


@pytest.mark.django_db(transaction=True)
async def test_mcp_edit_invalid_uuid() -> None:
    """Invalid UUIDs and missing fields fail fast with an error text."""
    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    api_key = SimpleNamespace(user=user)
    result = await _call_edit_harness_message(
        api_key, org.id, {"session_id": "nope", "message_id": "nope", "prompt": "x"}
    )
    assert "Invalid session_id UUID" in _parse_error(result)
    result = await _call_edit_harness_message(
        api_key,
        org.id,
        {
            "session_id": str(workspace.id),
            "message_id": "nope",
            "prompt": "x",
        },
    )
    assert "Invalid message_id UUID" in _parse_error(result)
    result = await _call_edit_harness_message(
        api_key, org.id, {"session_id": str(workspace.id)}
    )
    assert "session_id, message_id and prompt are required" in _parse_error(result)


@pytest.mark.django_db(transaction=True)
async def test_mcp_edit_busy_reports_conflict(monkeypatch) -> None:
    """Editing a busy session surfaces the conflict as an error text."""
    org, user, workspace = await sync_to_async(_setup_owned_workspace)()
    service = _service()
    session = await sync_to_async(service.create_session)(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="first",
        user_id=user.id,
    )
    user_msg = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="first"
    )
    loop = asyncio.new_event_loop()

    async def _never() -> None:
        await asyncio.Event().wait()

    task = loop.create_task(_never())
    service._tasks[str(session.id)] = task
    monkeypatch.setattr("apps.mcp_app.server._get_harness_service", lambda: service)
    try:
        api_key = SimpleNamespace(user=user)
        result = await _call_edit_harness_message(
            api_key,
            org.id,
            {
                "session_id": str(session.id),
                "message_id": str(user_msg.id),
                "prompt": "edited",
            },
        )
        assert "active run" in _parse_error(result)
    finally:
        task.cancel()
        loop.close()
