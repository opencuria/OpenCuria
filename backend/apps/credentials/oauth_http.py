"""Bounded, DNS-pinned HTTPS transport for outbound MCP OAuth requests.

This synchronous boundary owns endpoint resolution, connections and response
limits. OAuth discovery, client authentication and grant policy live in callers;
no settings, credentials or database state are needed here.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx

from .oauth_errors import OAuthError

HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_RESPONSE = 256 * 1024


def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve once, reject non-global answers, and pin requests to checked IPs."""
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise OAuthError("OAuth endpoint host could not be resolved") from None
    addresses = []
    for result in results:
        try:
            address = ipaddress.ip_address(result[4][0])
        except ValueError:
            raise OAuthError("OAuth endpoint DNS response is invalid") from None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise OAuthError("OAuth endpoints cannot target private or local addresses")
        addresses.append(str(address))
    if not addresses:
        raise OAuthError("OAuth endpoint host could not be resolved")
    return tuple(dict.fromkeys(addresses))


def validate_endpoint(
    url: str, *, oauth_server_url: bool = False
) -> tuple[str, tuple[str, ...]]:
    """Validate HTTPS syntax and resolve only public addresses before connecting."""
    if not isinstance(url, str) or not url or any(ord(char) < 0x21 for char in url):
        raise OAuthError("OAuth endpoint URL is invalid")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or "@" in parsed.netloc
        or parsed.fragment
        or (oauth_server_url and (parsed.query or "?" in url or "#" in url))
    ):
        raise OAuthError("OAuth endpoints must be valid HTTPS URLs")
    try:
        port = parsed.port or 443
    except ValueError:
        raise OAuthError("OAuth endpoint URL is invalid") from None
    if not 1 <= port <= 65535:
        raise OAuthError("OAuth endpoint URL is invalid")
    addresses = _public_addresses(parsed.hostname, port)
    return url, addresses


class _PinnedBackend(httpcore.NetworkBackend):
    """httpcore backend avoiding a second, untrusted DNS lookup."""

    def __init__(self, host: str, addresses: tuple[str, ...]) -> None:
        self.host = host.lower()
        self.addresses = addresses

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> httpcore.NetworkStream:
        """Connect to a checked address without resolving the hostname again."""
        if host.lower() != self.host:
            raise httpcore.ConnectError("OAuth request escaped its pinned host")
        last_error = None
        for address in self.addresses:
            try:
                sock = socket.create_connection(
                    (address, port),
                    timeout,
                    source_address=(local_address, 0) if local_address else None,
                )
                for option in socket_options or ():
                    sock.setsockopt(*option)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                from httpcore._backends.sync import SyncStream

                return SyncStream(sock)
            except OSError as exc:
                last_error = exc
        raise httpcore.ConnectError("OAuth endpoint connection failed") from last_error

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> httpcore.NetworkStream:
        """Reject local socket connections for outbound OAuth requests."""
        raise httpcore.ConnectError("OAuth endpoints cannot use local sockets")


def request(
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    """Bounded pinned HTTP; no redirects/proxies and sanitized transport errors."""
    _, addresses = validate_endpoint(url)
    parsed = urlparse(url)
    transport = httpx.HTTPTransport(trust_env=False)
    # httpx lacks a public custom-network-backend hook; replace its core pool
    # explicitly so DNS is not looked up again after address validation.
    transport._pool = httpcore.ConnectionPool(
        network_backend=_PinnedBackend(parsed.hostname, addresses),
        max_connections=1,
        max_keepalive_connections=0,
        ssl_context=ssl.create_default_context(),
    )
    try:
        with httpx.Client(
            transport=transport,
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                method,
                url,
                data=data,
                json=json_body,
                headers={"Accept": "application/json", **(headers or {})},
            ) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise OAuthError("OAuth endpoint response is too large")
                body = None
                if raw:
                    try:
                        body = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        body = None
                return response.status_code, dict(response.headers), body
    except OAuthError:
        raise
    except Exception:
        raise OAuthError("OAuth endpoint request failed") from None
