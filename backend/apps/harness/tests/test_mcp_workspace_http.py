"""Tests for the workspace HTTP bridge (no backend sockets).

Proves MCP HTTP traffic flows through ``WorkspaceAccessor.open_tcp``
(in-memory fake byte streams): chunked bodies, SSE streams, session
headers/cookies, same-origin redirect pinning, TLS-via-workspace, and a
full ``streamable_http_client`` + ``ClientSession`` round-trip.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from apps.harness.mcp_client.workspace_http import (
    WorkspaceNetworkStream,
    build_workspace_http_client,
    parse_mcp_http_url,
)


class FakeByteStream:
    """In-memory TCP byte stream double."""

    def __init__(self, script: list[bytes]) -> None:
        self._script = list(script)
        self.sent = bytearray()
        self.eof_sent = False
        self.closed = False

    async def receive(self) -> bytes:
        if self._script:
            await asyncio.sleep(0.005)
            return self._script.pop(0)
        await asyncio.sleep(3600)
        return b""

    async def send(self, data: bytes) -> None:
        self.sent.extend(bytes(data))

    async def send_eof(self) -> None:
        self.eof_sent = True

    async def aclose(self) -> None:
        self.closed = True

    async def wait_closed(self):  # pragma: no cover - unused
        return None


class FakeAccessor:
    """Accessor double serving scripted HTTP responses."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.calls: list[tuple] = []
        self.streams: list[FakeByteStream] = []

    async def open_tcp(self, host, port, tls=False, server_hostname=None, timeout=None):
        self.calls.append((host, int(port), bool(tls), server_hostname))
        stream = FakeByteStream(self.responder(host, int(port), bool(tls)))
        self.streams.append(stream)
        return stream


def _http_response(  # noqa: E501
    body: bytes, *, content_type="application/json", chunked=False, extra=""
) -> list[bytes]:
    if chunked:
        head = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {content_type}\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: close\r\n"
            f"{extra}\r\n"
        ).encode()
        chunks = [head]
        for i in range(0, len(body), 25):
            part = body[i : i + 25]
            chunks.append(f"{len(part):X}\r\n".encode())
            chunks.append(part)
            chunks.append(b"\r\n")
        chunks.append(b"0\r\n\r\n")
        chunks.append(b"")
        return chunks
    head = (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        f"{extra}\r\n"
    ).encode()
    return [head, body, b""]


@pytest.mark.asyncio
async def test_http_bridge_splits_large_writes_into_runner_chunks():
    from apps.harness.access.runner_accessor import STREAM_CHUNK_SIZE

    class BoundedStream(FakeByteStream):
        async def send(self, data: bytes) -> None:
            assert 0 < len(data) <= STREAM_CHUNK_SIZE
            await super().send(data)

    stream = BoundedStream([])
    bridge = WorkspaceNetworkStream(stream)
    payload = b"x" * (STREAM_CHUNK_SIZE * 2 + 7)
    await bridge.write(payload)
    assert stream.sent == payload


def test_parse_mcp_http_url_supports_path_query():
    scheme, host, port, target = parse_mcp_http_url(
        "http://localhost:8123/mcp/v1?key=1"
    )
    assert (scheme, host, port, target) == (
        "http",
        "localhost",
        8123,
        "/mcp/v1?key=1",
    )
    with pytest.raises(ValueError):
        parse_mcp_http_url("ftp://localhost:21/x")


@pytest.mark.asyncio
async def test_http_post_over_workspace_tcp_with_chunked_body():
    body = b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'

    def responder(host, port, tls):
        assert (host, port, tls) == ("localhost", 8123, False)
        return _http_response(body, chunked=True)

    accessor = FakeAccessor(responder)
    client, _transport = build_workspace_http_client(accessor, "http://localhost:8123/mcp")
    async with client:
        response = await client.post(
            "http://localhost:8123/mcp",
            content=b'{"jsonrpc":"2.0","id":1}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["result"] == {"ok": True}
    assert accessor.calls == [("localhost", 8123, False, None)]
    # Request bytes actually went through the fake stream.
    assert b"POST /mcp HTTP/1.1" in bytes(accessor.streams[0].sent)
    assert b"Host: localhost:8123" in bytes(accessor.streams[0].sent)


@pytest.mark.asyncio
async def test_http_sse_stream_over_workspace_tcp():
    sse_body = (
        b'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"x":1}}\n\n'
        b'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"x":2}}\n\n'
    )

    def responder(host, port, tls):
        return _http_response(sse_body, content_type="text/event-stream", chunked=True)

    accessor = FakeAccessor(responder)
    client, _transport = build_workspace_http_client(accessor, "http://localhost:9000/sse")
    async with client:
        async with client.stream("GET", "http://localhost:9000/sse") as response:
            assert response.status_code == 200
            text = await response.aread()
            assert '"x":1' in text.decode().replace(" ", "")
    assert accessor.calls[0][:2] == ("localhost", 9000)


@pytest.mark.asyncio
async def test_http_cross_origin_redirect_fails_closed():
    target = b'{"ok":true}'

    def responder(host, port, tls):
        if host == "localhost":
            body = b""  # noqa: F841
            head = (
                b"HTTP/1.1 302 Found\r\nLocation: http://evil:8123/mcp\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            return [head, b""]
        return _http_response(target)

    accessor = FakeAccessor(responder)
    client, _transport = build_workspace_http_client(accessor, "http://localhost:8123/mcp")
    async with client:
        with pytest.raises(Exception):
            await client.post("http://localhost:8123/mcp", content=b"{}")
    # Only the pinned origin was ever dialed through the workspace.
    assert all(call[0] == "localhost" for call in accessor.calls)


@pytest.mark.asyncio
async def test_http_tls_goes_through_workspace_relay_with_sni():
    body = b'{"jsonrpc":"2.0","id":1,"result":{}}'

    def responder(host, port, tls):
        return _http_response(body)

    accessor = FakeAccessor(responder)
    client, _transport = build_workspace_http_client(accessor, "https://example.internal:443/mcp")
    async with client:
        response = await client.post(
            "https://example.internal:443/mcp", content=b"{}"
        )
        assert response.status_code == 200
    # Plain relay first, then TLS upgrade via start_tls (both workspace-side).
    assert accessor.calls[0][:2] == ("example.internal", 443)
    assert accessor.calls[-1] == ("example.internal", 443, True, "example.internal")


@pytest.mark.asyncio
async def test_http_session_headers_and_cookies_round_trip():
    def responder(host, port, tls):
        body = b'{"jsonrpc":"2.0","id":9,"result":{"tools":[]}}'
        return _http_response(
            body, extra="Mcp-Session-Id: sess-123\r\nSet-Cookie: a=b; Path=/\r\n"
        )

    accessor = FakeAccessor(responder)
    client, _transport = build_workspace_http_client(
        accessor,
        "http://localhost:8123/mcp",
        headers={"X-Token": "secret-header"},
    )
    async with client:
        first = await client.post("http://localhost:8123/mcp", content=b"{}")
        assert first.headers.get("mcp-session-id") == "sess-123"
        second = await client.post(
            "http://localhost:8123/mcp",
            content=b"{}",
            headers={"Mcp-Session-Id": "sess-123"},
        )
        assert second.status_code == 200
    sent = bytes(accessor.streams[1].sent)
    assert b"Mcp-Session-Id: sess-123" in sent
    assert b"X-Token: secret-header" in sent
    assert b"Cookie: a=b" in sent


@pytest.mark.asyncio
async def test_streamable_http_client_end_to_end_over_workspace():
    """Full SDK round-trip (initialize + list_tools) over fake streams."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    def sse_envelope(obj) -> bytes:
        return (f"event: message\ndata: {json.dumps(obj)}\n\n").encode()

    def init_response() -> bytes:
        # Integer id 0: the SDK client sends integer request ids.
        return sse_envelope(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "ws-http", "version": "1"},
                },
            }
        )

    call_index = {"n": 0}

    def responder(host, port, tls):
        call_index["n"] += 1
        if call_index["n"] == 1:
            # POST initialize -> JSON response with session header.
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "ws-http", "version": "1"},
                    },
                }
            ).encode()
            return _http_response(
                body,
                content_type="application/json",
                extra="Mcp-Session-Id: sess-1\r\n",
            )
        return _http_response(b"", content_type="application/json")

    accessor = FakeAccessor(responder)
    client, _transport = build_workspace_http_client(
        accessor, "http://localhost:8123/mcp"
    )
    async with client:
        async with streamable_http_client(
            "http://localhost:8123/mcp", http_client=client
        ) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                assert result.serverInfo.name == "ws-http"
                assert get_session_id() == "sess-1"
    assert accessor.calls and all(c[0] == "localhost" for c in accessor.calls)


@pytest.mark.asyncio
async def test_workspace_network_stream_start_tls_contract():
    raw = FakeByteStream([])
    stream = WorkspaceNetworkStream(raw, already_tls=False)
    # Plain stream: start_tls without relay info is a consistent error.
    with pytest.raises(RuntimeError):
        await stream.start_tls(None)
    tls_stream = WorkspaceNetworkStream(raw, already_tls=True)
    with pytest.raises(RuntimeError, match="already terminated"):
        await tls_stream.start_tls(None)
    assert stream.get_extra_info("ssl_object") is None
    await stream.aclose()
    assert await stream.read(10) == b""
