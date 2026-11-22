"""OAuth MCP traffic leaves from the backend, never through the workspace.

OAuth bearer tokens are server-side credentials. A server bound to one is
reached over pinned public HTTPS from the backend; the workspace relay, which
runs in the member's guest, never sees the request or the token.
"""

from __future__ import annotations

import json
import uuid
from contextlib import AsyncExitStack

import httpx
import pytest

from apps.harness.mcp_client import connection as conn_module
from apps.harness.mcp_client import public_http
from apps.harness.mcp_client.connection import (
    McpServerConnection,
    McpServerHealthError,
)
from apps.harness.mcp_client.public_http import (
    PublicOrigin,
    build_public_http_client,
)

TOKEN = "server-side-oauth-token"
PUBLIC = "93.184.216.34"


class WorkspaceAccessor:
    """Accessor double that records every attempt to use the workspace relay."""

    def __init__(self) -> None:
        self.tcp_calls: list[tuple] = []

    async def open_tcp(self, host, port, tls=False, server_hostname=None, timeout=None):
        self.tcp_calls.append((host, port, tls, server_hostname))
        raise AssertionError("OAuth MCP traffic must not use the workspace relay")


def _oauth_connection(transport: str, url: str) -> McpServerConnection:
    return McpServerConnection(
        plugin_id=uuid.uuid4(),
        plugin_slug="notion",
        plugin_name="Notion",
        server_id=uuid.uuid4(),
        server_slug="mcp",
        server_name="MCP",
        transport=transport,
        command=[],
        cwd="/workspace",
        url=url,
        headers={"X-Plugin": "1"},
        oauth_credential_id=uuid.uuid4(),
        oauth_service_id=uuid.uuid4(),
        startup_timeout_seconds=5.0,
        request_timeout_seconds=5.0,
    )


def _mcp_server(requests: list[httpx.Request]):
    """Minimal streamable-HTTP MCP server for httpx.MockTransport."""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method != "POST":
            return httpx.Response(405)
        message = json.loads(request.content)
        method = message.get("method")
        if "id" not in message:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oauth-mcp", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "pong"}]}
        else:
            return httpx.Response(404)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
        )

    return handle


@pytest.fixture
def pinned(monkeypatch):
    """Replace the pinned socket transport with an in-memory MCP server."""
    calls: list[dict] = []
    requests: list[httpx.Request] = []

    def transport(host, addresses, *, max_connections=1, ssl_context=None):
        calls.append(
            {"host": host, "addresses": addresses, "max_connections": max_connections}
        )
        return httpx.MockTransport(_mcp_server(requests))

    monkeypatch.setattr(public_http, "async_pinned_transport", transport)
    return calls, requests


@pytest.fixture
def oauth_token(monkeypatch):
    from apps.credentials import mcp_oauth

    monkeypatch.setattr(
        mcp_oauth, "access_token_for_request", lambda *args: TOKEN, raising=True
    )


async def test_streamable_oauth_server_is_reached_from_the_backend_only(
    fake_dns, pinned, oauth_token
):
    calls, requests = pinned
    fake_dns["mcp.example"] = [PUBLIC]
    accessor = WorkspaceAccessor()
    connection = _oauth_connection("streamable_http", "https://mcp.example/mcp")
    try:
        tools = await connection.open(accessor)
        assert [tool.name for tool in tools] == ["echo"]
        result = await connection.call_tool("echo", {})
        assert result.output == "pong"
    finally:
        await connection.aclose()

    assert accessor.tcp_calls == []
    assert calls == [
        {
            "host": "mcp.example",
            "addresses": (PUBLIC,),
            "max_connections": public_http.MAX_CONNECTIONS,
        }
    ]
    posts = [request for request in requests if request.method == "POST"]
    assert posts
    for request in posts:
        assert request.url.host == "mcp.example"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["x-plugin"] == "1"


async def test_sse_oauth_clients_are_backend_clients(
    monkeypatch, fake_dns, pinned, oauth_token
):
    from mcp.client import sse

    from apps.credentials.mcp_oauth import McpOAuthHTTPAuth

    built: list[httpx.AsyncClient] = []

    def sse_client(url, *, headers=None, httpx_client_factory, **kwargs):
        built.append(httpx_client_factory(headers=headers, auth=None))
        raise RuntimeError("stop after the client is built")

    monkeypatch.setattr(sse, "sse_client", sse_client)
    accessor = WorkspaceAccessor()
    connection = _oauth_connection("sse", "https://mcp.example/sse")
    with pytest.raises(RuntimeError, match="stop after"):
        await connection._open_http_transport(AsyncExitStack(), accessor, "sse")

    assert accessor.tcp_calls == []
    (client,) = built
    assert isinstance(client.auth, McpOAuthHTTPAuth)
    assert isinstance(client._transport, public_http._PinnedOriginTransport)
    await client.aclose()


@pytest.mark.parametrize(
    "url, answers",
    [
        ("https://mcp.internal.example/mcp", ["10.0.0.5"]),
        ("https://rebind.example/mcp", [PUBLIC, "169.254.169.254"]),
        ("https://gone.example/mcp", []),
        ("http://mcp.example/mcp", None),
    ],
)
async def test_oauth_server_must_be_public_https_before_any_connection(
    fake_dns, pinned, url, answers
):
    calls, requests = pinned
    if answers is not None:
        fake_dns[httpx.URL(url).host] = answers
    accessor = WorkspaceAccessor()
    connection = _oauth_connection("streamable_http", url)
    with pytest.raises(McpServerHealthError, match="not a public HTTPS endpoint"):
        await connection._open_http_transport(
            AsyncExitStack(), accessor, "streamable_http"
        )
    assert accessor.tcp_calls == calls == requests == []


async def test_servers_without_oauth_still_use_the_workspace(monkeypatch):
    seen: list[str] = []

    def workspace_client(accessor, url, **kwargs):
        seen.append(url)
        raise RuntimeError("workspace transport selected")

    monkeypatch.setattr(conn_module, "build_workspace_http_client", workspace_client)
    connection = _oauth_connection("streamable_http", "http://localhost:8123/mcp")
    connection.oauth_credential_id = None
    with pytest.raises(RuntimeError, match="workspace transport selected"):
        await connection._open_http_transport(
            AsyncExitStack(), WorkspaceAccessor(), "streamable_http"
        )
    assert seen == ["http://localhost:8123/mcp"]


async def test_backend_client_never_leaves_its_origin(monkeypatch):
    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        public_http,
        "async_pinned_transport",
        lambda *args, **kwargs: httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200)
        ),
    )
    origin = PublicOrigin(host="mcp.example", port=443, addresses=(PUBLIC,))
    async with build_public_http_client(origin) as client:
        assert (await client.get("https://mcp.example/mcp")).status_code == 200
        for escaped in (
            "https://other.example/mcp",
            "http://mcp.example/mcp",
            "https://mcp.example:8443/mcp",
        ):
            with pytest.raises(httpx.ConnectError, match="pinned origin"):
                await client.get(escaped)
    assert [str(request.url) for request in requests] == ["https://mcp.example/mcp"]
