"""Public-only outbound connections: address policy, resolution and pinning."""

from __future__ import annotations

import ipaddress
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import httpx
import pytest

from common.public_egress import (
    NonPublicAddressError,
    PinnedBackend,
    UnresolvableHostError,
    async_pinned_transport,
    is_public_address,
    resolve_public_addresses,
)


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"],
)
def test_global_unicast_addresses_are_public(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.100.100.200",  # carrier-grade NAT, e.g. Alibaba Cloud metadata
        "0.0.0.0",
        "255.255.255.255",
        "224.0.0.1",
        "239.1.1.1",
        "198.18.0.1",
        "::1",
        "::",
        "fe80::1",
        "fd00::1",
        "ff02::1",
        "ff0e::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "2002:a00:1::",  # 6to4 of 10.0.0.1
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo of 10.0.0.1
    ],
)
def test_private_local_and_special_addresses_are_not_public(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is False


def test_resolution_returns_checked_unique_addresses(
    fake_dns: dict[str, list[str]],
) -> None:
    fake_dns["api.example"] = ["8.8.8.8", "::ffff:8.8.8.8", "2606:4700::1111"]
    assert resolve_public_addresses("api.example", 443) == (
        "8.8.8.8",
        "2606:4700::1111",
    )


@pytest.mark.parametrize("private", ["127.0.0.1", "10.0.0.7", "::1", "fd00::2"])
def test_one_private_answer_rejects_the_host(
    fake_dns: dict[str, list[str]], private: str
) -> None:
    """A rebinding name cannot hide a private answer behind a public one."""
    fake_dns["rebind.example"] = ["8.8.8.8", private]
    with pytest.raises(NonPublicAddressError):
        resolve_public_addresses("rebind.example", 443)


@pytest.mark.parametrize(
    "host", ["127.1", "2130706433", "0x7f000001", "::ffff:7f00:1", "localhost"]
)
def test_every_loopback_notation_is_rejected(
    fake_dns: dict[str, list[str]], host: str
) -> None:
    with pytest.raises(NonPublicAddressError):
        resolve_public_addresses(host, 80)


def test_resolver_failure_fails_closed(fake_dns: dict[str, list[str]]) -> None:
    fake_dns["missing.example"] = []
    with pytest.raises(UnresolvableHostError):
        resolve_public_addresses("missing.example", 443)


@pytest.fixture
def host_echo_server() -> Iterator[int]:
    """Serve on 127.0.0.1 and answer with the request's Host header."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = (self.headers.get("Host") or "").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


async def test_async_transport_connects_to_the_checked_address_only(
    host_echo_server: int,
) -> None:
    """The name is never resolved again; other hosts cannot use the pin."""
    port = host_echo_server
    transport = async_pinned_transport("pinned.test", ("127.0.0.1",))
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get(f"http://pinned.test:{port}/")
        assert response.text == f"pinned.test:{port}"
        with pytest.raises(httpx.ConnectError, match="pinned host"):
            await client.get(f"http://other.test:{port}/")


async def test_async_transport_serves_concurrent_requests_when_allowed(
    host_echo_server: int,
) -> None:
    """An open streaming response must not block the next request."""
    port = host_echo_server
    url = f"http://pinned.test:{port}/"
    transport = async_pinned_transport("pinned.test", ("127.0.0.1",), max_connections=2)
    timeout = httpx.Timeout(5.0, pool=1.0)
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        async with client.stream("GET", url):
            assert (await client.get(url)).text == f"pinned.test:{port}"


def test_sync_backend_rejects_other_hosts_and_local_sockets() -> None:
    backend = PinnedBackend("pinned.test", ("127.0.0.1",))
    with pytest.raises(httpcore.ConnectError, match="pinned host"):
        backend.connect_tcp("other.test", 80)
    with pytest.raises(httpcore.ConnectError, match="Local sockets"):
        backend.connect_unix_socket("/run/docker.sock")


def test_pin_requires_checked_addresses() -> None:
    with pytest.raises(ValueError):
        PinnedBackend("pinned.test", ())
