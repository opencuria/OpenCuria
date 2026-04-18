"""Regression tests for the desktop ASGI proxy."""

from __future__ import annotations

import asyncio
import base64
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.auth_backends import get_auth_backend
from apps.organizations.models import Membership, MembershipRole
from apps.runners.desktop_proxy import (
    _register_ws_tunnel,
    _unregister_ws_tunnel,
    desktop_proxy_app,
    push_runner_ws_closed,
    push_runner_ws_frame,
)
from apps.runners.models import Runner
from common.utils import hash_token


async def _call_http(scope: dict) -> list[dict]:
    """Execute the proxy app for a single HTTP request and collect ASGI events."""
    events: list[dict] = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict):
        events.append(message)

    await desktop_proxy_app(scope, receive, send)
    return events


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_proxy_returns_not_found_for_member_without_workspace_access(
    organization,
    monkeypatch,
):
    owner_model = get_user_model()
    owner = owner_model.objects.create_user(
        email=f"desktop-owner-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    member = owner_model.objects.create_user(
        email=f"desktop-member-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(
        user=owner,
        organization=organization,
        role=MembershipRole.ADMIN,
    )
    Membership.objects.create(
        user=member,
        organization=organization,
        role=MembershipRole.MEMBER,
    )
    runner = Runner.objects.create(
        name="desktop-proxy-runner",
        api_token_hash=hash_token("runner-token"),
        status="online",
        organization=organization,
        available_runtimes=["docker"],
    )
    workspace = runner.workspaces.create(
        name="desktop-proxy-workspace",
        status="running",
        created_by=owner,
    )
    token = get_auth_backend().generate_tokens(member).access_token
    proxy_http = AsyncMock()
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._get_desktop_proxy_target",
        AsyncMock(
            return_value={
                "runner_sid": "runner-sid",
                "runner_id": str(runner.id),
                "desktop_info": {"port": 6901},
            }
        ),
    )
    monkeypatch.setattr("apps.runners.desktop_proxy._proxy_http", proxy_http)

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/ws/desktop/{workspace.id}/",
        "query_string": f"token={token}".encode(),
        "headers": [],
    }

    events = await _call_http(scope)

    assert events[0]["type"] == "http.response.start"
    assert events[0]["status"] == 404
    assert events[1]["type"] == "http.response.body"
    assert events[1]["body"] == b"Not Found"
    proxy_http.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_http_fetches_asset_via_runner(monkeypatch):
    workspace_id = str(uuid.uuid4())
    sio = AsyncMock()
    sio.call = AsyncMock(
        return_value={
            "status": 200,
            "headers": [["Content-Type", "text/plain"]],
            "body": base64.b64encode(b"desktop asset").decode("ascii"),
            "body_encoding": "base64",
        }
    )
    monkeypatch.setattr("apps.runners.sio_server.get_sio_server", lambda: sio)
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._validate_token",
        AsyncMock(return_value=SimpleNamespace(pk=1)),
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._user_can_access_workspace",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._get_desktop_proxy_target",
        AsyncMock(
            return_value={
                "runner_sid": "runner-sid",
                "runner_id": "runner-id",
                "desktop_info": {"port": 6901},
            }
        ),
    )

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/ws/desktop/{workspace_id}/vnc.html",
        "query_string": b"token=test-token",
        "headers": [],
    }

    events = await _call_http(scope)

    sio.call.assert_awaited_once_with(
        "desktop:proxy_http_request",
        {
            "workspace_id": workspace_id,
            "path": "/vnc.html",
            "query_string": "token=test-token",
            "method": "GET",
        },
        to="runner-sid",
        timeout=15,
    )
    assert events[0]["status"] == 200
    assert [b"Content-Type", b"text/plain"] in events[0]["headers"]
    assert events[1]["body"] == b"desktop asset"


@pytest.mark.asyncio
async def test_runner_frames_are_forwarded_to_registered_websocket_tunnel():
    tunnel_id = uuid.uuid4().hex
    queue = _register_ws_tunnel(
        tunnel_id,
        workspace_id=str(uuid.uuid4()),
        runner_id="runner-1",
    )
    try:
        await push_runner_ws_frame(
            tunnel_id,
            "runner-1",
            data=base64.b64encode(b"hello").decode("ascii"),
            encoding="base64",
        )
        await push_runner_ws_closed(tunnel_id, "runner-1", code=1001)

        first = await asyncio.wait_for(queue.get(), timeout=1)
        second = await asyncio.wait_for(queue.get(), timeout=1)

        assert first == {"type": "binary", "data": b"hello"}
        assert second == {"type": "close", "code": 1001}
    finally:
        _unregister_ws_tunnel(tunnel_id)


@pytest.mark.asyncio
async def test_runner_frames_from_wrong_runner_are_ignored():
    tunnel_id = uuid.uuid4().hex
    queue = _register_ws_tunnel(
        tunnel_id,
        workspace_id=str(uuid.uuid4()),
        runner_id="runner-1",
    )
    try:
        await push_runner_ws_frame(
            tunnel_id,
            "runner-2",
            text="should-not-arrive",
        )

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)
    finally:
        _unregister_ws_tunnel(tunnel_id)
