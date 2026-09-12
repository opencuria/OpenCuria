"""Socket.IO tests for git:operation_result routing."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import socketio

from apps.runners import sio_server
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import hash_token


@pytest.fixture
def fresh_sio(monkeypatch):
    """Register handlers on a fresh server without touching singletons.

    Uses a sync MagicMock for handle_git_reply because production wraps
    the sync service method via sync_to_async; the real sync_to_async
    then runs the mock in a worker thread.
    """
    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(server)
    service = SimpleNamespace(handle_git_reply=MagicMock())
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    return server, service


def _handler(server, event: str):
    return server.handlers["/"][event]


def _make_runner_workspace(*, sid: str, prefix: str = "git-sio"):
    from django.contrib.auth import get_user_model

    from apps.organizations.models import (
        Membership,
        MembershipRole,
        Organization,
    )

    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    org = Organization.objects.create(
        name=f"Git SIO Org {uuid.uuid4().hex[:6]}",
        slug=f"git-sio-org-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name=f"{prefix}-runner",
        api_token_hash=hash_token(f"{prefix}-token-{uuid.uuid4().hex[:6]}"),
        status=RunnerStatus.ONLINE,
        sid=sid,
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name=f"{prefix} workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    return runner, workspace


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_git_reply_delegated_with_exact_runner_id(fresh_sio):
    """Authenticated git:operation_result delegates exact data + runner_id."""
    server, service = fresh_sio
    runner, workspace = _make_runner_workspace(
        sid="git-sio-sid-1", prefix="git-sio-happy"
    )
    server.get_session = AsyncMock(return_value={"runner_id": str(runner.id)})

    data = {
        "request_id": "req-abc",
        "workspace_id": str(workspace.id),
        "operation": "list_repos",
        "ok": True,
        "repos": [],
    }

    handler = _handler(server, "git:operation_result")
    await handler("any-sid", data)

    service.handle_git_reply.assert_called_once_with(
        "git:operation_result", data, runner_id=str(runner.id)
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_git_reply_dropped_for_unauthenticated_session(fresh_sio):
    """Unauthenticated runner events are dropped before the service."""
    server, service = fresh_sio
    _make_runner_workspace(sid="git-sio-sid-2", prefix="git-sio-drop")
    server.get_session = AsyncMock(return_value={})

    handler = _handler(server, "git:operation_result")
    await handler(
        "unknown-sid",
        {"request_id": "req-x", "workspace_id": str(uuid.uuid4()), "ok": True},
    )

    service.handle_git_reply.assert_not_called()
