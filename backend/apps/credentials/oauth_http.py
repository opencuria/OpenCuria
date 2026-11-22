"""Bounded, DNS-pinned HTTPS transport for outbound MCP OAuth requests.

This synchronous boundary owns endpoint resolution, connections and response
limits. OAuth discovery, client authentication and grant policy live in callers;
no settings, credentials or database state are needed here.
"""

from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx

from common.public_egress import (
    NonPublicAddressError,
    PinnedBackend,
    UnresolvableHostError,
    resolve_public_addresses,
)

from .oauth_errors import OAuthError

HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_RESPONSE = 256 * 1024


def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve once, reject non-public answers, and pin requests to checked IPs."""
    try:
        return resolve_public_addresses(host, port)
    except NonPublicAddressError:
        raise OAuthError(
            "OAuth endpoints cannot target private or local addresses"
        ) from None
    except UnresolvableHostError:
        raise OAuthError("OAuth endpoint host could not be resolved") from None


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
        network_backend=PinnedBackend(parsed.hostname, addresses),
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
