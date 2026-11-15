"""Computer Use uses ordinary subagent tools and the shared permission gates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.harness.agents.definitions import get_agent, subagent_descriptions
from apps.harness.mcp_client.connection import McpTool
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.invocation import AuthorizedToolDispatcher
from apps.harness.tools.subagents import ALLOWED_SUBAGENT_TYPES, TaskTool


async def test_task_accepts_computeruse_subagent(fake_accessor) -> None:
    """task(subagent_type=computeruse) passes agent validation."""
    ctx = ToolContext(
        session_id="sess-parent",
        workspace_id="ws-1",
        accessor=fake_accessor,
        model="parent-model",
        registry=default_tool_registry(),
        depth=1,
        max_depth=1,
    )
    with pytest.raises(ToolError, match="depth limit"):
        await TaskTool().execute(
            {
                "description": "desktop",
                "prompt": "click save",
                "subagent_type": "computeruse",
            },
            ctx,
        )


async def test_task_rejects_unknown_subagent(fake_accessor) -> None:
    """Unknown subagent types are still rejected."""
    ctx = ToolContext(
        session_id="sess-parent",
        workspace_id="ws-1",
        accessor=fake_accessor,
        model="parent-model",
        registry=default_tool_registry(),
    )
    with pytest.raises(ToolError, match="Unknown subagent"):
        await TaskTool().execute(
            {
                "description": "bad",
                "prompt": "nope",
                "subagent_type": "nope",
            },
            ctx,
        )


def test_allowed_subagent_types_includes_computeruse() -> None:
    """Task tool advertises computeruse as a valid subagent."""
    assert "computeruse" in ALLOWED_SUBAGENT_TYPES
    assert "computeruse" in subagent_descriptions()


def test_computer_use_schemas_and_permission_gates(fake_accessor) -> None:
    """Specialist gets only its MCP surface; plan asks and explore denies."""
    registry = default_tool_registry()
    connection = SimpleNamespace(tool_policy={"execution_tools": ["js", "js_reset"]})
    name = "mcp_computer-use_cua_repl_js"
    registry.register(
        McpTool(
            namespaced_name=name,
            original_name="js",
            description="Computer Use",
            input_schema={"type": "object"},
            connection=connection,
            title_text="Computer Use",
        )
    )
    dispatcher = AuthorizedToolDispatcher(
        tools=registry,
        accessor=fake_accessor,
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
    )
    specialist = dispatcher._filtered_schemas(
        get_agent("computeruse"), "build", depth=1, max_depth=1
    )
    assert [tool.name for tool in specialist] == [name]
    assert dispatcher._decide(get_agent("build"), name, "js", "build") == "allow"
    assert dispatcher._decide(get_agent("plan"), name, "js", "plan") == "ask"
    assert dispatcher._decide(get_agent("explore"), name, "js", "build") == "deny"
