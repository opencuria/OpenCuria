"""Focused Claude policy tests for identity, paths and child scope."""

from __future__ import annotations

import pytest

from apps.harness.agents.definitions import canonical_subagent_type, get_agent
from apps.harness.engines.claude.permissionbridge import PermissionBridge
from apps.harness.engines.claude.policy import decide_tool
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.runner import RunOptions
from apps.harness.tools import default_tool_registry


@pytest.mark.parametrize("source_name", ["Write", "Edit", "NotebookEdit"])
@pytest.mark.parametrize(
    ("agent_name", "expected"),
    [("plan", "ask"), ("general", "allow"), ("explore", "deny")],
)
def test_plan_file_edits_follow_scoped_agent_permissions(
    source_name: str, agent_name: str, expected: str
) -> None:
    """Plan uses each agent's edit rules, not an unconditional mode-level ask."""
    decision, _event_tool, _action, _title = decide_tool(
        source_name=source_name,
        arguments={"file_path": "/workspace/change.txt", "content": "x"},
        tools=default_tool_registry(),
        agent=get_agent(agent_name),
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
        mode="plan",
        depth=1 if agent_name != "plan" else 0,
        max_depth=2,
    )
    assert decision == expected


def test_global_bash_ask_applies_to_plan_test_commands() -> None:
    bridge = PermissionBridge(
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(global_rules={"bash": "ask"}),
        options=RunOptions(session_id="plan-ask"),
        agent=get_agent("plan"),
        mode="plan",
        max_depth=2,
    )
    decision, *_ = bridge.decide(
        "Bash", {"command": "pytest -q"}, agent=get_agent("plan"), depth=0
    )
    assert decision == "ask"


def test_plan_child_edit_deny_overrides_agent_allow() -> None:
    tools = default_tool_registry()
    decision, *_ = decide_tool(
        source_name="Write",
        arguments={"file_path": "/workspace/blocked.txt", "content": "x"},
        tools=tools,
        agent=get_agent("general"),
        evaluator=PermissionEvaluator(global_rules={"edit": "deny"}),
        mode="plan",
        depth=1,
        max_depth=2,
    )
    assert decision == "deny"


def test_native_identity_is_explicit_and_file_paths_are_canonicalized() -> None:
    tools = default_tool_registry()
    evaluator = PermissionEvaluator(
        global_rules={"edit": {"/workspace/secret.txt": "deny"}}
    )

    decision, event_tool, action, _title = decide_tool(
        source_name="Write",
        arguments={"file_path": "../secret.txt", "content": "x"},
        tools=tools,
        agent=get_agent("build"),
        evaluator=evaluator,
        mode="build",
        depth=0,
        max_depth=2,
        cwd="/workspace/project",
    )
    assert (decision, event_tool, action) == (
        "deny",
        "write",
        "/workspace/secret.txt",
    )

    decision, event_tool, _action, _title = decide_tool(
        source_name="Edit",
        arguments={"file_path": "/workspace/notes.txt", "old_string": "a"},
        tools=tools,
        agent=get_agent("build"),
        evaluator=PermissionEvaluator(),
        mode="build",
        depth=0,
        max_depth=2,
    )
    assert (decision, event_tool) == ("allow", "edit")


@pytest.mark.parametrize(
    "wire_name",
    ["persist_note", "mcp__opencuria__persist_note"],
)
def test_native_and_mcp_identity_routes_remain_explicit(wire_name: str) -> None:
    from apps.harness.tests.test_claude_permission_bridge import PersistNoteTool
    from apps.harness.tools.base import ToolRegistry

    tool = PersistNoteTool()
    registry = ToolRegistry()
    registry.register(tool)
    decision, event_tool, action, _title = decide_tool(
        source_name=wire_name,
        arguments={"text": "payload"},
        tools=registry,
        agent=get_agent("build"),
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
        mode="build",
        depth=0,
        max_depth=2,
    )
    assert (decision, event_tool, action) == (
        "allow",
        tool.name,
        tool.original_name,
    )


def test_unknown_server_mcp_identity_fails_closed() -> None:
    decision, *_ = decide_tool(
        source_name="mcp__other__persist_note",
        arguments={"text": "payload"},
        tools=default_tool_registry(),
        agent=get_agent("build"),
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
        mode="build",
        depth=0,
        max_depth=2,
    )
    assert decision == "deny"


def test_websearch_is_native_without_a_registry_entry() -> None:
    decision, event_tool, action, _title = decide_tool(
        source_name="WebSearch",
        arguments={"query": "OpenCuria"},
        tools=default_tool_registry(),
        agent=get_agent("explore"),
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
        mode="build",
        depth=1,
        max_depth=2,
    )
    assert (decision, event_tool, action) == ("allow", "websearch", "OpenCuria")


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("explore", "explore"),
        ("Explorer", "explore"),
        ("general", "general"),
        ("general-purpose", "general"),
        ("GENERAL_PURPOSE", "general"),
    ],
)
def test_valid_sdk_child_agent_aliases(raw: str, canonical: str) -> None:
    assert canonical_subagent_type(raw) == canonical


def test_unknown_sdk_child_agent_alias_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unknown Claude subagent type"):
        canonical_subagent_type("unknown-child")


def test_scope_never_escalates_unknown_or_unmapped_child_to_general(
    fake_accessor,
) -> None:
    tools = default_tool_registry()
    options = RunOptions(session_id="scope", workspace_id=fake_accessor.workspace_id)
    bridge = PermissionBridge(
        tools=tools,
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
        options=options,
        agent=get_agent("build"),
        mode="build",
        max_depth=2,
    )

    class EngineState:
        _agent_parent_tool = {"valid": "task-call", "unknown-type": "task-call"}
        _agent_type_raw = {
            "valid": "general-purpose",
            "unknown-type": "researcher",
        }
        _agent_depth = {"valid": 1, "unknown-type": 1}
        _tool_events = {"task-call": {"tool": "task"}}

    parent, child, depth = bridge.scope(EngineState, {"agent_id": "valid"})
    assert (parent, child.name, depth) == ("task-call", "general", 1)
    with pytest.raises(ValueError, match="Unknown Claude subagent type"):
        bridge.scope(EngineState, {"agent_id": "unknown-type"})
    with pytest.raises(ValueError, match="Unknown Claude subagent identity"):
        bridge.scope(EngineState, {"agent_id": "missing"})
    orphan = type(
        "Orphan",
        (),
        {
            "_agent_parent_tool": {"orphan": "missing-call"},
            "_agent_type_raw": {"orphan": "explorer"},
            "_agent_depth": {},
            "_tool_events": {},
        },
    )
    with pytest.raises(ValueError, match="Unmapped Claude subagent identity"):
        bridge.scope(orphan, {"agent_id": "orphan"})
