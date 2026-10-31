"""Characterize desktop proxy wire behavior against a real HTTP/WS upstream."""

from __future__ import annotations

import asyncio
import base64
import gzip
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web

from src.interfaces.desktop_proxy import DesktopProxy
from src.services.capture_fence import CaptureFence

ProxyFixture = tuple[DesktopProxy, asyncio.Queue[tuple[str, dict]]]
UpstreamRequests = list[tuple[str, dict]]


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


@pytest.fixture
async def upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[UpstreamRequests]:
    """Route the fixed guest address to an isolated local aiohttp server."""
    requests = []

    async def http(request: web.Request) -> web.Response:
        response = web.Response(
            status=207,
            body=b"\x00\xffdesktop",
            headers={"Connection": "keep-alive", "Keep-Alive": "timeout=5"},
        )
        response.enable_chunked_encoding()
        return response

    async def websocket(request: web.Request) -> web.WebSocketResponse:
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
        return web.Response(
            body=gzip.compress(b"<html><head></head><body>desktop</body></html>"),
            headers={"Content-Encoding": "gzip", "Content-Type": "text/html"},
        )

    app = web.Application()
    app.router.add_get("/vnc.html", http)
    app.router.add_get("/websockify", websocket)
    app.router.add_get("/compressed.html", compressed)
    runner = web.AppRunner(app)
    await runner.setup()
    server = await asyncio.get_running_loop().create_server(
        runner.server, "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    original_get = aiohttp.ClientSession.get
    original_ws_connect = aiohttp.ClientSession.ws_connect

    def routed(method: Callable[..., Any]) -> Callable[..., Any]:
        def call(session: aiohttp.ClientSession, url: str, **kwargs: Any) -> Any:
            requests.append((str(url), kwargs))
            parsed = urlsplit(url)
            target = f"{parsed.scheme}://127.0.0.1:{port}{parsed.path}"
            if parsed.query:
                target += f"?{parsed.query}"
            return method(session, target, **kwargs)

        return call

    monkeypatch.setattr(aiohttp.ClientSession, "get", routed(original_get))
    monkeypatch.setattr(
        aiohttp.ClientSession, "ws_connect", routed(original_ws_connect)
    )
    try:
        yield requests
    finally:
        server.close()
        await server.wait_closed()
        await runner.cleanup()


async def test_http_preserves_status_binary_body_and_end_to_end_headers(
    proxy: ProxyFixture, upstream: UpstreamRequests
) -> None:
    client, _ = proxy
    result = await client.fetch_http(uuid.uuid4(), "/vnc.html", "autoconnect=true")
    assert upstream[0][0] == "http://10.0.0.5:6901/vnc.html?autoconnect=true"
    assert result["status"] == 207
    assert result["body"] == base64.b64encode(b"\x00\xffdesktop").decode("ascii")
    assert result["body_encoding"] == "base64"
    headers = {key.lower(): value for key, value in result["headers"]}
    assert headers["content-type"] == "application/octet-stream"
    assert not {"transfer-encoding", "connection", "keep-alive"} & headers.keys()


async def test_http_headers_describe_decompressed_payload(
    proxy: ProxyFixture, upstream: UpstreamRequests
) -> None:
    """The browser must not decode aiohttp's already expanded bytes twice."""
    client, _ = proxy
    response = await client.fetch_http(uuid.uuid4(), "/compressed.html")
    body = base64.b64decode(response["body"])
    headers = {key.lower(): value for key, value in response["headers"]}
    assert body == b"<html><head></head><body>desktop</body></html>"
    assert "content-encoding" not in headers
    assert int(headers["content-length"]) == len(body)


@pytest.mark.parametrize("subprotocols", [["binary", "other"], [], None])
async def test_websocket_forwards_both_directions_and_releases_resources(
    proxy: ProxyFixture,
    upstream: UpstreamRequests,
    subprotocols: list[str] | None,
) -> None:
    client, frames = proxy
    workspace_id = uuid.uuid4()
    result = await client.open_tunnel(workspace_id, "tunnel", subprotocols)
    protocol = "binary" if subprotocols else None
    assert result == {"ok": True, "subprotocol": protocol}
    assert upstream[0] == (
        "ws://10.0.0.5:6901/websockify",
        {
            "protocols": ["binary"] if protocol else None,
            "max_msg_size": 16 * 1024 * 1024,
            "headers": {"Origin": "http://10.0.0.5:6901"},
        },
    )
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
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
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
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
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
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
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
    upstream: UpstreamRequests,
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
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
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
