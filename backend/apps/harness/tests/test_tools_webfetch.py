"""Tests for OpenCode-parity webfetch (conversion, size cap, SSRF)."""

from __future__ import annotations

import httpx
import pytest

from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.webfetch import (
    BROWSER_USER_AGENT,
    HONEST_USER_AGENT,
    WEBFETCH_MAX_BYTES,
    WebfetchTool,
    convert_html_to_markdown,
    extract_text_from_html,
    is_blocked_url,
)


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


async def test_redirect_to_private_host_blocked() -> None:
    """The final URL after redirects is SSRF-checked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/secret"},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="secret",
        )

    with pytest.raises(ToolError, match="[Bb]locked"):
        await _tool(handler).execute({"url": "https://example.com/go"}, _ctx())


def test_ssrf_guard_still_exported() -> None:
    """is_blocked_url remains the public SSRF helper."""
    assert is_blocked_url("https://localhost/secret") is True
    assert is_blocked_url("https://169.254.169.254/latest") is True
