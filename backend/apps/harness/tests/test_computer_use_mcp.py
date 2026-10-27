"""Model exposure and control-plane lifecycle of the desktop MCP connection."""

import asyncio
from unittest.mock import AsyncMock

import anyio
import pytest
from mcp.types import CallToolResult, TextContent

from apps.harness.mcp_client.connection import (
    DiscoveredMcpTool,
    McpServerConnection,
    McpServerHealthError,
)
from apps.harness.tools.base import ToolError
from apps.plugins.bundled.computer_use import TOOL_POLICY


def connection():
    return McpServerConnection(
        plugin_id="p",
        plugin_slug="computer-use",
        plugin_name="Computer Use",
        server_id="s",
        server_slug="cua_repl",
        server_name="CUA",
        transport="stdio",
        command=[],
        cwd="/workspace",
        tool_policy=TOOL_POLICY,
        run_context={"session_id": "session", "turn_id": "turn"},
    )


def test_internal_tools_are_filtered_and_missing_contract_fails():
    conn = connection()
    conn.tools = [
        DiscoveredMcpTool(name, name, "", {"type": "object"}, name)
        for name in ("js", "js_reset", "turn_ended", "extra")
    ]
    assert [t.name for t in conn.model_tools()] == ["js", "js_reset"]
    conn.tools = conn.tools[:2]
    with pytest.raises(McpServerHealthError, match="missing"):
        conn.model_tools()


async def test_lifecycle_bypasses_busy_execution_lock_and_is_idempotent():
    conn = connection()
    conn._lock = anyio.Lock()
    conn._session = AsyncMock()
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def call(name, args, **kwargs):
        assert kwargs["meta"]["opencuria/context"]["turn_id"] == "turn"
        if name == "js":
            started.set()
            await stopped.wait()
        else:
            assert name == "turn_ended"
            stopped.set()
        return CallToolResult(content=[TextContent(type="text", text="ok")])

    conn._session.call_tool.side_effect = call
    execution = asyncio.create_task(
        conn.call_tool("js", {"code": "await cua.getState()"})
    )
    await started.wait()
    await asyncio.wait_for(conn.finish(), 1)
    await execution
    await conn.finish()
    assert conn._session.call_tool.await_count == 2
    with pytest.raises(ToolError, match="Internal"):
        await conn.call_tool("turn_ended", {})


@pytest.mark.django_db
def test_stop_automation_finds_only_current_busy_desktop_runs(harness_workspace):
    """Root plugin usage participates in stop automation, without old-turn matches."""
    from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
    from apps.harness.repositories import HarnessSessionRepository

    workspace = harness_workspace

    def session(agent="build", status="busy"):
        return HarnessSession.objects.create(
            workspace=workspace,
            organization_id=workspace.runner.organization_id,
            created_by=workspace.created_by,
            agent_name=agent,
            status=status,
        )

    def message(owner, position, tool):
        assistant = HarnessMessage.objects.create(
            session=owner, role="assistant", position=position
        )
        HarnessPart.objects.create(
            message=assistant, type="tool", position=0, input={"tool": tool}
        )

    stale = session()
    message(stale, 0, "mcp_computer-use_cua_repl_js")
    message(stale, 1, "read")
    current = session()
    message(current, 0, "mcp_computer-use_cua_repl_js")
    specialist = session("computeruse")
    idle = session(status="idle")
    message(idle, 0, "mcp_computer-use_cua_repl_js")
    assert {
        row.id
        for row in HarnessSessionRepository.list_busy_computeruse_for_workspace(
            workspace.id
        )
    } == {current.id, specialist.id}
