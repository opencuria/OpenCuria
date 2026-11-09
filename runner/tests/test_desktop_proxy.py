"""Characterize desktop proxy wire behavior against a real HTTP/WS upstream."""

from __future__ import annotations

import asyncio
import base64
import gzip
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from src.interfaces.desktop_proxy import DesktopProxy, DesktopUpstreamError
from src.services.capture_fence import CaptureFence

ProxyFixture = tuple[DesktopProxy, asyncio.Queue[tuple[str, dict]]]


@pytest.fixture
async def proxy() -> AsyncIterator[ProxyFixture]:
    """Exercise proxy ownership independently of runner connection setup."""
    frames = asyncio.Queue()

    async def emit(event: str, payload: dict) -> None:
        await frames.put((event, payload))

    desktop = SimpleNamespace(
        get_desktop_guest_ip=lambda _: "10.0.0.5",
        get_desktop_session=lambda _: SimpleNamespace(generation=7),
    )
    events = SimpleNamespace(connected=True, emit=AsyncMock(side_effect=emit))
    client = DesktopProxy(
        desktop.get_desktop_guest_ip,
        desktop.get_desktop_session,
        events,
        CaptureFence(),
    )
    yield client, frames
    for tunnel_id in list(client._tunnels):
        await client._finalize(tunnel_id)


@dataclass
class Upstream:
    """A local KasmVNC stand-in the proxy is pinned to, plus a foreign target."""

    port: int
    requests: list[tuple[str, str, dict[str, str]]]
    foreign_hits: list[str]
    foreign_url: str
    behavior: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
async def upstream(proxy: ProxyFixture) -> AsyncIterator[Upstream]:
    """Pin the proxy to an isolated local server standing in for the guest."""
    requests: list[tuple[str, str, dict[str, str]]] = []
    foreign_hits: list[str] = []
    behavior: dict[str, Any] = {}

    def record(request: web.Request) -> web.Response | None:
        requests.append((request.path, request.query_string, dict(request.headers)))
        if "redirect" in behavior:
            return web.Response(status=302, headers={"Location": behavior["redirect"]})
        return None

    async def http(request: web.Request) -> web.Response:
        if redirect := record(request):
            return redirect
        response = web.Response(
            status=207,
            body=behavior.get("body", b"\x00\xffdesktop"),
            headers={"Connection": "keep-alive", "Keep-Alive": "timeout=5"},
        )
        response.enable_chunked_encoding()
        return response

    async def websocket(request: web.Request) -> web.StreamResponse:
        if redirect := record(request):
            return redirect
        response = web.WebSocketResponse(protocols=["binary"])
        await response.prepare(request)
        await response.send_str("ready")
        await response.send_bytes(b"\x00\xffdesktop")
        async for message in response:
            if message.type == aiohttp.WSMsgType.TEXT:
                if message.data == "bye":
                    await response.close()
                else:
                    await response.send_str(message.data)
            elif message.type == aiohttp.WSMsgType.BINARY:
                await response.send_bytes(message.data)
        return response

    async def compressed(request: web.Request) -> web.Response:
        record(request)
        return web.Response(
            body=gzip.compress(b"<html><head></head><body>desktop</body></html>"),
            headers={"Content-Encoding": "gzip", "Content-Type": "text/html"},
        )

    async def foreign(request: web.Request) -> web.Response:
        foreign_hits.append(request.path)
        return web.Response(text="another desktop")

    app = web.Application()
    app.router.add_get("/vnc.html", http)
    app.router.add_get("/websockify", websocket)
    app.router.add_get("/compressed.html", compressed)
    foreign_app = web.Application()
    foreign_app.router.add_get("/{tail:.*}", foreign)
    runners = [web.AppRunner(app), web.AppRunner(foreign_app)]
    servers = []
    for app_runner in runners:
        await app_runner.setup()
        servers.append(
            await asyncio.get_running_loop().create_server(
                app_runner.server, "127.0.0.1", 0
            )
        )
    port, foreign_port = (server.sockets[0].getsockname()[1] for server in servers)
    client, _ = proxy
    client._get_guest_ip = lambda _: "127.0.0.1"
    client._port = port
    try:
        yield Upstream(
            port=port,
            requests=requests,
            foreign_hits=foreign_hits,
            foreign_url=f"http://127.0.0.1:{foreign_port}/websockify",
            behavior=behavior,
        )
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        for app_runner in runners:
            await app_runner.cleanup()


async def test_http_preserves_status_binary_body_and_end_to_end_headers(
    proxy: ProxyFixture, upstream: Upstream
) -> None:
    client, _ = proxy
    result = await client.fetch_http(uuid.uuid4(), "/vnc.html")
    assert [request[:2] for request in upstream.requests] == [("/vnc.html", "")]
    assert result["status"] == 207
    assert result["body"] == base64.b64encode(b"\x00\xffdesktop").decode("ascii")
    assert result["body_encoding"] == "base64"
    headers = {key.lower(): value for key, value in result["headers"]}
    assert headers["content-type"] == "application/octet-stream"
    assert not {"transfer-encoding", "connection", "keep-alive"} & headers.keys()


async def test_http_headers_describe_decompressed_payload(
    proxy: ProxyFixture, upstream: Upstream
) -> None:
    """The browser must not decode aiohttp's already expanded bytes twice."""
    client, _ = proxy
    response = await client.fetch_http(uuid.uuid4(), "/compressed.html")
    body = base64.b64decode(response["body"])
    headers = {key.lower(): value for key, value in response["headers"]}
    assert body == b"<html><head></head><body>desktop</body></html>"
    assert "content-encoding" not in headers
    assert int(headers["content-length"]) == len(body)


@pytest.mark.parametrize(
    "rest_path",
    ["/vnc.html?token=secret", "/vnc.html#x", "vnc.html", "/../etc/passwd", "/a\\b"],
)
async def test_http_forwards_only_plain_paths(
    proxy: ProxyFixture, upstream: Upstream, rest_path: str
) -> None:
    """Viewer query strings may carry backend credentials; never forward them."""
    client, _ = proxy
    result = await client.fetch_http(uuid.uuid4(), rest_path)
    assert result["status"] == 502
    assert upstream.requests == []


@pytest.mark.parametrize("target", ["foreign", "same-origin"])
async def test_http_refuses_redirects_without_dialing_their_target(
    proxy: ProxyFixture, upstream: Upstream, target: str
) -> None:
    """A guest redirect must never steer the runner host to another address."""
    client, _ = proxy
    upstream.behavior["redirect"] = (
        upstream.foreign_url
        if target == "foreign"
        else f"http://127.0.0.1:{upstream.port}/compressed.html"
    )
    result = await client.fetch_http(uuid.uuid4(), "/vnc.html")
    assert result["status"] == 502
    assert base64.b64decode(result["body"]) == b"Bad Gateway"
    assert [request[0] for request in upstream.requests] == ["/vnc.html"]
    assert upstream.foreign_hits == []


async def test_http_refuses_oversized_bodies(
    proxy: ProxyFixture, upstream: Upstream
) -> None:
    client, _ = proxy
    upstream.behavior["body"] = b"x" * (8 * 1024 * 1024 + 1)
    result = await client.fetch_http(uuid.uuid4(), "/vnc.html")
    assert result["status"] == 502


async def test_websocket_refuses_redirected_handshake(
    proxy: ProxyFixture, upstream: Upstream
) -> None:
    """A 3xx to the upgrade request cannot open a tunnel to another desktop."""
    client, _ = proxy
    upstream.behavior["redirect"] = upstream.foreign_url.replace("http:", "ws:")
    with pytest.raises(DesktopUpstreamError):
        await client.open_tunnel(uuid.uuid4(), "tunnel", ["binary"])
    assert [request[0] for request in upstream.requests] == ["/websockify"]
    assert upstream.foreign_hits == []
    assert not client._tunnels and not client._opening


async def test_pinned_session_refuses_other_hosts_and_ports() -> None:
    proxy = DesktopProxy(
        lambda _: "127.0.0.1",
        lambda _: None,
        SimpleNamespace(connected=False, emit=AsyncMock()),
        upstream_port=6901,
    )
    async with proxy._client_session("127.0.0.1") as session:
        for url in ("http://127.0.0.2:6901/", "http://127.0.0.1:22/", "https://127.0.0.1:6901/"):
            with pytest.raises(DesktopUpstreamError):
                await session.get(url)


@pytest.mark.parametrize("subprotocols", [["binary", "other"], [], None])
async def test_websocket_forwards_both_directions_and_releases_resources(
    proxy: ProxyFixture,
    upstream: Upstream,
    subprotocols: list[str] | None,
) -> None:
    client, frames = proxy
    workspace_id = uuid.uuid4()
    result = await client.open_tunnel(workspace_id, "tunnel", subprotocols)
    protocol = "binary" if subprotocols else None
    assert result == {"ok": True, "subprotocol": protocol}
    path, query, headers = upstream.requests[0]
    assert (path, query) == ("/websockify", "")
    assert headers["Origin"] == f"http://127.0.0.1:{upstream.port}"
    assert headers.get("Sec-WebSocket-Protocol") == protocol
    tunnel = client._tunnels["tunnel"]
    assert (tunnel.workspace_id, tunnel.desktop_generation) == (workspace_id, 7)
    assert await asyncio.wait_for(frames.get(), 2) == (
        "desktop:proxy_ws_frame",
        {"tunnel_id": "tunnel", "text": "ready"},
    )
    binary = base64.b64encode(b"\x00\xffdesktop").decode("ascii")
    assert await asyncio.wait_for(frames.get(), 2) == (
        "desktop:proxy_ws_frame",
        {"tunnel_id": "tunnel", "data": binary, "encoding": "base64"},
    )
    for payload, expected in [
        ({"text": "hello", "data": "ignored"}, {"text": "hello"}),
        (
            {"data": binary, "encoding": "base64"},
            {"data": binary, "encoding": "base64"},
        ),
        ({"data": "plain"}, {"data": "cGxhaW4=", "encoding": "base64"}),
    ]:
        await client.send("tunnel", **payload)
        assert await asyncio.wait_for(frames.get(), 2) == (
            "desktop:proxy_ws_frame",
            {"tunnel_id": "tunnel", **expected},
        )
    await client.send("tunnel")
    await client.send("tunnel", text="bye")
    assert await asyncio.wait_for(frames.get(), 2) == (
        "desktop:proxy_ws_closed",
        {"tunnel_id": "tunnel", "code": 1000},
    )
    assert client._tunnels == {}
    assert tunnel.websocket.closed and tunnel.session.closed
    await asyncio.wait_for(tunnel.reader_task, 2)
    await client.close_tunnel("tunnel")
    with pytest.raises(RuntimeError, match="Unknown desktop proxy tunnel: tunnel"):
        await client.send("tunnel", text="late")


async def test_failed_handshake_closes_the_http_session(
    proxy: ProxyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = proxy
    session = SimpleNamespace(
        ws_connect=AsyncMock(side_effect=OSError("unavailable")), close=AsyncMock()
    )
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_: session)
    with pytest.raises(OSError, match="unavailable"):
        await client.open_tunnel(uuid.uuid4(), "tunnel")
    session.close.assert_awaited_once()
    assert client._tunnels == {}


@pytest.mark.parametrize("operation", ["tunnel", "workspace", "all", "generation"])
async def test_pending_handshake_cannot_outlive_teardown_or_desktop_generation(
    proxy: ProxyFixture, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Late handshakes cannot resurrect tunnels closed before the open ACK."""
    client, _ = proxy
    workspace_id = uuid.uuid4()
    entered = asyncio.Event()
    finish = asyncio.Event()
    websocket = SimpleNamespace(close=AsyncMock())

    async def connect(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await finish.wait()
        return websocket

    session = SimpleNamespace(ws_connect=connect, close=AsyncMock())
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_: session)
    task = asyncio.create_task(client.open_tunnel(workspace_id, "pending"))
    await asyncio.wait_for(entered.wait(), 2)
    if operation == "tunnel":
        await client.close_tunnel("pending")
    elif operation == "workspace":
        await client.close_workspace(workspace_id, generation=8, keep_generation=8)
    elif operation == "all":
        await client.close_all()
    else:
        client._get_session = lambda _: SimpleNamespace(generation=8)
    finish.set()
    with pytest.raises(RuntimeError, match="Desktop changed"):
        await asyncio.wait_for(task, 2)
    assert not client._tunnels and not client._opening
    websocket.close.assert_awaited_once()
    assert session.close.await_count >= 1


async def test_cancelled_handshake_closes_session(
    proxy: ProxyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = proxy
    entered = asyncio.Event()

    async def connect(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    session = SimpleNamespace(ws_connect=connect, close=AsyncMock())
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_: session)
    task = asyncio.create_task(client.open_tunnel(uuid.uuid4(), "pending"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session.close.assert_awaited_once()
    assert not client._tunnels and not client._opening


@pytest.mark.parametrize(
    "operation", ["tunnel", "workspace", "all", "cancel", "generation"]
)
async def test_queued_admission_is_owned_before_first_await(
    proxy: ProxyFixture, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """A closed request queued for lifecycle admission must never dial later."""
    client, _ = proxy
    workspace_id = uuid.uuid4()
    entered = asyncio.Event()
    finish = asyncio.Event()

    @asynccontextmanager
    async def admission(_: uuid.UUID) -> AsyncIterator[None]:
        entered.set()
        await finish.wait()
        yield

    client._interaction = admission
    create_session = AsyncMock()
    monkeypatch.setattr(aiohttp, "ClientSession", create_session)
    task = asyncio.create_task(client.open_tunnel(workspace_id, "queued"))
    await asyncio.wait_for(entered.wait(), 2)
    if operation == "tunnel":
        await client.close_tunnel("queued")
    elif operation == "workspace":
        await client.close_workspace(workspace_id, generation=8, keep_generation=8)
    elif operation == "all":
        await client.close_all()
    elif operation == "generation":
        client._get_session = lambda _: SimpleNamespace(generation=8)
    else:
        task.cancel()
    finish.set()
    expected = asyncio.CancelledError if operation == "cancel" else RuntimeError
    with pytest.raises(expected):
        await asyncio.wait_for(task, 2)
    create_session.assert_not_called()
    assert not client._tunnels and not client._opening


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("operation", ["tunnel", "workspace", "all"])
async def test_explicit_close_cleans_up_once_even_when_disconnected(
    proxy: ProxyFixture,
    upstream: Upstream,
    connected: bool,
    operation: str,
) -> None:
    client, frames = proxy
    workspace_id = uuid.uuid4()
    await client.open_tunnel(workspace_id, "tunnel")
    tunnel = client._tunnels["tunnel"]
    for _ in range(2):
        await asyncio.wait_for(frames.get(), 2)
    client._events.connected = connected
    client._events.emit.reset_mock()
    if operation == "tunnel":
        await client.close_tunnel("tunnel")
    elif operation == "workspace":
        await client.close_workspace(
            workspace_id, generation=7, include_generation=True
        )
    else:
        await client.close_all()
    await asyncio.wait_for(
        asyncio.gather(tunnel.reader_task, return_exceptions=True), 2
    )
    await client._finalize("tunnel")
    assert tunnel.websocket.closed and tunnel.session.closed
    assert client._tunnels == {}
    if connected:
        client._events.emit.assert_awaited_once_with(
            "desktop:proxy_ws_closed", {"tunnel_id": "tunnel", "code": 1000}
        )
    else:
        client._events.emit.assert_not_awaited()


async def test_reader_failure_reports_internal_error_and_releases_resources(
    proxy: ProxyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, frames = proxy

    class BrokenWebSocket:
        closed = False
        close_code = None
        close = AsyncMock()

        def __aiter__(self) -> BrokenWebSocket:
            return self

        async def __anext__(self) -> aiohttp.WSMessage:
            raise OSError("connection lost")

    websocket = BrokenWebSocket()
    session = SimpleNamespace(
        ws_connect=AsyncMock(return_value=websocket), close=AsyncMock()
    )
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_: session)
    await client.open_tunnel(uuid.uuid4(), "tunnel")
    task = client._tunnels["tunnel"].reader_task
    assert await asyncio.wait_for(frames.get(), 2) == (
        "desktop:proxy_ws_closed",
        {"tunnel_id": "tunnel", "code": 1011},
    )
    await asyncio.wait_for(task, 2)
    assert client._tunnels == {}
    websocket.close.assert_awaited_once()
    session.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("generation", "keep", "include", "expected"),
    [
        (None, None, False, ["unknown", "old", "current", "new"]),
        (7, 7, False, ["unknown", "old"]),
        (7, None, True, ["unknown", "old", "current"]),
    ],
)
async def test_workspace_close_respects_generation_and_workspace(
    proxy: ProxyFixture,
    generation: int | None,
    keep: int | None,
    include: bool,
    expected: list[str],
) -> None:
    client, _ = proxy
    workspace_id = uuid.uuid4()
    for name, value in [("unknown", None), ("old", 6), ("current", 7), ("new", 8)]:
        client._tunnels[name] = SimpleNamespace(
            workspace_id=workspace_id, desktop_generation=value
        )
    client._tunnels["other"] = SimpleNamespace(
        workspace_id=uuid.uuid4(), desktop_generation=None
    )
    finalize = AsyncMock()
    client._finalize = finalize
    await client.close_workspace(
        workspace_id,
        generation=generation,
        keep_generation=keep,
        include_generation=include,
    )
    assert [call.args[0] for call in finalize.await_args_list] == expected
    client._tunnels.clear()
