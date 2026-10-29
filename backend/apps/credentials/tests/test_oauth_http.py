"""Characterize the bounded HTTP boundary used by MCP OAuth workflows."""

from __future__ import annotations

import ssl
import threading
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from apps.credentials import oauth_http
from apps.credentials.oauth_errors import OAuthError


@pytest.fixture
def http_boundary(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Keep real response parsing while replacing DNS and the network client."""
    monkeypatch.setattr(
        oauth_http, "_public_addresses", lambda host, port: ("93.184.216.34",)
    )
    handler = MagicMock()
    client_class = httpx.Client

    def make_client(**kwargs: object) -> httpx.Client:
        handler.client = client_class(**kwargs)
        return handler.client

    constructor = MagicMock(side_effect=make_client)
    monkeypatch.setattr(oauth_http.httpx, "Client", constructor)
    monkeypatch.setattr(
        oauth_http.httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(handler)
    )
    handler.constructor = constructor
    return handler


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b'{"value": 1}', {"value": 1}),
        (b"[]", []),
        (b"", None),
        (b"invalid-json", None),
        (b"\xff", None),
    ],
)
def test_request_preserves_status_headers_and_json_body(
    http_boundary: MagicMock, raw: bytes, expected: object
) -> None:
    response = httpx.Response(
        401, headers={"X-Test": "value"}, stream=httpx.ByteStream(raw)
    )
    http_boundary.return_value = response

    status, headers, body = oauth_http.request(
        "POST",
        "https://auth.example/token",
        data={"code": "example"},
        headers={"Authorization": "Basic example"},
    )

    assert (status, headers["x-test"], body) == (401, "value", expected)
    options = http_boundary.constructor.call_args.kwargs
    assert options["trust_env"] is False
    assert options["follow_redirects"] is False
    assert options["timeout"] == oauth_http.HTTP_TIMEOUT
    request = http_boundary.call_args.args[0]
    assert request.method == "POST"
    assert str(request.url) == "https://auth.example/token"
    assert request.content == b"code=example"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Authorization"] == "Basic example"
    assert response.is_closed
    assert http_boundary.client.is_closed


@pytest.mark.parametrize("extra", [0, 1])
def test_response_limit_and_resource_cleanup(
    http_boundary: MagicMock, extra: int
) -> None:
    response = httpx.Response(
        200,
        stream=httpx.ByteStream(b" " * (oauth_http.MAX_RESPONSE - 4 + extra) + b"null"),
    )
    http_boundary.return_value = response

    if extra:
        with pytest.raises(OAuthError, match="OAuth endpoint response is too large"):
            oauth_http.request("GET", "https://auth.example/metadata")
    else:
        assert oauth_http.request("GET", "https://auth.example/metadata")[2] is None

    assert response.is_closed
    assert http_boundary.client.is_closed


def test_network_errors_are_sanitized_and_client_is_closed(
    http_boundary: MagicMock,
) -> None:
    http_boundary.side_effect = RuntimeError("secret transport detail")
    with pytest.raises(OAuthError, match="^OAuth endpoint request failed$") as error:
        oauth_http.request("GET", "https://auth.example/metadata")
    assert error.value.__suppress_context__
    assert http_boundary.client.is_closed


@pytest.mark.parametrize(
    "private", ["127.0.0.1", "10.0.0.1", "::1", "::ffff:127.0.0.1"]
)
def test_dns_rejects_mixed_public_and_private_answers(
    monkeypatch: pytest.MonkeyPatch, private: str
) -> None:
    monkeypatch.setattr(
        oauth_http.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", (address, 443)) for address in ("93.184.216.34", private)
        ],
    )
    with pytest.raises(OAuthError, match="private or local"):
        oauth_http.validate_endpoint("https://auth.example/token")


def test_pinned_request_uses_checked_ip_and_disables_redirects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise HTTPTransport + the real pinned backend over local TLS."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mcp.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(dt_timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(dt_timezone.utc) + timedelta(minutes=5))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("mcp.test")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "server.pem"
    key_file = tmp_path / "server-key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", "https://127.0.0.1/collect")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert_file), str(key_file))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_create_default_context = ssl.create_default_context

    def trusted_default_context(
        *,
        cafile: str | None = None,
        capath: str | None = None,
        cadata: str | bytes | None = None,
    ) -> ssl.SSLContext:
        return original_create_default_context(
            cafile=str(cert_file), capath=capath, cadata=cadata
        )

    monkeypatch.setattr(
        oauth_http.ssl, "create_default_context", trusted_default_context
    )
    monkeypatch.setattr(
        oauth_http, "_public_addresses", lambda host, port: ("127.0.0.1",)
    )
    try:
        status, headers, _ = oauth_http.request(
            "GET", f"https://mcp.test:{server.server_port}/mcp"
        )
        assert status == 302
        assert headers["location"] == "https://127.0.0.1/collect"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
