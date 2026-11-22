"""Backend-side HTTP for MCP servers that carry an OAuth bearer token.

OAuth tokens are server-side credentials (``CredentialType.MCP_OAUTH``), so
the requests that carry them never pass through the workspace. The backend
connects itself, only to the public addresses checked when the MCP connection
opens (:mod:`common.public_egress`). TLS verifies the certificate against the
configured host, and every request stays on the configured HTTPS origin.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from common.public_egress import (
    EgressError,
    async_pinned_transport,
    resolve_public_addresses,
)

from .workspace_http import parse_mcp_http_url, request_origin, validate_header_items

#: Concurrent connections per client: the SSE or streamable-HTTP GET stream
#: stays open while tool calls POST on further connections.
MAX_CONNECTIONS = 10


class PublicEndpointError(ValueError):
    """The MCP server is not a public HTTPS endpoint."""


@dataclass(frozen=True)
class PublicOrigin:
    """One HTTPS origin and the public addresses checked for it."""

    host: str
    port: int
    addresses: tuple[str, ...]


async def resolve_public_origin(url: str) -> PublicOrigin:
    """Resolve *url*'s host once and require HTTPS and public addresses only.

    Fails closed on resolver errors and on a single non-public answer.
    """
    try:
        scheme, host, port, _ = parse_mcp_http_url(url)
    except ValueError:
        raise PublicEndpointError("MCP server URL is invalid") from None
    if scheme != "https":
        raise PublicEndpointError("OAuth MCP servers must use HTTPS")
    try:
        addresses = await asyncio.to_thread(resolve_public_addresses, host, port)
    except EgressError as exc:
        raise PublicEndpointError(str(exc)) from None
    return PublicOrigin(host=host.lower(), port=port, addresses=addresses)


class _PinnedOriginTransport(httpx.AsyncBaseTransport):
    """Send requests for one origin only, to its checked addresses only."""

    def __init__(self, origin: PublicOrigin) -> None:
        self._origin = ("https", origin.host, origin.port)
        self._inner = async_pinned_transport(
            origin.host, origin.addresses, max_connections=MAX_CONNECTIONS
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Reject every request that leaves the pinned origin."""
        if request_origin(request.url) != self._origin:
            raise httpx.ConnectError(
                f"MCP HTTP request escapes pinned origin {self._origin}"
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        """Close the pinned connection pool."""
        await self._inner.aclose()


def build_public_http_client(
    origin: PublicOrigin,
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
) -> httpx.AsyncClient:
    """Build a backend client bound to *origin*'s checked addresses.

    No redirects, no proxies from the environment and no cookie jar shared
    across clients. Header names and values are checked against CRLF
    injection like on the workspace transport.
    """
    return httpx.AsyncClient(
        transport=_PinnedOriginTransport(origin),
        headers=validate_header_items(headers),
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        follow_redirects=False,
        trust_env=False,
        cookies=httpx.Cookies(),
    )
