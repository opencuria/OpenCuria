"""Forward HTTP and WebSocket traffic to a process inside a workspace.

Each upstream connection is a workspace-local TCP stream through the runner
(:class:`WorkspaceByteStream`); HTTP/1.1 runs over it with httpcore and
WebSockets with wsproto. The process sees requests as if a client in the
workspace called ``http://localhost:<port>``: ``Host`` is rewritten (dev
servers often only accept localhost) and ``X-Forwarded-Host`` names the
host the app used. Redirects and cookies are mapped back into the gateway
path of the backend, so clients follow them without knowing the mapping.
Bodies stream in both directions, so server-sent events and large
downloads work; a client disconnect closes the upstream connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpcore
import structlog
from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Ping,
    RejectConnection,
    RejectData,
    Request,
    TextMessage,
)
from wsproto.utilities import ProtocolError

from apps.harness.access.base import StreamClosedError, WorkspaceByteStream

from .gateway_protocol import (
    BACKEND_DOMAIN,
    SESSION_HEADER,
    BackendAddress,
    is_label,
)

log = structlog.get_logger(__name__)

ASGIReceive = Callable[[], Awaitable[dict]]
ASGISend = Callable[[dict], Awaitable[None]]
StreamOpener = Callable[[], Awaitable[WorkspaceByteStream]]

#: Connection-level headers never forwarded in either direction.
HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
#: Request headers the gateway owns or that must not reach the process.
_REQUEST_DROPPED = HOP_BY_HOP | {
    b"host",
    b"expect",
    b"forwarded",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"x-forwarded-proto",
    SESSION_HEADER.encode(),
}
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_MAX_CHUNK = WorkspaceByteStream.MAX_CHUNK_SIZE
#: Read timeout while waiting for the response head or a WebSocket handshake.
_HEAD_TIMEOUT = 300.0


@dataclass(frozen=True)
class Upstream:
    """One request's target: a port in the workspace and its gateway paths."""

    open_stream: StreamOpener
    #: Port inside the workspace.
    port: int
    address: BackendAddress
    #: Gateway path of the app (``/ws/app-backends/<app_id>``).
    app_path: str

    @property
    def base_path(self) -> str:
        """Gateway path that maps to ``http://<address.host>/``."""
        return f"{self.app_path}/{self.address.label}/{self.address.port}"


class UpstreamError(Exception):
    """The workspace process could not be reached or broke the connection."""


# -- Header mapping ------------------------------------------------------


def upstream_headers(
    headers: list[tuple[bytes, bytes]], upstream: Upstream
) -> tuple[list[tuple[bytes, bytes]], bool]:
    """Request headers for the process and whether the request has a body."""
    out = [(b"host", f"localhost:{upstream.port}".encode())]
    length = chunked = False
    for name, value in headers:
        lower = name.lower()
        if lower == b"content-length":
            length = value.strip() not in (b"", b"0")
        elif lower == b"transfer-encoding":
            chunked = True
        if lower in _REQUEST_DROPPED or lower.startswith(b"sec-websocket-"):
            continue
        out.append((lower, value))
    if chunked and not length:
        out.append((b"transfer-encoding", b"chunked"))
    out.append((b"x-forwarded-host", upstream.address.host.encode()))
    out.append((b"x-forwarded-proto", b"http"))
    return out, length or chunked


def rewrite_location(value: str, upstream: Upstream) -> str:
    """Map a redirect target into the gateway when it points at a backend."""
    if value.startswith("/") and not value.startswith("//"):
        return upstream.base_path + value
    try:
        parts = urlsplit(f"http:{value}" if value.startswith("//") else value)
        port = parts.port
    except ValueError:
        return value
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https", "ws", "wss") or not host:
        return value
    rest = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    rest += f"#{parts.fragment}" if parts.fragment else ""
    default = 443 if parts.scheme in ("https", "wss") else 80
    if host in _LOOPBACK_HOSTS:
        if (port or default) == upstream.port:
            return upstream.base_path + rest
        return f"{upstream.app_path}/{upstream.address.label}/{port or default}{rest}"
    suffix = f".{BACKEND_DOMAIN}"
    if host.endswith(suffix) and is_label(host[: -len(suffix)]):
        return f"{upstream.app_path}/{host[: -len(suffix)]}/{port or default}{rest}"
    return value


def rewrite_set_cookie(value: str, upstream: Upstream) -> str:
    """Scope a cookie to its backend's gateway path (host-only, like a browser)."""
    name_value, *attributes = value.split(";")
    kept: list[str] = []
    path = "/"
    for attribute in attributes:
        key, _, raw = attribute.strip().partition("=")
        if key.lower() == "domain":
            continue
        if key.lower() == "path":
            path = raw.strip() if raw.strip().startswith("/") else "/"
            continue
        if attribute.strip():
            kept.append(attribute.strip())
    return "; ".join([name_value.strip(), *kept, f"Path={upstream.base_path}{path}"])


def downstream_headers(
    headers: list[tuple[bytes, bytes]], upstream: Upstream
) -> list[tuple[bytes, bytes]]:
    """Response headers for the app, with redirects and cookies mapped.

    Reverse proxies in front of OpenCuria must not buffer responses, or event
    streams arrive late: ``X-Accel-Buffering: no`` tells nginx so.
    """
    out: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        lower = name.lower()
        if lower in HOP_BY_HOP:
            continue
        if lower == b"location":
            value = rewrite_location(value.decode("latin-1"), upstream).encode(
                "latin-1"
            )
        elif lower == b"set-cookie":
            value = rewrite_set_cookie(value.decode("latin-1"), upstream).encode(
                "latin-1"
            )
        out.append((lower, value))
    if all(name != b"x-accel-buffering" for name, _ in out):
        out.append((b"x-accel-buffering", b"no"))
    return out


def error_body(message: str, code: str) -> bytes:
    """JSON error body in the API's ``{detail, code}`` shape."""
    return json.dumps({"detail": message, "code": code}).encode()


async def send_error(
    scope: dict,
    send: ASGISend,
    status: int,
    message: str,
    code: str,
    *,
    retry_after: int | None = None,
) -> None:
    """Refuse an HTTP request or WebSocket handshake with a gateway error.

    WebSockets get the same HTTP answer when the server supports denial
    responses, otherwise a close code (1013 "try again later" for 503).
    """
    headers = [
        (b"content-type", b"application/json"),
        (b"cache-control", b"no-store"),
        (b"x-opencuria-error", code.encode()),
    ]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode()))
    if scope["type"] == "http":
        prefix = "http.response"
    elif "websocket.http.response" in scope.get("extensions", {}):
        prefix = "websocket.http.response"
    else:
        await send({"type": "websocket.close", "code": 1013 if status == 503 else 1011})
        return
    await send({"type": f"{prefix}.start", "status": status, "headers": headers})
    await send({"type": f"{prefix}.body", "body": error_body(message, code)})


# -- httpcore over workspace streams --------------------------------------


class _StreamConnection(httpcore.AsyncNetworkStream):
    """An httpcore network stream backed by a workspace byte stream."""

    def __init__(self, stream: WorkspaceByteStream) -> None:
        self._stream = stream
        self._pending = b""
        self._eof = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if not self._pending and not self._eof:
            try:
                chunk = await asyncio.wait_for(self._stream.receive(), timeout)
            except TimeoutError as exc:
                raise httpcore.ReadTimeout("The process did not answer.") from exc
            except StreamClosedError as exc:
                raise httpcore.ReadError(str(exc)) from exc
            if not chunk:
                self._eof = True
            self._pending = chunk
        data, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        for start in range(0, len(buffer), _MAX_CHUNK):
            try:
                await asyncio.wait_for(
                    self._stream.send(bytes(buffer[start : start + _MAX_CHUNK])),
                    timeout,
                )
            except TimeoutError as exc:
                raise httpcore.WriteTimeout("The process stopped reading.") from exc
            except StreamClosedError as exc:
                raise httpcore.WriteError(str(exc)) from exc

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._stream.aclose()

    async def start_tls(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise NotImplementedError("Workspace streams carry plain HTTP only.")

    def get_extra_info(self, info: str) -> Any:
        return None


class _WorkspaceBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend whose connections open workspace streams."""

    def __init__(self, open_stream: StreamOpener) -> None:
        self._open_stream = open_stream

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            stream = await asyncio.wait_for(self._open_stream(), timeout)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("The workspace did not connect.") from exc
        except (RuntimeError, OSError) as exc:
            # Runner stream failures; service errors (e.g. no free
            # connection) propagate with their own status.
            raise httpcore.ConnectError(str(exc)) from exc
        return _StreamConnection(stream)

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Workspace streams are TCP only.")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _ClientGoneError(Exception):
    """The app closed its request before the response was complete."""


async def _await_disconnect(receive: ASGIReceive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


async def forward_http(
    scope: dict, receive: ASGIReceive, send: ASGISend, upstream: Upstream, path: str
) -> None:
    """Forward one HTTP request and stream the response back.

    Raises:
        UpstreamError: When the process could not be reached before a
            response started (the caller answers with an error).
    """
    headers, has_body = upstream_headers(list(scope.get("headers", [])), upstream)
    body_done = asyncio.Event()
    if not has_body:
        body_done.set()

    async def body() -> AsyncIterator[bytes]:
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    raise _ClientGoneError
                if message.get("body"):
                    yield message["body"]
                if not message.get("more_body", False):
                    return
        finally:
            body_done.set()

    query = scope.get("query_string", b"")
    target = path.encode("latin-1") + (b"?" + query if query else b"")
    request = httpcore.Request(
        scope["method"],
        httpcore.URL(
            scheme=b"http", host=b"localhost", port=upstream.port, target=target
        ),
        headers=headers,
        content=body() if has_body else None,
        # No read timeout: event streams and long polls may idle for long.
        # No connect timeout: opening a stream bounds its own wait for a free
        # connection and answers 503 when there is none.
        extensions={"timeout": {"connect": None, "read": None, "write": 60.0}},
    )

    async def watch() -> None:
        await body_done.wait()
        await _await_disconnect(receive)

    pool = httpcore.AsyncConnectionPool(
        network_backend=_WorkspaceBackend(upstream.open_stream),
        max_connections=1,
        keepalive_expiry=0.0,
    )
    async with pool:
        watching = asyncio.create_task(watch())
        try:
            response = await _response_head(
                asyncio.create_task(pool.handle_async_request(request)), watching
            )
            if response is None:
                return  # The app went away before the process answered.
            try:
                await _stream_response(response, send, upstream, watching)
            finally:
                await response.aclose()
        finally:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)


async def _response_head(
    requesting: asyncio.Task, watching: asyncio.Task
) -> httpcore.Response | None:
    """Wait for the response head, a client disconnect or the head timeout."""
    done, _ = await asyncio.wait(
        {requesting, watching},
        timeout=_HEAD_TIMEOUT,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if requesting not in done:
        requesting.cancel()
        await asyncio.gather(requesting, return_exceptions=True)
        if watching in done:
            return None
        raise UpstreamError("The process did not answer within five minutes.")
    try:
        return requesting.result()
    except _ClientGoneError:
        return None
    except (httpcore.TimeoutException, httpcore.NetworkError) as exc:
        raise UpstreamError(str(exc) or type(exc).__name__) from exc
    except httpcore.ProtocolError as exc:
        raise UpstreamError(f"The process broke the HTTP protocol: {exc}") from exc


async def _stream_response(
    response: httpcore.Response,
    send: ASGISend,
    upstream: Upstream,
    watching: asyncio.Task,
) -> None:
    """Send the response to the app until it is complete or the app leaves."""
    await send(
        {
            "type": "http.response.start",
            "status": response.status,
            "headers": downstream_headers(list(response.headers), upstream),
        }
    )

    async def pump() -> None:
        async for chunk in response.aiter_stream():
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    pumping = asyncio.create_task(pump())
    try:
        await asyncio.wait({pumping, watching}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        pumping.cancel()
        await asyncio.gather(pumping, return_exceptions=True)
    if not pumping.cancelled() and pumping.exception():
        # The response already started: the connection to the app breaks.
        log.info("app_backend_response_aborted", error=str(pumping.exception()))


# -- WebSockets -------------------------------------------------------------


class _UpstreamSocket:
    """A wsproto client connection over a workspace byte stream."""

    def __init__(self, stream: WorkspaceByteStream) -> None:
        self.stream = stream
        self.ws = WSConnection(ConnectionType.CLIENT)
        # Both directions send (messages, pongs, closes); a frame's chunks
        # must not interleave with another frame's.
        self._sending = asyncio.Lock()

    async def send(self, event: Any) -> None:
        async with self._sending:
            data = self.ws.send(event)
            for start in range(0, len(data), _MAX_CHUNK):
                await self.stream.send(data[start : start + _MAX_CHUNK])

    async def events(self) -> AsyncIterator[Any]:
        """Protocol events until the stream ends."""
        while True:
            chunk = await self.stream.receive()
            self.ws.receive_data(chunk or None)
            for event in self.ws.events():
                yield event
            if not chunk:
                return


def _handshake_headers(
    headers: list[tuple[bytes, bytes]], upstream: Upstream
) -> list[tuple[bytes, bytes]]:
    # wsproto writes Host, Upgrade, Connection and Sec-WebSocket-* itself.
    forwarded, _ = upstream_headers(headers, upstream)
    return [(k, v) for k, v in forwarded if k not in (b"host", b"content-length")]


async def forward_websocket(
    scope: dict, receive: ASGIReceive, send: ASGISend, upstream: Upstream, path: str
) -> None:
    """Bridge an app WebSocket to the process's WebSocket endpoint.

    The caller has already received ``websocket.connect``.

    Raises:
        UpstreamError: When the process could not be reached before the
            handshake completed (the caller rejects the socket).
    """
    query = scope.get("query_string", b"").decode("latin-1")
    try:
        socket = _UpstreamSocket(await upstream.open_stream())
    except (RuntimeError, OSError, TimeoutError) as exc:
        raise UpstreamError(str(exc)) from exc
    try:
        await socket.send(
            Request(
                host=f"localhost:{upstream.port}",
                target=path + (f"?{query}" if query else ""),
                subprotocols=list(scope.get("subprotocols", [])),
                extra_headers=_handshake_headers(
                    list(scope.get("headers", [])), upstream
                ),
            )
        )
        events = socket.events()
        accepted = await asyncio.wait_for(
            _handshake(events, send, scope, upstream), _HEAD_TIMEOUT
        )
        if accepted:
            await _bridge(socket, events, receive, send)
    except (RuntimeError, OSError, TimeoutError, ProtocolError) as exc:
        # ProtocolError: the stream ended or the process answered something
        # other than a WebSocket handshake.
        raise UpstreamError(str(exc) or "The process closed the connection.") from exc
    finally:
        with contextlib.suppress(Exception):
            await socket.stream.aclose()


async def _handshake(
    events: AsyncIterator[Any], send: ASGISend, scope: dict, upstream: Upstream
) -> bool:
    """Complete the handshake toward the app with the process's answer."""
    reject: RejectConnection | None = None
    body = b""
    async for event in events:
        if isinstance(event, AcceptConnection):
            await send(
                {
                    "type": "websocket.accept",
                    "subprotocol": event.subprotocol,
                    "headers": [
                        (k, v)
                        for k, v in downstream_headers(
                            list(event.extra_headers), upstream
                        )
                        if k == b"set-cookie"
                    ],
                }
            )
            return True
        if isinstance(event, RejectConnection):
            reject = event
            if not event.has_body:
                break
        elif isinstance(event, RejectData):
            body += event.data
            if event.body_finished:
                break
    if reject is None:
        raise StreamClosedError("The process closed the connection.")
    if "websocket.http.response" in scope.get("extensions", {}):
        await send(
            {
                "type": "websocket.http.response.start",
                "status": reject.status_code,
                "headers": downstream_headers(list(reject.headers), upstream),
            }
        )
        await send({"type": "websocket.http.response.body", "body": body})
    else:
        await send({"type": "websocket.close", "code": 1008})
    return False


async def _bridge(
    socket: _UpstreamSocket,
    events: AsyncIterator[Any],
    receive: ASGIReceive,
    send: ASGISend,
) -> None:
    """Relay messages both ways until either side closes."""
    # Without a close frame the process went away (e.g. it was restarted).
    close_code = 1001

    async def from_app() -> None:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                with contextlib.suppress(Exception):
                    await socket.send(CloseConnection(code=message.get("code", 1000)))
                return
            if message.get("text") is not None:
                await socket.send(TextMessage(data=message["text"]))
            elif message.get("bytes") is not None:
                await socket.send(BytesMessage(data=message["bytes"]))

    async def from_process() -> None:
        nonlocal close_code
        text: list[str] = []
        data: list[bytes] = []
        async for event in events:
            if isinstance(event, TextMessage):
                text.append(event.data)
                if event.message_finished:
                    await send({"type": "websocket.send", "text": "".join(text)})
                    text.clear()
            elif isinstance(event, BytesMessage):
                data.append(bytes(event.data))
                if event.message_finished:
                    await send({"type": "websocket.send", "bytes": b"".join(data)})
                    data.clear()
            elif isinstance(event, Ping):
                await socket.send(event.response())
            elif isinstance(event, CloseConnection):
                close_code = event.code
                with contextlib.suppress(Exception):
                    await socket.send(event.response())
                return

    tasks = {asyncio.create_task(from_app()), asyncio.create_task(from_process())}
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # 1005 (a close frame without code) and 1006 (no close frame: the process
    # went away, e.g. it was restarted) cannot be sent.
    code = {1005: 1000, 1006: 1001}.get(close_code, close_code)
    with contextlib.suppress(Exception):
        await send({"type": "websocket.close", "code": code})
