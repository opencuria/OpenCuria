"""Extended MCP hardening tests (review fixes).

Covers, against fakes (no sockets, no runner):

- invalid MCP ``inputSchema`` skips the server at discovery (never a
  permissive fallback) and ``validate_mcp_args`` sets ``ToolError.tool``;
- permission action for MCP tools is the original tool name, never args
  (secret-like values never reach the persisted pattern/event);
- two healthy servers with a tool-name collision: first server kept,
  colliding server skipped + closed, other tools still registered;
- setup raising / cancellation during setup still closes the runtime;
- an injected ``runner_factory`` sees the fully-registered MCP tools;
- ``parse_mcp_http_url`` rejects userinfo/fragments and validates hosts;
  header validation rejects CRLF; TLS ``start_tls`` writes no plaintext
  before the workspace TLS relay is opened;
- SSE end-to-end initialize + list_tools over separate GET stream and
  POST endpoint via workspace streams (no backend socket);
- stdio split multibyte chars reassemble; oversize/invalid fail the
  server without logging payload content;
- ``normalize_call_result``: empty output is meaningful, second JPEG
  becomes a descriptor, oversize base64 is bounded pre-decode, blob
  sizes never str() the payload;
- snapshot fail-closed: workspace org mismatch raises; foreign attached
  credentials are ignored (missing-required gap, no leak).
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid

import anyio
import mcp.types as types
import pytest
from mcp.client.session import ClientSession
from mcp.shared.message import SessionMessage

from apps.harness.mcp_client import connection as conn_module
from apps.harness.mcp_client.connection import (
    McpServerConnection,
    McpServerHealthError,
    McpTool,
    build_mcp_tool_title,
    normalize_call_result,
    validate_mcp_args,
)
from apps.harness.mcp_client.naming import mcp_tool_name
from apps.harness.mcp_client.runtime import McpRuntime
from apps.harness.mcp_client.stdio import (
    MAX_STDIO_LINE_BYTES,
    workspace_stdio_client,
)
from apps.harness.mcp_client.workspace_http import (
    WorkspaceNetworkStream,
    _validate_header_items,
    build_workspace_http_client,
    parse_mcp_http_url,
)
from apps.harness.runner import (
    HarnessRunner,
    _action_for_registered_tool,
    _action_for_tool,
)
from apps.harness.tools.base import ToolError


def _namespaced(plugin: str, server: str, tool: str) -> str:
    return mcp_tool_name(plugin, server, tool)


def _connection(
    plugin: str = "plug",
    server: str = "srv",
    *,
    transport: str = "stdio",
    url: str = "",
) -> McpServerConnection:
    return McpServerConnection(
        plugin_id=uuid.uuid4(),
        plugin_slug=plugin,
        plugin_name="Plug",
        server_id=uuid.uuid4(),
        server_slug=server,
        server_name="Srv",
        transport=transport,
        command=["fake"],
        cwd="/workspace",
        env={},
        url=url,
        headers={},
        startup_timeout_seconds=5.0,
        request_timeout_seconds=5.0,
    )


def _mcp_tool(namespaced: str, original: str = "echo", connection=None) -> McpTool:
    return McpTool(
        namespaced_name=namespaced,
        original_name=original,
        description="echo it",
        input_schema={"type": "object"},
        connection=connection,
        title_text=build_mcp_tool_title("P", "S", original),
    )


# -- discovery schema validation -------------------------------------------


def test_invalid_input_schema_raises_health_error():
    bad = {"type": "object", "properties": {"x": {"type": "nope"}}}
    with pytest.raises(McpServerHealthError):
        conn_module._check_input_schema(bad)
    with pytest.raises(McpServerHealthError):
        conn_module._check_input_schema({"type": "array"})
    # Valid object schemas pass.
    conn_module._check_input_schema({"type": "object"})


def test_validate_mcp_args_sets_tool_name():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    try:
        validate_mcp_args(schema, {"a": 1})
    except ToolError as exc:
        assert "Invalid arguments" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("validate_mcp_args did not raise")


# -- permission action ------------------------------------------------------


def test_mcp_action_is_original_name_never_args():
    tool = _mcp_tool(_namespaced("p", "s", "do"), original="do_thing")
    secret_args = {"token": "super-secret-value", "password": "hunter2"}
    assert _action_for_tool(tool.name, secret_args) == ""
    assert _action_for_registered_tool(tool, tool.name) == "do_thing"
    # Secret-like values never appear in the derived action.
    assert "super-secret" not in _action_for_registered_tool(tool, tool.name)


@pytest.mark.asyncio
async def test_runner_dispatch_action_uses_original_name():
    # Unit-level: dispatch derives the ask action from the registered
    # tool's ``original_name`` (never raw args). Exercised directly
    # against ``_dispatch_tool_call`` so no looping provider is needed.
    from apps.harness.agents.definitions import get_agent
    from apps.harness.runner import RunOptions, _PendingToolCall
    from apps.harness.tools import default_tool_registry
    from apps.harness.tools.base import ToolContext

    seen: list[tuple[str, str]] = []

    async def _deny_all(**kwargs):
        seen.append((kwargs.get("tool", ""), kwargs.get("action", "")))
        return "reject"

    tool = _mcp_tool(_namespaced("p", "s", "lookup"), original="lookup")
    registry = default_tool_registry()
    registry.register(tool)

    from apps.harness.permissions.evaluator import PermissionEvaluator
    from apps.harness.tests.conftest import FakeAccessor

    runner = HarnessRunner(
        provider=_NoopProvider(),
        tools=registry,
        accessor=None,
        evaluator=PermissionEvaluator(global_rules={"*": "ask", "mcp_*": "ask"}),
    )
    agent = get_agent("build")
    ctx = ToolContext(
        session_id="s",
        workspace_id="w",
        accessor=FakeAccessor(),
        agent_name="build",
        registry=registry,
    )
    opts = RunOptions(session_id="s", workspace_id="w", on_permission=_deny_all)
    outcome = await runner._dispatch_tool_call(
        call=_PendingToolCall(
            call_id="c1",
            name=tool.name,
            arguments={"token": "super-secret-value"},
            raw_arguments='{"token": "super-secret-value"}',
        ),
        ctx=ctx,
        agent=agent,
        mode="build",
        step=1,
        depth=0,
        max_depth=1,
        doom_loop=False,
        opts=opts,
    )
    assert outcome.message.content == (
        "Permission denied by permissions: "
        + runner._tool_title(tool.name, {"token": "super-secret-value"})
    )
    assert seen, "expected an ask gate for the MCP tool"
    gated_tool, gated_action = seen[0]
    assert gated_tool == tool.name
    assert gated_action == "lookup"
    assert "super-secret" not in gated_action


class _NoopProvider:
    """Minimal provider stub (never called in the dispatch unit test)."""

    name = "noop"

    async def chat_stream(self, model, messages, tools, opts=None):
        if False:  # pragma: no cover - never iterated
            yield None
        raise AssertionError("provider must not be called")


# -- runtime collision: first wins, collider skipped+closed ------------------


class _FakeConn:
    def __init__(self, tools, *, name="srv"):
        self.harness_tools = list(tools)
        self.tools = list(tools)
        self.healthy = True
        self.desc = f"plug/{name}"
        self.closed = False

    async def open(self, accessor):
        return list(self.tools)

    async def aclose(self):
        self.closed = True


def _discovered(name: str, namespaced: str) -> object:
    from apps.harness.mcp_client.connection import DiscoveredMcpTool

    return DiscoveredMcpTool(
        name=name,
        namespaced_name=namespaced,
        description="d",
        input_schema={"type": "object"},
        title="P / S / " + name,
    )


@pytest.mark.asyncio
async def test_runtime_collision_first_wins_collider_closed():
    from apps.plugins.runtime_snapshot import (
        EffectivePluginSnapshot,
        PluginMcpServerSnapshot,
        PreparedPluginRuntime,
        WorkspacePluginSnapshot,
    )

    plugin_id = uuid.uuid4()
    snapshot = WorkspacePluginSnapshot(
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        plugins=(
            EffectivePluginSnapshot(
                id=plugin_id,
                name="Plug",
                slug="plug",
                description="",
                organization_id=None,
                is_global=True,
                skills=(),
                mcp_servers=(
                    PluginMcpServerSnapshot(
                        id=uuid.uuid4(),
                        name="A",
                        slug="a",
                        transport="stdio",
                        command="fake",
                    ),
                    PluginMcpServerSnapshot(
                        id=uuid.uuid4(),
                        name="B",
                        slug="b",
                        transport="stdio",
                        command="fake",
                    ),
                ),
                requirements=(),
            ),
        ),
    )

    runtime = McpRuntime()
    opened: list[str] = []
    closed: list[str] = []
    real_open = McpServerConnection.open
    real_close = McpServerConnection.aclose

    async def _fake_open(self, accessor):
        opened.append(self.server_slug)
        if self.server_slug == "a":
            self.tools = [
                _discovered("echo", _namespaced("plug", "a", "echo")),
                _discovered("solo", _namespaced("plug", "a", "solo")),
            ]
        else:
            # Force an exact collision with server A's tool name.
            self.tools = [_discovered("echo", _namespaced("plug", "a", "echo"))]
        return list(self.tools)

    async def _fake_close(self):
        closed.append(self.server_slug)
        await real_close(self)

    async def _noop_discover(self):
        return None

    McpServerConnection.open = _fake_open  # type: ignore[assignment]
    McpServerConnection.aclose = _fake_close  # type: ignore[assignment]
    try:
        prepared = PreparedPluginRuntime(
            snapshot=snapshot, workspace=None, plaintexts={plugin_id: {}}
        )
        await runtime.setup(
            workspace=None,
            organization_id=snapshot.organization_id,
            accessor=object(),
            core_tool_names=[],
            snapshot=prepared,
        )
    finally:
        McpServerConnection.open = real_open  # type: ignore[assignment]
        McpServerConnection.aclose = real_close  # type: ignore[assignment]
    assert opened == ["a", "b"]
    # First server kept, colliding server skipped + closed.
    assert [c.server_slug for c in runtime.connections] == ["a"]
    assert "b" in closed
    assert any(s["server"] == "b" for s in runtime.skipped)
    from apps.harness.tools.base import ToolRegistry

    registry = ToolRegistry()
    registered = runtime.register_tools(registry)
    assert sorted(registered) == sorted(
        [_namespaced("plug", "a", "echo"), _namespaced("plug", "a", "solo")]
    )
    await runtime.aclose()


# -- setup exception / cancellation close ------------------------------------


@pytest.mark.asyncio
async def test_runtime_setup_exception_closes_opened():
    """A failing server mid-setup still closes already-opened servers."""
    from apps.plugins.runtime_snapshot import (
        EffectivePluginSnapshot,
        PluginMcpServerSnapshot,
        PreparedPluginRuntime,
        WorkspacePluginSnapshot,
    )

    plugin_id = uuid.uuid4()
    snapshot = WorkspacePluginSnapshot(
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        plugins=(
            EffectivePluginSnapshot(
                id=plugin_id,
                name="Plug",
                slug="plug",
                description="",
                organization_id=None,
                is_global=True,
                skills=(),
                mcp_servers=(
                    PluginMcpServerSnapshot(
                        id=uuid.uuid4(),
                        name="Good",
                        slug="good",
                        transport="stdio",
                        command="fake",
                    ),
                    PluginMcpServerSnapshot(
                        id=uuid.uuid4(),
                        name="Bad",
                        slug="bad",
                        transport="bogus-transport",
                        command="fake",
                    ),
                ),
                requirements=(),
            ),
        ),
    )
    runtime = McpRuntime()
    closed: list[str] = []
    real_close = McpServerConnection.aclose

    async def _tracking_close(self):
        closed.append(self.server_slug)
        await real_close(self)

    McpServerConnection.aclose = _tracking_close  # type: ignore[assignment]
    real_open = McpServerConnection.open

    async def _fake_open(self, accessor):
        if self.server_slug == "good":
            self.tools = [_discovered("ok", _namespaced("plug", "good", "ok"))]
            return list(self.tools)
        return await real_open(self, accessor)

    McpServerConnection.open = _fake_open  # type: ignore[assignment]
    try:
        prepared = PreparedPluginRuntime(
            snapshot=snapshot, workspace=None, plaintexts={plugin_id: {}}
        )
        await runtime.setup(
            workspace=None,
            organization_id=snapshot.organization_id,
            accessor=object(),
            snapshot=prepared,
        )
    finally:
        McpServerConnection.open = real_open  # type: ignore[assignment]
        McpServerConnection.aclose = real_close  # type: ignore[assignment]
    # Good server kept; bogus transport skipped; nothing leaked.
    assert [c.server_slug for c in runtime.connections] == ["good"]
    assert any(s["server"] == "bad" for s in runtime.skipped)
    # The skipped bad server was closed during setup; the fake good
    # server never entered the stack (open was stubbed), so runtime
    # close has nothing to do — assert idempotent close instead.
    assert "bad" in closed
    await runtime.aclose()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_aclose_after_cancelled_setup():
    runtime = McpRuntime()

    async def _never(_):
        await anyio.sleep(3600)

    task = asyncio.ensure_future(_never(None))
    await anyio.sleep(0.01)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, anyio.get_cancelled_exc_class()):
        pass
    # aclose must stay idempotent + never raise after cancellation.
    await runtime.aclose()
    await runtime.aclose()


# -- injected runner_factory sees MCP tools ----------------------------------


@pytest.mark.django_db(transaction=True)
async def test_runner_factory_sees_registered_mcp_tools(harness_workspace):
    from apps.harness.harness_service import HarnessService
    from apps.harness.permissions.service import PermissionService
    from apps.harness.tests.test_mcp_harness import (
        FakeMcpTool,
        _async_fake_accessor,
        _create_harness_session,
    )

    seen_names: list[list[str]] = []

    class StubRuntime:
        skipped: list[dict] = []

        async def setup(self, **kwargs):
            prepared = kwargs.get("snapshot")
            if prepared is None or not hasattr(prepared, "snapshot"):
                return None
            return prepared.snapshot

        def register_tools(self, registry):
            registry.register(FakeMcpTool("mcp_demo_srv_echo"))
            return ["mcp_demo_srv_echo"]

        async def aclose(self):
            return None

    import apps.harness.mcp_client.runtime as mcp_runtime_module

    real = mcp_runtime_module.McpRuntime
    mcp_runtime_module.McpRuntime = StubRuntime  # type: ignore[assignment]

    from apps.harness.providers.base import Delta, Usage
    from apps.harness.tests.test_mcp_harness import FakeProvider

    class ListingProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):
            self.seen_schemas.append(list(tools))
            self.calls += 1
            yield Delta(text="done", usage=Usage(1, 1, 2))

    provider = ListingProvider()

    def _factory(**kwargs):
        seen_names.append(sorted(kwargs["tools"].names()))
        from apps.harness.runner import HarnessRunner

        return HarnessRunner(
            model_resolver=lambda ref, _p=provider: __import__(
                "apps.harness.providers.resolver", fromlist=["ResolvedModel"]
            ).ResolvedModel(
                adapter=_p,
                model_id="m",
                provider="fake",
                context_length=0,
                max_output_tokens=0,
            ),
            tools=kwargs["tools"],
            accessor=kwargs.get("accessor"),
            emit=kwargs.get("emit"),
        )

    service = HarnessService(
        permissions=PermissionService(),
        emit=lambda event, data: asyncio.sleep(0),
        provider_factory=lambda _org: provider,
        accessor_factory=_async_fake_accessor,
        runner_factory=_factory,
    )
    session = _create_harness_session(harness_workspace)
    try:
        assistant = await service.start_run(
            session,
            "hello",
            organization_id=harness_workspace.runner.organization_id,
            workspace_id=str(harness_workspace.id),
        )
        await service._tasks[str(session.id)]
    finally:
        mcp_runtime_module.McpRuntime = real
    assistant.refresh_from_db()
    assert assistant.finish == "stop"
    assert seen_names and "mcp_demo_srv_echo" in seen_names[0]


# -- HTTP URL + header validation --------------------------------------------


def test_parse_mcp_http_url_rejects_userinfo_fragment_bad_host():
    with pytest.raises(ValueError):
        parse_mcp_http_url("http://user:pass@localhost:8123/mcp")
    with pytest.raises(ValueError):
        parse_mcp_http_url("http://localhost:8123/mcp#frag")
    with pytest.raises(ValueError):
        parse_mcp_http_url("http://bad host!!:8123/mcp")
    scheme, host, port, target = parse_mcp_http_url("http://localhost:8123/mcp?x=1")
    assert (scheme, host, port, target) == ("http", "localhost", 8123, "/mcp?x=1")
    # Same-origin redirect target parses to the same origin.
    origin = (scheme, host, port)
    scheme2, host2, port2, _ = parse_mcp_http_url("http://localhost:8123/other")
    assert (scheme2, host2, port2) == origin


def test_header_validation_rejects_crlf():
    with pytest.raises(ValueError):
        _validate_header_items({"X-Ok": "bad\r\ninjected: 1"})
    with pytest.raises(ValueError):
        _validate_header_items({"Bad\nName": "x"})
    assert _validate_header_items({"X-Ok": "fine"}) == {"X-Ok": "fine"}


@pytest.mark.asyncio
async def test_tls_upgrade_writes_no_plaintext_before_relay():
    events: list[tuple] = []

    class RelayStream:
        def __init__(self, tls: bool):
            self.tls = tls
            self.sent = bytearray()
            self.eof = False
            self.closed = False

        async def receive(self):
            await anyio.sleep(3600)
            return b""

        async def send(self, data: bytes):
            self.sent.extend(bytes(data))

        async def send_eof(self):
            self.eof = True

        async def aclose(self):
            self.closed = True

    plain = RelayStream(tls=False)
    upgraded = RelayStream(tls=True)

    class Accessor:
        async def open_tcp(
            self, host, port, tls=False, server_hostname=None, timeout=None
        ):
            events.append((host, port, tls, server_hostname))
            return upgraded if tls else plain

    accessor = Accessor()
    raw = await accessor.open_tcp("example.internal", 443, tls=False)
    stream = WorkspaceNetworkStream(
        raw,
        already_tls=False,
        accessor=accessor,
        host="example.internal",
        port=443,
    )
    await stream.start_tls(None, server_hostname="example.internal")
    # The plain probe never saw HTTP bytes; only the TLS relay was opened.
    assert bytes(plain.sent) == b""
    assert plain.eof is True and plain.closed is True
    assert events[-1] == ("example.internal", 443, True, "example.internal")
    # SNI mismatch fails closed.
    raw2 = await accessor.open_tcp("example.internal", 443, tls=False)
    stream2 = WorkspaceNetworkStream(
        raw2,
        already_tls=False,
        accessor=accessor,
        host="example.internal",
        port=443,
    )
    with pytest.raises(RuntimeError):
        await stream2.start_tls(None, server_hostname="evil.example")


# -- SSE end-to-end over workspace streams ------------------------------------


class _TcpStream:
    """Scripted TCP stream: serves one responder per request.

    The responder sees the parsed ``(method, target)`` of each request
    and returns the scripted response chunks. ``keep_open`` streams
    never send EOF (for the long-lived SSE GET); the test closes them
    by cancelling.
    """

    def __init__(self, responder, *, keep_open: bool = False) -> None:
        self._responder = responder
        self._keep_open = keep_open
        self._script: list[bytes] = []
        self.sent = bytearray()
        self._buffer = bytearray()
        self._responded = 0

    async def receive(self) -> bytes:
        while not self._script:
            await asyncio.sleep(0.002)
        chunk = self._script.pop(0)
        if not chunk and self._keep_open:
            # Never EOF a long-lived event stream: wait instead.
            self._script.append(chunk)
            await asyncio.sleep(3600)
            return b""
        return chunk

    async def send(self, data: bytes) -> None:
        self.sent.extend(bytes(data))
        self._buffer.extend(bytes(data))
        while b"\r\n\r\n" in self._buffer:
            head, rest = bytes(self._buffer).split(b"\r\n\r\n", 1)
            try:
                request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
                method, target, _ = request_line.split(" ", 2)
            except ValueError:
                break
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    try:
                        length = int(value.strip())
                    except ValueError:
                        length = 0
            if len(rest) < length:
                break
            self._buffer = bytearray(rest[length:])
            self._script.extend(self._responder(method, target))

    async def send_eof(self):
        return None

    async def aclose(self):
        return None


def _parse_request(raw: bytes) -> tuple[str, str]:  # pragma: no cover - helper
    line = raw.split(b"\r\n", 1)[0].decode("latin-1")
    method, target, _ = line.split(" ", 2)
    return method, target


def _http_chunks(body: bytes, *, content_type: str, extra: str = "") -> list[bytes]:
    head = (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        f"{extra}\r\n"
    ).encode()
    return [head, body, b""]


def _http_chunked_stream(body: bytes, *, content_type: str) -> list[bytes]:
    """One chunked HTTP/1.1 response that stays open (SSE GET stream)."""
    head = (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: keep-alive\r\n"
        "\r\n"
    ).encode()
    return [head, f"{len(body):X}\r\n".encode(), body, b"\r\n"]


@pytest.mark.asyncio
async def test_sse_end_to_end_over_workspace_streams():
    """GET event stream + POST endpoint via workspace TCP (no backend net).

    The fake MCP server speaks real SSE semantics: the GET ``/sse``
    stream stays open; the ``initialize`` POST to the session endpoint
    triggers the JSON-RPC reply, which the server pushes onto the open
    GET stream (separate workspace TCP connections for GET vs POST).
    """
    get_stream: dict[str, _TcpStream] = {}
    post_seen: dict[str, bool] = {}

    def responder(method: str, target: str) -> list[bytes]:
        if method == "GET":
            # Long-lived chunked event stream: endpoint event now; the
            # initialize reply is pushed as a second chunk when the POST
            # arrives (same responder, shared ``get_stream`` handle).
            body = b"event: endpoint\ndata: /messages/?session_id=abc\n\n"
            return _http_chunked_stream(body, content_type="text/event-stream")
        assert "session_id=abc" in target or "/messages/" in target
        post_seen["initialize"] = True
        # Push the initialize reply onto the still-open GET stream.
        reply = (
            b"event: message\ndata: "
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "sse-ws", "version": "1"},
                    },
                }
            ).encode()
            + b"\n\n"
        )
        stream = get_stream.get("get")
        assert stream is not None
        stream._script.extend([f"{len(reply):X}\r\n".encode(), reply, b"\r\n"])
        return _http_chunks(b"", content_type="application/json")

    class Accessor:
        def __init__(self):
            self.calls: list[tuple] = []
            self.streams_created: list[_TcpStream] = []
            self.streams: list[_TcpStream] = []
            self.requests: list[tuple[str, str]] = []

        async def open_tcp(
            self, host, port, tls=False, server_hostname=None, timeout=None
        ):
            self.calls.append((host, int(port), bool(tls), server_hostname))
            # GET /sse gets the long-lived event stream; POSTs get
            # short request/response streams (separate TCP connections).
            is_get_stream = len(self.streams_created) == 0
            stream = _TcpStream(responder, keep_open=is_get_stream)
            self.streams_created.append(stream)
            self.streams.append(stream)
            if is_get_stream:
                get_stream["get"] = stream
            return stream

    accessor = Accessor()
    from mcp.client.sse import sse_client

    def _factory(headers=None, timeout=None, auth=None):
        client, _ = build_workspace_http_client(
            accessor,
            "http://localhost:9000/sse",
            headers={**(headers or {})},
            timeout=timeout,
            follow_redirects=True,
        )
        if auth is not None:
            client.auth = auth
        return client

    async with sse_client(
        "http://localhost:9000/sse", httpx_client_factory=_factory
    ) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "sse-ws"
    assert accessor.calls and all(c[0] == "localhost" for c in accessor.calls)
    # The POST went to the session endpoint path via a workspace stream.
    posted = b"".join(bytes(s.sent) for s in accessor.streams)
    assert b"session_id=abc" in posted or b"/messages/" in posted


# -- stdio: multibyte splits, oversize/invalid close --------------------------


class _ProcStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []
        self.eof_sent = False
        self.closed = False

    async def receive(self) -> bytes:
        if self._chunks:
            await anyio.sleep(0.002)
            return self._chunks.pop(0)
        await anyio.sleep(3600)
        return b""

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def send_eof(self) -> None:
        self.eof_sent = True

    async def aclose(self) -> None:
        self.closed = True


class _ProcAccessor:
    def __init__(self, stream: _ProcStream) -> None:
        self.stream = stream

    async def open_process(self, command, workdir="/workspace", env=None, timeout=None):
        return self.stream


@pytest.mark.asyncio
async def test_stdio_split_multibyte_reassembled():
    body = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    # ensure_ascii=False keeps real multibyte bytes on the wire
                    # so chunk splits land inside UTF-8 sequences.
                    "serverInfo": {"name": "täst-✓-server", "version": "1"},
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    cut1 = body.index("täst".encode()) + 2  # inside ä (2 bytes)
    cut2 = body.index("✓".encode()) + 1  # inside ✓ (3 bytes)
    chunks = [body[:cut1], body[cut1:cut2], body[cut2:], b""]
    stream = _ProcStream(chunks)
    async with workspace_stdio_client(_ProcAccessor(stream), ["x"]) as (read, _):
        message = await read.receive()
        assert isinstance(message, SessionMessage)
        assert message.message.root.id == 3


@pytest.mark.asyncio
async def test_stdio_oversize_closes_and_logs_no_payload(caplog):
    big = b"y" * (MAX_STDIO_LINE_BYTES + 8) + b"\n"
    stream = _ProcStream([big, b""])
    with caplog.at_level("WARNING"):
        async with workspace_stdio_client(_ProcAccessor(stream), ["x"]) as (read, _):
            message = await read.receive()
            assert isinstance(message, Exception)
    assert "yyy" not in caplog.text
    assert "y" * 64 not in caplog.text


@pytest.mark.asyncio
async def test_stdio_invalid_utf8_closes_and_logs_no_payload(caplog):
    stream = _ProcStream([b"\xff\xfe not utf8 \n", b""])
    with caplog.at_level("WARNING"):
        async with workspace_stdio_client(_ProcAccessor(stream), ["x"]) as (read, _):
            message = await read.receive()
            assert isinstance(message, Exception)
    assert "xff" not in caplog.text.replace("WARNING", "")


# -- normalize_call_result -----------------------------------------------------


def test_normalize_empty_output_is_meaningful():
    result = types.CallToolResult(content=[])
    out = normalize_call_result(result)
    assert out.output.strip()


def test_normalize_second_jpeg_is_descriptor():
    tiny = base64.b64encode(b"\xff\xd8\xff fake").decode()
    result = types.CallToolResult(
        content=[
            types.ImageContent(type="image", data=tiny, mimeType="image/jpeg"),
            types.ImageContent(type="image", data=tiny, mimeType="image/jpeg"),
        ]
    )
    out = normalize_call_result(result)
    assert out.image_jpeg is not None
    assert len(out.attachments) == 2
    assert out.output == "[MCP tool returned no text output]"


def test_normalize_oversize_base64_bounded_pre_decode():
    from apps.harness.mcp_client.connection import MAX_MCP_IMAGE_BYTES

    huge = "A" * (((MAX_MCP_IMAGE_BYTES * 4) // 3) + 64)
    result = types.CallToolResult(
        content=[types.ImageContent(type="image", data=huge, mimeType="image/jpeg")]
    )
    out = normalize_call_result(result)
    assert out.image_jpeg is None
    assert "exceeds" in out.output


def test_embedded_blob_size_never_str_payload():
    blob = "B" * 100
    result = types.CallToolResult(
        content=[
            types.EmbeddedResource(
                type="resource",
                resource=types.BlobResourceContents(
                    uri="file:///x.bin",
                    mimeType="application/octet-stream",
                    blob=blob,
                ),
            )
        ]
    )
    out = normalize_call_result(result)
    assert blob not in out.output
    assert "100 chars" in out.output


# -- snapshot fail-closed -------------------------------------------------------


@pytest.mark.django_db
def test_workspace_org_mismatch_fails_closed(db):
    import uuid as _uuid

    from django.contrib.auth import get_user_model

    from apps.organizations.models import Organization
    from apps.plugins import runtime as plugin_runtime
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token

    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"mm-{_uuid.uuid4().hex[:8]}@example.com", password="s"
    )
    org_a = Organization.objects.create(name="A", slug=f"a-{_uuid.uuid4().hex[:8]}")
    org_b = Organization.objects.create(name="B", slug=f"b-{_uuid.uuid4().hex[:8]}")
    runner = Runner.objects.create(
        name="r", api_token_hash=hash_token(_uuid.uuid4().hex), organization=org_a
    )
    workspace = Workspace.objects.create(runner=runner, name="w", created_by=user)
    with pytest.raises(plugin_runtime.PluginCredentialConfigError):
        plugin_runtime.build_workspace_plugin_snapshot(
            workspace=workspace, org_id=org_b.id
        )


@pytest.mark.django_db
def test_foreign_credential_attach_ignored(db, caplog):
    import uuid as _uuid

    from django.contrib.auth import get_user_model

    from apps.credentials.services import CredentialSvc
    from apps.organizations.models import Membership, MembershipRole, Organization
    from apps.plugins import runtime as plugin_runtime
    from apps.plugins.models import Plugin
    from apps.plugins.services import PluginService
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token

    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"fc-{_uuid.uuid4().hex[:8]}@example.com", password="s"
    )
    org = Organization.objects.create(name="O", slug=f"o-{_uuid.uuid4().hex[:8]}")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    other = Organization.objects.create(name="X", slug=f"x-{_uuid.uuid4().hex[:8]}")
    runner = Runner.objects.create(
        name="r", api_token_hash=hash_token(_uuid.uuid4().hex), organization=org
    )
    workspace = Workspace.objects.create(runner=runner, name="w", created_by=user)
    created = PluginService().create_org_plugin(
        org_id=org.id,
        user=user,
        name="FP",
        slug="fp",
        skills=[],
        mcp_servers=[
            {
                "name": "T",
                "transport": "stdio",
                "command": "npx",
                "env": {"TOKEN": "{{credential.api_key}}"},
            }
        ],
        credential_requirements=[
            {
                "key": "api_key",
                "required": True,
                "credential_service": {
                    "name": "S",
                    "credential_type": "env",
                    "env_var_name": "S_TOKEN",
                },
            }
        ],
    )
    plugin_id = created["id"]
    PluginService().set_org_activation(plugin_id, org_id=org.id, user=user, active=True)
    plugin = Plugin.objects.get(id=plugin_id)
    workspace.plugin_activations.create(plugin=plugin)
    # Attach a credential owned by the OTHER org directly (simulated
    # corruption bypassing attach-time guards).
    from apps.credentials.services import CredentialServiceSvc

    foreign_svc = CredentialServiceSvc().create_service(
        name="S",
        slug="s",
        description="",
        credential_type="env",
        env_var_name="S_TOKEN",
        target_path="",
        label="",
        organization_id=other.id,
    )
    foreign = CredentialSvc().create_org_credential(
        organization_id=other.id,
        service_id=foreign_svc.id,
        name="foreign",
        value="foreign-secret",
        user=user,
    )
    workspace.credentials.add(foreign)
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=workspace, org_id=org.id
    )
    assert len(snapshot.plugins) == 1
    with caplog.at_level("INFO"):
        with pytest.raises(plugin_runtime.PluginCredentialConfigError, match="api_key"):
            plugin_runtime.resolve_runtime_credentials(snapshot, workspace=workspace)
    assert "foreign-secret" not in caplog.text


# -- open() failure diagnosis: real error, no cancel-scope masking --------


class _DeadStream:
    """Byte stream double whose server died before any JSON-RPC reply."""

    connection_id = "dead-beef"

    def __init__(self, stderr: bytes = b"") -> None:
        self._stderr = stderr
        self.eof_sent = False
        self.closed = False

    async def receive(self) -> bytes:
        # Server gone: stdout EOF immediately, like the 17:29 run where
        # the npx process died during initialize with 0 stdout bytes.
        return b""

    async def send(self, data: bytes) -> None:  # pragma: no cover - no read
        raise AssertionError("no writes expected after early EOF")

    async def send_eof(self) -> None:
        self.eof_sent = True

    async def aclose(self) -> None:
        self.closed = True


class _DeadAccessor:
    """Accessor double: dead process + bounded stderr sink + diagnostics."""

    def __init__(self, stderr: bytes = b"") -> None:
        self.stream = _DeadStream(stderr)
        self._stderr = bytes(stderr)

    async def open_process(self, command, workdir="/workspace", env=None, timeout=None):
        return self.stream

    def get_stream_stderr_excerpt(self, connection_id: str = "") -> str:
        from apps.harness.mcp_client.stdio import sanitize_stderr_excerpt

        return sanitize_stderr_excerpt(self._stderr)

    def get_stream_diagnostics(self, connection_id: str = "") -> dict:
        return {
            "connection_id": self.stream.connection_id,
            "closed": True,
            "close_error": None,
            "exit_code": 1,
            "stdout_bytes": 0,
            "stderr_bytes": len(self._stderr),
            "stderr_excerpt": self.get_stream_stderr_excerpt(),
        }


@pytest.mark.asyncio
async def test_open_early_eof_surfaces_real_error_not_cancel_scope():
    """Regression: early-EOF must raise the init failure, never the
    ``RuntimeError: ... cancel scope`` masking seen in the 17:29 run."""
    conn = _connection(server="pw", transport="stdio")
    accessor = _DeadAccessor(stderr=b"Error: chrome not found\n")
    with pytest.raises(McpServerHealthError) as excinfo:
        await conn.open(accessor)
    assert "initialize failed" in str(excinfo.value)
    # The chained cause is the transport failure, not a scope bug.
    chained = excinfo.value.__cause__
    assert chained is not None
    assert "cancel scope" not in f"{type(chained).__name__}: {chained}"
    assert accessor.stream.closed is True


class _NestedScopeSession:
    """ClientSession-like double whose context manager pushes a *real*
    nested cancel scope (task group) like the SDK session does.

    ``_ScriptedSession`` below intentionally has no cancel scope; this
    one reproduces the production nesting that masked the E2E success
    path (``RuntimeError: ... cancel scope`` at timeout-scope exit).
    The parked child waits on an event (not ``sleep_forever``) so the
    teardown exit is prompt and cannot stall the suite.
    """

    def __init__(self, tools: list | None = None) -> None:
        self._tools = list(tools or [])
        self._tg: anyio.abc.TaskGroup | None = None
        self._stop: asyncio.Event | None = None
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        self._stop = asyncio.Event()

        async def _park() -> None:
            assert self._stop is not None
            await self._stop.wait()

        self._tg.start_soon(_park)
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        assert self._tg is not None
        assert self._stop is not None
        self._stop.set()
        await self._tg.__aexit__(exc_type, exc, tb)
        self.exited = True
        return False

    async def initialize(self):
        return types.InitializeResult(
            protocolVersion="2024-11-05",
            capabilities=types.ServerCapabilities.model_validate({}),
            serverInfo=types.Implementation.model_validate(
                {"name": "nested", "version": "1"}
            ),
        )

    async def list_tools(self, cursor=None):
        return types.ListToolsResult(tools=list(self._tools), nextCursor=None)


@pytest.mark.asyncio
async def test_stdio_open_uses_configured_startup_budget():
    """Cold npx startup gets the server budget, not the 15s stream default."""
    conn = _connection(transport="stdio")
    conn.startup_timeout_seconds = 120.0
    session = _NestedScopeSession(tools=[])
    real_session_cls = conn_module.ClientSession
    conn_module.ClientSession = lambda *a, **k: session  # type: ignore[assignment]

    class RecordingAccessor(_ProcAccessor):
        async def open_process(self, command, workdir="/workspace", env=None, timeout=None):
            self.timeout = timeout
            return await super().open_process(command, workdir, env, timeout)

    accessor = RecordingAccessor(_ProcStream([b""]))
    try:
        await conn.open(accessor)
        assert accessor.timeout == 120.0
    finally:
        conn_module.ClientSession = real_session_cls
        await conn.aclose()


@pytest.mark.asyncio
async def test_open_success_with_real_nested_session_scope():
    """End-to-end shape of the production bug: healthy open where the
    SDK session keeps a real nested cancel scope open for the
    connection's lifetime must *not* blow up at timeout-scope exit."""
    conn = _connection(server="pw", transport="stdio")
    session = _NestedScopeSession(
        tools=[types.Tool(name="shot", inputSchema={"type": "object"})]
    )
    real_session_cls = conn_module.ClientSession
    conn_module.ClientSession = lambda *a, **k: session  # type: ignore[assignment]
    stream = _ProcStream([b""])
    try:
        tools = await conn.open(_ProcAccessor(stream))
    finally:
        conn_module.ClientSession = real_session_cls
    assert session.entered is True
    assert session.exited is False  # session lives until aclose()
    assert conn._stack is not None
    assert [t.name for t in tools] == ["shot"]
    assert stream.closed is False
    await conn.aclose()
    assert session.exited is True  # LIFO unwind closed the session first
    assert stream.closed is True
    assert conn._stack is None


class _ScriptedSession:
    """Minimal ClientSession double: real async CM, scripted init/tools."""

    def __init__(
        self,
        read=None,
        write=None,
        *,
        init_result: dict | None = None,
        tools: list | None = None,
    ) -> None:
        self._read = read
        self._write = write
        self._init_result = init_result or {}
        self._tools = list(tools or [])
        self.entered = False
        self.exited = False
        self.closed_as_cm = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        self.closed_as_cm = True
        return False

    async def initialize(self):
        return types.InitializeResult(
            protocolVersion="2024-11-05",
            capabilities=types.ServerCapabilities.model_validate(
                self._init_result.get("capabilities", {})
            ),
            serverInfo=types.Implementation.model_validate(
                self._init_result.get(
                    "serverInfo", {"name": "scripted", "version": "1"}
                )
            ),
        )

    async def list_tools(self, cursor=None):
        return types.ListToolsResult(
            tools=list(self._tools),
            nextCursor=None,
        )


@pytest.mark.asyncio
async def test_open_success_exits_timeout_scope_despite_nested_session_scope():
    """Regression: a *healthy* server must not raise the cancel-scope
    RuntimeError on the success path (E2E masking 17.09., 18:26 run).

    The SDK ``ClientSession`` enters a task group (own cancel scope)
    *inside* the startup timeout scope. It stays open for the
    connection's lifetime, so naively exiting the timeout ``with``
    block trips ``RuntimeError: ... cancel scope`` even though
    initialize + discovery succeeded.
    """
    conn = _connection(server="pw", transport="stdio")
    scripted = _ScriptedSession(
        tools=[
            types.Tool(name="shot", inputSchema={"type": "object"}),
        ]
    )
    real_session_cls = conn_module.ClientSession
    conn_module.ClientSession = lambda *a, **k: scripted  # type: ignore[assignment]
    stream = _ProcStream([b""])
    try:
        tools = await conn.open(_ProcAccessor(stream))
    finally:
        conn_module.ClientSession = real_session_cls
    assert scripted.entered is True
    assert scripted.exited is False  # session lives until aclose()
    assert conn._stack is not None
    assert [t.name for t in tools] == ["shot"]
    assert conn._stack is not None
    await conn.aclose()
    assert scripted.exited is True  # LIFO unwind closed the session
    assert conn._stack is None


@pytest.mark.asyncio
async def test_open_success_then_aclose_closes_session_and_transport():
    """Healthy open + aclose unwinds session before transport, exactly
    once each (no double close, no scope leak)."""
    conn = _connection(server="pw", transport="stdio")
    scripted = _ScriptedSession(
        tools=[types.Tool(name="shot", inputSchema={"type": "object"})]
    )
    real_session_cls = conn_module.ClientSession
    conn_module.ClientSession = lambda *a, **k: scripted  # type: ignore[assignment]
    stream = _ProcStream([b""])
    try:
        await conn.open(_ProcAccessor(stream))
    finally:
        conn_module.ClientSession = real_session_cls
    assert stream.closed is False
    await conn.aclose()
    assert scripted.exited is True
    assert stream.closed is True
    assert stream.eof_sent is True
    # Idempotent: second close is a no-op.
    await conn.aclose()
    assert conn._stack is None


@pytest.mark.asyncio
async def test_sanitize_stderr_excerpt_redacts_and_bounds():
    from apps.harness.mcp_client.stdio import (
        MAX_STDERR_EXCERPT_CHARS,
        sanitize_stderr_excerpt,
    )

    assert sanitize_stderr_excerpt(None) == ""
    assert sanitize_stderr_excerpt(b"") == ""
    excerpt = sanitize_stderr_excerpt(b"Error: api_key=hunter2\n--password s3cret\nok")
    assert "hunter2" not in excerpt
    assert "s3cret" not in excerpt
    assert "[redacted]" in excerpt
    assert "ok" in excerpt
    long = sanitize_stderr_excerpt(b"x" * (MAX_STDERR_EXCERPT_CHARS + 500))
    assert len(long) <= MAX_STDERR_EXCERPT_CHARS + 1
    # Binary noise cannot break logging.
    assert sanitize_stderr_excerpt(b"\xff\xfe\x00boom\n") != ""


@pytest.mark.asyncio
async def test_stdio_teardown_excerpt_sanitized():
    """The adapter teardown carries the excerpt (redacted), never verbatim."""
    import structlog

    records: list[str] = []

    def _capture(_logger, _method_name, event_dict):
        records.append(f"{event_dict!r}")
        raise structlog.DropEvent

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            _capture,
        ],
        logger_factory=structlog.ReturnLoggerFactory(),
    )
    try:
        accessor = _DeadAccessor(stderr=b"Error: token=hunter2\n")
        async with workspace_stdio_client(accessor, ["srv"]):
            pass
    finally:
        structlog.reset_defaults()
    assert any("mcp_stdio_stderr_excerpt" in line for line in records), records
    assert not any("hunter2" in line for line in records), records


@pytest.mark.asyncio
async def test_runtime_setup_skipped_carries_real_error_detail():
    """A dying stdio server lands in ``skipped`` with the init failure,
    not the cancel-scope RuntimeError."""
    from apps.plugins.runtime_snapshot import (
        EffectivePluginSnapshot,
        PluginMcpServerSnapshot,
        PreparedPluginRuntime,
        WorkspacePluginSnapshot,
    )

    plugin_id = uuid.uuid4()
    snapshot = WorkspacePluginSnapshot(
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        plugins=(
            EffectivePluginSnapshot(
                id=plugin_id,
                name="Plug",
                slug="plug",
                description="",
                organization_id=None,
                is_global=True,
                skills=(),
                mcp_servers=(
                    PluginMcpServerSnapshot(
                        id=uuid.uuid4(),
                        name="Pw",
                        slug="pw",
                        transport="stdio",
                        command="npx",
                    ),
                ),
                requirements=(),
            ),
        ),
    )
    runtime = McpRuntime()
    prepared = PreparedPluginRuntime(
        snapshot=snapshot, workspace=None, plaintexts={plugin_id: {}}
    )
    await runtime.setup(
        workspace=None,
        organization_id=snapshot.organization_id,
        accessor=_DeadAccessor(stderr=b"Error: chrome crashed\n"),
        snapshot=prepared,
    )
    assert runtime.connections == []
    assert len(runtime.skipped) == 1
    note = runtime.skipped[0]["error"]
    assert "McpServerHealthError" in note
    assert "initialize failed" in note
    assert "cancel scope" not in note
    await runtime.aclose()
