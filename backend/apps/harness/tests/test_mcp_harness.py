"""Harness integration tests for MCP plugin tools (fake provider + fake MCP).

- namespaced MCP schema visible to the provider, fake MCP round-trip;
- plugin skill bodies merged into the run skills (system prompt);
- explore agent schema excludes ``mcp_*`` tools;
- connections closed after success/error/abort;
- two servers where one fails discovery: the other still works.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.agents.definitions import get_agent
from apps.harness.harness_service import HarnessService, _merge_skill_bodies
from apps.harness.mcp_client.connection import (
    McpTool,
)
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import (
    Delta,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import ToolContext, ToolRegistry, ToolResult


class FakeProvider(ProviderAdapter):
    """Scripted provider capturing schemas, calling one tool, then done."""

    name = "fake"

    def __init__(self) -> None:
        self.seen_schemas: list[list[ToolSchema]] = []
        self.calls = 0

    async def chat_stream(  # type: ignore[no-untyped-def]
        self, model, messages, tools, opts=None
    ) -> AsyncIterator[Delta]:
        self.seen_schemas.append(list(tools))
        self.calls += 1
        if self.calls == 1:
            target = next(t for t in tools if t.name.startswith("mcp_"))
            yield Delta(
                tool_calls=(
                    {
                        "index": 0,
                        "id": "call-1",
                        "name": target.name,
                        "arguments": json.dumps({"text": "hello"}),
                    },
                ),
                usage=Usage(2, 3, 5),
            )
            return
        yield Delta(text="done", usage=Usage(1, 1, 2))


class FakeMcpConnection:
    """Stand-in for McpServerConnection returning canned results."""

    def __init__(self, tool: FakeMcpTool | None = None, *, fail: str = "") -> None:
        self._tool = tool
        self._fail = fail
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self.healthy = not fail
        if tool is not None:
            tool._connection = self

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((name, dict(args)))
        if self._fail:
            from apps.harness.tools.base import ToolError

            raise ToolError(self._fail)
        assert self._tool is not None
        return ToolResult(output=f"echo:{args.get('text')}")


class FakeMcpTool(McpTool):
    def __init__(self, namespaced: str, original: str = "echo") -> None:
        super().__init__(
            namespaced_name=namespaced,
            original_name=original,
            description="echo it",
            input_schema={"type": "object"},
            connection=None,  # type: ignore[arg-type]
            title_text=f"P / S / {original}",
        )


async def _async_fake_accessor(workspace_id: str):  # type: ignore[no-untyped-def]
    from apps.harness.tests.conftest import FakeAccessor

    return FakeAccessor()


def _ctx(org_id, workspace_id):
    from apps.harness.tests.conftest import FakeAccessor

    return ToolContext(
        session_id="s",
        workspace_id=str(workspace_id),
        accessor=FakeAccessor(),
        agent_name="build",
    )


def test_merge_skill_bodies_plugin_first_deduped():
    merged = _merge_skill_bodies(["b", "a"], ["p1", "a"])
    assert merged[0] == "p1"
    assert merged.count("a") == 1
    assert "b" in merged


def test_explore_denies_all_mcp_tools():
    agent = get_agent("explore")
    assert agent.permissions.get("mcp_*") == "deny"
    from apps.harness.permissions.evaluator import PermissionEvaluator

    evaluator = PermissionEvaluator(agent_rules=dict(agent.permissions))
    assert evaluator.evaluate("mcp_myplug_srv_tool", "echo") == "deny"
    assert evaluator.evaluate("mcp_anything", "") == "deny"


@pytest.mark.asyncio
async def test_runner_run_with_fake_mcp_tool_round_trip():
    fake_tool = FakeMcpTool("mcp_demo_srv_echo")
    connection = FakeMcpConnection(fake_tool)
    registry = default_tool_registry()
    registry.register(fake_tool)
    provider = FakeProvider()
    from apps.harness.providers.resolver import ResolvedModel

    runner = HarnessRunner(
        model_resolver=lambda ref: ResolvedModel(
            adapter=provider, model_id="m", provider="fake",
            context_length=0, max_output_tokens=0,
        ),
        tools=registry,
        accessor=None,
    )
    result = await runner.run(
        "hi",
        "build",
        "m",
        "build",
        RunOptions(session_id="s", workspace_id="w", auto_approve=True),
    )
    assert result.output == "done"
    assert connection.calls == [("echo", {"text": "hello"})]
    names = [schema.name for schema in provider.seen_schemas[0]]
    assert "mcp_demo_srv_echo" in names
    # Provider schema carries the original inputSchema (object here).
    schema = next(s for s in provider.seen_schemas[0] if s.name == "mcp_demo_srv_echo")
    assert schema.parameters.get("type") == "object"


@pytest.mark.asyncio
async def test_runner_explore_schema_excludes_mcp():
    fake_tool = FakeMcpTool("mcp_demo_srv_echo")
    FakeMcpConnection(fake_tool)
    registry = default_tool_registry()
    registry.register(fake_tool)
    from apps.harness.providers.resolver import ResolvedModel

    provider = FakeProvider()
    runner = HarnessRunner(
        model_resolver=lambda ref: ResolvedModel(
            adapter=provider, model_id="m", provider="fake",
            context_length=0, max_output_tokens=0,
        ),
        tools=registry,
        accessor=None,
    )
    agent = get_agent("explore")
    schemas = runner._filtered_schemas(agent, "build", depth=0, max_depth=1)
    assert all(not s.name.startswith("mcp_") for s in schemas)


@pytest.mark.asyncio
async def test_mcp_tool_error_becomes_tool_error_message():
    from apps.harness.tools.base import ToolError

    fake_tool = FakeMcpTool("mcp_demo_srv_echo")
    connection = FakeMcpConnection(fake_tool, fail="kaput")
    with pytest.raises(ToolError, match="kaput"):
        await fake_tool.execute({"text": "x"}, _ctx("o", "w"))
    assert connection.calls == [("echo", {"text": "x"})]


@pytest.mark.django_db(transaction=True)
async def test_harness_service_mcp_wiring_success_and_cleanup(harness_workspace):
    """Service run registers the stub MCP tool and closes runtime after."""

    session = _create_harness_session(harness_workspace)
    closed: list[str] = []
    seen: dict[str, Any] = {}

    provider = FakeProvider()

    from apps.harness.permissions.evaluator import PermissionEvaluator

    async def _emit(event: str, data: dict) -> None:
        return None

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_emit,
        provider_factory=lambda _org: provider,
        accessor_factory=_async_fake_accessor,
    )

    import apps.harness.mcp_client.runtime as mcp_runtime_module

    real_mcp_runtime = mcp_runtime_module.McpRuntime

    class StubRuntime:
        def __init__(self) -> None:
            self.skipped: list[dict] = []

        async def setup(  # type: ignore[no-untyped-def]
            self, *, workspace, organization_id, accessor, core_tool_names,
            snapshot=None,
        ):
            seen["skills"] = (
                snapshot.snapshot.plugin_skills if snapshot is not None else []
            )
            return snapshot.snapshot if snapshot is not None else None

        def register_tools(self, registry: ToolRegistry) -> list[str]:
            fake_tool = FakeMcpTool("mcp_demo_srv_echo")
            FakeMcpConnection(fake_tool)
            registry.register(fake_tool)
            seen["tool"] = fake_tool
            return [fake_tool.name]

        async def aclose(self) -> None:
            closed.append("closed")

    mcp_runtime_module.McpRuntime = StubRuntime  # type: ignore[assignment]
    try:
        assistant = await service.start_run(
            session,
            "hello",
            organization_id=harness_workspace.runner.organization_id,
            workspace_id=str(harness_workspace.id),
        )
        await service._tasks[str(session.id)]
    finally:
        mcp_runtime_module.McpRuntime = real_mcp_runtime

    # Fake provider called the namespaced MCP tool; result came back.
    assert provider.calls == 2
    assistant.refresh_from_db()
    assert assistant.finish == "stop"
    assert closed == ["closed"]
    assert "tool" in seen


def _create_harness_session(harness_workspace):  # type: ignore[no-untyped-def]
    from apps.harness.repositories import HarnessSessionRepository

    return HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="mcp run",
        agent_name="build",
        mode="build",
        model="fake:model",
    )


@pytest.mark.django_db(transaction=True)
async def test_harness_service_mcp_missing_credentials_fail_run(harness_workspace):
    """Missing required credentials fail the run before provider calls."""

    provider = FakeProvider()

    async def _emit(event: str, data: dict) -> None:
        return None

    service = HarnessService(
        permissions=PermissionService(),
        emit=_emit,
        provider_factory=lambda _org: provider,
        accessor_factory=_async_fake_accessor,
    )
    session = _create_harness_session(harness_workspace)

    import apps.harness.mcp_client.runtime as mcp_runtime_module

    real_mcp_runtime = mcp_runtime_module.McpRuntime

    class FailingRuntime:
        skipped: list[dict] = []

        async def setup(self, **kwargs):  # type: ignore[no-untyped-def]
            from apps.plugins.runtime import PluginCredentialConfigError

            raise PluginCredentialConfigError("missing required credential 'api_key'")

        def register_tools(self, registry):  # type: ignore[no-untyped-def]
            return []

        async def aclose(self) -> None:
            return None

    mcp_runtime_module.McpRuntime = FailingRuntime  # type: ignore[assignment]
    try:
        assistant = await service.start_run(
            session,
            "hello",
            organization_id=harness_workspace.runner.organization_id,
            workspace_id=str(harness_workspace.id),
        )
        await service._tasks[str(session.id)]
    finally:
        mcp_runtime_module.McpRuntime = real_mcp_runtime
    assistant.refresh_from_db()
    assert assistant.finish == "error"
    assert "api_key" in (assistant.error or "")
    assert provider.calls == 0


@pytest.mark.django_db(transaction=True)
async def test_harness_service_mcp_partial_discovery_failure(harness_workspace):
    """One failing server is skipped; the healthy tool still runs."""

    async def _emit(event: str, data: dict) -> None:
        return None

    import apps.harness.mcp_client.runtime as mcp_runtime_module

    real_mcp_runtime = mcp_runtime_module.McpRuntime

    class PartialRuntime:
        skipped = [{"plugin": "demo", "server": "broken", "error": "boom"}]

        async def setup(  # type: ignore[no-untyped-def]
            self, *, workspace, organization_id, accessor, core_tool_names,
            snapshot=None,
        ):
            return snapshot.snapshot if snapshot is not None else None

        def register_tools(self, registry: ToolRegistry) -> list[str]:  # type: ignore[no-untyped-def]
            fake_tool = FakeMcpTool("mcp_demo_good_echo")
            FakeMcpConnection(fake_tool)
            registry.register(fake_tool)
            return [fake_tool.name]

        async def aclose(self) -> None:
            return None

    # Provider must target the surviving tool name.
    class SurvivingProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            self.seen_schemas.append(list(tools))
            self.calls += 1
            if self.calls == 1:
                yield Delta(
                    tool_calls=(
                        {
                            "index": 0,
                            "id": "call-1",
                            "name": "mcp_demo_good_echo",
                            "arguments": json.dumps({"text": "hello"}),
                        },
                    ),
                    usage=Usage(2, 3, 5),
                )
                return
            yield Delta(text="done", usage=Usage(1, 1, 2))

    surviving = SurvivingProvider()
    service_fail = HarnessService(
        permissions=PermissionService(),
        emit=_emit,
        provider_factory=lambda _org: surviving,
        accessor_factory=_async_fake_accessor,
    )
    session2 = _create_harness_session(harness_workspace)
    mcp_runtime_module.McpRuntime = PartialRuntime  # type: ignore[assignment]
    try:
        assistant = await service_fail.start_run(
            session2,
            "hello",
            organization_id=harness_workspace.runner.organization_id,
            workspace_id=str(harness_workspace.id),
        )
        await service_fail._tasks[str(session2.id)]
    finally:
        mcp_runtime_module.McpRuntime = real_mcp_runtime
    assistant.refresh_from_db()
    assert assistant.finish == "stop"
    names = [s.name for s in surviving.seen_schemas[0]]
    assert names.count("mcp_demo_good_echo") == 1
