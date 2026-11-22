"""Outbound connections restricted to public internet addresses.

Resolve a host once with :func:`resolve_public_addresses`, then pin every
connection to the checked addresses (:class:`PinnedBackend`,
:class:`AsyncPinnedBackend`, :func:`async_pinned_transport`). The HTTP client
never resolves the name again, so DNS rebinding cannot swap the target after
the check. Callers follow redirects themselves and check every hop.

Leaf module: no settings, ORM or application imports.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Iterable

import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: NAT64 well-known prefix (RFC 6052): translators reach the embedded IPv4.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class EgressError(Exception):
    """An outbound destination is unresolvable or not public."""


class UnresolvableHostError(EgressError):
    """The host has no usable address."""


class NonPublicAddressError(EgressError):
    """The host resolves to at least one private, local or special address."""


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return the IPv4 destination a 6to4, Teredo or NAT64 address reaches."""
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in _NAT64_PREFIX:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def is_public_address(address: IPAddress) -> bool:
    """Return True only for globally routable unicast addresses.

    IPv4-mapped IPv6 is judged as its IPv4 address; transition addresses must
    be public themselves and embed a public IPv4 address.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public_address(address.ipv4_mapped)
        embedded = _embedded_ipv4(address)
        if embedded is not None and not is_public_address(embedded):
            return False
    return address.is_global and not address.is_multicast


def resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve *host* once and return its addresses if every answer is public.

    Fails closed: resolver errors, invalid answers and a single non-public
    answer among public ones all raise :class:`EgressError`. IP literals go
    through the same resolver, so every notation is judged by its address.
    This call blocks; async callers run it in a worker thread.
    """
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        raise UnresolvableHostError(f"Host {host!r} could not be resolved") from None
    addresses: list[str] = []
    for result in results:
        try:
            address = ipaddress.ip_address(result[4][0])
        except ValueError:
            raise UnresolvableHostError(
                f"Host {host!r} returned an invalid address"
            ) from None
        if not is_public_address(address):
            raise NonPublicAddressError(
                f"Host {host!r} resolves to a private or local address"
            )
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        addresses.append(str(address))
    if not addresses:
        raise UnresolvableHostError(f"Host {host!r} could not be resolved")
    return tuple(dict.fromkeys(addresses))


def _pinned(host: str, addresses: Iterable[str]) -> tuple[str, tuple[str, ...]]:
    """Normalize a pin; an empty address list is a programming error."""
    checked = tuple(addresses)
    if not host or not checked:
        raise ValueError("A pinned connection needs a host and checked addresses")
    return host.lower(), checked


class PinnedBackend(httpcore.NetworkBackend):
    """Sync httpcore backend that connects only to pre-checked addresses."""

    def __init__(self, host: str, addresses: Iterable[str]) -> None:
        self.host, self.addresses = _pinned(host, addresses)
        self._inner = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        """Connect to a checked address without resolving *host* again."""
        if host.lower() != self.host:
            raise httpcore.ConnectError("Connection escaped its pinned host")
        last_error: Exception | None = None
        for address in self.addresses:
            try:
                return self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise httpcore.ConnectError("Pinned connection failed") from last_error

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        """Reject local sockets."""
        raise httpcore.ConnectError("Local sockets are not allowed")

    def sleep(self, seconds: float) -> None:
        """Delegate retry back-off to the default backend."""
        self._inner.sleep(seconds)


class AsyncPinnedBackend(httpcore.AsyncNetworkBackend):
    """Async httpcore backend that connects only to pre-checked addresses."""

    def __init__(self, host: str, addresses: Iterable[str]) -> None:
        self.host, self.addresses = _pinned(host, addresses)
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Connect to a checked address without resolving *host* again."""
        if host.lower() != self.host:
            raise httpcore.ConnectError("Connection escaped its pinned host")
        last_error: Exception | None = None
        for address in self.addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise httpcore.ConnectError("Pinned connection failed") from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Reject local sockets."""
        raise httpcore.ConnectError("Local sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        """Delegate retry back-off to the default backend."""
        await self._inner.sleep(seconds)


def async_pinned_transport(
    host: str,
    addresses: Iterable[str],
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> httpx.AsyncHTTPTransport:
    """Return a single-connection httpx transport pinned to checked addresses.

    TLS still verifies the certificate and SNI against *host*. Proxies from
    the environment are ignored because a proxy would resolve the name itself.
    """
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    # httpx has no public hook for a custom network backend; replace its core
    # pool so the hostname is never resolved again after the address check.
    transport._pool = httpcore.AsyncConnectionPool(  # type: ignore[attr-defined]
        network_backend=AsyncPinnedBackend(host, addresses),
        ssl_context=ssl_context or httpx.create_ssl_context(),
        max_connections=1,
        max_keepalive_connections=0,
    )
    return transport
