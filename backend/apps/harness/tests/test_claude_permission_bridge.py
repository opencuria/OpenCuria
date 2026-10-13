"""Focused tests for the Claude SDK/OpenCuria permission bridge."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    create_sdk_mcp_server,
)
from mcp.server.lowlevel.server import RequestContext, request_ctx
from mcp.types import CallToolRequest, CallToolRequestParams, RequestParams
from pydantic import BaseModel

from apps.harness.agents.definitions import get_agent
from apps.harness.engines.claude.engine import ClaudeEngine
from apps.harness.engines.claude.permissionbridge import PermissionBridge
from apps.harness.engines.claude.policy import native_tools_for_claude
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.scopes import permission_scope_matches
from apps.harness.runner import RunOptions
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


class PersistNoteArgs(BaseModel):
    """Arguments accepted by the bridge test MCP tool."""

    text: str


class PersistNoteTool(Tool):
    """Record dispatched raw arguments and call IDs without side effects."""

    name = "mcp_fixture_persist"
    original_name = "persist_note"
    permission_key = "fixture.persist_note"
    description = "Persist one fixture note."
    args_schema = PersistNoteArgs

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def title(self, args: BaseModel) -> str:
        return "Persist fixture note"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        validated = self.coerce_args(args)
        assert isinstance(validated, PersistNoteArgs)
        self.calls.append((validated.text, ctx.call_id))
        await asyncio.sleep(0)
        return ToolResult(output=f"stored {validated.text}")


def _bridge_case(
    accessor: Any,
    *,
    agent: str = "build",
    mode: str = "build",
    evaluator: PermissionEvaluator | None = None,
    on_permission: Any = None,
    registry: ToolRegistry | None = None,
) -> tuple[ClaudeEngine, PermissionBridge, RunOptions, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    tools = registry or default_tool_registry()
    engine = ClaudeEngine(
        tools=tools,
        accessor=accessor,
        emit=emit,
        auth_env={"ANTHROPIC_API_KEY": "fixture-secret-token"},
        evaluator=evaluator,
    )
    options = RunOptions(
        session_id="permission-bridge-test",
        workspace_id=accessor.workspace_id,
        on_permission=on_permission,
        max_depth=2,
    )
    definition = get_agent(agent)
    bridge = PermissionBridge(
        tools=tools,
        evaluator=engine.evaluator,
        options=options,
        agent=definition,
        mode=mode,
        max_depth=options.max_depth,
        engine=engine,
    )
    # Set the exact per-run state normally installed by ClaudeEngine.run(); no
    # SDK process or run-loop is needed for direct hook/handler tests.
    engine._options = options
    engine._agent = definition
    engine._bridge = bridge
    engine._mode = mode
    engine._managed_skill_names = {"selected-skill"}
    return engine, bridge, options, events


def _pre_hook(bridge: PermissionBridge, engine: ClaudeEngine):
    return bridge.hooks(engine)["PreToolUse"][0].hooks[0]


def _post_hook(bridge: PermissionBridge, engine: ClaudeEngine):
    return bridge.hooks(engine)["PostToolUse"][0].hooks[0]


def _request_meta(tool_use_id: str, *, nested: bool = False) -> RequestParams.Meta:
    if nested:
        return RequestParams.Meta(claudecode={"toolUseId": tool_use_id})
    return RequestParams.Meta(**{"claudecode/toolUseId": tool_use_id})


async def _call_sdk_handler(
    handler: Any,
    tool_name: str,
    arguments: dict[str, Any],
    tool_use_id: str,
    *,
    nested_meta: bool = False,
) -> dict[str, Any]:
    """Invoke the SDK-registered MCP handler with real MCP request metadata."""
    request = CallToolRequest(
        params=CallToolRequestParams(
            name=tool_name,
            arguments=arguments,
            _meta=_request_meta(tool_use_id, nested=nested_meta),
        )
    )
    context = RequestContext(
        request_id=tool_use_id,
        meta=request.params.meta,
        session=None,
        lifespan_context=None,
        request=request,
    )
    token = request_ctx.set(context)
    try:
        return await handler(arguments)
    finally:
        request_ctx.reset(token)


@pytest.mark.asyncio
async def test_native_and_mcp_hook_gates_fail_closed_and_keep_question_on_mcp(
    fake_accessor,
) -> None:
    """Native allowlists and the configured MCP server both require policy."""
    callbacks: list[dict[str, Any]] = []

    async def approve(**kwargs: Any) -> str:
        callbacks.append(kwargs)
        return "once"

    engine, bridge, _options, events = _bridge_case(
        fake_accessor,
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
        on_permission=approve,
    )
    pre_tool = _pre_hook(bridge, engine)

    unknown_native = await pre_tool(
        {"tool_name": "NotAClaudeTool", "tool_input": {}}, "unknown-native", None
    )
    unknown_server = await pre_tool(
        {"tool_name": "mcp__other__question", "tool_input": {"questions": []}},
        "unknown-server",
        None,
    )
    unknown_registered = await pre_tool(
        {"tool_name": "mcp__opencuria__not_registered", "tool_input": {}},
        "unknown-mcp",
        None,
    )
    assert unknown_native["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert unknown_server["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert unknown_registered["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert callbacks == []

    # Claude's built-in question UI is intentionally disabled; the registered
    # OpenCuria question tool remains available via the managed MCP server.
    native_question = await pre_tool(
        {"tool_name": "AskUserQuestion", "tool_input": {}}, "native-question", None
    )
    mcp_question = await pre_tool(
        {
            "tool_name": "mcp__opencuria__question",
            "tool_input": {"questions": [{"question": "Continue?"}]},
        },
        "mcp-question",
        None,
    )
    assert native_question["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert mcp_question["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "question" in {
        schema["name"] for schema in engine._schemas(get_agent("build"), "build", 0, 2)
    }
    schemas = engine._schemas(get_agent("build"), "build", 0, 2)
    assert {"question", "todowrite"}.issubset({schema["name"] for schema in schemas})
    assert not {"read", "edit", "bash", "glob", "grep"} & {
        schema["name"] for schema in schemas
    }

    # A can_use_tool permission request without the matching hook state cannot
    # bypass the gate, even when the evaluator has a permissive catch-all.
    unpaired = await bridge.can_use_tool(
        engine,
        "mcp__opencuria__question",
        {"questions": [{"question": "No hook"}]},
        ToolPermissionContext(tool_use_id="no-hook"),
    )
    assert isinstance(unpaired, PermissionResultDeny)
    assert callbacks == []
    assert {event["call_id"] for event in events if event["type"] == "tool_error"} == {
        "unknown-native",
        "unknown-server",
        "unknown-mcp",
        "native-question",
    }


@pytest.mark.asyncio
async def test_plan_and_child_native_tool_policy_is_applied_at_hook_boundary(
    fake_accessor,
) -> None:
    native_approvals: list[dict[str, Any]] = []

    async def approve_native(**kwargs: Any) -> str:
        native_approvals.append(dict(kwargs))
        return "once"

    engine, bridge, options, _events = _bridge_case(
        fake_accessor,
        agent="plan",
        mode="plan",
        evaluator=PermissionEvaluator(),
        on_permission=approve_native,
    )
    engine._agent_parent_tool["explore-child"] = "parent-task-call"
    engine._agent_type_raw["explore-child"] = "explore"
    engine._agent_depth["explore-child"] = 1
    engine._tool_events["parent-task-call"] = {"tool": "task"}
    engine._agent_parent_tool["max-depth-child"] = "parent-task-call"
    engine._agent_type_raw["max-depth-child"] = "build"
    engine._agent_depth["max-depth-child"] = options.max_depth
    pre_tool = _pre_hook(bridge, engine)

    read = await pre_tool(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "/workspace/a.txt"},
        },
        "plan-read",
        None,
    )
    plan_write = await pre_tool(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/workspace/plan.txt", "content": "x"},
        },
        "plan-write",
        None,
    )
    bash_read_only = await pre_tool(
        {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        "plan-bash-read",
        None,
    )
    bash_mutation = await pre_tool(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git branch -D fixture"},
        },
        "plan-bash-mutation",
        None,
    )
    explore_write = await pre_tool(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/workspace/child.txt", "content": "x"},
            "agent_id": "explore-child",
        },
        "explore-write",
        None,
    )
    explore_bash = await pre_tool(
        {
            "tool_name": "Bash",
            "tool_input": {"command": 'env python -c \'open("x", "w").write("x")\''},
            "agent_id": "explore-child",
        },
        "explore-bash",
        None,
    )
    selected_skill = await pre_tool(
        {"tool_name": "Skill", "tool_input": {"skill": "selected-skill"}},
        "selected-skill",
        None,
    )
    unselected_skill = await pre_tool(
        {"tool_name": "Skill", "tool_input": {"skill": "not-managed"}},
        "unselected-skill",
        None,
    )
    max_depth_task = await pre_tool(
        {
            "tool_name": "Task",
            "tool_input": {"description": "nested task"},
            "agent_id": "max-depth-child",
        },
        "max-depth-task",
        None,
    )

    def decision(result: dict[str, Any]) -> str:
        return result["hookSpecificOutput"]["permissionDecision"]

    assert decision(read) == "allow"
    native_read_result = await bridge.can_use_tool(
        engine,
        "Read",
        {"file_path": "/workspace/a.txt"},
        ToolPermissionContext(tool_use_id="plan-read"),
    )
    assert isinstance(native_read_result, PermissionResultAllow)
    assert decision(plan_write) == "ask"
    plan_write_result = await bridge.can_use_tool(
        engine,
        "Write",
        {"file_path": "/workspace/plan.txt", "content": "x"},
        ToolPermissionContext(tool_use_id="plan-write"),
    )
    assert isinstance(plan_write_result, PermissionResultAllow)
    assert native_approvals == [
        {
            "tool": "write",
            "action": "/workspace/plan.txt",
            "title": "Write /workspace/plan.txt",
            "call_id": "plan-write",
            "key": "permission",
        }
    ]
    assert decision(bash_read_only) == "allow"
    assert decision(bash_mutation) == "ask"
    assert decision(explore_write) == "deny"
    assert decision(explore_bash) == "deny"
    assert get_agent("explore").permissions["mcp_*"] == "deny"
    assert "WebFetch" not in get_agent("explore").system_prompt
    assert "WebSearch" not in get_agent("explore").system_prompt
    explore_webfetch = await pre_tool(
        {
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://example.test"},
            "agent_id": "explore-child",
        },
        "explore-webfetch",
        None,
    )
    explore_websearch = await pre_tool(
        {
            "tool_name": "WebSearch",
            "tool_input": {"query": "OpenCuria policy"},
            "agent_id": "explore-child",
        },
        "explore-websearch",
        None,
    )
    assert decision(explore_webfetch) == "allow"
    assert decision(explore_websearch) == "allow"
    assert decision(selected_skill) == "allow"
    assert decision(unselected_skill) == "deny"
    assert decision(max_depth_task) == "deny"
    schemas = engine._schemas(get_agent("plan"), "plan", 0, options.max_depth)
    assert {"question", "todowrite"}.issubset({schema["name"] for schema in schemas})
    assert not {"read", "edit", "bash", "glob", "grep"} & {
        schema["name"] for schema in schemas
    }
    root_native_names = native_tools_for_claude(
        tools=engine.tools,
        agent=get_agent("plan"),
        evaluator=engine.evaluator,
        mode="plan",
        depth=0,
        max_depth=options.max_depth,
        selected_skills=["selected-skill"],
        plan_mode=True,
    )
    assert "Task" in root_native_names
    max_depth_native_names = native_tools_for_claude(
        tools=engine.tools,
        agent=get_agent("build"),
        evaluator=engine.evaluator,
        mode="plan",
        depth=options.max_depth,
        max_depth=options.max_depth,
        selected_skills=["selected-skill"],
        plan_mode=True,
    )
    assert "Task" not in max_depth_native_names
    explore_native_names = native_tools_for_claude(
        tools=engine.tools,
        agent=get_agent("explore"),
        evaluator=engine.evaluator,
        mode="plan",
        depth=1,
        max_depth=options.max_depth,
        selected_skills=["selected-skill"],
        plan_mode=True,
    )
    assert "Task" not in explore_native_names
    assert {"WebFetch", "WebSearch"}.issubset(explore_native_names)
    assert "mcp_web_search" not in engine.tools
    explore_engine_schemas = engine._schemas(
        get_agent("explore"), "plan", 1, options.max_depth
    )
    assert all(
        not schema["name"].startswith("mcp_") for schema in explore_engine_schemas
    )
    assert options.max_depth == 2


@pytest.mark.asyncio
async def test_mcp_ask_uses_registry_identity_raw_args_and_exact_concurrent_call_ids(
    fake_accessor,
) -> None:
    tool = PersistNoteTool()
    registry = ToolRegistry()
    registry.register(tool)
    approvals: list[dict[str, Any]] = []

    async def approve(**kwargs: Any) -> str:
        approvals.append(dict(kwargs))
        return "once"

    engine, bridge, _options, events = _bridge_case(
        fake_accessor,
        evaluator=PermissionEvaluator(global_rules={tool.permission_key: "ask"}),
        on_permission=approve,
        registry=registry,
    )
    sdk_tools = engine._sdk_tools(
        [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters_schema(),
            }
        ]
    )
    assert [definition.name for definition in sdk_tools] == [tool.original_name]
    server = create_sdk_mcp_server("opencuria", tools=sdk_tools)
    assert server["name"] == "opencuria"
    handler = sdk_tools[0].handler
    wire = f"mcp__opencuria__{tool.original_name}"
    arguments = {"text": "fixture-secret-token"}
    pre_tool = _pre_hook(bridge, engine)
    permission_context = ToolPermissionContext

    for call_id in ("persist-call-1", "persist-call-2"):
        hook_result = await pre_tool(
            {"tool_name": wire, "tool_input": arguments}, call_id, None
        )
        assert hook_result["hookSpecificOutput"]["permissionDecision"] == "ask"
        assert engine._tool_arguments[call_id] == arguments
        result = await bridge.can_use_tool(
            engine,
            wire,
            arguments,
            permission_context(tool_use_id=call_id),
        )
        assert isinstance(result, PermissionResultAllow)

    assert len(approvals) == 2
    assert all(item["tool"] == tool.name for item in approvals)
    assert all(item["action"] == tool.original_name for item in approvals)
    assert {item["call_id"] for item in approvals} == {
        "persist-call-1",
        "persist-call-2",
    }
    queued = [event for event in events if event["type"] == "tool_queued"]
    assert len(queued) == 2
    assert all("fixture-secret-token" not in event["arguments"] for event in queued)

    # A valid metadata ID does not excuse altered arguments: the unmatched
    # attempt fails closed without consuming the approved original invocation.
    mismatch = await _call_sdk_handler(
        handler,
        tool.original_name,
        {"text": "different raw arguments"},
        "persist-call-1",
    )
    assert mismatch["is_error"] is True
    assert tool.calls == []

    # Identical calls are independently claimed by their SDK metadata, even
    # while handlers overlap; each registry execution receives its own call ID.
    results = await asyncio.gather(
        _call_sdk_handler(handler, tool.original_name, arguments, "persist-call-1"),
        _call_sdk_handler(
            handler,
            tool.original_name,
            arguments,
            "persist-call-2",
            nested_meta=True,
        ),
    )
    assert all(not result["is_error"] for result in results)
    assert tool.calls == [
        ("fixture-secret-token", "persist-call-1"),
        ("fixture-secret-token", "persist-call-2"),
    ]

    # The consumed IDs cannot dispatch a second time. PostToolUse then retires
    # each lifecycle exactly once.
    duplicate_dispatch = await _call_sdk_handler(
        handler, tool.original_name, arguments, "persist-call-1"
    )
    assert duplicate_dispatch["is_error"] is True
    post_tool = _post_hook(bridge, engine)
    await post_tool({}, "persist-call-1", None)
    await post_tool({}, "persist-call-2", None)
    assert len(tool.calls) == 2
    assert len([event for event in events if event["type"] == "tool_started"]) == 2
    assert len([event for event in events if event["type"] == "tool_completed"]) == 2


@pytest.mark.asyncio
async def test_redacted_native_once_scope_separates_secret_actions(
    fake_accessor,
) -> None:
    secret_one = "fixture-secret-action-one"
    secret_two = "fixture-secret-action-two"
    approvals: list[dict[str, Any]] = []

    async def approve(**kwargs: Any) -> str:
        approvals.append(dict(kwargs))
        return "once"

    engine, bridge, options, events = _bridge_case(
        fake_accessor,
        evaluator=PermissionEvaluator(global_rules={"bash": "ask"}),
        on_permission=approve,
    )
    engine.auth_env["BASH_SECRET_ONE"] = secret_one
    engine.auth_env["BASH_SECRET_TWO"] = secret_two
    pre_tool = _pre_hook(bridge, engine)

    async def invoke(command: str, call_id: str) -> None:
        hook_result = await pre_tool(
            {"tool_name": "Bash", "tool_input": {"command": command}},
            call_id,
            None,
        )
        assert hook_result["hookSpecificOutput"]["permissionDecision"] == "ask"
        result = await bridge.can_use_tool(
            engine,
            "Bash",
            {"command": command},
            ToolPermissionContext(tool_use_id=call_id),
        )
        assert isinstance(result, PermissionResultAllow)

    command_one = f"echo {secret_one}"
    command_two = f"echo {secret_two}"
    await invoke(command_one, "secret-once-one")
    assert len(approvals) == 1
    assert approvals[0]["action"] == "echo [redacted]"
    scope_one = approvals[0]["permission_action"]
    assert permission_scope_matches(scope_one, scope_one)

    # Same safe display, but a distinct raw secret action, must ask again.
    await invoke(command_two, "secret-once-two")
    assert len(approvals) == 2
    assert approvals[1]["action"] == approvals[0]["action"]
    scope_two = approvals[1]["permission_action"]
    assert not permission_scope_matches(scope_one, scope_two)

    # A repeated identical secret action consumes only its own once grant.
    await invoke(command_two, "secret-once-two-repeat")
    assert len(approvals) == 2
    for secret in (secret_one, secret_two):
        assert secret not in repr(approvals)
        assert secret not in repr(options.once_approved)
        assert secret not in repr(engine._tool_events)
        assert secret not in repr(events)


@pytest.mark.asyncio
async def test_permission_wait_cancellation_does_not_approve_or_dispatch(
    fake_accessor,
) -> None:
    entered = asyncio.Event()
    never_approve = asyncio.Event()
    callbacks: list[str] = []

    async def wait_for_user(**kwargs: Any) -> str:
        callbacks.append(str(kwargs["call_id"]))
        entered.set()
        await never_approve.wait()
        return "once"

    engine, bridge, _options, events = _bridge_case(
        fake_accessor,
        evaluator=PermissionEvaluator(global_rules={"edit": "ask"}),
        on_permission=wait_for_user,
    )
    pre_tool = _pre_hook(bridge, engine)
    hook_result = await pre_tool(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/workspace/a.txt",
                "content": "updated",
            },
        },
        "cancelled-write",
        None,
    )
    assert hook_result["hookSpecificOutput"]["permissionDecision"] == "ask"

    pending = asyncio.create_task(
        bridge.can_use_tool(
            engine,
            "Write",
            {"file_path": "/workspace/a.txt", "content": "updated"},
            ToolPermissionContext(tool_use_id="cancelled-write"),
        )
    )
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert callbacks == ["cancelled-write"]
    assert engine._tool_events["cancelled-write"]["started"] is False
    assert not any(event["type"] == "tool_started" for event in events)
    # The active run's abandonment cleanup owns final state disposal.
    await engine._finalize_abandoned_tools("Run cancelled")
    assert "cancelled-write" not in engine._tool_events
    assert any(
        event["type"] == "tool_error" and event["call_id"] == "cancelled-write"
        for event in events
    )


@pytest.mark.asyncio
async def test_plan_process_start_respects_global_process_ask(
    fake_accessor,
) -> None:
    engine, bridge, _options, _events = _bridge_case(
        fake_accessor,
        agent="plan",
        mode="plan",
        evaluator=PermissionEvaluator(global_rules={"process": "ask"}),
    )
    start = _pre_hook(bridge, engine)
    test_result = await start(
        {
            "tool_name": "mcp__opencuria__process_start",
            "tool_input": {"command": "pytest -q", "name": "tests"},
        },
        "test-process",
        None,
    )
    research_result = await start(
        {
            "tool_name": "mcp__opencuria__process_start",
            "tool_input": {"command": "cat README.md", "name": "research"},
        },
        "research-process",
        None,
    )
    assert test_result["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert research_result["hookSpecificOutput"]["permissionDecision"] == "ask"

    async def approve(**_kwargs: Any) -> str:
        return "once"

    _engine, allowed_bridge, _options, _events = _bridge_case(
        fake_accessor,
        agent="plan",
        mode="plan",
        evaluator=PermissionEvaluator(global_rules={"process": "ask"}),
        on_permission=approve,
    )
    allow_start = _pre_hook(allowed_bridge, _engine)
    approved = await allow_start(
        {
            "tool_name": "mcp__opencuria__process_start",
            "tool_input": {"command": "pytest -q", "name": "approved-tests"},
        },
        "approved-test-process",
        None,
    )
    assert approved["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.asyncio
async def test_plan_process_restart_keeps_wildcard_allow_at_ask_precedence(
    fake_accessor,
) -> None:
    engine, bridge, _options, _events = _bridge_case(
        fake_accessor,
        agent="plan",
        mode="plan",
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
    )
    running = await fake_accessor.process_start(
        "npm run dev", name="dev-server", kind="persistent"
    )
    pre_tool = _pre_hook(bridge, engine)

    allowed = await pre_tool(
        {
            "tool_name": "mcp__opencuria__process_restart",
            "tool_input": {"process_id": running["process_id"]},
        },
        "restart-server",
        None,
    )
    # Existing operational process tools retain policy decisions, while the
    # stored command is checked before an approval-free restart.
    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"

    unknown = await pre_tool(
        {
            "tool_name": "mcp__opencuria__process_restart",
            "tool_input": {"process_id": "missing-process"},
        },
        "restart-missing",
        None,
    )
    assert unknown["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.asyncio
async def test_plan_process_restart_checks_stored_command_with_explicit_allow(
    fake_accessor,
) -> None:
    engine, bridge, _options, _events = _bridge_case(
        fake_accessor,
        agent="plan",
        mode="plan",
        evaluator=PermissionEvaluator(global_rules={"process": "allow"}),
    )
    safe = await fake_accessor.process_start(
        "npm run dev", name="dev-server", kind="persistent"
    )
    unsafe = await fake_accessor.process_start(
        "rm -rf /workspace/tmp", name="unsafe-job", kind="persistent"
    )
    pre_tool = _pre_hook(bridge, engine)

    safe_result = await pre_tool(
        {
            "tool_name": "mcp__opencuria__process_restart",
            "tool_input": {"process_id": safe["process_id"]},
        },
        "restart-safe-server",
        None,
    )
    unsafe_result = await pre_tool(
        {
            "tool_name": "mcp__opencuria__process_restart",
            "tool_input": {"process_id": unsafe["process_id"]},
        },
        "restart-unsafe-job",
        None,
    )
    assert safe_result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert unsafe_result["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.asyncio
async def test_scope_allows_live_sdk_child_after_parent_tool_completion(
    fake_accessor,
) -> None:
    _engine, bridge, _options, _events = _bridge_case(fake_accessor)
    engine = bridge.engine
    assert engine is not None
    engine._agent_parent_tool["live-child"] = "completed-task-call"
    engine._agent_type_raw["live-child"] = "explore"
    engine._agent_depth["live-child"] = 1
    engine._started_subtasks.add("live-child")
    # A native Task call can finish while its background child is still alive.
    assert "completed-task-call" not in engine._tool_events

    parent, agent, depth = bridge.scope(engine, {"agent_id": "live-child"})

    assert parent == "completed-task-call"
    assert agent.name == "explore"
    assert depth == 1


@pytest.mark.asyncio
async def test_scope_rejects_finished_child_when_parent_tool_is_gone(
    fake_accessor,
) -> None:
    _engine, bridge, _options, _events = _bridge_case(fake_accessor)
    engine = bridge.engine
    assert engine is not None
    engine._agent_parent_tool["finished-child"] = "completed-task-call"
    engine._agent_type_raw["finished-child"] = "general"
    engine._agent_depth["finished-child"] = 1
    engine._started_subtasks.add("finished-child")
    engine._finished_subtasks.add("finished-child")
    engine._agent_parent_tool["unstarted-child"] = "missing-task-call"
    engine._agent_type_raw["unstarted-child"] = "general"
    engine._agent_depth["unstarted-child"] = 1

    with pytest.raises(ValueError, match="Claude subagent parent call is unavailable"):
        bridge.scope(engine, {"agent_id": "finished-child"})
    with pytest.raises(ValueError, match="Claude subagent parent call is unavailable"):
        bridge.scope(engine, {"agent_id": "unstarted-child"})
    with pytest.raises(ValueError, match="Unknown Claude subagent identity"):
        bridge.scope(engine, {"agent_id": "invented-child"})


@pytest.mark.asyncio
async def test_ambiguous_subagent_parent_correlation_fails_closed(
    fake_accessor,
) -> None:
    engine, bridge, _options, _events = _bridge_case(fake_accessor)
    engine._pending_task_tool_ids.update({"task-one", "task-two"})
    engine._tool_events.update(
        {
            "task-one": {
                "tool": "task",
                "step": 0,
                "parent_tool_use_id": None,
            },
            "task-two": {
                "tool": "task",
                "step": 0,
                "parent_tool_use_id": None,
            },
        }
    )
    start = bridge.hooks(engine)["SubagentStart"][0].hooks[0]

    await start({"agent_id": "ambiguous-child", "agent_type": "general"}, None, None)

    assert not engine._pending_task_tool_ids
    assert "ambiguous-child" not in engine._agent_parent_tool
    assert {event["call_id"] for event in _events if event["type"] == "tool_error"} == {
        "task-one",
        "task-two",
    }


@pytest.mark.asyncio
async def test_subagent_stop_waits_for_terminal_task_notification(
    fake_accessor,
) -> None:
    engine, bridge, _options, events = _bridge_case(fake_accessor)
    engine._subtask_by_tool["parent-task-call"] = "child-task"
    engine._agent_kind["child-task"] = "explore"
    engine._agent_parent_tool["child-task"] = "parent-task-call"
    engine._agent_type_raw["child-task"] = "explore"
    engine._tool_events["parent-task-call"] = {"tool": "task"}
    stop_hook = bridge.hooks(engine)["SubagentStop"][0].hooks[0]

    await stop_hook({"agent_id": "child-task"}, None, None)
    assert "child-task" in engine._stopped_subagents
    assert not [event for event in events if event["type"] == "subtask_finished"]

    await engine._handle_task_notification(
        type(
            "TaskNotification",
            (),
            {
                "task_id": "child-task",
                "tool_use_id": "parent-task-call",
                "status": "killed",
                "summary": "Completed the first file before cancellation.",
            },
        )()
    )
    finished = [event for event in events if event["type"] == "subtask_finished"]
    assert len(finished) == 1
    assert finished[0]["status"] == "aborted"
    assert finished[0]["summary"] == "Completed the first file before cancellation."
    assert "child-task" not in engine._stopped_subagents


@pytest.mark.asyncio
async def test_plan_process_research_and_bookkeeping_are_free(fake_accessor) -> None:
    engine, bridge, _options, _events = _bridge_case(
        fake_accessor, agent="plan", mode="plan"
    )
    pre_tool = _pre_hook(bridge, engine)
    for name, arguments in (
        ("process_start", {"command": "npm run dev", "name": "server"}),
        ("process_start", {"command": "git log -1", "name": "research"}),
        ("process_get", {"process_id": "server"}),
        ("process_list", {}),
        ("process_stop", {"process_id": "server"}),
    ):
        result = await pre_tool(
            {"tool_name": f"mcp__opencuria__{name}", "tool_input": arguments},
            f"free-{name}-{len(engine._tool_events)}",
            None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    denied = await pre_tool(
        {
            "tool_name": "mcp__opencuria__process_start",
            "tool_input": {"command": "rm source.py", "name": "mutation"},
        },
        "ask-start-mutation",
        None,
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "ask"
