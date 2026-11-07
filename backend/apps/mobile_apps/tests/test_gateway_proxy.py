"""Forwarding app requests to a workspace process (HTTP, streaming, WebSocket).

A real local aiohttp server plays the workspace process; a socket-backed
byte stream stands in for the runner's workspace TCP stream.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from apps.harness.access.base import StreamClosedError, WorkspaceByteStream
from apps.mobile_apps.gateway_protocol import BackendAddress
from apps.mobile_apps.proxy import (
    Upstream,
    UpstreamError,
    forward_http,
    forward_websocket,
    rewrite_location,
    rewrite_set_cookie,
    send_error,
    upstream_headers,
)

from .conftest import SocketStream

APP = "/ws/app-backends/7b0c4f1e-3c39-4d4e-8f1e-111111111111"


def upstream_for(port: int, address: BackendAddress | None = None) -> Upstream:
    async def open_stream() -> WorkspaceByteStream:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return SocketStream(reader, writer)

    return Upstream(
        open_stream=open_stream,
        port=port,
        address=address or BackendAddress("api", 80),
        app_path=APP,
    )


async def call_http(
    upstream, method="GET", path="/", *, query=b"", headers=(), body=b""
):
    inbox: asyncio.Queue = asyncio.Queue()
    inbox.put_nowait({"type": "http.request", "body": body, "more_body": False})
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "headers": list(headers),
        "query_string": query,
    }
    await forward_http(scope, inbox.get, send, upstream, path)
    return sent


def body_of(sent: list[dict]) -> bytes:
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )


async def test_request_reaches_the_process_as_a_local_call(workspace_process):
    sent = await call_http(
        upstream_for(workspace_process.port),
        "POST",
        "/echo",
        query=b"page=2",
        headers=[
            (b"host", b"10.0.2.2:8000"),
            (b"x-opencuria-app-session", b"secret-token"),
            (b"authorization", b"Bearer app-own-token"),
            (b"content-type", b"application/json"),
            (b"content-length", b"11"),
            (b"connection", b"keep-alive"),
        ],
        body=b'{"todo": 1}',
    )

    request = workspace_process.requests[0]
    assert (request["method"], request["path_qs"]) == ("POST", "/echo?page=2")
    assert request["body"] == b'{"todo": 1}'
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["host"] == f"localhost:{workspace_process.port}"
    assert headers["x-forwarded-host"] == "api.opencuria.local"
    # The app's own credentials pass, OpenCuria's session never does.
    assert headers["authorization"] == "Bearer app-own-token"
    assert "x-opencuria-app-session" not in headers

    start = sent[0]
    assert start["status"] == 200
    assert (b"x-upstream", b"yes") in start["headers"]
    # Proxies in front of OpenCuria must not buffer (event streams).
    assert (b"x-accel-buffering", b"no") in start["headers"]
    assert json.loads(body_of(sent)) == {"ok": True}


async def test_chunked_response_streams_through(workspace_process):
    sent = await call_http(upstream_for(workspace_process.port), path="/chunks")
    assert sent[0]["status"] == 200
    assert body_of(sent) == b"one,two,three"
    assert sent[-1] == {"type": "http.response.body", "body": b""}


async def test_redirects_and_cookies_stay_on_the_backend(workspace_process):
    sent = await call_http(upstream_for(workspace_process.port), path="/login")
    headers = dict(sent[0]["headers"])
    assert sent[0]["status"] == 302
    assert headers[b"location"] == f"{APP}/api/80/home".encode()
    assert headers[b"set-cookie"] == f"sid=42; HttpOnly; Path={APP}/api/80/".encode()


async def test_app_disconnect_closes_the_event_stream(workspace_process):
    inbox: asyncio.Queue = asyncio.Queue()
    inbox.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    received: list[bytes] = []

    async def send(message: dict) -> None:
        if message.get("body"):
            received.append(message["body"])
            if len(received) == 3:
                inbox.put_nowait({"type": "http.disconnect"})

    scope = {"type": "http", "method": "GET", "headers": [], "query_string": b""}
    await asyncio.wait_for(
        forward_http(
            scope, inbox.get, send, upstream_for(workspace_process.port), "/events"
        ),
        5,
    )
    assert received[:3] == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"]
    await asyncio.wait_for(workspace_process.stream_closed.wait(), 5)


async def test_unreachable_process_raises_before_answering():
    async def refused() -> WorkspaceByteStream:
        raise StreamClosedError("connect failed: [Errno 111] Connection refused")

    upstream = Upstream(
        open_stream=refused, port=3000, address=BackendAddress("api", 80), app_path=APP
    )
    with pytest.raises(UpstreamError, match="Connection refused"):
        await call_http(upstream, path="/")


async def test_websocket_messages_flow_both_ways(workspace_process):
    inbox: asyncio.Queue = asyncio.Queue()
    inbox.put_nowait({"type": "websocket.receive", "text": "hi"})
    inbox.put_nowait({"type": "websocket.receive", "bytes": b"abc"})
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)
        if sum(m["type"] == "websocket.send" for m in sent) == 2:
            inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})

    scope = {
        "type": "websocket",
        "headers": [(b"origin", b"http://api.opencuria.local")],
        "query_string": b"room=1",
        "subprotocols": ["chat"],
        "extensions": {"websocket.http.response": {}},
    }
    await asyncio.wait_for(
        forward_websocket(
            scope, inbox.get, send, upstream_for(workspace_process.port), "/socket"
        ),
        5,
    )
    assert sent[0]["type"] == "websocket.accept"
    assert sent[0]["subprotocol"] == "chat"
    assert {"type": "websocket.send", "text": "echo:hi"} in sent
    assert {"type": "websocket.send", "bytes": b"cba"} in sent


async def test_websocket_refused_by_the_process_gets_its_status(workspace_process):
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "headers": [],
        "query_string": b"",
        "subprotocols": [],
        "extensions": {"websocket.http.response": {}},
    }
    await asyncio.wait_for(
        forward_websocket(
            scope,
            asyncio.Queue().get,
            send,
            upstream_for(workspace_process.port),
            "/echo",
        ),
        5,
    )
    assert sent[0]["type"] == "websocket.http.response.start"
    assert sent[0]["status"] != 101


def test_upstream_headers_frame_chunked_bodies():
    upstream = upstream_for(3000, BackendAddress("api", 8080))
    headers, has_body = upstream_headers(
        [(b"transfer-encoding", b"chunked"), (b"x-forwarded-for", b"1.2.3.4")],
        upstream,
    )
    assert has_body
    assert (b"transfer-encoding", b"chunked") in headers
    assert (b"x-forwarded-host", b"api.opencuria.local:8080") in headers
    assert all(name != b"x-forwarded-for" for name, _ in headers)


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/login?next=/", f"{APP}/api/80/login?next=/"),
        ("http://localhost:3000/a", f"{APP}/api/80/a"),
        ("http://127.0.0.1:9000/a#top", f"{APP}/api/9000/a#top"),
        ("http://files.opencuria.local/x", f"{APP}/files/80/x"),
        ("http://files.opencuria.local:8081/x", f"{APP}/files/8081/x"),
        ("https://github.com/login", "https://github.com/login"),
        ("relative/path", "relative/path"),
        ("//localhost:3000/b", f"{APP}/api/80/b"),
    ],
)
def test_redirect_targets_map_into_the_gateway(location, expected):
    assert rewrite_location(location, upstream_for(3000)) == expected


def test_cookies_lose_their_domain_and_gain_the_backend_path():
    upstream = upstream_for(3000)
    assert (
        rewrite_set_cookie("a=1; Path=/api; Domain=.example.com; Secure", upstream)
        == f"a=1; Secure; Path={APP}/api/80/api"
    )
    assert rewrite_set_cookie("b=2", upstream) == f"b=2; Path={APP}/api/80/"


async def test_errors_are_json_with_retry_hints_and_websocket_denials():
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await send_error({"type": "http"}, send, 503, "Starting", "busy", retry_after=5)
    assert sent[0]["status"] == 503
    assert (b"retry-after", b"5") in sent[0]["headers"]
    assert json.loads(sent[1]["body"]) == {"detail": "Starting", "code": "busy"}

    sent.clear()
    await send_error({"type": "websocket", "extensions": {}}, send, 503, "x", "busy")
    assert sent == [{"type": "websocket.close", "code": 1013}]
