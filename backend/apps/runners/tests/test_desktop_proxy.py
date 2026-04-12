"""Regression tests for the desktop ASGI proxy."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.auth_backends import get_auth_backend
from apps.organizations.models import Membership, MembershipRole
from apps.runners.desktop_proxy import desktop_proxy_app
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
        "apps.runners.desktop_proxy._get_desktop_info",
        lambda _workspace_id: {"container_ip": "172.19.0.3", "port": 6901},
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
