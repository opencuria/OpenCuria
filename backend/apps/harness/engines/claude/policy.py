"""Claude Code tool identity and permission-policy helpers."""

from __future__ import annotations

from typing import Any

from ...agents.definitions import AgentDefinition
from ...permissions.evaluator import ASK, DENY, PermissionEvaluator
from ...tools.base import ToolRegistry

# Claude Code native tools mapped to OpenCuria registry/policy keys.
NATIVE_TOOL_KEYS: dict[str, str] = {
    "Bash": "bash",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "NotebookEdit": "edit",
    "Glob": "glob",
    "Grep": "grep",
    "Task": "task",
    "Agent": "task",
    "TaskStop": "task",
    "TaskCreate": "task",
    "TaskUpdate": "task",
    "TaskGet": "task",
    "TaskList": "task",
    "TaskOutput": "task",
    "AskUserQuestion": "question",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "Skill": "skill",
    "TodoWrite": "todowrite",
    "ExitPlanMode": "exit_plan_mode",
}


def combine_decisions(*decisions: str) -> str:
    """Merge policy decisions with deny > ask > allow precedence."""
    if DENY in decisions:
        return DENY
    if ASK in decisions:
        return ASK
    return "allow"


def tool_identity(
    source_name: str,
    arguments: dict[str, Any],
    tools: ToolRegistry,
) -> tuple[str, str, str]:
    """Return (event tool, permission key, action) for Claude tool calls."""
    raw_name = str(source_name or "")
    if raw_name.startswith("mcp__opencuria__"):
        # A configured MCP tool is always resolved by the exact registry wire
        # name; never reinterpret source names as Claude-native tool names.
        wire_name = raw_name.removeprefix("mcp__opencuria__")
        try:
            registered = tools.get(wire_name)
        except KeyError:
            return wire_name, "__unknown_mcp_tool__", ""
        return (
            registered.name,
            registered.permission_key or registered.name,
            _action(registered.name, arguments, registered),
        )

    if raw_name.startswith("mcp__"):
        tool_name = raw_name.split("__", 2)[2]
        try:
            registered = tools.get(tool_name)
        except KeyError:
            return tool_name, "__unknown_mcp_tool__", ""
        return (
            registered.name,
            registered.permission_key or registered.name,
            _action(registered.name, arguments, registered),
        )

    if raw_name in NATIVE_TOOL_KEYS:
        key = NATIVE_TOOL_KEYS[raw_name]
        try:
            registered = tools.get(key)
        except KeyError:
            registered = None
        if registered is None:
            return raw_name, key, _action(key, arguments, None)
        key = registered.permission_key or key
        return registered.name, key, _action(key, arguments, registered)

    try:
        registered = tools.get(raw_name)
    except KeyError:
        return raw_name, "__unknown_tool__", ""
    key = registered.permission_key or registered.name
    return registered.name, key, _action(registered.name, arguments, registered)


def _action(name: str, arguments: dict[str, Any], registered: Any | None) -> str:
    """Extract the same non-secret action strings as the native harness."""
    original_name = str(getattr(registered, "original_name", "") or "")
    if original_name:
        return original_name
    key = (getattr(registered, "permission_key", "") or name or "").lower()
    if key == "question":
        return ""
    if key in ("bash", "process"):
        return str(arguments.get("command", "") or "")
    if key in ("read", "edit", "write", "list"):
        return str(
            arguments.get("file_path")
            or arguments.get("path")
            or arguments.get("notebook_path")
            or ""
        )
    if key == "glob":
        return str(arguments.get("path") or "")
    if key == "grep":
        return str(arguments.get("path") or arguments.get("pattern") or "")
    if key == "webfetch":
        return str(arguments.get("url", "") or "")
    if key == "websearch":
        return str(arguments.get("query", "") or "")
    if key == "task":
        return str(arguments.get("description", "") or arguments.get("prompt", ""))
    if key in ("todowrite", "skill", "exit_plan_mode"):
        return str(arguments.get("skill", "") or "")
    return ""


def registry_tool_identity(
    source_name: str,
    arguments: dict[str, Any],
    tools: ToolRegistry,
) -> tuple[str, str, str]:
    """Resolve a normalized configured SDK tool name through the registry."""
    try:
        registered = tools.get(source_name)
    except KeyError:
        candidates = [
            tool
            for tool in tools.list()
            if getattr(tool, "original_name", "") == source_name
        ]
        if len(candidates) != 1:
            return source_name, "__unknown_tool__", ""
        registered = candidates[0]
    permission_key = registered.permission_key or registered.name
    action = _action(registered.name, arguments, registered)
    return registered.name, permission_key, action


def decide_tool(
    *,
    source_name: str,
    arguments: dict[str, Any],
    tools: ToolRegistry,
    agent: AgentDefinition,
    evaluator: PermissionEvaluator,
    mode: str,
    depth: int,
    max_depth: int,
    selected_skills: list[str] | None = None,
) -> tuple[str, str, str, str]:
    """Return decision, event tool, safe action and human-readable title."""
    event_tool, key, action = (
        registry_tool_identity(source_name, arguments, tools)
        if source_name in tools
        else tool_identity(source_name, arguments, tools)
    )
    raw = source_name
    is_sdk_mcp = raw.startswith("mcp__")
    if key.startswith("__unknown_"):
        return DENY, event_tool, action, event_tool
    if is_sdk_mcp and not raw.startswith("mcp__opencuria__"):
        return DENY, event_tool, action, event_tool
    if not is_sdk_mcp and raw not in NATIVE_TOOL_KEYS and raw not in tools:
        return DENY, event_tool, action, event_tool

    if key == "task" and raw in {"Task", "Agent", "TaskCreate"} and depth >= max_depth:
        return DENY, event_tool, action, "Subagent depth limit reached"
    if key == "todowrite" and depth > 0:
        return DENY, event_tool, action, "TodoWrite is unavailable to subagents"
    if raw == "ExitPlanMode":
        return DENY, event_tool, action, "ExitPlanMode requires an OpenCuria build-mode restart"
    if raw == "AskUserQuestion":
        return DENY, event_tool, action, "Use the OpenCuria question tool instead"

    registered = None
    try:
        registered = tools.get(event_tool)
    except KeyError:
        if raw not in ("WebSearch", "Skill"):
            return DENY, event_tool, action, event_tool

    agent_eval = PermissionEvaluator(agent_rules=dict(agent.permissions or {}))
    if raw == "Skill":
        skill_name = str(arguments.get("skill", "") or "")
        if skill_name not in set(selected_skills or ()):
            return DENY, event_tool, action, "Skill is not selected in OpenCuria"
        title = f"Skill: {skill_name}"[:240]
        decision = combine_decisions(
            evaluator.evaluate(key, action, mode=mode),
            agent_eval.evaluate(key, action, mode=mode),
        )
        return decision, event_tool, action, title

    if registered is not None:
        normalized_arguments = dict(arguments)
        if "path" in registered.args_schema.model_fields:
            path = (
                normalized_arguments.get("path")
                or normalized_arguments.get("file_path")
                or normalized_arguments.get("notebook_path")
            )
            if path is not None:
                normalized_arguments["path"] = path
        try:
            title = registered.title(registered.coerce_args(normalized_arguments))
        except Exception:
            title = f"{registered.name}: {action}"[:240] or registered.name
    elif raw == "Bash":
        title = str(arguments.get("command", "Bash"))[:240]
    elif raw in ("Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep"):
        title = f"{raw}: {action}"[:240]
    elif raw in {"Task", "Agent", "TaskCreate", "TaskStop"}:
        title = f"Subagent: {action}"[:240]
    else:
        title = raw

    decision = combine_decisions(
        evaluator.evaluate(key, action, mode=mode),
        agent_eval.evaluate(key, action, mode=mode),
    )
    if key in ("bash", "process") and action:
        from ...tools.shell import detect_external_directory

        if detect_external_directory(action):
            decision = combine_decisions(
                evaluator.evaluate(key, action, mode=mode, external_directory=True),
                agent_eval.evaluate(key, action, mode=mode, external_directory=True),
            )
    return decision, event_tool, action, title


def tool_schemas_for_claude(
    *,
    tools: ToolRegistry,
    agent: AgentDefinition,
    evaluator: PermissionEvaluator,
    mode: str,
    depth: int,
    max_depth: int,
) -> list[dict[str, Any]]:
    """Return registered tool schemas visible to Claude Code."""
    schemas: list[dict[str, Any]] = []
    agent_eval = PermissionEvaluator(agent_rules=dict(agent.permissions or {}))
    for registered in tools.list():
        name = (registered.name or "").strip().lower()
        if name == "task" and depth >= max_depth:
            continue
        if name == "todowrite" and depth > 0:
            continue
        key = registered.permission_key or registered.name
        if name.startswith("mcp_") and agent.name == "explore":
            continue
        if combine_decisions(
            evaluator.evaluate(key, "", mode=mode),
            agent_eval.evaluate(key, "", mode=mode),
        ) == DENY:
            continue
        schemas.append(
            {
                "name": registered.name,
                "description": registered.description,
                "input_schema": registered.parameters_schema(),
            }
        )
    return schemas


def native_tools_for_claude(
    *,
    tools: ToolRegistry,
    agent: AgentDefinition,
    evaluator: PermissionEvaluator,
    mode: str,
    depth: int,
    max_depth: int,
    selected_skills: list[str] | None = None,
    plan_mode: bool = False,
) -> list[str]:
    """Build a small allowlist of Claude Code built-ins for this registry."""
    names: list[str] = []
    agent_eval = PermissionEvaluator(agent_rules=dict(agent.permissions or {}))
    for cli_name, registry_name in (
        ("Bash", "bash"),
        ("Read", "read"),
        ("Write", "write"),
        ("Edit", "edit"),
        ("NotebookEdit", "edit"),
        ("Glob", "glob"),
        ("Grep", "grep"),
        ("Task", "task"),
        ("TaskStop", "task"),
        ("WebFetch", "webfetch"),
        ("WebSearch", "websearch"),
        ("Skill", "skill"),
    ):
        if cli_name == "Task" and depth >= max_depth:
            continue
        if cli_name in {"Task", "TaskStop"} and agent.name == "explore":
            continue
        if cli_name == "Skill" and agent.name == "explore":
            continue
        if cli_name == "WebSearch":
            permission_key = registry_name
        elif cli_name == "Skill":
            if not selected_skills:
                continue
            permission_key = registry_name
        else:
            try:
                registered = tools.get(registry_name)
            except KeyError:
                continue
            permission_key = registered.permission_key or registry_name
        decision = combine_decisions(
            evaluator.evaluate(permission_key, "", mode=mode),
            agent_eval.evaluate(permission_key, "", mode=mode),
        )
        if cli_name == "Bash" and plan_mode and decision == "allow":
            decision = ASK
        if decision != DENY and (
            cli_name != "WebSearch" or "mcp_web_search" in tools
        ):
            names.append(cli_name)
    return names


__all__ = [
    "ASK",
    "DENY",
    "NATIVE_TOOL_KEYS",
    "combine_decisions",
    "decide_tool",
    "native_tools_for_claude",
    "registry_tool_identity",
    "tool_identity",
    "tool_schemas_for_claude",
]
