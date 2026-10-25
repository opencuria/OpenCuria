"""HTTP endpoint: API key auth, organization scope and stateless JSON-RPC."""

from __future__ import annotations

import uuid

import httpx
import pytest
from asgiref.sync import sync_to_async
from mcp.types import LATEST_PROTOCOL_VERSION

from apps.accounts.models import APIKeyPermission
from apps.accounts.services import APIKeyService
from apps.mcp_app.transport import mcp_asgi_app

pytestmark = pytest.mark.django_db(transaction=True)

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _issue(user, permissions: list[str]) -> str:
    _key, token = APIKeyService().create_key(
        user=user, name="mcp test", expires_at=None, permissions=permissions
    )
    return token


async def _rpc(token: str | None, method: str, params: dict | None = None, **headers):
    request_headers = dict(HEADERS, **headers)
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=mcp_asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/mcp",
            headers=request_headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )


async def test_rejects_missing_keys_keys_without_mcp_access_and_get(world):
    token = await sync_to_async(_issue)(world.user, ["skills:read"])

    missing = await _rpc(None, "tools/list")
    no_access = await _rpc(token, "tools/list")
    transport = httpx.ASGITransport(app=mcp_asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        get = await client.get("/mcp", headers={"X-API-Key": token})

    assert missing.status_code == 401
    assert no_access.status_code == 403
    assert "mcp:access" in no_access.json()["error"]
    assert get.status_code == 405


async def test_initialize_list_and_call_over_http(world):
    token = await sync_to_async(_issue)(
        world.user,
        [
            APIKeyPermission.MCP_ACCESS.value,
            APIKeyPermission.SKILLS_READ.value,
            APIKeyPermission.SKILLS_WRITE.value,
        ],
    )

    init = await _rpc(
        token,
        "initialize",
        {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    )
    listed = await _rpc(token, "tools/list")
    created = await _rpc(
        token,
        "tools/call",
        {"name": "skill_create", "arguments": {"name": "Tone", "body": "Be brief."}},
    )
    denied = await _rpc(token, "tools/call", {"name": "chat_list", "arguments": {}})

    assert init.status_code == 200
    result = init.json()["result"]
    assert result["serverInfo"]["name"] == "opencuria"
    assert "chat_start" in result["instructions"]
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {
        "opencuria_context",
        "skill_list",
        "skill_get",
        "skill_create",
        "skill_update",
        "skill_delete",
    }
    call = created.json()["result"]
    assert call["isError"] is False
    assert call["structuredContent"]["name"] == "Tone"
    assert denied.json()["result"]["isError"] is True


async def test_organization_header_must_name_a_membership(world):
    token = await sync_to_async(_issue)(world.user, ["mcp:access"])

    chosen = await _rpc(
        token,
        "tools/call",
        {"name": "opencuria_context", "arguments": {}},
        **{"X-Organization-Id": str(world.org.id)},
    )
    foreign = await _rpc(
        token, "tools/list", **{"X-Organization-Id": str(uuid.uuid4())}
    )

    context = chosen.json()["result"]["structuredContent"]
    assert context["organization"]["organization_id"] == str(world.org.id)
    assert foreign.status_code == 403
