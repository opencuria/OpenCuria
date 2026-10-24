"""Workspace Git MCP tools.

``generate_git_commit_message`` additionally requires ``harness:run``;
``git_operation`` requires ``workspaces:git_write`` for its mutations.
"""

from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async
from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.runners.services import RunnerService

from .. import access
from ..results import ToolHandler, error_result, text_result

_GIT_MUTATION_OPERATIONS = tuple(
    op
    for op in RunnerService.GIT_OPERATIONS
    if op not in RunnerService.GIT_READ_OPERATIONS
)


TOOLS: list[Tool] = [
    Tool(
        name="get_git_state",
        description="List light git repo summaries under /workspace (id, name, path, branch, head).",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
            },
            "required": ["workspace_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_git_diff",
        description="Get staged/unstaged working-tree diff entries for one repo.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "repo_path": {
                    "type": "string",
                    "description": "Absolute repo path under /workspace.",
                },
            },
            "required": ["workspace_id", "repo_path"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_git_commit",
        description="Get full details (message, authors, file changes) for one commit.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "repo_path": {
                    "type": "string",
                    "description": "Absolute repo path under /workspace.",
                },
                "commit": {
                    "type": "string",
                    "description": "Commit hash (4-64 hex chars).",
                },
            },
            "required": ["workspace_id", "repo_path", "commit"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="generate_git_commit_message",
        description=(
            "Suggest a commit subject for one repo's staged changes (all "
            "changes when nothing is staged) using the org's small model."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "repo_path": {
                    "type": "string",
                    "description": "Absolute repo path under /workspace.",
                },
            },
            "required": ["workspace_id", "repo_path"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="git_operation",
        description=(
            "Run one whitelisted git operation (stage/unstage/discard/commit, "
            "branch/checkout/merge, fetch/pull/push/sync). Only the whitelisted "
            "operation plus typed fields are forwarded — no shell or env input. "
            "Commit identity comes from the API key account."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "operation": {
                    "type": "string",
                    "enum": list(_GIT_MUTATION_OPERATIONS),
                    "description": (
                        "Whitelisted mutation-only git operation. Reads use "
                        "get_git_state/get_git_diff/get_git_commit instead."
                    ),
                },
                "repo_path": {
                    "type": "string",
                    "description": "Absolute repo path under /workspace.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repo-relative file paths (stage/unstage/discard).",
                },
                "message": {
                    "type": "string",
                    "description": "Commit/merge message.",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch name (checkout/create/delete/merge/pull).",
                },
                "commit": {
                    "type": "string",
                    "description": "Commit hash (checkout_commit).",
                },
                "target": {
                    "type": "string",
                    "description": "Merge target branch (merge_current_into).",
                },
                "new_branch": {
                    "type": "string",
                    "description": "New branch name (rename_branch).",
                },
                "old_branch": {
                    "type": "string",
                    "description": "Branch to rename (rename_branch, optional: current).",
                },
                "start_point": {
                    "type": "string",
                    "description": "Start ref for create_branch (optional).",
                },
                "remote": {
                    "type": "string",
                    "description": "Remote name for fetch/pull/push (optional).",
                },
                "remote_ref": {
                    "type": "string",
                    "description": "Remote tracking ref (checkout_remote_branch, e.g. origin/feature/x).",
                },
                "local_name": {
                    "type": "string",
                    "description": "Local branch name (checkout_remote_branch, optional: derived from remote_ref).",
                },
                "checkout": {
                    "type": "boolean",
                    "description": "Check out the new branch (create_branch).",
                },
                "set_upstream": {
                    "type": "boolean",
                    "description": "Set upstream on push (push).",
                },
                "stash": {
                    "type": "string",
                    "description": "Stash selector, e.g. stash@{0} (stash_apply/stash_pop/stash_drop/stash_branch).",
                },
            },
            "required": ["workspace_id", "operation"],
            "additionalProperties": False,
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "get_git_state": APIKeyPermission.WORKSPACES_GIT_READ,
    "get_git_diff": APIKeyPermission.WORKSPACES_GIT_READ,
    "get_git_commit": APIKeyPermission.WORKSPACES_GIT_READ,
    "generate_git_commit_message": APIKeyPermission.WORKSPACES_GIT_READ,
    "git_operation": APIKeyPermission.WORKSPACES_GIT_WRITE,
}


async def _owned_git_workspace(api_key, org_id, args: dict):
    """Resolve ``workspace_id`` to an owned workspace as ``(workspace, error)``."""
    raw_workspace_id = args.get("workspace_id")
    if not raw_workspace_id:
        return None, error_result("workspace_id is required")
    try:
        workspace_id = uuid.UUID(str(raw_workspace_id))
    except ValueError:
        return None, error_result("Invalid workspace_id UUID")
    return await sync_to_async(access.owned_workspace_or_error)(
        api_key, org_id, workspace_id
    )


async def _run_git_operation_mcp(
    api_key, org_id, args: dict, operation: str
) -> list[TextContent]:
    """Authorize the workspace, then let RunnerService validate and dispatch.

    Only MCP envelope fields are removed. All operation arguments, including
    unknown or caller-controlled keys, reach the service's strict whitelist.
    Commit identity is derived there from the API key user, never MCP input.
    """
    workspace, owned_error = await _owned_git_workspace(api_key, org_id, args)
    if owned_error is not None:
        return owned_error

    try:
        result = await access.runner_service().run_git_operation(
            workspace.id,
            operation,
            repo_path=args.get("repo_path"),
            args={
                key: value
                for key, value in args.items()
                if key not in {"workspace_id", "repo_path"}
            },
            user=api_key.user,
        )
    except Exception as exc:
        return error_result(str(exc))
    return text_result(result)


async def _call_get_git_state(api_key, org_id, args: dict) -> list[TextContent]:
    """List light git repo summaries under /workspace."""
    return await _run_git_operation_mcp(api_key, org_id, args, "list_repos")


async def _call_get_git_diff(api_key, org_id, args: dict) -> list[TextContent]:
    """Return staged/unstaged diff entries for one repo."""
    return await _run_git_operation_mcp(api_key, org_id, args, "working_diff")


async def _call_get_git_commit(api_key, org_id, args: dict) -> list[TextContent]:
    """Return full details for one commit."""
    return await _run_git_operation_mcp(api_key, org_id, args, "commit_details")


async def _call_generate_git_commit_message(
    api_key, org_id, args: dict
) -> list[TextContent]:
    """Suggest a commit subject for one repo with the org's small model."""
    if not api_key.has_permission(APIKeyPermission.HARNESS_RUN):
        return error_result(
            f"Permission denied: {APIKeyPermission.HARNESS_RUN.value} required"
        )
    unknown = sorted(set(args) - {"workspace_id", "repo_path"})
    if unknown:
        return error_result(f"Unknown argument {unknown[0]!r}")
    repo_path = str(args.get("repo_path") or "").strip()
    if not repo_path:
        return error_result("repo_path is required")
    workspace, owned_error = await _owned_git_workspace(api_key, org_id, args)
    if owned_error is not None:
        return owned_error
    try:
        message = await access.harness_service().generate_commit_message(
            workspace_id=workspace.id,
            repo_path=repo_path,
            organization_id=org_id,
            user_id=api_key.user.id,
        )
    except Exception as exc:
        return error_result(str(exc))
    return text_result({"message": message})


async def _call_git_operation(api_key, org_id, args: dict) -> list[TextContent]:
    """Run a git mutation; reads use the dedicated get_git_* tools."""
    operation = str(args.get("operation", "") or "").strip()
    if not operation:
        return error_result("operation is required")
    if operation not in _GIT_MUTATION_OPERATIONS:
        return error_result(
            f"Unknown git operation: {operation!r}. "
            "Reads use get_git_state/get_git_diff/get_git_commit instead."
        )
    if not api_key.has_permission(APIKeyPermission.WORKSPACES_GIT_WRITE):
        return error_result(
            f"Permission denied: {APIKeyPermission.WORKSPACES_GIT_WRITE.value} required"
        )
    return await _run_git_operation_mcp(
        api_key,
        org_id,
        {key: value for key, value in args.items() if key != "operation"},
        operation,
    )


HANDLERS: dict[str, ToolHandler] = {
    "get_git_state": _call_get_git_state,
    "get_git_diff": _call_get_git_diff,
    "get_git_commit": _call_get_git_commit,
    "generate_git_commit_message": _call_generate_git_commit_message,
    "git_operation": _call_git_operation,
}
