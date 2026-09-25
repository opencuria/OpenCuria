"""Per-server MCP connections: discovery, tools, and normalized calls.

One :class:`McpServerConnection` owns exactly one SDK
:class:`~mcp.client.session.ClientSession` for one plugin MCP server
within one harness run. Discovery paginates ``list_tools`` (cursor,
duplicate guard, caps); tool calls are serialized per server with an
``anyio.Lock`` while different servers run in parallel.

A server-level failure never takes down the run: the server is skipped
and a structured health warning is logged; only missing *required*
credentials fail the run before any provider call (configured in
:mod:`apps.plugins.runtime`).
"""

from __future__ import annotations

import asyncio
import json
import math
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import anyio
import httpx
import mcp.types as types
import structlog
from mcp.client.session import ClientSession
from pydantic import BaseModel, ConfigDict

from ..tools.base import Tool, ToolContext, ToolError, ToolResult
from .naming import TOOL_NAME_RE, mcp_tool_name
from .stdio import workspace_stdio_client
from .workspace_http import build_workspace_http_client, parse_mcp_http_url

log = structlog.get_logger(__name__)

#: Discovery caps (fail-closed, bounded memory).
MAX_DISCOVERY_PAGES = 1000
MAX_TOOLS_PER_SERVER = 256
MAX_SCHEMA_SERIALIZED_BYTES = 256 * 1024

#: Hard cap for decoded MCP image bytes (never unbounded into the model).
# Fits both the 8 MiB stdio line cap and 8M-character persisted attachment cap.
MAX_MCP_IMAGE_BYTES = 5 * 1024 * 1024

#: Marker for MCP-backed tools (permission key + evaluator rules).
MCP_TOOL_PREFIX = "mcp_"


class McpServerHealthError(RuntimeError):
    """Raised when a server cannot be discovered/used (skipped, not fatal)."""


def _safe_diagnostics(accessor: Any) -> dict[str, Any]:
    """Best-effort secret-free stream diagnostics (never raises)."""
    get_diagnostics = getattr(accessor, "get_stream_diagnostics", None)
    if not callable(get_diagnostics):
        return {}
    try:
        result = get_diagnostics()
    except Exception:  # pragma: no cover - logs must never break
        return {}
    return dict(result) if isinstance(result, dict) else {}


def _safe_stderr_excerpt(accessor: Any) -> str:
    """Best-effort sanitized stderr excerpt (never raises, never secrets)."""
    get_excerpt = getattr(accessor, "get_stream_stderr_excerpt", None)
    if not callable(get_excerpt):
        return ""
    try:
        result = get_excerpt()
    except Exception:  # pragma: no cover - logs must never break
        return ""
    if isinstance(result, str):
        from .stdio import MAX_STDERR_EXCERPT_CHARS

        return result[:MAX_STDERR_EXCERPT_CHARS]
    try:
        from .stdio import sanitize_stderr_excerpt

        return sanitize_stderr_excerpt(result)
    except Exception:  # pragma: no cover - defensive
        return ""


class _AllowExtraArgs(BaseModel):
    """Container validating raw MCP args without lossy type mapping."""

    model_config = ConfigDict(extra="allow")

    def raw_dict(self) -> dict[str, Any]:
        return dict(self.model_dump())


def validate_mcp_args(input_schema: dict[str, Any], args: dict[str, Any]) -> None:
    """Validate *args* against the original MCP input schema.

    Uses ``jsonschema`` (an explicit backend dependency) without
    translating the schema into Pydantic types, so no constraint is
    lost. Raises :class:`ToolError` on violations.
    """
    import jsonschema

    try:
        jsonschema.validate(instance=args, schema=input_schema or {"type": "object"})
    except (
        jsonschema.ValidationError,
        jsonschema.SchemaError,
    ) as exc:
        raise ToolError(f"Invalid arguments: {exc.message}", tool="") from exc


@dataclass
class DiscoveredMcpTool:
    """One discovered MCP tool with its original schema."""

    name: str  # original MCP tool name (for call_tool)
    namespaced_name: str  # provider-facing name (permission_key)
    description: str
    input_schema: dict[str, Any]
    title: str


class McpTool(Tool):
    """Harness tool proxying one MCP server tool.

    The provider-facing schema is the *original* MCP ``inputSchema``
    (never a lossy Pydantic translation); ``args_schema`` stays an
    ``extra='allow'`` container so registry dispatch still validates.
    """

    def __init__(
        self,
        *,
        namespaced_name: str,
        original_name: str,
        description: str,
        input_schema: dict[str, Any],
        connection: McpServerConnection,
        title_text: str,
    ) -> None:
        self.name = namespaced_name
        self.description = description or f"MCP tool {original_name}"
        self.args_schema = _AllowExtraArgs
        self.permission_key = namespaced_name
        self.truncate_direction = "head"
        self.original_name = original_name
        self.input_schema = dict(input_schema or {"type": "object"})
        self._connection = connection
        self._title_text = title_text

    def title(self, args: BaseModel) -> str:
        """Return the Plugin / Server / Tool title for permission UI."""
        return self._title_text

    def parameters_schema(self) -> dict[str, Any]:
        """Return the original MCP input schema for provider payloads."""
        return dict(self.input_schema)

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Validate raw args and proxy the call to the MCP server."""
        if isinstance(args, BaseModel):
            raw = dict(args.model_dump())
        elif isinstance(args, dict):
            raw = dict(args)
        else:
            raise ToolError(f"Invalid arguments for tool '{self.name}'", tool=self.name)
        validate_mcp_args(self.input_schema, raw)
        try:
            return await self._connection.call_tool(self.original_name, raw)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(
                f"MCP tool '{self.original_name}' failed", tool=self.name
            ) from exc


def build_mcp_tool_title(plugin_name: str, server_name: str, tool_name: str) -> str:
    """Return the human title ``Plugin / Server / Tool``."""
    return f"{plugin_name} / {server_name} / {tool_name}"


def check_name_collisions(
    namespaced: list[str], *, existing: list[str] | None = None
) -> None:
    """Fail closed on duplicate or core-overwriting namespaced names.

    Kept for unit checks; the run-scoped :class:`McpRuntime` resolves
    cross-server collisions per server (first wins, colliding server
    skipped) instead of failing the whole run.
    """
    seen: set[str] = set()
    core = {name.strip().lower() for name in (existing or [])}
    for name in namespaced:
        lowered = name.strip().lower()
        if lowered in seen:
            raise McpServerHealthError(f"Duplicate MCP tool name: {name}")
        if lowered in core:
            raise McpServerHealthError(f"MCP tool name overwrites a core tool: {name}")
        seen.add(lowered)


def _check_input_schema(schema: dict[str, Any]) -> None:
    """Validate an MCP ``inputSchema`` at discovery (fail-closed).

    An invalid schema skips the whole server (see ``open``): never fall
    back to a permissive object schema, which would widen the tool.
    """
    import jsonschema

    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except (jsonschema.SchemaError, jsonschema.ValidationError) as exc:
        raise McpServerHealthError("MCP tool schema is invalid") from exc
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise McpServerHealthError("MCP tool schema must be an object schema")


async def discover_tools(
    session: ClientSession, *, server_desc: str = ""
) -> list[types.Tool]:
    """Paginate ``list_tools`` with cursor + duplicate guards and caps."""
    collected: list[types.Tool] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for _ in range(MAX_DISCOVERY_PAGES):
        try:
            page = await session.list_tools(cursor=cursor)
        except Exception as exc:
            # Secret-free on purpose: the chained exception (``from
            # exc``) keeps the traceback for debugging, but the health
            # message itself only carries the exception *type* — remote
            # error text may echo request data.
            raise McpServerHealthError(
                "MCP discovery failed for "
                f"{server_desc or 'server'}: {type(exc).__name__}"
            ) from exc
        tools = list(page.tools or [])
        for tool in tools:
            try:
                serialized = json.dumps(tool.inputSchema or {}).encode("utf-8")
            except Exception:  # pragma: no cover - defensive
                serialized = b"{}"
            if len(serialized) > MAX_SCHEMA_SERIALIZED_BYTES:
                raise McpServerHealthError(
                    f"MCP tool '{tool.name}' schema exceeds "
                    f"{MAX_SCHEMA_SERIALIZED_BYTES} bytes"
                )
            collected.append(tool)
            if len(collected) > MAX_TOOLS_PER_SERVER:
                raise McpServerHealthError(
                    f"MCP server {server_desc or ''} exposes more than "
                    f"{MAX_TOOLS_PER_SERVER} tools"
                )
        cursor = getattr(page, "nextCursor", None)
        if not cursor:
            return collected
        if cursor in seen_cursors:
            raise McpServerHealthError(
                f"MCP discovery cursor repeated for {server_desc or 'server'}"
            )
        seen_cursors.add(cursor)
    raise McpServerHealthError(
        f"MCP discovery exceeded {MAX_DISCOVERY_PAGES} pages "
        f"for {server_desc or 'server'}"
    )


def normalize_call_result(result: types.CallToolResult) -> ToolResult:
    """Normalize an MCP ``CallToolResult`` into a harness ``ToolResult``.

    - ``isError`` -> :class:`ToolError` (existing tool-error path).
    - ``TextContent`` blocks are joined; ``structuredContent`` is appended
      as JSON when no text exists (or supplements it).
    - ``ResourceLink``/``EmbeddedResource`` become readable descriptors.
    - Bounded image blocks become tool attachments for the model and
      persisted chat; JPEG also retains the legacy ``image_jpeg`` field.
    """
    if getattr(result, "isError", False):
        texts = [
            block.text
            for block in (result.content or [])
            if getattr(block, "type", "") == "text"
        ]
        message = "\n".join(texts).strip() or "MCP tool reported an error"
        raise ToolError(message[:2000], tool="")
    texts: list[str] = []
    attachments: list[dict[str, str]] = []
    image_jpeg: bytes | None = None
    for block in result.content or []:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            texts.append(getattr(block, "text", "") or "")
        elif block_type == "image":
            mime = str(getattr(block, "mimeType", "") or "").lower()
            data = str(getattr(block, "data", "") or "")
            image_jpeg, descriptor, attachment = _decode_image_block(
                mime, data, image_jpeg, len(attachments)
            )
            if attachment:
                attachments.append(attachment)
            if descriptor:
                texts.append(descriptor)
        elif block_type == "resource_link":
            uri = str(getattr(block, "uri", "") or "")
            name = str(getattr(block, "name", "") or uri)
            texts.append(f"[resource: {name} ({uri})]")
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            texts.append(_embedded_resource_text(resource))
        elif block_type == "audio":
            mime = str(getattr(block, "mimeType", "") or "audio")
            texts.append(f"[audio content ({mime}) not supported]")
        else:  # pragma: no cover - forward-compatible
            texts.append(f"[unsupported MCP content: {block_type}]")
    structured = getattr(result, "structuredContent", None)
    if structured:
        try:
            rendered = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        except Exception:  # pragma: no cover - defensive
            rendered = str(structured)
        if not texts:
            texts.append(rendered)
        elif rendered and rendered not in "\n".join(texts):
            texts.append(rendered)
    output = "\n".join(part for part in texts if part).strip()
    if not output:
        output = "[MCP tool returned no text output]"
    return ToolResult(
        output=output,
        metadata={"attachments": attachments} if attachments else {},
        image_jpeg=image_jpeg,
        attachments=attachments,
    )


def _decode_image_block(
    mime: str, data: str, current_jpeg: bytes | None, attachment_count: int
) -> tuple[bytes | None, str, dict[str, str] | None]:
    """Decode a bounded image into a model-visible attachment.

    Keep at most two images (the persistence cap) and the legacy first
    JPEG. Restrict MIME to formats accepted by our image provider path;
    never send untrusted/unsupported MIME data URLs to a provider.
    """
    import base64 as _base64

    supported = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    normalized_mime = "image/jpeg" if mime == "image/jpg" else mime
    if normalized_mime not in supported:
        return current_jpeg, f"[image ({mime or 'unknown'}) not supported]", None
    if not data:
        return current_jpeg, "[empty image content]", None
    if len(data) > (MAX_MCP_IMAGE_BYTES * 4) // 3 + 16:
        return (
            current_jpeg,
            (f"[image ({mime}) exceeds {MAX_MCP_IMAGE_BYTES} byte limit]"),
            None,
        )
    if attachment_count >= 2:
        return current_jpeg, f"[additional image ({mime}) omitted]", None
    try:
        raw = _base64.b64decode(data, validate=True)
    except Exception:
        return current_jpeg, f"[image ({mime}) with undecodable data]", None
    if len(raw) > MAX_MCP_IMAGE_BYTES:
        return (
            current_jpeg,
            (
                f"[image ({mime}, {len(raw)} bytes) exceeds "
                f"{MAX_MCP_IMAGE_BYTES} byte limit]"
            ),
            None,
        )
    attachment = {
        "type": "file",
        "mime": normalized_mime,
        "url": f"data:{normalized_mime};base64,{data}",
        "filename": "",
    }
    if normalized_mime == "image/jpeg" and current_jpeg is None:
        current_jpeg = raw
    return current_jpeg, "", attachment


def _embedded_resource_text(resource: Any) -> str:
    """Render an embedded resource as a short text descriptor."""
    if resource is None:
        return "[embedded resource]"
    uri = str(getattr(resource, "uri", "") or "")
    mime = str(getattr(resource, "mimeType", "") or "")
    text = getattr(resource, "text", None)
    if isinstance(text, str) and text.strip():
        snippet = text.strip()
        if len(snippet) > 4000:
            snippet = snippet[:4000] + "…"
        prefix = (
            f"[embedded resource {uri} ({mime})]"
            if uri or mime
            else "[embedded resource]"
        )
        return f"{prefix}\n{snippet}"
    blob = getattr(resource, "blob", None)
    size_note = ""
    if isinstance(blob, str) and blob:
        # Never str() the whole payload: it is already a base64 string
        # in memory — its length alone sizes the descriptor.
        size_note = f" ({len(blob)} chars)"
    label = uri or mime or "resource"
    return f"[embedded resource: {label}{size_note}]"


@dataclass
class McpServerConnection:
    """One live MCP server connection for one harness run.

    ``env``/``headers`` hold *rendered* plaintext secrets for this run:
    they are ``repr=False`` so connection reprs in logs, tracebacks, or
    health payloads never carry credential material.
    """

    plugin_id: Any
    plugin_slug: str
    plugin_name: str
    server_id: Any
    server_slug: str
    server_name: str
    transport: str
    command: list[str]
    cwd: str
    url: str = ""
    env: dict[str, str] = field(default_factory=dict, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    startup_timeout_seconds: float = 30.0
    request_timeout_seconds: float = 60.0
    tools: list[DiscoveredMcpTool] = field(default_factory=list)
    healthy: bool = True
    harness_tools: list[Any] = field(default_factory=list)

    _stack: AsyncExitStack | None = None
    _session: ClientSession | None = None
    _lock: Any = None
    _closed: bool = False

    @property
    def desc(self) -> str:
        return f"{self.plugin_slug}/{self.server_slug}"

    async def open(self, accessor: Any) -> list[DiscoveredMcpTool]:
        """Open the transport, initialize the session, discover tools.

        Startup is bounded by ``fail_after``: an outer timeout raises a
        health error (server skipped) while ``CancelledError`` from an
        abort propagates untouched (never converted to a health error).

        The timeout scope is the *innermost* entry of the connection's
        exit stack and is disarmed (``deadline = inf``) on successful
        startup: the SDK session's task group pushes its own cancel
        scope inside the timeout scope when it is entered. Until it is
        closed (LIFO, at ``aclose``) the timeout scope is therefore not
        the task's current scope, and exiting its ``with`` block in the
        success path would raise ``RuntimeError: ... cancel scope`` —
        exactly the E2E masking seen in production. The stack closes in
        reverse entry order, so closing restores a strictly nested
        unwind (session task group, then transport, then timeout).
        """
        if self._lock is None:
            self._lock = anyio.Lock()
        stack = AsyncExitStack()
        timeout = float(self.startup_timeout_seconds or 30.0)
        log.info(
            "mcp_server_open",
            server=self.desc,
            transport=(self.transport or "").strip().lower(),
            timeout_s=timeout,
        )
        scope = stack.enter_context(anyio.fail_after(timeout))
        try:
            try:
                await self._open_transport(stack, accessor)
                await self._open_session(stack)
                await self._discover()
            except BaseException:
                # Close the entered scopes (SDK sessions, transport)
                # while the timeout scope is still the outermost entry:
                # LIFO unwind closes the inner scopes first, which keeps
                # the original error's type and message.
                try:
                    await stack.aclose()
                except (
                    asyncio.CancelledError,
                    anyio.get_cancelled_exc_class(),
                ):
                    raise
                except Exception as close_exc:
                    log.warning(
                        "mcp_server_open_close_failed",
                        server=self.desc,
                        error=f"{type(close_exc).__name__}",
                    )
                raise
        except TimeoutError as exc:
            log.warning(
                "mcp_server_open_timeout",
                server=self.desc,
                timeout_s=timeout,
                diagnostics=_safe_diagnostics(accessor),
            )
            raise McpServerHealthError(
                f"MCP server {self.desc} startup timed out after {timeout}s"
            ) from exc
        except McpServerHealthError as exc:
            # Startup failure (transport/initialize/discovery): log phase
            # + secret-free stream diagnostics + sanitized stderr tail so
            # the next failure is diagnosable from this one line + the
            # chained traceback (kept via ``exc_info`` in _record_skip).
            log.warning(
                "mcp_server_open_failed",
                server=self.desc,
                error=str(exc),
                diagnostics=_safe_diagnostics(accessor),
                stderr_excerpt=_safe_stderr_excerpt(accessor),
            )
            raise
        # Startup succeeded: disarm the timeout for the connection's
        # lifetime. The scope is still exited by the stack (last, after
        # the session/transport) exactly once in ``aclose``.
        scope.deadline = math.inf
        self._stack = stack
        log.info(
            "mcp_server_open_ok",
            server=self.desc,
            tools=len(self.tools),
        )
        return list(self.tools)

    async def _open_transport(self, stack: AsyncExitStack, accessor: Any) -> None:
        """Open the workspace-local transport into ``(read, write)``."""
        transport = (self.transport or "").strip().lower()
        if transport == "stdio":
            if not self.command:
                raise McpServerHealthError(
                    f"MCP server {self.desc} has no stdio command"
                )
            ctx = workspace_stdio_client(
                accessor,
                list(self.command),
                cwd=self.cwd,
                env=dict(self.env),
                server_desc=self.desc,
                timeout=float(self.startup_timeout_seconds or 30.0),
            )
            read, write = await stack.enter_async_context(ctx)
            self._read, self._write = read, write
        elif transport in ("streamable_http", "sse"):
            await self._open_http_transport(stack, accessor, transport)
        else:
            raise McpServerHealthError(
                f"MCP server {self.desc} has unsupported transport '{self.transport}'"
            )

    async def _open_http_transport(
        self, stack: AsyncExitStack, accessor: Any, transport: str
    ) -> None:
        """Open an MCP HTTP client over the workspace TCP bridge.

        Each transport owns its client lifecycle: ``streamable_http``
        uses one entered client passed explicitly; ``sse`` builds its
        own per-call clients through a factory that opens a fresh
        workspace-backed client per SDK call (never a shared, already
        entered client), so closing stays idempotent per transport.
        """
        _, _, _, _ = parse_mcp_http_url(self.url)
        timeout = httpx.Timeout(30.0, read=float(self.request_timeout_seconds or 60.0))
        if transport == "streamable_http":
            from mcp.client.streamable_http import streamable_http_client

            client, _ = build_workspace_http_client(
                accessor,
                self.url,
                headers=dict(self.headers or {}),
                timeout=timeout,
                follow_redirects=True,
            )
            await stack.enter_async_context(client)
            ctx = streamable_http_client(self.url, http_client=client)
            read, write, _session_id_cb = await stack.enter_async_context(ctx)
        else:
            from mcp.client.sse import sse_client

            def _factory(
                headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None,
                auth: Any | None = None,
            ) -> httpx.AsyncClient:
                # Fresh workspace-backed client per SDK call: the SDK
                # enters/closes it itself (async-with in sse_client), so
                # no shared entered client is reused here.
                merged = dict(self.headers or {})
                if headers:
                    merged.update(headers)
                client, _ = build_workspace_http_client(
                    accessor,
                    self.url,
                    headers=merged,
                    timeout=timeout or httpx.Timeout(30.0, read=300.0),
                    follow_redirects=True,
                )
                if auth is not None:
                    client.auth = auth
                return client

            ctx = sse_client(
                self.url,
                headers=dict(self.headers or {}),
                httpx_client_factory=_factory,  # type: ignore[arg-type]
            )
            read, write = await stack.enter_async_context(ctx)
        self._read, self._write = read, write

    async def _open_session(self, stack: AsyncExitStack) -> None:
        """Enter ``ClientSession`` and run ``initialize``."""
        session = ClientSession(
            self._read,
            self._write,
            read_timeout_seconds=timedelta(
                seconds=float(self.request_timeout_seconds or 60.0)
            ),
        )
        await stack.enter_async_context(session)
        try:
            await session.initialize()
        except McpServerHealthError:
            raise
        except Exception as exc:
            raise McpServerHealthError(
                f"MCP server {self.desc} initialize failed"
            ) from exc
        self._session = session

    async def _discover(self) -> None:
        """Discover tools into :attr:`tools` (health error on invalid)."""
        assert self._session is not None
        try:
            raw_tools = await discover_tools(self._session, server_desc=self.desc)
        except McpServerHealthError:
            raise
        except Exception as exc:
            raise McpServerHealthError(f"MCP discovery failed for {self.desc}") from exc
        discovered: list[DiscoveredMcpTool] = []
        for tool in raw_tools:
            try:
                schema = dict(tool.inputSchema or {"type": "object"})
            except Exception as exc:
                raise McpServerHealthError(
                    f"MCP tool '{tool.name}' has an invalid schema"
                ) from exc
            try:
                _check_input_schema(schema)
            except McpServerHealthError:
                raise
            namespaced = mcp_tool_name(self.plugin_slug, self.server_slug, tool.name)
            if not TOOL_NAME_RE.match(namespaced):
                raise McpServerHealthError(
                    f"MCP tool name out of range after namespacing: {namespaced}"
                )
            discovered.append(
                DiscoveredMcpTool(
                    name=tool.name,
                    namespaced_name=namespaced,
                    description=tool.description or "",
                    input_schema=schema,
                    title=build_mcp_tool_title(
                        self.plugin_name, self.server_name, tool.name
                    ),
                )
            )
        self.tools = discovered

    async def call_tool(self, original_name: str, args: dict[str, Any]) -> ToolResult:
        """Call one tool with per-server serialization + request timeout.

        Cancellation (abort) propagates untouched; only a genuine outer
        timeout becomes a :class:`ToolError`. The SDK ``read_timeout``
        stays aligned with the outer budget.
        """
        if self._session is None or self._closed:
            raise ToolError(f"MCP server {self.desc} is not connected", tool=self.desc)
        lock = self._lock or anyio.Lock()
        async with lock:
            timeout = float(self.request_timeout_seconds or 60.0)
            try:
                try:
                    with anyio.fail_after(timeout):
                        result = await self._session.call_tool(
                            original_name,
                            dict(args or {}),
                            read_timeout_seconds=timedelta(seconds=timeout),
                        )
                except TimeoutError as exc:
                    raise ToolError(
                        f"MCP tool '{original_name}' timed out after {timeout}s",
                        tool=original_name,
                    ) from exc
            except ToolError:
                raise
            except (asyncio.CancelledError, anyio.get_cancelled_exc_class()):
                raise
            except Exception as exc:
                raise ToolError(
                    f"MCP tool '{original_name}' failed", tool=original_name
                ) from exc
            try:
                return normalize_call_result(result)
            except ToolError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                raise ToolError("MCP result invalid", tool=original_name) from exc

    async def aclose(self) -> None:
        """Close the session + transport (idempotent, never raises)."""
        if self._closed:
            return
        self._closed = True
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:  # pragma: no cover - best effort
                log.warning("mcp_server_close_failed", server=self.desc)
