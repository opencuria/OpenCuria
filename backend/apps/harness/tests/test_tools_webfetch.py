"""Tests for OpenCode-parity webfetch (conversion, size cap, SSRF)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import webfetch
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.webfetch import (
    BROWSER_USER_AGENT,
    HONEST_USER_AGENT,
    WEBFETCH_MAX_BYTES,
    WEBFETCH_MAX_REDIRECTS,
    WebfetchTool,
    convert_html_to_markdown,
    extract_text_from_html,
)

pytestmark = pytest.mark.usefixtures("fake_dns")


def _ctx(accessor: FakeAccessor | None = None) -> ToolContext:
    return ToolContext(
        session_id="sess-1",
        workspace_id="ws-1",
        accessor=accessor or FakeAccessor(files={}),
    )


def _tool(
    handler: object,
) -> WebfetchTool:
    return WebfetchTool(transport=httpx.MockTransport(handler))


SAMPLE_HTML = (
    "<html><head><style>.hidden{}</style><script>alert('x')</script>"
    "<meta charset='utf-8'><link rel='stylesheet' href='x.css'></head>"
    "<body><h1>Hello</h1><p>world</p><ul><li>one</li></ul></body></html>"
)
TEXT_HTML = (
    "<html><head><style>.hidden{}</style><script>alert('x')</script></head>"
    "<body>Hello <b>world</b></body></html>"
)


def test_extract_text_from_html_skips_chrome() -> None:
    """Visible text drops script/style content."""
    assert extract_text_from_html(TEXT_HTML) == "Hello world"


def test_convert_html_to_markdown_keeps_structure() -> None:
    """Markdown conversion keeps headings/lists and drops scripts."""
    markdown = convert_html_to_markdown(SAMPLE_HTML)
    assert "alert" not in markdown
    assert ".hidden" not in markdown
    assert "Hello" in markdown
    assert "world" in markdown
    assert "one" in markdown


async def test_default_format_converts_html_to_markdown() -> None:
    """Default format is markdown and converts text/html bodies."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("accept", "").startswith("text/markdown")
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=SAMPLE_HTML,
        )

    result = await _tool(handler).execute(
        {"url": "https://example.com/page"}, _ctx()
    )
    assert "alert" not in result.output
    assert "Hello" in result.output
    assert result.metadata["format"] == "markdown"
    assert "text/html" in result.metadata["contentType"]


async def test_format_text_extracts_visible_text() -> None:
    """format=text returns visible HTML text without scripts."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=TEXT_HTML,
        )

    result = await _tool(handler).execute(
        {"url": "https://example.com/page", "format": "text"}, _ctx()
    )
    assert result.output == "Hello world"
    assert result.metadata["format"] == "text"


async def test_format_html_returns_raw_html() -> None:
    """format=html keeps the raw document."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=SAMPLE_HTML,
        )

    result = await _tool(handler).execute(
        {"url": "https://example.com/page", "format": "html"}, _ctx()
    )
    assert result.output == SAMPLE_HTML
    assert "<script>" in result.output


async def test_non_html_body_returned_unchanged() -> None:
    """Non-HTML text is not converted."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="hello from webfetch",
        )

    result = await _tool(handler).execute(
        {"url": "https://example.com/file.txt", "format": "markdown"}, _ctx()
    )
    assert result.output == "hello from webfetch"


async def test_http_urls_are_allowed() -> None:
    """http:// URLs fetch (OpenCode Core parity)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("http://")
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="ok",
        )

    result = await _tool(handler).execute(
        {"url": "http://example.com/x"}, _ctx()
    )
    assert result.output == "ok"


async def test_rejects_non_http_schemes() -> None:
    """ftp:// and missing schemes are rejected before the network."""
    with pytest.raises(ToolError, match="http:// or https://"):
        await WebfetchTool().execute({"url": "ftp://example.com/x"}, _ctx())
    with pytest.raises(ToolError, match="http:// or https://"):
        await WebfetchTool().execute({"url": "example.com"}, _ctx())


async def test_content_length_over_cap_rejected() -> None:
    """Content-Length above 5 MiB fails without buffering the body."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/plain",
                "content-length": str(WEBFETCH_MAX_BYTES + 1),
            },
            content=b"x",
        )

    with pytest.raises(ToolError, match="too large"):
        await _tool(handler).execute({"url": "https://example.com/big"}, _ctx())


async def test_streamed_body_over_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A streamed body larger than 5 MiB is aborted when Content-Length is absent."""
    monkeypatch.setattr(
        "apps.harness.tools.webfetch._declared_size", lambda _response: None
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"x" * (WEBFETCH_MAX_BYTES + 1),
        )

    with pytest.raises(ToolError, match="too large"):
        await _tool(handler).execute({"url": "https://example.com/stream"}, _ctx())


async def test_image_mime_rejected() -> None:
    """Raster images are unsupported (SVG stays textual)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"\x89PNG",
        )

    with pytest.raises(ToolError, match="image/png"):
        await _tool(handler).execute({"url": "https://example.com/a.png"}, _ctx())


async def test_octet_stream_rejected() -> None:
    """Non-textual MIME types are rejected."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"\x00\x01",
        )

    with pytest.raises(ToolError, match="octet-stream"):
        await _tool(handler).execute({"url": "https://example.com/bin"}, _ctx())


async def test_svg_stays_text() -> None:
    """image/svg+xml is treated as text, not an image attachment."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/svg+xml; charset=UTF-8"},
            text='<svg xmlns="http://www.w3.org/2000/svg"><text>hello</text></svg>',
        )

    result = await _tool(handler).execute(
        {"url": "https://example.com/image.svg", "format": "html"}, _ctx()
    )
    assert "<svg" in result.output


async def test_cloudflare_challenge_retries_with_honest_ua() -> None:
    """A CF challenge retries once with User-Agent opencuria."""
    agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("user-agent") or ""
        agents.append(ua)
        if ua != HONEST_USER_AGENT:
            return httpx.Response(
                403,
                headers={
                    "cf-mitigated": "challenge",
                    "content-type": "text/plain",
                },
                text="blocked",
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="ok",
        )

    result = await _tool(handler).execute(
        {"url": "https://example.com/cf"}, _ctx()
    )
    assert result.output == "ok"
    assert agents[0] == BROWSER_USER_AGENT
    assert agents[1] == HONEST_USER_AGENT


async def test_max_size_arg_ignored_large_html_succeeds() -> None:
    """Legacy max_size is ignored; a 20 KiB HTML page still converts."""
    body = "<html><body>" + ("<p>readme line</p>" * 1500) + "</body></html>"
    assert len(body.encode("utf-8")) > 15000

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=body,
        )

    result = await _tool(handler).execute(
        {"url": "https://github.com/ahujasid/blender-mcp", "max_size": 15000},
        _ctx(),
    )
    assert "readme line" in result.output
    assert "<p>" not in result.output or "readme" in result.output


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"blocked URL reached the network: {request.url}")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/secret",
        "http://127.0.0.1:8000/",
        "http://2130706433/",
        "http://10.1.2.3/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://[fd00:ec2::254]/",
        "http://[64:ff9b::a9fe:a9fe]/",
    ],
)
async def test_private_targets_blocked_before_connecting(url: str) -> None:
    """Every notation of a private target fails before any request is sent."""
    with pytest.raises(ToolError, match="Blocked private/internal URL"):
        await _tool(_never_called).execute({"url": url}, _ctx())


async def test_name_with_any_private_answer_blocked(
    fake_dns: dict[str, list[str]],
) -> None:
    """DNS rebinding: one private A/AAAA answer blocks the whole name."""
    fake_dns["rebind.example"] = ["93.184.216.34", "::1"]
    with pytest.raises(ToolError, match="Blocked private/internal URL"):
        await _tool(_never_called).execute(
            {"url": "https://rebind.example/"}, _ctx()
        )


async def test_unresolvable_host_fails_closed(
    fake_dns: dict[str, list[str]],
) -> None:
    fake_dns["metadata.google.internal"] = []
    with pytest.raises(ToolError, match="could not be resolved"):
        await _tool(_never_called).execute(
            {"url": "http://metadata.google.internal/computeMetadata/v1/"}, _ctx()
        )


async def test_redirect_hop_to_private_host_blocked_before_request() -> None:
    """Each redirect hop is checked before it connects, not after."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    with pytest.raises(ToolError, match="Blocked private/internal URL"):
        await _tool(handler).execute({"url": "https://example.com/go"}, _ctx())
    assert requested == ["https://example.com/go"]


async def test_public_redirects_are_followed() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/moved"})
        if request.url.path == "/moved":
            return httpx.Response(
                307, headers={"location": "https://docs.example/final"}
            )
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    result = await _tool(handler).execute({"url": "https://example.com/a"}, _ctx())
    assert result.output == "ok"
    assert requested == [
        "https://example.com/a",
        "https://example.com/moved",
        "https://docs.example/final",
    ]


async def test_redirect_to_other_scheme_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    with pytest.raises(ToolError, match="http:// or https://"):
        await _tool(handler).execute({"url": "https://example.com/"}, _ctx())


async def test_redirect_loop_is_bounded() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"location": f"/{len(hops)}"})

    with pytest.raises(ToolError, match="Too many redirects"):
        await _tool(handler).execute({"url": "https://example.com/"}, _ctx())
    assert len(hops) == WEBFETCH_MAX_REDIRECTS + 1


@pytest.fixture
def local_page() -> Iterator[int]:
    """Serve a text page on 127.0.0.1 that echoes the request's Host header."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = f"host={self.headers.get('Host')}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
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


async def test_default_transport_connects_to_the_checked_address(
    monkeypatch: pytest.MonkeyPatch, local_page: int
) -> None:
    """Without an injected transport the request uses the pinned addresses.

    ``pinned.test`` has no DNS record; the page is only reachable because the
    connection goes to the address the check returned, with the original Host.
    """
    checked: list[tuple[str, int]] = []

    def resolve(host: str, port: int) -> tuple[str, ...]:
        checked.append((host, port))
        return ("127.0.0.1",)

    monkeypatch.setattr(webfetch, "resolve_public_addresses", resolve)
    url = f"http://pinned.test:{local_page}/page"
    result = await WebfetchTool().execute({"url": url}, _ctx())
    assert result.output == f"host=pinned.test:{local_page}"
    assert checked == [("pinned.test", local_page)]
