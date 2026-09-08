"""Webfetch tool: HTTP(S) fetch with OpenCode-compatible conversion.

Downloads up to 5 MiB, converts HTML to markdown/text by default, and
relies on the registry's 50 KiB truncation + spill for model-facing
bounds. SSRF blocking of private/loopback/metadata hosts is an
OpenCuria overlay OpenCode does not have.
"""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import ATX, markdownify
from pydantic import BaseModel, ConfigDict, Field

from .base import Tool, ToolContext, ToolError, ToolResult

WEBFETCH_MAX_BYTES = 5 * 1024 * 1024
WEBFETCH_DEFAULT_TIMEOUT = 30.0
WEBFETCH_MAX_TIMEOUT = 120.0

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
HONEST_USER_AGENT = "opencuria"

_SKIP_TEXT_TAGS = frozenset(
    {"script", "style", "noscript", "iframe", "object", "embed"}
)

WebfetchFormat = Literal["text", "markdown", "html"]


class WebfetchArgs(BaseModel):
    """Arguments for the webfetch tool."""

    model_config = ConfigDict(extra="ignore")

    url: str = Field(description="The HTTP or HTTPS URL to fetch content from")
    format: WebfetchFormat = Field(
        default="markdown",
        description=(
            "The format to return the content in. Defaults to markdown."
        ),
    )
    timeout: float | None = Field(
        default=None,
        gt=0,
        le=WEBFETCH_MAX_TIMEOUT,
        description=(
            f"Optional timeout in seconds (maximum: {WEBFETCH_MAX_TIMEOUT:.0f})"
        ),
    )


#: Hostnames that always resolve to local/cloud-metadata targets.
WEBFETCH_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
    }
)

#: IP literal blocked as a cloud-metadata endpoint.
WEBFETCH_BLOCKED_IP = "169.254.169.254"


def _host_is_blocked(host: str) -> bool:
    """Return True when *host* is a localhost/metadata name."""
    normalized = (host or "").strip().lower().rstrip(".")
    if not normalized:
        return True
    if normalized in WEBFETCH_BLOCKED_HOSTS:
        return True
    if normalized == WEBFETCH_BLOCKED_IP:
        return True
    if normalized.endswith(".localhost"):
        return True
    return False


def _ip_is_blocked(address: ipaddress._BaseAddress) -> bool:
    """Return True for private/loopback/link-local/reserved IPs."""
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def is_blocked_url(url: str) -> bool:
    """Return True when *url* targets a blocked SSRF host.

    Checks the hostname blocklist, IP-literal classification, and a
    best-effort DNS lookup (``socket.gethostbyname``): a resolvable
    private IP blocks, an unresolvable name does not fail closed.
    """
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return True
    if _host_is_blocked(host):
        return True
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return _ip_is_blocked(literal)
    try:
        resolved = socket.gethostbyname(host)
    except (OSError, ValueError):
        return False
    if resolved == WEBFETCH_BLOCKED_IP:
        return True
    try:
        return _ip_is_blocked(ipaddress.ip_address(resolved))
    except ValueError:
        return False


def _assert_http_url(url: str) -> None:
    """Raise ToolError when *url* is not http(s)."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ToolError(
            f"URL must use http:// or https://: {url}",
            tool="webfetch",
        ) from exc
    if parsed.scheme not in {"http", "https"}:
        raise ToolError(
            f"URL must use http:// or https://: {url}",
            tool="webfetch",
        )


def _accept_header(fmt: WebfetchFormat) -> str:
    """Return the OpenCode Accept header for *fmt*."""
    if fmt == "markdown":
        return (
            "text/markdown;q=1.0, text/x-markdown;q=0.9, "
            "text/plain;q=0.8, text/html;q=0.7, */*;q=0.1"
        )
    if fmt == "text":
        return (
            "text/plain;q=1.0, text/markdown;q=0.9, "
            "text/html;q=0.8, */*;q=0.1"
        )
    return (
        "text/html;q=1.0, application/xhtml+xml;q=0.9, "
        "text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1"
    )


def _headers(fmt: WebfetchFormat, user_agent: str) -> dict[str, str]:
    """Build request headers for a webfetch call."""
    return {
        "User-Agent": user_agent,
        "Accept": _accept_header(fmt),
        "Accept-Language": "en-US,en;q=0.9",
    }


def _mime_from(content_type: str) -> str:
    """Return the MIME type from a Content-Type header."""
    return content_type.split(";", 1)[0].strip().lower()


def _is_image_attachment(mime: str) -> bool:
    """Return True for raster/binary image types (SVG stays text)."""
    return mime.startswith("image/") and mime not in {
        "image/svg+xml",
        "image/vnd.fastbidsheet",
    }


def _is_textual_mime(mime: str) -> bool:
    """Return True when *mime* is a text-like type OpenCode accepts."""
    return (
        not mime
        or mime.startswith("text/")
        or mime == "application/json"
        or mime.endswith("+json")
        or mime == "application/xml"
        or mime.endswith("+xml")
        or mime == "application/javascript"
        or mime == "application/x-javascript"
    )


def _declared_size(response: httpx.Response) -> int | None:
    """Parse Content-Length when it is a safe non-negative integer."""
    raw = response.headers.get("content-length")
    if not raw:
        return None
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return None
    if size < 0:
        return None
    return size


def _is_cloudflare_challenge(response: httpx.Response) -> bool:
    """Return True for a Cloudflare bot-management challenge response."""
    return (
        response.status_code == 403
        and response.headers.get("cf-mitigated") == "challenge"
    )


class _VisibleTextExtractor(HTMLParser):
    """Collect visible text, skipping script/style chrome like htmlparser2."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Increment skip depth for chrome tags and anything nested in them."""
        if self._skip_depth > 0 or tag in _SKIP_TEXT_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        """Decrement skip depth on any close while skipping."""
        if self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        """Append text that is not inside a skipped tag."""
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        """Return concatenated visible text."""
        return "".join(self._parts).strip()


def extract_text_from_html(html: str) -> str:
    """Return visible text from *html*, dropping script/style/chrome tags."""
    parser = _VisibleTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def convert_html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown with Turndown-equivalent options.

    Turndown ``remove`` drops script/style/meta/link *and* their
    contents. markdownify ``strip`` keeps the text, so those tags are
    decomposed first.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()
    return markdownify(
        str(soup),
        heading_style=ATX,
        bullets="-",
    )


def convert_fetched_content(
    content: str, content_type: str, fmt: WebfetchFormat
) -> str:
    """Convert a fetched body using OpenCode's HTML format rules."""
    if "text/html" not in content_type:
        return content
    if fmt == "markdown":
        return convert_html_to_markdown(content)
    if fmt == "text":
        return extract_text_from_html(content)
    return content


class WebfetchTool(Tool):
    """Fetch a URL and return it as text, markdown, or HTML."""

    name = "webfetch"
    description = (
        "Fetch content from an HTTP or HTTPS URL and return it as text, "
        "markdown, or HTML. Markdown is the default.\n\n"
        "Use a more targeted tool when one is available. This tool is "
        "read-only. Large text results may be replaced with a preview "
        "while the complete output is retained in managed storage."
    )
    args_schema: type[BaseModel] = WebfetchArgs
    permission_key = "webfetch"

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        """Optionally inject an httpx transport (tests use MockTransport)."""
        self._transport = transport

    def title(self, args: BaseModel) -> str:
        """Return a short title for a webfetch invocation."""
        assert isinstance(args, WebfetchArgs)
        return f"Fetch {args.url[:80]}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Fetch *url* via httpx without touching the workspace."""
        validated = self.coerce_args(args)
        assert isinstance(validated, WebfetchArgs)
        args = validated
        url = args.url.strip()
        _assert_http_url(url)
        if is_blocked_url(url):
            raise ToolError(
                f"Blocked private/internal URL: {url}",
                tool=self.name,
            )
        timeout = args.timeout if args.timeout is not None else WEBFETCH_DEFAULT_TIMEOUT
        client_kwargs: dict[str, object] = {
            "timeout": timeout,
            "follow_redirects": True,
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                raw, content_type = await self._fetch_body(
                    client, url, args.format
                )
        except ToolError:
            raise
        except httpx.TimeoutException as exc:
            raise ToolError("Request timed out", tool=self.name) from exc
        except Exception as exc:
            raise ToolError(
                f"Unable to fetch {url}",
                tool=self.name,
            ) from exc
        text = raw.decode("utf-8", errors="replace")
        output = convert_fetched_content(text, content_type, args.format)
        return ToolResult(
            output=output,
            metadata={
                "url": url,
                "contentType": content_type,
                "format": args.format,
                "size": len(raw),
            },
        )

    async def _fetch_body(
        self,
        client: httpx.AsyncClient,
        url: str,
        fmt: WebfetchFormat,
    ) -> tuple[bytes, str]:
        """GET *url*, retrying once on a Cloudflare challenge."""
        body = await self._stream_get(
            client, url, _headers(fmt, BROWSER_USER_AGENT)
        )
        if body is not None:
            return body
        retried = await self._stream_get(
            client, url, _headers(fmt, HONEST_USER_AGENT)
        )
        if retried is None:
            raise ToolError(f"Unable to fetch {url}", tool=self.name)
        return retried

    async def _stream_get(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> tuple[bytes, str] | None:
        """Stream one GET. Return None to signal a Cloudflare retry."""
        async with client.stream("GET", url, headers=headers) as response:
            if (
                _is_cloudflare_challenge(response)
                and headers.get("User-Agent") != HONEST_USER_AGENT
            ):
                return None
            return await self._consume_response(response)

    async def _consume_response(
        self, response: httpx.Response
    ) -> tuple[bytes, str]:
        """Validate the final URL/MIME and read a size-capped body."""
        final_url = str(response.url)
        scheme = str(response.url.scheme or "").lower()
        if scheme not in {"http", "https"}:
            raise ToolError(
                f"URL must use http:// or https://: {final_url}",
                tool=self.name,
            )
        if is_blocked_url(final_url):
            raise ToolError(
                f"Blocked private/internal URL: {final_url}",
                tool=self.name,
            )
        content_type = response.headers.get("content-type") or ""
        mime = _mime_from(content_type)
        if _is_image_attachment(mime):
            raise ToolError(
                f"Unsupported fetched image content type: {mime}",
                tool=self.name,
            )
        if not _is_textual_mime(mime):
            raise ToolError(
                f"Unsupported fetched file content type: {mime}",
                tool=self.name,
            )
        declared = _declared_size(response)
        if declared is not None and declared > WEBFETCH_MAX_BYTES:
            raise ToolError(
                f"Response too large (exceeds {WEBFETCH_MAX_BYTES} byte limit)",
                tool=self.name,
            )
        response.raise_for_status()
        chunks: list[bytes] = []
        buffered = 0
        async for chunk in response.aiter_bytes(65536):
            buffered += len(chunk)
            if buffered > WEBFETCH_MAX_BYTES:
                raise ToolError(
                    f"Response too large (exceeds {WEBFETCH_MAX_BYTES} byte limit)",
                    tool=self.name,
                )
            chunks.append(chunk)
        return b"".join(chunks), content_type
