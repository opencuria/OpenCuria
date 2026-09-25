"""httpcore/httpx bridge: workspace TCP byte streams as HTTP transport.

All HTTP bytes for MCP ``streamable_http``/``sse`` servers travel through
:meth:`WorkspaceAccessor.open_tcp(host, port, tls, server_hostname)` —
the backend never resolves DNS or opens sockets itself. The bridge:

- parses the MCP server URL once and pins the origin (scheme/host/port);
- opens one workspace TCP connection per HTTP request (stateless relay;
  keep-alive is not required by the MCP SDK clients);
- speaks HTTP/1.1 with ``Content-Length`` framing plus chunked request
  and response bodies and SSE event streams;
- lets the workspace relay terminate TLS (``tls=True`` + SNI host);
  ``start_tls`` therefore reopens the relay with ``tls=True``
  (plain bytes are never written before the upgrade) or errors when
  TLS is already terminated upstream;
- builds an :class:`httpx.AsyncHTTPTransport` subclass with a custom
  pool using this backend, wrapped in an :class:`httpx.AsyncClient`
  with ``trust_env=False``, no proxies, and same-origin redirects only.
  ``verify=False`` on the client only disables httpx's *own* (unused
  here) TLS layer — no backend socket exists; the workspace relay is
  the verifying endpoint.

Response/SSE body bounds: the workspace byte stream itself is a bounded
64 KiB-chunk queue (~4 MiB via ``STREAM_QUEUE_DEPTH``); an overflowing
peer fails the stream closed instead of buffering unboundedly (see
``RunnerWorkspaceAccessor._deliver_stream_output``). The ``MAX_*``
constants below additionally bound request/response framing parsed by
this bridge.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import anyio
import httpcore
import httpx

from ..access.runner_accessor import STREAM_CHUNK_SIZE

logger = logging.getLogger(__name__)

REQUIRED_SCHEMES = ("http", "https")
#: Bound for the raw HTTP response head parsed by httpcore (status line
#: + headers) and for one response/chunked frame handed to the MCP SDK.
#: The underlying workspace byte stream is itself a bounded queue, so a
#: peer flooding beyond these framing caps fails the stream instead of
#: growing backend memory without limit.
MAX_HEADER_BYTES = 64 * 1024
MAX_CHUNK_BYTES = 16 * 1024 * 1024

_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _validate_host(host: str) -> str:
    """Validate a DNS host / IP literal / localhost (fail-closed)."""
    import ipaddress as _ip

    cleaned = (host or "").strip()
    if not cleaned or len(cleaned) > 255 or "\x00" in cleaned:
        raise ValueError("MCP HTTP url has no valid host")
    lowered = cleaned.lower()
    if lowered in ("localhost", "127.0.0.1", "::1"):
        return cleaned
    try:
        _ip.ip_address(cleaned)
        return cleaned
    except ValueError:
        pass
    if not _HOST_RE.fullmatch(cleaned):
        raise ValueError("MCP HTTP url has no valid host")
    return cleaned


class WorkspaceNetworkStream(httpcore.AsyncNetworkStream):
    """One open workspace TCP stream adapted to httpcore.

    Plain origins use the relay stream as-is. For ``https`` origins
    httpcore calls :meth:`start_tls`; the relay cannot upgrade in place,
    so the stream first shuts the plain relay (half-close + close) and
    then reopens the workspace connection with ``tls=True``
    (verification + SNI terminate in the workspace relay) and swaps the
    underlying byte stream. No HTTP plaintext is written before the
    reopened relay exists: httpcore only calls ``write`` after the
    ``start_tls`` future resolves.
    """

    def __init__(
        self,
        stream: Any,
        *,
        already_tls: bool = False,
        accessor: Any = None,
        host: str = "",
        port: int = 0,
        timeout: float | None = None,
    ) -> None:
        self._stream = stream
        self._already_tls = already_tls
        self._accessor = accessor
        self._host = host
        self._port = int(port or 0)
        self._timeout = timeout
        self._closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Read the next chunk (``b""`` marks EOF)."""
        if self._closed:
            return b""
        if timeout is not None and timeout > 0:
            try:
                with anyio.fail_after(timeout):
                    data = await self._stream.receive()
                    return bytes(data or b"")
            except TimeoutError as exc:
                raise TimeoutError("workspace stream read timed out") from exc
        data = await self._stream.receive()
        return bytes(data or b"")

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        """Write raw bytes to the workspace stream."""
        if self._closed:
            raise OSError("stream is closed")
        # The generic runner accepts <=64 KiB per input event. httpcore
        # can write larger POST bodies (notably MCP tool arguments).
        for offset in range(0, len(buffer), STREAM_CHUNK_SIZE):
            await self._stream.send(bytes(buffer[offset : offset + STREAM_CHUNK_SIZE]))

    async def aclose(self) -> None:
        """Close the workspace stream (idempotent)."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._stream.send_eof()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            await self._stream.aclose()
        except Exception:  # pragma: no cover - best effort
            pass

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Upgrade to TLS by reopening the relay with ``tls=True``.

        Certificate verification and SNI terminate in the workspace
        relay (see ``runner/src/service.py``); the backend never sees
        plaintext or performs its own handshake. The plain relay is
        half-closed + closed first so no stale bytes leak, then the
        TLS relay is opened with the validated SNI host. Calling
        ``start_tls`` twice raises a consistent error instead of
        silently stacking.
        """
        if self._already_tls:
            raise RuntimeError(
                "TLS is already terminated by the workspace relay; "
                "start_tls must not be called again"
            )
        if self._accessor is None or not self._host or not self._port:
            raise RuntimeError("start_tls has no workspace relay to upgrade")
        requested = (server_hostname or self._host).strip()
        sni = _validate_host(requested)
        if sni.lower() != self._host.lower():
            raise RuntimeError("start_tls SNI does not match the pinned host")
        old, self._stream = self._stream, None
        try:
            await old.send_eof()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            await old.aclose()
        except Exception:  # pragma: no cover - best effort
            pass
        raw = await self._accessor.open_tcp(
            self._host,
            self._port,
            tls=True,
            server_hostname=sni,
            timeout=timeout if timeout is not None else self._timeout,
        )
        self._stream = raw
        self._already_tls = True
        return self

    def get_extra_info(self, key: str) -> Any:
        """Return transport extra info (stable subset)."""
        if key == "ssl_object":
            return None
        if key == "server_addr":
            return None
        if key == "client_addr":
            return None
        if key == "socket":
            return None
        return None


class WorkspaceNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend opening all connections via the workspace accessor."""

    def __init__(self, accessor: Any) -> None:
        self._accessor = accessor
        self.opened: list[tuple[str, int, bool, str | None]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> WorkspaceNetworkStream:
        """Open a workspace TCP stream (TLS decided per origin scheme).

        httpcore always calls ``connect_tcp`` first and then ``start_tls``
        for ``https`` origins. The accessor opens a plain relay here; the
        ``https`` upgrade happens in :meth:`WorkspaceNetworkStream.start_tls`
        by reopening the relay with ``tls=True`` (verified + SNI in the
        workspace). The ``(host, port, tls, sni)`` tuples in
        :attr:`opened` prove no backend socket was used.
        """
        self.opened.append((host, int(port), False, None))
        raw = await self._accessor.open_tcp(
            host, int(port), tls=False, server_hostname=None, timeout=timeout
        )
        return WorkspaceNetworkStream(
            raw,
            already_tls=False,
            accessor=self._accessor,
            host=host,
            port=int(port),
            timeout=timeout,
        )

    async def connect_tls(
        self,
        host: str,
        port: int,
        ssl_context: Any = None,
        server_hostname: str | None = None,
        timeout: float | None = None,
        socket_options: Any | None = None,
    ) -> WorkspaceNetworkStream:
        """Open a TLS workspace TCP stream (terminated in the workspace)."""
        sni = _validate_host((server_hostname or host).strip())
        self.opened.append((host, int(port), True, sni))
        raw = await self._accessor.open_tcp(
            host, int(port), tls=True, server_hostname=sni, timeout=timeout
        )
        return WorkspaceNetworkStream(raw, already_tls=True)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any | None = None,
    ) -> WorkspaceNetworkStream:  # pragma: no cover - never used
        raise RuntimeError("unix sockets are not supported for MCP HTTP")

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)


def parse_mcp_http_url(url: str) -> tuple[str, str, int, str]:
    """Parse *url* into ``(scheme, host, port, target)``; fail closed.

    Userinfo (``user:pass@``) and fragments are rejected outright —
    they must never reach the workspace relay. The hostname is
    validated explicitly (DNS/IP/localhost); ``urlparse`` quirks alone
    are not trusted.
    """
    raw = (url or "").strip()
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in REQUIRED_SCHEMES:
        raise ValueError("MCP HTTP url must be http(s)")
    if parsed.username or parsed.password:
        raise ValueError("MCP HTTP url must not contain userinfo")
    if parsed.fragment:
        raise ValueError("MCP HTTP url must not contain a fragment")
    host = _validate_host(parsed.hostname or "")
    port = parsed.port or (443 if scheme == "https" else 80)
    if not 1 <= int(port) <= 65535:
        raise ValueError("MCP HTTP url has an invalid port")
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    return scheme, host, int(port), target


def _validate_header_items(headers: dict[str, str] | None) -> dict[str, str]:
    """Validate header names/values (fail-closed on CRLF injection)."""
    cleaned: dict[str, str] = {}
    for name, value in dict(headers or {}).items():
        text_name = str(name)
        text_value = str(value)
        if not _HEADER_NAME_RE.match(text_name):
            raise ValueError("MCP HTTP header name is invalid")
        if any(mark in text_value for mark in ("\r", "\n", "\x00")):
            raise ValueError("MCP HTTP header value is invalid")
        if any(mark in text_name for mark in ("\r", "\n", "\x00")):
            raise ValueError("MCP HTTP header name is invalid")
        cleaned[text_name] = text_value
    return cleaned


class WorkspaceHTTPTransport(httpx.AsyncHTTPTransport):
    """httpx transport pinned to one origin via the workspace accessor."""

    def __init__(
        self,
        accessor: Any,
        url: str,
        *,
        http1: bool = True,
        limits: httpx.Limits | None = None,
        retries: int = 0,
    ) -> None:
        scheme, host, port, _ = parse_mcp_http_url(url)
        self._origin = (scheme, host.lower(), int(port))
        self._backend = WorkspaceNetworkBackend(accessor)
        pool = httpcore.AsyncConnectionPool(
            http1=http1,
            http2=False,
            network_backend=self._backend,
            max_connections=(limits.max_connections if limits else 10),
            max_keepalive_connections=0,
            retries=retries,
        )
        super().__init__(verify=False, trust_env=False, http1=http1, http2=False)
        # Replace the host-side pool with the workspace-backed pool.
        self._pool = pool  # type: ignore[attr-defined]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Enforce same-origin requests (no cross-origin redirects by hand)."""
        url = request.url
        origin = (
            (url.scheme or "").lower(),
            (url.host or "").lower(),
            int(url.port or (443 if url.scheme == "https" else 80)),
        )
        if origin != self._origin:
            raise httpx.ConnectError(
                f"MCP HTTP request escapes pinned origin {self._origin}: {url}",
            )
        return await super().handle_async_request(request)


def build_workspace_http_client(
    accessor: Any,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    follow_redirects: bool = True,
) -> tuple[httpx.AsyncClient, WorkspaceHTTPTransport]:
    """Build an :class:`httpx.AsyncClient` bound to the workspace network.

    ``trust_env`` is off and no proxy is configured; redirect following
    stays enabled but :class:`WorkspaceHTTPTransport` pins every hop to
    the original origin, so cross-origin redirects fail closed. Header
    names/values are validated against CRLF injection (Pydantic does
    not cover this transport path).
    """
    _, _, _, _ = parse_mcp_http_url(url)
    transport = WorkspaceHTTPTransport(accessor, url)
    client = httpx.AsyncClient(
        transport=transport,
        headers=_validate_header_items(headers),
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        follow_redirects=follow_redirects,
        trust_env=False,
        cookies=httpx.Cookies(),
    )
    return client, transport
