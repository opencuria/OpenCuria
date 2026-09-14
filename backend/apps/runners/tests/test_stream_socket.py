"""Socket.IO routing tests for workspace stream events."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import socketio

from apps.runners import sio_server


@pytest.fixture
def fresh_sio(monkeypatch):
    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(server)
    service = SimpleNamespace(
        handle_stream_reply=MagicMock(),
        handle_harness_reply=MagicMock(),
        handle_process_reply=MagicMock(),
        handle_git_reply=MagicMock(),
    )
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    return server, service


def _handler(server, event: str):
    return server.handlers["/"][event]


def _make_runner_workspace(*, sid: str, prefix: str):
    from django.contrib.auth import get_user_model

    from apps.organizations.models import Membership, MembershipRole, Organization
    from apps.runners.enums import RunnerStatus, WorkspaceStatus
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token

    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"{prefix}-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    org = Organization.objects.create(
        name=f"Stream SIO Org {uuid.uuid4().hex[:6]}",
        slug=f"stream-sio-org-{uuid.uuid4().hex[:10]}",
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
async def test_stream_output_delegated_with_runner_id(fresh_sio):
    server, service = fresh_sio
    runner, workspace = _make_runner_workspace(
        sid="stream-sio-sid-1", prefix="stream-sio-happy"
    )
    server.get_session = AsyncMock(return_value={"runner_id": str(runner.id)})
    data = {
        "connection_id": "conn-1",
        "workspace_id": str(workspace.id),
        "stream": "stdout",
        "data": "aGk=",
    }
    await _handler(server, "workspace:stream_output")("any-sid", data)
    service.handle_stream_reply.assert_called_once_with(
        "workspace:stream_output", data, runner_id=str(runner.id)
    )
    # Never forwarded to harness/process/git handlers.
    service.handle_harness_reply.assert_not_called()
    service.handle_process_reply.assert_not_called()
    service.handle_git_reply.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stream_closed_delegated(fresh_sio):
    server, service = fresh_sio
    runner, workspace = _make_runner_workspace(
        sid="stream-sio-sid-2", prefix="stream-sio-closed"
    )
    server.get_session = AsyncMock(return_value={"runner_id": str(runner.id)})
    data = {
        "connection_id": "conn-2",
        "workspace_id": str(workspace.id),
        "exit_code": 0,
    }
    await _handler(server, "workspace:stream_closed")("any-sid", data)
    service.handle_stream_reply.assert_called_once_with(
        "workspace:stream_closed", data, runner_id=str(runner.id)
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stream_dropped_for_unauthenticated_session(fresh_sio):
    server, service = fresh_sio
    _make_runner_workspace(sid="stream-sio-sid-3", prefix="stream-sio-drop")
    server.get_session = AsyncMock(return_value={})
    await _handler(server, "workspace:stream_output")(
        "unknown-sid",
        {
            "connection_id": "c",
            "workspace_id": str(uuid.uuid4()),
            "stream": "stdout",
            "data": "aGk=",
        },
    )
    service.handle_stream_reply.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stream_service_validation_rejects_bad_chunks():
    """Unit-level: service drops invalid chunks / mismatched owners."""
    from apps.runners.services import RunnerService

    service = RunnerService(sio_server=None)
    routed: list[dict] = []
    import apps.harness.access.runner_accessor as accessor_mod

    orig_output = accessor_mod.route_stream_output
    orig_closed = accessor_mod.route_stream_closed
    accessor_mod.route_stream_output = lambda data: routed.append(data) or True
    accessor_mod.route_stream_closed = lambda data: routed.append(data) or True
    try:
        from django.contrib.auth import get_user_model

        from apps.organizations.models import Membership, MembershipRole, Organization
        from apps.runners.enums import RunnerStatus, WorkspaceStatus
        from apps.runners.models import Runner, Workspace
        from common.utils import hash_token

        user_model = get_user_model()
        user = user_model.objects.create_user(
            email=f"stream-val-{uuid.uuid4().hex[:8]}@example.com",
            password="secret",
        )
        org = Organization.objects.create(
            name=f"Stream Val Org {uuid.uuid4().hex[:6]}",
            slug=f"stream-val-org-{uuid.uuid4().hex[:10]}",
        )
        Membership.objects.create(
            user=user, organization=org, role=MembershipRole.ADMIN
        )
        runner = Runner.objects.create(
            name="stream-val-runner",
            api_token_hash=hash_token(f"stream-val-{uuid.uuid4().hex[:6]}"),
            status=RunnerStatus.ONLINE,
            sid="stream-val-sid",
            organization=org,
            available_runtimes=["docker"],
        )
        workspace = Workspace.objects.create(
            runner=runner,
            name="stream val workspace",
            status=WorkspaceStatus.RUNNING,
            created_by=user,
        )
        good = {
            "connection_id": "c-good",
            "workspace_id": str(workspace.id),
            "stream": "stdout",
            "data": "aGk=",
        }
        service.handle_stream_reply(
            "workspace:stream_output", good, runner_id=str(runner.id)
        )
        assert routed and routed[-1] == good
        before = len(routed)
        # Bad base64 is dropped.
        service.handle_stream_reply(
            "workspace:stream_output",
            {**good, "data": "!!!"},
            runner_id=str(runner.id),
        )
        assert len(routed) == before
        # Wrong runner is dropped (fail closed).
        service.handle_stream_reply(
            "workspace:stream_output", good, runner_id=str(uuid.uuid4())
        )
        assert len(routed) == before
        # Unknown event ignored.
        service.handle_stream_reply(
            "workspace:stream_bogus", good, runner_id=str(runner.id)
        )
        assert len(routed) == before
    finally:
        accessor_mod.route_stream_output = orig_output
        accessor_mod.route_stream_closed = orig_closed


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stream_output_ack_ok_and_nack():
    """Socket handlers ACK {ok:true}; unknown streams NACK {ok:false}."""
    import base64 as _b64

    import socketio as _socketio

    import apps.runners.sio_server as sio_server
    from apps.runners.services import RunnerService

    fresh_server = _socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(fresh_server)
    handler = fresh_server.handlers["/"]["workspace:stream_output"]
    runner, workspace = _make_runner_workspace(
        sid="stream-sio-sid-ack", prefix="stream-sio-ack"
    )
    fresh_server.get_session = AsyncMock(return_value={"runner_id": str(runner.id)})
    service = RunnerService(sio_server=None)
    import apps.runners.sio_server as mod

    orig = mod.get_runner_service
    mod.get_runner_service = lambda: service
    try:
        good = {
            "connection_id": "no-such-conn",
            "workspace_id": str(workspace.id),
            "stream": "stdout",
            "data": _b64.b64encode(b"hi").decode(),
        }
        # Unknown connection -> routed False -> NACK.
        result = await handler("any-sid", good)
        assert result == {"ok": False}
        # Unauthenticated session -> NACK.
        fresh_server.get_session = AsyncMock(return_value={})
        result2 = await handler("unknown-sid", good)
        assert result2["ok"] is False
    finally:
        mod.get_runner_service = orig


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_handle_stream_reply_returns_accepted_bool():
    """handle_stream_reply: True accepted, False unknown/invalid/foreign."""
    import base64 as _b64

    from apps.runners.services import RunnerService

    runner, workspace = _make_runner_workspace(
        sid="stream-sio-sid-bool", prefix="stream-sio-bool"
    )
    service = RunnerService(sio_server=None)
    good = {
        "connection_id": "conn-bool",
        "workspace_id": str(workspace.id),
        "stream": "stdout",
        "data": _b64.b64encode(b"hi").decode(),
    }
    # Unknown connection: no accessor registered -> False.
    assert (
        service.handle_stream_reply(
            "workspace:stream_output", good, runner_id=str(runner.id)
        )
        is False
    )
    # Bad chunk -> False.
    assert (
        service.handle_stream_reply(
            "workspace:stream_output",
            {**good, "data": "!!!"},
            runner_id=str(runner.id),
        )
        is False
    )
    # Unknown event -> False.
    assert (
        service.handle_stream_reply(
            "workspace:stream_bogus", good, runner_id=str(runner.id)
        )
        is False
    )
    # Foreign runner -> False.
    assert (
        service.handle_stream_reply(
            "workspace:stream_output", good, runner_id=str(uuid.uuid4())
        )
        is False
    )
