"""Mid-run plugin pickup: runtime refresh, reloader, skill tool, SDK server."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest

from apps.harness.mcp_client.connection import DiscoveredMcpTool, McpServerConnection
from apps.harness.mcp_client.reload import PluginChange, PluginReloader
from apps.harness.mcp_client.runtime import McpRuntime, RuntimeChange
from apps.harness.prompts.composer import render_skill_catalog
from apps.harness.tools.base import ToolContext, ToolError, ToolRegistry
from apps.harness.tools.skill import SkillCatalog, SkillTool
from apps.plugins.runtime import PluginCredentialConfigError
from apps.plugins.runtime_snapshot import (
    DesktopResourceSnapshot,
    EffectivePluginSnapshot,
    PluginMcpServerSnapshot,
    PluginResourcesSnapshot,
    PluginSkillSnapshot,
    PreparedPluginRuntime,
    SkillEntry,
    WorkspacePluginSnapshot,
)


def _server(slug: str, *, version: str = "v1", desktop: bool = False):
    return PluginMcpServerSnapshot(
        id=uuid4(),
        name=slug.title(),
        slug=slug,
        transport="stdio",
        command="server",
        updated_at=version,
        resources=PluginResourcesSnapshot(
            desktop=DesktopResourceSnapshot(activation="first_tool")
            if desktop
            else None
        ),
    )


def _plugin(slug: str, *servers, skills=()) -> EffectivePluginSnapshot:
    return EffectivePluginSnapshot(
        id=uuid4(),
        name=slug.title(),
        slug=slug,
        description="",
        organization_id=None,
        is_global=False,
        scope="personal",
        mcp_servers=tuple(servers),
        skills=tuple(skills),
    )


def _prepared(*plugins: EffectivePluginSnapshot) -> PreparedPluginRuntime:
    return PreparedPluginRuntime(
        WorkspacePluginSnapshot(
            workspace_id=uuid4(), organization_id=uuid4(), plugins=plugins
        )
    )


@pytest.fixture
def fake_open(monkeypatch):
    """Open connections without a workspace; each server offers one tool."""
    opened: list[McpServerConnection] = []
    failing: set[str] = set()

    async def open_connection(conn, accessor):
        if conn.server_slug in failing:
            raise RuntimeError("npx: package not found")
        conn._accessor = accessor
        conn._session = AsyncMock()
        # Like the SDK session, each connection nests a cancel scope in the
        # run's task: closing them out of order raises RuntimeError.
        stack = AsyncExitStack()
        stack.enter_context(anyio.CancelScope())
        conn._stack = stack
        conn.tools = [
            DiscoveredMcpTool(
                name="search",
                namespaced_name=f"mcp_{conn.plugin_slug}_{conn.server_slug}_search",
                description="Search.",
                input_schema={"type": "object", "properties": {}},
                title="Search",
            )
        ]
        opened.append(conn)
        return []

    monkeypatch.setattr(McpServerConnection, "open", open_connection)
    return SimpleNamespace(opened=opened, failing=failing)


async def test_refresh_adds_changes_and_removes_servers(fake_open) -> None:
    alpha = _plugin("alpha", _server("one"))
    runtime = McpRuntime(scope_enabled=False)
    registry = ToolRegistry()
    accessor = SimpleNamespace(workspace_id="ws")
    try:
        await runtime.setup(prepared=_prepared(alpha), accessor=accessor)
        runtime.register_tools(registry)
        assert registry.names() == ["mcp_alpha_one_search"]

        # Nothing changed: nothing is reopened.
        assert not await runtime.refresh(
            prepared=_prepared(alpha), accessor=accessor, registry=registry
        )

        beta = _plugin("beta", _server("two"))
        change = await runtime.refresh(
            prepared=_prepared(alpha, beta), accessor=accessor, registry=registry
        )
        assert change.added_tools == ["mcp_beta_two_search"]
        assert "mcp_beta_two_search" in registry

        # An edited server (new version) restarts with the same tool name.
        edited = replace(
            beta, mcp_servers=(replace(beta.mcp_servers[0], updated_at="v2"),)
        )
        change = await runtime.refresh(
            prepared=_prepared(alpha, edited), accessor=accessor, registry=registry
        )
        assert change.restarted_tools == ["mcp_beta_two_search"]
        assert change.added_tools == change.removed_tools == []
        assert "mcp_beta_two_search" in registry

        # Disabling a plugin removes its tools at once; the connection
        # (opened first) stays open until the run closes all in order.
        change = await runtime.refresh(
            prepared=_prepared(edited), accessor=accessor, registry=registry
        )
        assert change.removed_tools == ["mcp_alpha_one_search"]
        assert registry.names() == ["mcp_beta_two_search"]
        assert not fake_open.opened[0]._closed
        assert runtime.register_tools(ToolRegistry()) == []
    finally:
        await runtime.aclose()
    assert all(conn._closed for conn in fake_open.opened)


async def test_refresh_reports_failures_once_and_defers_desktop(fake_open) -> None:
    runtime = McpRuntime(scope_enabled=False)
    registry = ToolRegistry()
    accessor = SimpleNamespace(workspace_id="ws")
    fake_open.failing.add("broken")
    plugin = _plugin("tools", _server("broken"), _server("screen", desktop=True))
    try:
        change = await runtime.refresh(
            prepared=_prepared(plugin), accessor=accessor, registry=registry
        )
        assert [failure["server"] for failure in change.failed] == ["broken"]
        assert "npx: package not found" in change.failed[0]["error"]
        assert change.deferred == ["Screen"]
        assert registry.names() == []
        # The same broken version is not retried on every step.
        again = await runtime.refresh(
            prepared=_prepared(plugin), accessor=accessor, registry=registry
        )
        assert again.failed == []
    finally:
        await runtime.aclose()


async def test_reloader_checks_fingerprint_and_reports_skills(fake_open) -> None:
    skill = PluginSkillSnapshot(
        id=uuid4(),
        name="Release",
        slug="release",
        body="Steps",
        position=0,
        description="Ship a release.",
    )
    states = {"fingerprint": "a", "prepared": _prepared()}
    prepare_calls = []

    def prepare():
        prepare_calls.append(1)
        return states["prepared"]

    catalog = SkillCatalog()
    registry = ToolRegistry()
    reloader = PluginReloader(
        fingerprint=lambda: states["fingerprint"],
        prepare=prepare,
        runtime=McpRuntime(scope_enabled=False),
        registry=registry,
        accessor=SimpleNamespace(workspace_id="ws"),
        catalog=catalog,
    )
    try:
        await reloader.prime()
        assert await reloader.check() is None
        assert prepare_calls == []

        states["fingerprint"] = "b"
        states["prepared"] = _prepared(_plugin("ops", _server("cli"), skills=(skill,)))
        change = await reloader.check()
        assert change is not None and change.tools_changed
        assert change.added_skills == ["release"]
        message = change.message()
        assert "mcp_ops_cli_search" in message and "release" in message
        assert catalog.get("release").description == "Ship a release."
        assert await reloader.check() is None
    finally:
        await reloader.runtime.aclose()


async def test_reloader_reports_credential_errors_without_failing() -> None:
    def prepare():
        raise PluginCredentialConfigError("Plugin 'x' is missing credential 'token'")

    reloader = PluginReloader(
        fingerprint=lambda: "changed",
        prepare=prepare,
        runtime=McpRuntime(scope_enabled=False),
        registry=ToolRegistry(),
        accessor=None,
    )
    change = await reloader.check()
    assert change is not None and not change.tools_changed
    assert "missing credential 'token'" in change.message()


def test_change_message_lists_deferred_and_removed() -> None:
    change = PluginChange(
        tools=RuntimeChange(removed_tools=["mcp_a_b_c"], deferred=["Playwright"])
    )
    text = change.message()
    assert "no longer available: mcp_a_b_c" in text
    assert "Playwright take effect with the next message" in text


async def test_skill_tool_loads_catalog_entries() -> None:
    catalog = SkillCatalog(
        [
            SkillEntry(
                identifier="release-checklist",
                name="Release checklist",
                plugin_name="Ops",
                description="Ship releases.",
                body="1. Tag\n2. Push",
            )
        ]
    )
    tool = SkillTool(catalog)
    ctx = ToolContext(session_id="s", workspace_id="w", accessor=None)
    result = await tool.execute({"name": "Release checklist"}, ctx)
    assert result.output.startswith('<skill name="release-checklist" plugin="Ops">')
    assert "1. Tag" in result.output
    with pytest.raises(ToolError, match="Available: release-checklist"):
        await tool.execute({"name": "missing"}, ctx)
    added, removed = catalog.replace([])
    assert (added, removed) == ([], ["release-checklist"])
    assert not catalog


def test_skill_catalog_prompt_lists_names_and_descriptions_only() -> None:
    text = render_skill_catalog([("release-checklist", "Ship releases.")])
    assert "- release-checklist: Ship releases." in text
    assert "skill tool" in text


def test_snapshot_skill_entries_disambiguate_duplicate_slugs() -> None:
    def skill(body: str) -> PluginSkillSnapshot:
        return PluginSkillSnapshot(
            id=uuid4(), name="Review", slug="review", body=body, position=0
        )

    snapshot = WorkspacePluginSnapshot(
        workspace_id=uuid4(),
        organization_id=uuid4(),
        plugins=(
            _plugin("frontend", skills=(skill("UI"),)),
            _plugin("backend", skills=(skill("API"),)),
        ),
    )
    assert [entry.identifier for entry in snapshot.skill_entries] == [
        "frontend-review",
        "backend-review",
    ]
    document = snapshot.plugin_skills[0]
    assert document.startswith("---\nname: frontend-review\ndescription: ")
    assert document.rstrip().endswith("UI")


async def test_dynamic_sdk_server_relists_changed_tools() -> None:
    """The CLI re-reads tools/list on reconnect; the list must be live."""
    from claude_agent_sdk import tool as sdk_tool
    from mcp.shared.memory import create_connected_server_and_client_session

    from apps.harness.engines.claude.sdk_server import DynamicSdkServer

    async def handler(arguments):
        return {"content": [{"type": "text", "text": json.dumps(arguments)}]}

    def definition(name: str):
        return sdk_tool(name, "A tool.", {"type": "object", "properties": {}})(handler)

    server = DynamicSdkServer()
    assert server.set_tools([definition("alpha")]) is True
    instance = server.config["instance"]
    async with create_connected_server_and_client_session(instance) as client:
        assert [tool.name for tool in (await client.list_tools()).tools] == ["alpha"]
        assert server.set_tools([definition("alpha"), definition("beta")]) is True
        assert server.set_tools([definition("alpha"), definition("beta")]) is False
        assert [tool.name for tool in (await client.list_tools()).tools] == [
            "alpha",
            "beta",
        ]
        result = await client.call_tool("beta", {"x": 1})
        assert result.isError is False
        assert json.loads(result.content[0].text) == {"x": 1}
        missing = await client.call_tool("gamma", {})
        assert missing.isError is True
    await anyio.sleep(0)


async def test_native_runner_offers_new_tools_in_the_same_turn() -> None:
    """A plugin enabled during a step reaches the next provider call."""
    from collections.abc import AsyncIterator

    from pydantic import BaseModel

    from apps.harness.engines.base import RunOptions
    from apps.harness.permissions.evaluator import PermissionEvaluator
    from apps.harness.providers.base import (
        ChatOptions,
        Delta,
        LLMMessage,
        ProviderAdapter,
        ToolSchema,
        Usage,
    )
    from apps.harness.providers.resolver import StaticModelResolver
    from apps.harness.runner import HarnessRunner
    from apps.harness.tests.conftest import FakeAccessor
    from apps.harness.tools import default_tool_registry
    from apps.harness.tools.base import Tool, ToolResult

    class EchoArgs(BaseModel):
        pass

    class Echo(Tool):
        name = "mcp_ops_cli_echo"
        description = "Echo."
        args_schema: type[BaseModel] = EchoArgs

        async def execute(self, args, ctx):  # type: ignore[no-untyped-def]
            return ToolResult(output="echoed")

    class Provider(ProviderAdapter):
        name = "fake"

        def __init__(self) -> None:
            self.tools: list[list[str]] = []
            self.messages: list[list[LLMMessage]] = []

        async def chat_stream(  # type: ignore[no-untyped-def]
            self,
            model: str,
            messages: list[LLMMessage],
            tools: list[ToolSchema],
            opts: ChatOptions | None = None,
        ) -> AsyncIterator[Delta]:
            self.tools.append([schema.name for schema in tools])
            self.messages.append(list(messages))
            if len(self.tools) == 1:
                yield Delta(
                    tool_calls=(
                        {
                            "index": 0,
                            "id": "c1",
                            "name": "read",
                            "arguments": json.dumps({"path": "a.txt"}),
                        },
                    ),
                    usage=Usage(1, 1, 2),
                )
            elif len(self.tools) == 2:
                yield Delta(
                    tool_calls=(
                        {
                            "index": 0,
                            "id": "c2",
                            "name": "mcp_ops_cli_echo",
                            "arguments": "{}",
                        },
                    ),
                    usage=Usage(1, 1, 2),
                )
            else:
                yield Delta(text="done", usage=Usage(1, 1, 2))

    registry = default_tool_registry()
    checks = []

    async def refresh():
        checks.append(1)
        if len(checks) != 1:
            return None
        registry.register(Echo())
        return PluginChange(tools=RuntimeChange(added_tools=["mcp_ops_cli_echo"]))

    async def emit(_event):  # type: ignore[no-untyped-def]
        return None

    provider = Provider()
    runner = HarnessRunner(
        model_resolver=StaticModelResolver(provider).resolve,
        tools=registry,
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
        accessor=FakeAccessor(files={"/workspace/a.txt": b"hi"}),
        emit=emit,
    )
    result = await runner.run(
        "go", "build", "m", "build", RunOptions(refresh_plugins=refresh)
    )
    assert result.output == "done"
    assert "mcp_ops_cli_echo" not in provider.tools[0]
    assert "mcp_ops_cli_echo" in provider.tools[1]
    note = provider.messages[1][-1]
    assert note.role == "user" and "mcp_ops_cli_echo" in note.content
    assert provider.messages[2][-1].role == "tool"


async def test_real_claude_cli_sees_tools_of_a_plugin_enabled_mid_turn(
    tmp_path,
) -> None:
    """The pinned CLI re-reads the OpenCuria server's tools after a reload.

    The model calls a tool whose side effect enables a plugin; the reloader
    registers the plugin's tool, the engine swaps the SDK server's tool list
    and reconnects, and a later model request in the same turn offers it.
    """
    from pydantic import BaseModel

    from apps.harness.tests.test_claude_engine import (
        _api_final_message,
        _api_tool_message,
        _run_local_cli_fixture,
        require_pinned_cli,
    )
    from apps.harness.tools import default_tool_registry
    from apps.harness.tools.base import Tool, ToolResult

    require_pinned_cli()

    class NoArgs(BaseModel):
        pass

    class Probe(Tool):
        name = "mcp_probe_server_ping"
        original_name = "ping"
        description = "Ping the probe."
        args_schema: type[BaseModel] = NoArgs

        async def execute(self, args, ctx):  # type: ignore[no-untyped-def]
            return ToolResult(output="pong")

    class EnablePlugin(Tool):
        name = "mcp_opencuria_mcp_plugin_set_active"
        original_name = "plugin_set_active"
        description = "Enable the probe plugin."
        args_schema: type[BaseModel] = NoArgs

        async def execute(self, args, ctx):  # type: ignore[no-untyped-def]
            enabled.append(True)
            return ToolResult(output="enabled")

    enabled: list[bool] = []
    registry = default_tool_registry()
    registry.register(EnablePlugin())

    async def refresh():
        if enabled and "mcp_probe_server_ping" not in registry:
            registry.register(Probe())
            return PluginChange(
                tools=RuntimeChange(added_tools=["mcp_probe_server_ping"])
            )
        return None

    seen: list[set[str]] = []
    payloads: list[dict] = []

    def responder(payload, index):  # type: ignore[no-untyped-def]
        names = {str(item.get("name") or "") for item in payload.get("tools") or []}
        seen.append(names)
        payloads.append(payload)
        if not enabled:
            enable = next(name for name in names if name.endswith("plugin_set_active"))
            return _api_tool_message(enable, f"call-{index}", {})
        offered = next((name for name in names if name.endswith("_ping")), None)
        history = json.dumps(payload.get("messages", []))
        if offered and "pong" not in history:
            return _api_tool_message(offered, f"call-{index}", {})
        return _api_final_message(f"final-{index}")

    result, events, *_rest = await _run_local_cli_fixture(
        tmp_path,
        tool_name="",
        tool_arguments={},
        tools=registry,
        prompt="Enable the probe, then ping it.",
        responder=responder,
        refresh_plugins=refresh,
    )
    assert result.output == "fixture answer"
    assert enabled
    assert not any(name.endswith("mcp_probe_server_ping") for name in seen[0])
    # The request right after the enabling call already offers the new tool,
    # on the plugin server, and the enabling call's result carries the note.
    enabling = next(
        index
        for index, names in enumerate(seen)
        if any(name.endswith("plugin_set_active") for name in names)
    )
    assert "mcp__opencuria__plugin_set_active" in seen[enabling]
    assert "mcp__opencuria_plugins__ping" in seen[enabling + 1]
    assert "New tools you can call now: mcp__opencuria_plugins__ping" in json.dumps(
        payloads[enabling + 1]["messages"]
    )
    completed = [
        event.get("tool") for event in events if event.get("type") == "tool_completed"
    ]
    assert "mcp_probe_server_ping" in completed, completed
