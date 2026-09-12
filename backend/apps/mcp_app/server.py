"""
MCP (Model Context Protocol) Server for opencuria.

Exposes opencuria functionality as MCP tools. Authentication is performed via
API key (``Authorization: Bearer kai_...`` or ``X-API-Key: kai_...`` header).

The server is mounted into the Django ASGI application at the ``/mcp/``
path prefix using a Starlette sub-application.

Tools and their required permissions
-------------------------------------
- list_workspaces        → workspaces:read
- get_workspace          → workspaces:read
- create_workspace       → workspaces:create
- update_workspace       → workspaces:update
- stop_workspace         → workspaces:stop
- resume_workspace       → workspaces:resume
- remove_workspace       → workspaces:delete
- list_runners           → runners:read
- list_image_artifacts   → images:read
- create_image_artifact  → images:create
- list_image_definitions → image_definitions:read
- create_image_definition → image_definitions:write
- update_image_definition → image_definitions:write
- delete_image_definition → image_definitions:write
- list_build_jobs → image_definitions:read
- create_build_job → image_definitions:manage_runners
- update_build_job → image_definitions:manage_runners
- delete_build_job → image_definitions:manage_runners
- get_build_job_log → image_definitions:read
- list_credentials       → credentials:read
- get_provider_config → harness:read
- list_provider_models → harness:read
- save_provider_config → harness:run
- delete_provider_config → harness:run
- list_provider_connections → harness:providers
- save_provider_connection → harness:providers
- delete_provider_connection → harness:providers
- get_agent_s_config → harness:read
- save_agent_s_config → harness:run
- chatgpt_oauth_start → harness:providers
- chatgpt_oauth_status → harness:providers
- chatgpt_oauth_cancel → harness:providers
- list_harness_sessions → harness:read
- create_harness_session → harness:run
- send_harness_message → harness:run
- fork_harness_session → harness:run
- edit_harness_message → harness:run
- abort_harness_session → harness:run
- take_desktop_control → harness:run (also requires terminal:access)
- list_harness_parts → harness:read
- list_harness_todos → harness:read
- resolve_harness_permission → harness:permissions
- resolve_harness_question → harness:permissions
- mark_harness_session_read → harness:read
- mark_harness_session_unread → harness:read
- patch_harness_session → harness:run
- set_harness_session_mode → harness:run
- delete_harness_session → harness:run
- list_harness_conversations → harness:read
- list_org_credential_services       → org_credential_services:read
- toggle_org_credential_service_activation → org_credential_services:write
- list_processes         → workspaces:processes_read
- get_process            → workspaces:processes_read
- start_process          → workspaces:processes_run
- stop_process           → workspaces:processes_run
- restart_process        → workspaces:processes_run
- delete_process         → workspaces:processes_run
- get_git_state          → workspaces:git_read
- get_git_diff           → workspaces:git_read
- get_git_commit         → workspaces:git_read
- git_operation          → workspaces:git_write (read-only ops via this tool
  still require workspaces:git_read — enforced per-operation at call time)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.types import (
    TextContent,
    Tool,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount

from apps.accounts.models import APIKeyPermission

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions (JSON Schema input schemas)
# ---------------------------------------------------------------------------

_TOOLS: list[Tool] = [
    Tool(
        name="list_workspaces",
        description="List workspaces in the active organization.",
        inputSchema={
            "type": "object",
            "properties": {
                "runner_id": {
                    "type": "string",
                    "description": "Filter by runner UUID (optional).",
                }
            },
        },
    ),
    Tool(
        name="get_workspace",
        description="Get details of a single workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="create_workspace",
        description="Create a new workspace on a runner.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Workspace name."},
                "runner_id": {
                    "type": "string",
                    "description": "Runner UUID to host the workspace.",
                },
                "repos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "branch": {"type": "string"},
                        },
                        "required": ["url"],
                    },
                    "description": "Repositories to clone into the workspace.",
                },
                "runtime_type": {
                    "type": "string",
                    "enum": ["docker", "qemu"],
                    "description": "Virtualisation backend (default: docker).",
                },
                "image_artifact_id": {
                    "type": "string",
                    "description": "Optional image artifact UUID to start workspace from.",
                },
                "desktop_width": {
                    "type": "integer",
                    "description": "Fixed desktop width in pixels (default 1920).",
                },
                "desktop_height": {
                    "type": "integer",
                    "description": "Fixed desktop height in pixels (default 1080).",
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="update_workspace",
        description="Update workspace metadata, including the fixed desktop size.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace UUID.",
                },
                "name": {"type": "string", "description": "New workspace name."},
                "desktop_width": {
                    "type": "integer",
                    "description": "Fixed desktop width in pixels.",
                },
                "desktop_height": {
                    "type": "integer",
                    "description": "Fixed desktop height in pixels.",
                },
                "qemu_vcpus": {"type": "integer"},
                "qemu_memory_mb": {"type": "integer"},
                "qemu_disk_size_gb": {"type": "integer"},
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="stop_workspace",
        description="Stop a running workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="resume_workspace",
        description="Resume a stopped workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="remove_workspace",
        description="Remove a workspace and its container permanently.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="list_runners",
        description="List runners in the active organization.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_image_artifacts",
        description="List image artifacts owned by the current user.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="create_image_artifact",
        description="Create an image artifact of a workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "name": {"type": "string", "description": "Image artifact name."},
            },
            "required": ["workspace_id", "name"],
        },
    ),
    Tool(
        name="list_image_definitions",
        description="List image definitions for the active organization.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="create_image_definition",
        description="Create an image definition in the active organization (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "runtime_type": {"type": "string", "enum": ["docker", "qemu"]},
                "base_distro": {"type": "string"},
                "packages": {"type": "array", "items": {"type": "string"}},
                "env_vars": {"type": "object"},
                "custom_dockerfile": {"type": "string"},
                "custom_init_script": {"type": "string"},
                "is_active": {"type": "boolean"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="duplicate_image_definition",
        description="Duplicate a visible image definition into the active organization (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "definition_id": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["definition_id"],
        },
    ),
    Tool(
        name="update_image_definition",
        description="Update an image definition in the active organization (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "definition_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "runtime_type": {"type": "string", "enum": ["docker", "qemu"]},
                "base_distro": {"type": "string"},
                "packages": {"type": "array", "items": {"type": "string"}},
                "env_vars": {"type": "object"},
                "custom_dockerfile": {"type": "string"},
                "custom_init_script": {"type": "string"},
                "is_active": {"type": "boolean"},
            },
            "required": ["definition_id"],
        },
    ),
    Tool(
        name="delete_image_definition",
        description="Delete an image definition from the active organization (admin).",
        inputSchema={
            "type": "object",
            "properties": {"definition_id": {"type": "string"}},
            "required": ["definition_id"],
        },
    ),
    Tool(
        name="list_build_jobs",
        description="List runner image builds for an image definition.",
        inputSchema={
            "type": "object",
            "properties": {"definition_id": {"type": "string"}},
            "required": ["definition_id"],
        },
    ),
    Tool(
        name="create_build_job",
        description="Assign and activate an image definition on a runner (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "definition_id": {"type": "string"},
                "runner_id": {"type": "string"},
                "activate": {"type": "boolean"},
            },
            "required": ["definition_id", "runner_id"],
        },
    ),
    Tool(
        name="update_build_job",
        description="Update runner image build state (deactivate, activate, rebuild) (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "definition_id": {"type": "string"},
                "runner_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["deactivate", "activate", "rebuild"],
                },
            },
            "required": ["definition_id", "runner_id", "action"],
        },
    ),
    Tool(
        name="delete_build_job",
        description="Remove a runner image build assignment (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "definition_id": {"type": "string"},
                "runner_id": {"type": "string"},
            },
            "required": ["definition_id", "runner_id"],
        },
    ),
    Tool(
        name="get_build_job_log",
        description="Get the build log for a runner image build.",
        inputSchema={
            "type": "object",
            "properties": {
                "definition_id": {"type": "string"},
                "runner_id": {"type": "string"},
            },
            "required": ["definition_id", "runner_id"],
        },
    ),
    Tool(
        name="list_credentials",
        description="List credentials (metadata only, no secrets) for the current user.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_provider_config",
        description="Get the org-wide harness provider config (no API key secret).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_provider_models",
        description="List models from all connected org providers (merged catalog).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="save_provider_config",
        description="Save (upsert) the org-wide harness provider config.",
        inputSchema={
            "type": "object",
            "properties": {
                "api_key": {"type": "string"},
                "base_url": {"type": "string"},
                "default_model": {"type": "string"},
                "small_model": {"type": "string"},
                "computer_use_model": {"type": "string"},
            },
        },
    ),
    Tool(
        name="delete_provider_config",
        description="Delete the org-wide harness provider config.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_provider_connections",
        description=(
            "List connection status for all org providers "
            "(openrouter, chatgpt, amazon-bedrock, openai-compatible)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="save_provider_connection",
        description=(
            "Upsert credentials for openrouter, openai-compatible, or "
            "amazon-bedrock. ChatGPT uses chatgpt_oauth_start instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": (
                        "Provider id: openrouter, openai-compatible, "
                        "or amazon-bedrock."
                    ),
                },
                "api_key": {"type": "string"},
                "base_url": {"type": "string"},
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "OpenAI-compatible only: manual model ids for "
                        "endpoints without /models."
                    ),
                },
                "auth_method": {
                    "type": "string",
                    "description": "Bedrock only: access_keys or bearer.",
                },
                "region": {"type": "string"},
                "access_key_id": {"type": "string"},
                "secret_access_key": {"type": "string"},
                "session_token": {"type": "string"},
                "bearer_token": {"type": "string"},
            },
            "required": ["provider"],
        },
    ),
    Tool(
        name="delete_provider_connection",
        description="Delete one org provider connection.",
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": "Provider id to disconnect.",
                }
            },
            "required": ["provider"],
        },
    ),
    Tool(
        name="get_agent_s_config",
        description="Get the org-wide Agent-S harness config (defaults when unstored).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="save_agent_s_config",
        description="Save (upsert) the org-wide Agent-S harness config.",
        inputSchema={
            "type": "object",
            "properties": {
                "grounding_model": {"type": "string"},
                "grounding_width": {"type": "integer"},
                "grounding_height": {"type": "integer"},
                "model_temperature": {"type": ["number", "null"]},
                "max_steps": {"type": "integer"},
                "max_trajectory_length": {"type": "integer"},
                "enable_reflection": {"type": "boolean"},
                "enable_code_agent": {"type": "boolean"},
                "screenshot_max_dimension": {"type": "integer"},
                "action_pre_delay": {"type": "number"},
                "action_post_delay": {"type": "number"},
                "wait_delay": {"type": "number"},
            },
        },
    ),
    Tool(
        name="chatgpt_oauth_start",
        description="Start the ChatGPT device OAuth flow for the org.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="chatgpt_oauth_status",
        description=(
            "Poll once for ChatGPT OAuth completion. Returns status: "
            "pending, connected, expired, denied, or no_flow."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="chatgpt_oauth_cancel",
        description="Cancel a pending ChatGPT OAuth flow for the org.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_harness_sessions",
        description="List harness sessions for a workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="create_harness_session",
        description="Create a harness session and start the first run.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "prompt": {"type": "string"},
                "agent_name": {"type": "string"},
                "mode": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["workspace_id", "prompt"],
        },
    ),
    Tool(
        name="send_harness_message",
        description="Send a follow-up prompt to a harness session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["session_id", "prompt"],
        },
    ),
    Tool(
        name="fork_harness_session",
        description=(
            "Fork a harness session, copying messages before a "
            "message (or the full session)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="edit_harness_message",
        description="Edit a user message, then drop later turns and rerun.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message_id": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["session_id", "message_id", "prompt"],
        },
    ),
    Tool(
        name="abort_harness_session",
        description="Abort the active run of a harness session.",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="take_desktop_control",
        description=(
            "Abort busy computer-use agents on a workspace so a human can "
            "drive the shared desktop."
        ),
        inputSchema={
            "type": "object",
            "properties": {"workspace_id": {"type": "string"}},
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="list_harness_parts",
        description="List messages, parts, and pending permission/question gates of a harness session.",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="list_harness_todos",
        description="List todos of a harness session.",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="resolve_harness_permission",
        description="Resolve a pending harness permission request.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "request_id": {"type": "string"},
                "response": {"type": "string"},
            },
            "required": ["session_id", "request_id", "response"],
        },
    ),
    Tool(
        name="patch_harness_session",
        description="Rename a harness session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["session_id", "title"],
        },
    ),
    Tool(
        name="set_harness_session_mode",
        description="Switch a harness session plan|build mode (idle sessions only).",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["plan", "build"]},
            },
            "required": ["session_id", "mode"],
        },
    ),
    Tool(
        name="delete_harness_session",
        description="Delete a harness session (aborts active runs first).",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="list_harness_conversations",
        description="List harness conversations across owned workspaces in the org.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="mark_harness_session_read",
        description="Mark a harness session as read (dashboard unread state).",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="mark_harness_session_unread",
        description="Mark a harness session as unread (dashboard unread state).",
        inputSchema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    ),
    Tool(
        name="resolve_harness_question",
        description="Answer a pending harness question request.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "question_id": {"type": "string"},
                "answers": {"type": "array", "items": {}},
                "reject": {"type": "boolean"},
            },
            "required": ["session_id", "question_id"],
        },
    ),
    Tool(
        name="list_org_credential_services",
        description="List all credential services with organization activation status (admin).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="toggle_org_credential_service_activation",
        description="Activate or deactivate a credential service for the organization (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "active": {"type": "boolean"},
            },
            "required": ["service_id", "active"],
        },
    ),
    Tool(
        name="list_processes",
        description="List background processes of a workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."}
            },
            "required": ["workspace_id"],
        },
    ),
    Tool(
        name="get_process",
        description="Get the status of one background process.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "process_id": {
                    "type": "string",
                    "description": "Background process UUID or exact name.",
                },
            },
            "required": ["workspace_id", "process_id"],
        },
    ),
    Tool(
        name="start_process",
        description="Start or restart a detached background process in a workspace (same name restarts the same app with a new log, run_count+1).",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "command": {"type": "string", "description": "Shell command."},
                "workdir": {
                    "type": "string",
                    "description": "Working directory (default /workspace).",
                },
                "env": {
                    "type": "object",
                    "description": "Environment variables.",
                },
                "name": {"type": "string", "description": "Process display name."},
            },
            "required": ["workspace_id", "command", "name"],
        },
    ),
    Tool(
        name="stop_process",
        description="Stop a background process (SIGTERM, then SIGKILL after grace).",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "process_id": {
                    "type": "string",
                    "description": "Background process UUID or exact name.",
                },
            },
            "required": ["workspace_id", "process_id"],
        },
    ),
    Tool(
        name="restart_process",
        description="Restart a background process on the same row with a new log. Accepts UUID or name, keeps stored command/workdir.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "process_id": {
                    "type": "string",
                    "description": "Background process UUID or exact name.",
                },
            },
            "required": ["workspace_id", "process_id"],
        },
    ),
    Tool(
        name="delete_process",
        description="Delete a background process (stops first if running). Accepts UUID or name.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "process_id": {
                    "type": "string",
                    "description": "Background process UUID or exact name.",
                },
            },
            "required": ["workspace_id", "process_id"],
        },
    ),
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
                    "enum": [
                        "stage",
                        "unstage",
                        "discard",
                        "commit",
                        "fetch",
                        "pull",
                        "push",
                        "sync",
                        "checkout_branch",
                        "checkout_commit",
                        "checkout_remote_branch",
                        "create_branch",
                        "rename_branch",
                        "delete_branch",
                        "merge_into_current",
                        "merge_current_into",
                        "merge_abort",
                    ],
                    "description": (
                        "Whitelisted mutation-only git operation. Reads use "
                        "get_git_state/get_git_diff/get_git_commit instead."
                    ),
                },
                "repo_path": {
                    "type": "string",
                    "description": "Absolute repo path under /workspace (not needed for list_repos).",
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
            },
            "required": ["workspace_id", "operation"],
            "additionalProperties": False,
        },
    ),
]

# Map tool name → required permission
_TOOL_PERMISSIONS: dict[str, APIKeyPermission] = {
    "list_workspaces": APIKeyPermission.WORKSPACES_READ,
    "get_workspace": APIKeyPermission.WORKSPACES_READ,
    "create_workspace": APIKeyPermission.WORKSPACES_CREATE,
    "update_workspace": APIKeyPermission.WORKSPACES_UPDATE,
    "stop_workspace": APIKeyPermission.WORKSPACES_STOP,
    "resume_workspace": APIKeyPermission.WORKSPACES_RESUME,
    "remove_workspace": APIKeyPermission.WORKSPACES_DELETE,
    "list_runners": APIKeyPermission.RUNNERS_READ,
    "list_image_artifacts": APIKeyPermission.IMAGES_READ,
    "create_image_artifact": APIKeyPermission.IMAGES_CREATE,
    "list_image_definitions": APIKeyPermission.IMAGE_DEFINITIONS_READ,
    "create_image_definition": APIKeyPermission.IMAGE_DEFINITIONS_WRITE,
    "duplicate_image_definition": APIKeyPermission.IMAGE_DEFINITIONS_WRITE,
    "update_image_definition": APIKeyPermission.IMAGE_DEFINITIONS_WRITE,
    "delete_image_definition": APIKeyPermission.IMAGE_DEFINITIONS_WRITE,
    "list_build_jobs": APIKeyPermission.IMAGE_DEFINITIONS_READ,
    "create_build_job": APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS,
    "update_build_job": APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS,
    "delete_build_job": APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS,
    "get_build_job_log": APIKeyPermission.IMAGE_DEFINITIONS_READ,
    "list_credentials": APIKeyPermission.CREDENTIALS_READ,
    "get_provider_config": APIKeyPermission.HARNESS_READ,
    "list_provider_models": APIKeyPermission.HARNESS_READ,
    "save_provider_config": APIKeyPermission.HARNESS_RUN,
    "delete_provider_config": APIKeyPermission.HARNESS_RUN,
    "list_provider_connections": APIKeyPermission.HARNESS_PROVIDERS,
    "save_provider_connection": APIKeyPermission.HARNESS_PROVIDERS,
    "delete_provider_connection": APIKeyPermission.HARNESS_PROVIDERS,
    "get_agent_s_config": APIKeyPermission.HARNESS_READ,
    "save_agent_s_config": APIKeyPermission.HARNESS_RUN,
    "chatgpt_oauth_start": APIKeyPermission.HARNESS_PROVIDERS,
    "chatgpt_oauth_status": APIKeyPermission.HARNESS_PROVIDERS,
    "chatgpt_oauth_cancel": APIKeyPermission.HARNESS_PROVIDERS,
    "list_harness_sessions": APIKeyPermission.HARNESS_READ,
    "create_harness_session": APIKeyPermission.HARNESS_RUN,
    "send_harness_message": APIKeyPermission.HARNESS_RUN,
    "fork_harness_session": APIKeyPermission.HARNESS_RUN,
    "edit_harness_message": APIKeyPermission.HARNESS_RUN,
    "abort_harness_session": APIKeyPermission.HARNESS_RUN,
    "take_desktop_control": APIKeyPermission.HARNESS_RUN,
    "list_harness_parts": APIKeyPermission.HARNESS_READ,
    "list_harness_todos": APIKeyPermission.HARNESS_READ,
    "resolve_harness_permission": APIKeyPermission.HARNESS_PERMISSIONS,
    "patch_harness_session": APIKeyPermission.HARNESS_RUN,
    "set_harness_session_mode": APIKeyPermission.HARNESS_RUN,
    "delete_harness_session": APIKeyPermission.HARNESS_RUN,
    "list_harness_conversations": APIKeyPermission.HARNESS_READ,
    "mark_harness_session_read": APIKeyPermission.HARNESS_READ,
    "mark_harness_session_unread": APIKeyPermission.HARNESS_READ,
    "resolve_harness_question": APIKeyPermission.HARNESS_PERMISSIONS,
    "list_org_credential_services": APIKeyPermission.ORG_CREDENTIAL_SERVICES_READ,
    "toggle_org_credential_service_activation": APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE,
    "list_processes": APIKeyPermission.WORKSPACES_PROCESSES_READ,
    "get_process": APIKeyPermission.WORKSPACES_PROCESSES_READ,
    "start_process": APIKeyPermission.WORKSPACES_PROCESSES_RUN,
    "stop_process": APIKeyPermission.WORKSPACES_PROCESSES_RUN,
    "restart_process": APIKeyPermission.WORKSPACES_PROCESSES_RUN,
    "delete_process": APIKeyPermission.WORKSPACES_PROCESSES_RUN,
    "get_git_state": APIKeyPermission.WORKSPACES_GIT_READ,
    "get_git_diff": APIKeyPermission.WORKSPACES_GIT_READ,
    "get_git_commit": APIKeyPermission.WORKSPACES_GIT_READ,
    "git_operation": APIKeyPermission.WORKSPACES_GIT_WRITE,
}


# ---------------------------------------------------------------------------
# Helper: serialise Django model instances to JSON-friendly dicts
# ---------------------------------------------------------------------------


def _serialise(obj) -> dict:
    """Convert a Django ORM-returned dict/object to a JSON-safe dict."""
    import uuid
    from datetime import datetime

    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialise(item) for item in obj]
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _text(data) -> list[TextContent]:
    """Wrap arbitrary data as a JSON text MCP response."""
    return [
        TextContent(
            type="text", text=json.dumps(_serialise(data), indent=2, default=str)
        )
    ]


def _error(msg: str) -> list[TextContent]:
    """Return an error as a text MCP response."""
    return [TextContent(type="text", text=f"Error: {msg}")]


def _runner_service():
    """Return the process-wide RunnerService (lazy import, no cycle)."""
    from apps.runners.sio_server import get_runner_service

    return get_runner_service()


def _get_owned_workspace_or_error(api_key, org_id, workspace_id):
    """Return an owned workspace or an MCP-formatted error payload."""
    from apps.organizations.services import OrganizationService
    from common.exceptions import NotFoundError

    svc = _runner_service()
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    try:
        workspace = svc.get_workspace_for_user(
            workspace_id,
            user=api_key.user,
            organization_id=org_id,
        )
    except NotFoundError:
        return None, _error("Workspace not found")
    return workspace, None


# ---------------------------------------------------------------------------
# Tool execution logic
# ---------------------------------------------------------------------------


def _call_list_workspaces(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.runners.sio_server import get_runner_service
    from apps.organizations.services import OrganizationService

    import uuid as _uuid

    svc = get_runner_service()
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)

    runner_id = None
    if args.get("runner_id"):
        try:
            runner_id = _uuid.UUID(args["runner_id"])
        except ValueError:
            return _error("Invalid runner_id UUID")

    workspaces = svc.list_workspaces(
        runner_id=runner_id,
        organization_id=org_id,
        user=api_key.user,
    )
    result = [
        {
            "id": str(w.id),
            "name": w.name,
            "status": str(w.status),
            "runner_id": str(w.runner_id),
            "runtime_type": str(w.runtime_type),
            "created_at": w.created_at.isoformat(),
        }
        for w in workspaces
    ]
    return _text(result)


def _call_get_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    import uuid as _uuid

    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return _error("workspace_id is required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
    if error is not None:
        return error

    result = {
        "id": str(workspace.id),
        "name": workspace.name,
        "status": str(workspace.status),
        "runner_id": str(workspace.runner_id),
        "runtime_type": str(workspace.runtime_type),
        "desktop_width": workspace.desktop_width,
        "desktop_height": workspace.desktop_height,
        "created_at": workspace.created_at.isoformat(),
    }
    return _text(result)


def _call_create_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    """Synchronously dispatch workspace creation (fires and returns task info)."""
    from apps.runners.sio_server import get_runner_service
    from apps.organizations.services import OrganizationService
    from common.exceptions import NotFoundError, ConflictError

    import asyncio
    import uuid as _uuid

    name = args.get("name")
    if not name:
        return _error("name is required")

    runner_id = None
    if args.get("runner_id"):
        try:
            runner_id = _uuid.UUID(args["runner_id"])
        except ValueError:
            return _error("Invalid runner_id UUID")

    repos = args.get("repos", [])
    runtime_type = args.get("runtime_type", "docker")
    desktop_width = args.get("desktop_width")
    desktop_height = args.get("desktop_height")
    image_artifact_id = None
    if args.get("image_artifact_id"):
        try:
            image_artifact_id = _uuid.UUID(args["image_artifact_id"])
        except ValueError:
            return _error("Invalid image_artifact_id UUID")

    svc = get_runner_service()

    async def _create():
        workspace, task = await svc.create_workspace(
            name=name,
            repos=repos,
            runtime_type=runtime_type,
            env_vars={},
            ssh_keys=[],
            credentials=[],
            runner_id=runner_id,
            image_artifact_id=image_artifact_id,
            desktop_width=desktop_width,
            desktop_height=desktop_height,
            user=api_key.user,
            organization_id=org_id,
        )
        return workspace, task

    try:
        loop = asyncio.new_event_loop()
        workspace, task = loop.run_until_complete(_create())
        loop.close()
        return _text(
            {
                "workspace_id": str(workspace.id),
                "task_id": str(task.id),
                "status": str(workspace.status),
                "message": "Workspace creation started. Use get_workspace to check status.",
            }
        )
    except (NotFoundError, ConflictError, ValueError) as e:
        return _error(str(e))


def _call_update_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    """Update mutable workspace metadata, including desktop geometry."""
    from apps.runners.sio_server import get_runner_service
    from common.exceptions import NotFoundError, ConflictError

    import asyncio
    import uuid as _uuid

    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return _error("workspace_id is required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    _workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
    if error is not None:
        return error

    svc = get_runner_service()

    async def _update():
        return await svc.update_workspace(
            workspace_id,
            name=args.get("name"),
            qemu_vcpus=args.get("qemu_vcpus"),
            qemu_memory_mb=args.get("qemu_memory_mb"),
            qemu_disk_size_gb=args.get("qemu_disk_size_gb"),
            desktop_width=args.get("desktop_width"),
            desktop_height=args.get("desktop_height"),
        )

    try:
        loop = asyncio.new_event_loop()
        updated = loop.run_until_complete(_update())
        loop.close()
        return _text(
            {
                "id": str(updated.id),
                "name": updated.name,
                "desktop_width": updated.desktop_width,
                "desktop_height": updated.desktop_height,
                "updated_at": updated.updated_at.isoformat(),
            }
        )
    except (NotFoundError, ConflictError, ValueError) as e:
        return _error(str(e))


def _call_stop_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    from common.exceptions import NotFoundError, ConflictError

    import asyncio
    import uuid as _uuid

    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return _error("workspace_id is required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    try:
        _workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
        if error is not None:
            return error

        from apps.runners.sio_server import get_runner_service

        svc = get_runner_service()

        async def _stop():
            return await svc.stop_workspace(workspace_id)

        loop = asyncio.new_event_loop()
        task = loop.run_until_complete(_stop())
        loop.close()
        return _text({"task_id": str(task.id), "message": "Stop task dispatched."})
    except (NotFoundError, ConflictError) as e:
        return _error(str(e))


def _call_resume_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    from common.exceptions import NotFoundError, ConflictError

    import asyncio
    import uuid as _uuid

    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return _error("workspace_id is required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    try:
        _workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
        if error is not None:
            return error

        from apps.runners.sio_server import get_runner_service

        svc = get_runner_service()

        async def _resume():
            return await svc.resume_workspace(workspace_id)

        loop = asyncio.new_event_loop()
        task = loop.run_until_complete(_resume())
        loop.close()
        return _text({"task_id": str(task.id), "message": "Resume task dispatched."})
    except (NotFoundError, ConflictError) as e:
        return _error(str(e))


def _call_remove_workspace(api_key, org_id, args: dict) -> list[TextContent]:
    from common.exceptions import NotFoundError, ConflictError

    import asyncio
    import uuid as _uuid

    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return _error("workspace_id is required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    try:
        _workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
        if error is not None:
            return error

        from apps.runners.sio_server import get_runner_service

        svc = get_runner_service()

        async def _remove():
            return await svc.remove_workspace(workspace_id)

        loop = asyncio.new_event_loop()
        task = loop.run_until_complete(_remove())
        loop.close()
        return _text({"task_id": str(task.id), "message": "Remove task dispatched."})
    except (NotFoundError, ConflictError) as e:
        return _error(str(e))


def _call_list_runners(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.runners.sio_server import get_runner_service
    from apps.organizations.services import OrganizationService

    svc = get_runner_service()
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)

    runners = svc.list_runners(organization_id=org_id)
    result = [
        {
            "id": str(r.id),
            "name": r.name,
            "status": str(r.status),
        }
        for r in runners
    ]
    return _text(result)


def _call_list_image_artifacts(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.runners.sio_server import get_runner_service
    from apps.organizations.services import OrganizationService

    svc = get_runner_service()
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    svc.image_instances.timeout_stale(timeout_hours=1)
    artifacts = svc.list_image_artifacts_for_user(user=api_key.user)
    result = [
        {
            "id": str(s.id),
            "source_workspace_id": (
                str(s.origin_workspace_id) if s.origin_workspace_id else None
            ),
            "name": s.name,
            "status": str(s.status),
            "size_bytes": s.size_bytes,
            "created_at": s.created_at.isoformat(),
        }
        for s in artifacts
    ]
    return _text(result)


def _call_create_image_artifact(api_key, org_id, args: dict) -> list[TextContent]:
    from common.exceptions import NotFoundError

    import asyncio
    import uuid as _uuid

    workspace_id_str = args.get("workspace_id")
    name = args.get("name")
    if not workspace_id_str or not name:
        return _error("workspace_id and name are required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    try:
        workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
        if error is not None:
            return error

        from apps.runners.sio_server import get_runner_service

        svc = get_runner_service()

        async def _create():
            return await svc.create_image_artifact(
                workspace_id=workspace_id,
                name=name,
                organization_id=org_id,
            )

        loop = asyncio.new_event_loop()
        workspace, task = loop.run_until_complete(_create())
        loop.close()
        return _text({"task_id": str(task.id), "workspace_id": str(workspace.id)})
    except (NotFoundError, ValueError) as e:
        return _error(str(e))


def _image_definition_payload(definition) -> dict:
    """Serialize an image definition the same way REST list responses do."""
    from apps.runners.repositories import ImageDefinitionRepository

    return {
        "id": str(definition.id),
        "organization_id": (
            str(definition.organization_id) if definition.organization_id else None
        ),
        "name": definition.name,
        "description": definition.description,
        "is_standard": definition.organization_id is None,
        "runtime_type": definition.runtime_type,
        "base_distro": definition.base_distro,
        "packages": list(definition.packages or []),
        "env_vars": dict(definition.env_vars or {}),
        "custom_dockerfile": definition.custom_dockerfile or "",
        "custom_init_script": definition.custom_init_script or "",
        "is_active": bool(definition.is_active),
        "status": getattr(definition, "status", "active"),
        "runner_build_summary": ImageDefinitionRepository.build_summary(definition),
        "delete_last_error": getattr(definition, "delete_last_error", "") or "",
        "created_at": definition.created_at.isoformat(),
        "updated_at": definition.updated_at.isoformat(),
    }


def _call_list_image_definitions(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.sio_server import get_runner_service

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    svc = get_runner_service()
    definitions = svc.list_image_definitions(org_id)
    return _text([_image_definition_payload(definition) for definition in definitions])


def _call_create_image_definition(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.models import ImageDefinition

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    if org_service.get_user_role(api_key.user, org_id) != "admin":
        return _error("Admin role required")

    name = (args.get("name") or "").strip()
    if not name:
        return _error("name is required")

    runtime_type = args.get("runtime_type") or "docker"
    base_distro = args.get("base_distro") or "ubuntu:22.04"
    if runtime_type == "qemu" and not base_distro.lower().startswith("ubuntu:"):
        return _error(
            "QEMU image definitions currently require an ubuntu:<version> base distro"
        )

    definition = ImageDefinition.objects.create(
        organization_id=org_id,
        created_by=api_key.user,
        name=name,
        description=args.get("description") or "",
        runtime_type=runtime_type,
        base_distro=base_distro,
        packages=list(args.get("packages") or []),
        env_vars=dict(args.get("env_vars") or {}),
        custom_dockerfile=args.get("custom_dockerfile") or "",
        custom_init_script=args.get("custom_init_script") or "",
        is_active=bool(args.get("is_active", True)),
    )
    return _text({"id": str(definition.id), "name": definition.name})


def _call_duplicate_image_definition(api_key, org_id, args: dict) -> list[TextContent]:
    from django.db.models import Q
    from apps.organizations.services import OrganizationService
    from apps.runners.models import ImageDefinition

    import uuid as _uuid

    definition_id_str = args.get("definition_id")
    if not definition_id_str:
        return _error("definition_id is required")
    try:
        definition_id = _uuid.UUID(definition_id_str)
    except ValueError:
        return _error("Invalid definition_id UUID")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    if org_service.get_user_role(api_key.user, org_id) != "admin":
        return _error("Admin role required")

    source = (
        ImageDefinition.objects.filter(id=definition_id)
        .filter(Q(organization__isnull=True) | Q(organization_id=org_id))
        .first()
    )
    if source is None:
        return _error("Image definition not found")

    raw_name = args.get("name")
    if raw_name is not None and not str(raw_name).strip():
        return _error("name cannot be empty")

    base_name = str(raw_name).strip() if raw_name is not None else source.name
    if not base_name:
        base_name = "image"
    base_name = base_name[:255]
    candidate = base_name
    if ImageDefinition.objects.filter(organization_id=org_id, name=candidate).exists():
        suffix = " (Copy)"
        candidate = f"{base_name[: 255 - len(suffix)]}{suffix}"
        index = 2
        while ImageDefinition.objects.filter(
            organization_id=org_id,
            name=candidate,
        ).exists():
            suffix = f" (Copy {index})"
            candidate = f"{base_name[: 255 - len(suffix)]}{suffix}"
            index += 1

    copied = ImageDefinition.objects.create(
        organization_id=org_id,
        created_by=api_key.user,
        name=candidate,
        description=source.description,
        runtime_type=source.runtime_type,
        base_distro=source.base_distro,
        packages=list(source.packages or []),
        env_vars=dict(source.env_vars or {}),
        custom_dockerfile=source.custom_dockerfile or "",
        custom_init_script=source.custom_init_script or "",
        is_active=bool(source.is_active),
    )
    return _text({"id": str(copied.id), "name": copied.name})


def _call_update_image_definition(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.repositories import ImageDefinitionRepository

    import uuid as _uuid

    definition_id_str = args.get("definition_id")
    if not definition_id_str:
        return _error("definition_id is required")
    try:
        definition_id = _uuid.UUID(definition_id_str)
    except ValueError:
        return _error("Invalid definition_id UUID")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    if org_service.get_user_role(api_key.user, org_id) != "admin":
        return _error("Admin role required")

    definition = ImageDefinitionRepository.get_by_id_and_org(definition_id, org_id)
    if definition is None:
        return _error("Image definition not found")

    runtime_type = args.get("runtime_type") or definition.runtime_type
    base_distro = args.get("base_distro") or definition.base_distro
    if runtime_type == "qemu" and not base_distro.lower().startswith("ubuntu:"):
        return _error(
            "QEMU image definitions currently require an ubuntu:<version> base distro"
        )

    for field in [
        "name",
        "description",
        "runtime_type",
        "base_distro",
        "packages",
        "env_vars",
        "custom_dockerfile",
        "custom_init_script",
        "is_active",
    ]:
        if field in args and args[field] is not None:
            setattr(definition, field, args[field])
    definition.save()
    return _text({"id": str(definition.id), "name": definition.name})


def _call_delete_image_definition(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.repositories import ImageDefinitionRepository
    from apps.runners.sio_server import get_runner_service
    from common.exceptions import ConflictError

    import asyncio
    import uuid as _uuid

    definition_id_str = args.get("definition_id")
    if not definition_id_str:
        return _error("definition_id is required")
    try:
        definition_id = _uuid.UUID(definition_id_str)
    except ValueError:
        return _error("Invalid definition_id UUID")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    if org_service.get_user_role(api_key.user, org_id) != "admin":
        return _error("Admin role required")

    definition = ImageDefinitionRepository.get_by_id_and_org(definition_id, org_id)
    if definition is None:
        return _error("Image definition not found")

    svc = get_runner_service()

    async def _delete():
        await svc.delete_image_definition(definition_id)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_delete())
    except ConflictError as e:
        return _error(e.message)
    except ValueError as e:
        return _error(str(e))
    finally:
        loop.close()

    updated = ImageDefinitionRepository.get_by_id(definition_id)
    if updated is None or updated.status == "deleted":
        return _text({"deleted": True, "status": "deleted"})
    return _text(_image_definition_payload(updated))


def _call_list_build_jobs(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.repositories import ImageDefinitionRepository
    from apps.runners.sio_server import get_runner_service

    import uuid as _uuid

    definition_id_str = args.get("definition_id")
    if not definition_id_str:
        return _error("definition_id is required")
    try:
        definition_id = _uuid.UUID(definition_id_str)
    except ValueError:
        return _error("Invalid definition_id UUID")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    if ImageDefinitionRepository.get_by_id_and_org(definition_id, org_id) is None:
        return _error("Image definition not found")

    svc = get_runner_service()
    builds = svc.list_build_jobs(definition_id, org_id)
    return _text(
        [
            {
                "image_tag": (
                    build.image_instance.runner_ref
                    if getattr(build, "image_instance", None) is not None
                    and build.image_definition.runtime_type == "docker"
                    else ""
                ),
                "image_path": (
                    build.image_instance.runner_ref
                    if getattr(build, "image_instance", None) is not None
                    and build.image_definition.runtime_type == "qemu"
                    else ""
                ),
                "id": str(build.id),
                "image_definition_id": str(build.image_definition_id),
                "runner_id": str(build.runner_id),
                "status": build.status,
                "build_log": build.build_log,
                "build_task_id": str(build.build_task_id)
                if build.build_task_id
                else None,
                "built_at": build.built_at.isoformat() if build.built_at else None,
                "deactivated_at": (
                    build.deactivated_at.isoformat() if build.deactivated_at else None
                ),
                "created_at": build.created_at.isoformat(),
                "updated_at": build.updated_at.isoformat(),
            }
            for build in builds
        ]
    )


def _call_create_build_job(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.repositories import ImageDefinitionRepository, RunnerRepository
    from apps.runners.sio_server import get_runner_service

    import asyncio
    import uuid as _uuid

    definition_id_str = args.get("definition_id")
    runner_id_str = args.get("runner_id")
    if not definition_id_str or not runner_id_str:
        return _error("definition_id and runner_id are required")
    try:
        definition_id = _uuid.UUID(definition_id_str)
        runner_id = _uuid.UUID(runner_id_str)
    except ValueError:
        return _error("Invalid definition_id or runner_id UUID")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    if org_service.get_user_role(api_key.user, org_id) != "admin":
        return _error("Admin role required")

    definition = ImageDefinitionRepository.get_by_id_and_org(definition_id, org_id)
    runner = RunnerRepository.get_by_id(runner_id)
    if definition is None:
        return _error("Image definition not found")
    if runner is None or runner.organization_id != org_id:
        return _error("Runner not found")

    svc = get_runner_service()

    async def _create():
        return await svc.trigger_build_job(
            image_definition=definition,
            runner=runner,
            activate=bool(args.get("activate", True)),
            created_by=api_key.user,
        )

    loop = asyncio.new_event_loop()
    build = loop.run_until_complete(_create())
    loop.close()
    return _text({"id": str(build.id), "status": build.status})


def _call_update_build_job(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.models import ImageBuildJob
    from apps.runners.repositories import ImageBuildJobRepository
    from apps.runners.sio_server import get_runner_service
    from common.exceptions import ConflictError
    from django.utils import timezone

    import asyncio
    import uuid as _uuid

    definition_id_str = args.get("definition_id")
    runner_id_str = args.get("runner_id")
    action = (args.get("action") or "").strip().lower()
    if not definition_id_str or not runner_id_str or not action:
        return _error("definition_id, runner_id and action are required")
    try:
        definition_id = _uuid.UUID(definition_id_str)
        runner_id = _uuid.UUID(runner_id_str)
    except ValueError:
        return _error("Invalid definition_id or runner_id UUID")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    if org_service.get_user_role(api_key.user, org_id) != "admin":
        return _error("Admin role required")

    build = ImageBuildJobRepository.get_for_org(definition_id, runner_id, org_id)
    if build is None:
        return _error("Runner image build not found")

    if action == "deactivate":
        try:
            svc = get_runner_service()
            svc._ensure_definition_mutable(build.image_definition)
        except ConflictError as e:
            return _error(e.message)
        build.status = ImageBuildJob.Status.DEACTIVATED
        build.deactivated_at = timezone.now()
        build.save(update_fields=["status", "deactivated_at", "updated_at"])
        return _text({"id": str(build.id), "status": build.status})

    if action not in {"activate", "rebuild"}:
        return _error("action must be one of: deactivate, activate, rebuild")

    svc = get_runner_service()

    async def _update():
        if action == "activate":
            return await svc.activate_build_job(
                build,
                created_by=api_key.user,
            )
        return await svc.trigger_build_job(
            image_definition=build.image_definition,
            runner=build.runner,
            activate=True,
            created_by=api_key.user,
        )

    loop = asyncio.new_event_loop()
    try:
        updated = loop.run_until_complete(_update())
    except ConflictError as e:
        return _error(e.message)
    finally:
        loop.close()
    return _text({"id": str(updated.id), "status": updated.status})


def _call_delete_build_job(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.repositories import ImageBuildJobRepository
    from apps.runners.sio_server import get_runner_service
    from common.exceptions import ConflictError

    import asyncio
    import uuid as _uuid

    definition_id_str = args.get("definition_id")
    runner_id_str = args.get("runner_id")
    if not definition_id_str or not runner_id_str:
        return _error("definition_id and runner_id are required")
    try:
        definition_id = _uuid.UUID(definition_id_str)
        runner_id = _uuid.UUID(runner_id_str)
    except ValueError:
        return _error("Invalid definition_id or runner_id UUID")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    if org_service.get_user_role(api_key.user, org_id) != "admin":
        return _error("Admin role required")

    build = ImageBuildJobRepository.get_for_org(definition_id, runner_id, org_id)
    if build is None:
        return _error("Runner image build not found")

    svc = get_runner_service()

    async def _delete():
        await svc.delete_build_job(build.id)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_delete())
    except ConflictError as e:
        return _error(e.message)
    except ValueError as e:
        return _error(str(e))
    finally:
        loop.close()
    return _text({"deleted": True})


def _call_get_build_job_log(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.repositories import ImageBuildJobRepository

    import uuid as _uuid

    definition_id_str = args.get("definition_id")
    runner_id_str = args.get("runner_id")
    if not definition_id_str or not runner_id_str:
        return _error("definition_id and runner_id are required")
    try:
        definition_id = _uuid.UUID(definition_id_str)
        runner_id = _uuid.UUID(runner_id_str)
    except ValueError:
        return _error("Invalid definition_id or runner_id UUID")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)

    build = ImageBuildJobRepository.get_for_org(definition_id, runner_id, org_id)
    if build is None:
        return _error("Runner image build not found")
    return _text({"build_log": build.build_log})


def _call_list_credentials(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.credentials.services import CredentialSvc

    svc = CredentialSvc()
    creds = svc.list_credentials(api_key.user, org_id)
    result = [
        {
            "id": str(c.id),
            "name": c.name,
            "service_name": c.service.name,
            "service_slug": c.service.slug,
            "credential_type": str(c.service.credential_type),
            "created_at": c.created_at.isoformat(),
        }
        for c in creds
    ]
    return _text(result)


def _get_harness_service():
    """Return the process-wide HarnessService (shared with REST)."""
    from apps.harness.harness_service import get_harness_service

    return get_harness_service()


def _session_dict(session, *, unread: bool = False) -> dict:
    """Serialize a HarnessSession ORM row for MCP responses."""
    return {
        "id": str(session.id),
        "workspace_id": str(session.workspace_id),
        "title": session.title or "",
        "mode": session.mode,
        "agent_name": session.agent_name,
        "model": session.model or "",
        "reasoning_effort": getattr(session, "reasoning_effort", "") or "",
        "status": session.status,
        "cost": float(session.cost or 0.0),
        "tokens": dict(session.tokens or {}),
        "unread": unread,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


def _session_dict_with_unread(service, session) -> dict:
    """Serialize a session and compute its unread flag."""
    return _session_dict(session, unread=service.is_session_unread(session))


def _optional_text(value: object) -> str | None:
    """Return the stripped string or None when blank (edit no-op)."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _owned_harness_session_or_error(api_key, org_id, session_id):
    """Return a harness session scoped to an owned workspace."""
    import uuid as _uuid

    from common.exceptions import NotFoundError

    try:
        session_uuid = _uuid.UUID(str(session_id))
    except ValueError:
        return None, _error("Invalid session_id UUID")

    service = _get_harness_service()
    try:
        session = service.get_session(session_uuid)
    except NotFoundError:
        return None, _error("Harness session not found")

    _workspace, error = _get_owned_workspace_or_error(
        api_key, org_id, session.workspace_id
    )
    if error is not None:
        return None, error
    return session, None


def _call_get_provider_config(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.harness.api import _fetch_org_provider_config
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    return _text(_fetch_org_provider_config(org_id).model_dump(mode="json"))


def _call_list_provider_models(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.harness.api import _list_org_provider_models
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    models = _list_org_provider_models(org_id)
    return _text([model.model_dump(mode="json") for model in models])


def _call_save_provider_config(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.harness.api import ProviderConfigIn, _save_org_provider_config
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    payload = ProviderConfigIn(**args)
    return _text(_save_org_provider_config(org_id, payload).model_dump(mode="json"))


def _call_delete_provider_config(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.harness.api import _delete_org_provider_config
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    _delete_org_provider_config(org_id)
    return _text({"deleted": True})


def _call_list_provider_connections(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.harness.api import _list_org_provider_connections
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    connections = _list_org_provider_connections(org_id)
    return _text([row.model_dump(mode="json") for row in connections])


def _call_save_provider_connection(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.harness.api import (
        BedrockConnectionIn,
        OpenAICompatibleConnectionIn,
        OpenRouterConnectionIn,
        ProviderConnectionUpsertIn,
        _parse_provider_param,
        _upsert_bedrock_connection,
        _upsert_openai_compatible_connection,
        _upsert_openrouter_connection,
    )
    from apps.harness.enums import ProviderType
    from apps.organizations.services import OrganizationService

    provider = args.get("provider")
    if not provider:
        return _error("provider is required")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)

    try:
        provider_id = _parse_provider_param(str(provider))
    except ValueError as exc:
        return _error(str(exc))

    if provider_id == ProviderType.CHATGPT:
        return _error("ChatGPT uses OAuth connect; use chatgpt_oauth_start")

    payload = ProviderConnectionUpsertIn(
        **{key: value for key, value in args.items() if key != "provider"}
    )

    try:
        if provider_id == ProviderType.OPENROUTER:
            body = OpenRouterConnectionIn(
                api_key=payload.api_key,
                base_url=payload.base_url,
            )
            connection = _upsert_openrouter_connection(org_id, body)
        elif provider_id == ProviderType.OPENAI_COMPATIBLE:
            # MCP dict args carry key presence: omitted "models" keeps
            # stored models, explicit [] clears them (REST parity via
            # model_fields_set is handled by the shared business helper).
            raw = {key: value for key, value in args.items() if key != "provider"}
            compat_kwargs: dict = {
                "api_key": payload.api_key,
                "base_url": payload.base_url,
            }
            if "models" in raw:
                compat_kwargs["models"] = list(payload.models or [])
                compat_body = OpenAICompatibleConnectionIn(**compat_kwargs)
            else:
                compat_body = OpenAICompatibleConnectionIn(**compat_kwargs)
                compat_body.model_fields_set.discard("models")
            connection = _upsert_openai_compatible_connection(org_id, compat_body)
        else:
            body = BedrockConnectionIn(
                auth_method=payload.auth_method,
                region=payload.region,
                access_key_id=payload.access_key_id,
                secret_access_key=payload.secret_access_key,
                session_token=payload.session_token,
                bearer_token=payload.bearer_token,
            )
            connection = _upsert_bedrock_connection(org_id, body)
    except (ValueError, KeyError, TypeError) as exc:
        return _error(str(exc))

    return _text(connection.model_dump(mode="json"))


def _call_get_agent_s_config(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.harness.api import _fetch_org_agent_s_config
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    return _text(_fetch_org_agent_s_config(org_id).model_dump(mode="json"))


def _call_save_agent_s_config(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.harness.api import _save_org_agent_s_config_partial
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    try:
        # MCP dict args carry no "explicitly set" tracking, so the service
        # merge treats provided keys as the partial update (never defaults).
        return _text(
            _save_org_agent_s_config_partial(org_id, args).model_dump(mode="json")
        )
    except (ValueError, KeyError, TypeError) as exc:
        return _error(str(exc))


def _call_delete_provider_connection(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.harness.api import _delete_org_provider_connection, _parse_provider_param
    from apps.organizations.services import OrganizationService
    from common.exceptions import NotFoundError

    provider = args.get("provider")
    if not provider:
        return _error("provider is required")

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)

    try:
        provider_id = _parse_provider_param(str(provider))
        _delete_org_provider_connection(org_id, provider_id)
    except ValueError as exc:
        return _error(str(exc))
    except NotFoundError as exc:
        return _error(exc.message)

    return _text({"deleted": True, "provider": provider_id})


async def _call_chatgpt_oauth_start(api_key, org_id, args: dict) -> list[TextContent]:
    import time

    from asgiref.sync import sync_to_async

    from apps.harness.api import (
        CHATGPT_OAUTH_FLOW_EXPIRES_SECONDS,
        _PendingChatGPTOAuth,
        _chatgpt_oauth_lock,
        _chatgpt_oauth_pending,
    )
    from apps.harness.providers.base import ProviderError
    from apps.harness.providers.chatgpt_oauth import start_device_flow
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(api_key.user, org_id)

    try:
        flow = await start_device_flow()
        async with _chatgpt_oauth_lock:
            _chatgpt_oauth_pending[org_id] = _PendingChatGPTOAuth(
                device_auth_id=flow.device_auth_id,
                user_code=flow.user_code,
                interval=flow.interval,
                created_at=time.monotonic(),
            )
        return _text(
            {
                "user_code": flow.user_code,
                "verification_url": flow.verification_url,
                "interval": flow.interval,
                "expires_in": CHATGPT_OAUTH_FLOW_EXPIRES_SECONDS,
            }
        )
    except ProviderError as exc:
        return _error(str(exc))


async def _call_chatgpt_oauth_status(api_key, org_id, args: dict) -> list[TextContent]:
    import time

    from asgiref.sync import sync_to_async

    from apps.harness.api import (
        CHATGPT_OAUTH_FLOW_EXPIRES_SECONDS,
        _chatgpt_oauth_lock,
        _chatgpt_oauth_pending,
    )
    from apps.harness.enums import ProviderType
    from apps.harness.providers.base import ProviderError
    from apps.harness.providers.chatgpt_oauth import poll_device_flow_once
    from apps.harness.services import ProviderConfigService
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(api_key.user, org_id)

    try:
        async with _chatgpt_oauth_lock:
            pending = _chatgpt_oauth_pending.get(org_id)
        if pending is None:
            return _text(
                {
                    "status": "no_flow",
                    "detail": "No pending ChatGPT OAuth flow for this organization",
                }
            )
        if (
            time.monotonic() - pending.created_at
            > CHATGPT_OAUTH_FLOW_EXPIRES_SECONDS
        ):
            async with _chatgpt_oauth_lock:
                _chatgpt_oauth_pending.pop(org_id, None)
            return _text({"status": "expired"})

        result = await poll_device_flow_once(
            pending.device_auth_id,
            pending.user_code,
        )
        if result.status == "pending":
            return _text({"status": "pending"})
        if result.status == "expired":
            async with _chatgpt_oauth_lock:
                _chatgpt_oauth_pending.pop(org_id, None)
            return _text({"status": "expired"})
        if result.status == "denied":
            async with _chatgpt_oauth_lock:
                _chatgpt_oauth_pending.pop(org_id, None)
            return _text({"status": "denied"})
        if result.tokens is None:
            return _error("OAuth completed without tokens")

        credentials = {
            "access": result.tokens.access,
            "refresh": result.tokens.refresh,
            "expires": result.tokens.expires,
            "account_id": result.tokens.account_id or "",
            "residency": result.tokens.residency or "",
        }
        service = ProviderConfigService()
        await sync_to_async(service.save_connection)(
            organization_id=org_id,
            provider=ProviderType.CHATGPT,
            credentials=credentials,
            config={},
        )
        async with _chatgpt_oauth_lock:
            _chatgpt_oauth_pending.pop(org_id, None)
        return _text(
            {
                "status": "connected",
                "account_id": result.tokens.account_id or "",
            }
        )
    except ProviderError as exc:
        return _error(str(exc))


async def _call_chatgpt_oauth_cancel(api_key, org_id, args: dict) -> list[TextContent]:
    from asgiref.sync import sync_to_async

    from apps.harness.api import _chatgpt_oauth_lock, _chatgpt_oauth_pending
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    await sync_to_async(org_service.require_membership)(api_key.user, org_id)
    async with _chatgpt_oauth_lock:
        _chatgpt_oauth_pending.pop(org_id, None)
    return _text({"cancelled": True})


def _call_list_harness_sessions(api_key, org_id, args: dict) -> list[TextContent]:
    import uuid as _uuid

    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return _error("workspace_id is required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
    if error is not None:
        return error

    service = _get_harness_service()
    sessions = service.list_sessions(workspace_id)
    unread_map = service.unread_for_sessions(sessions)
    return _text(
        [
            _session_dict(session, unread=unread_map.get(session.id, False))
            for session in sessions
        ]
    )


async def _call_create_harness_session(
    api_key, org_id, args: dict
) -> list[TextContent]:
    """Create a session and start the first run without nested event loops."""
    import uuid as _uuid

    from asgiref.sync import sync_to_async
    from common.exceptions import ConflictError, NotFoundError

    workspace_id_str = args.get("workspace_id")
    prompt = (args.get("prompt") or "").strip()
    if not workspace_id_str or not prompt:
        return _error("workspace_id and prompt are required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    workspace, error = await sync_to_async(_get_owned_workspace_or_error)(
        api_key, org_id, workspace_id
    )
    if error is not None:
        return error

    service = _get_harness_service()

    try:
        session = await sync_to_async(service.create_session)(
            workspace_id=workspace.id,
            organization_id=org_id,
            prompt=prompt,
            agent_name=args.get("agent_name") or "build",
            mode=args.get("mode") or "build",
            model=args.get("model") or "",
            reasoning_effort=args.get("reasoning_effort") or "",
            skill_ids=list(args.get("skill_ids") or []),
            user_id=api_key.user.id,
        )
        await service.start_run(
            session,
            prompt,
            organization_id=org_id,
            workspace_id=str(workspace.id),
            user_id=api_key.user.id,
            skill_ids=list(args.get("skill_ids") or []),
        )
        fresh = await sync_to_async(service.get_session)(session.id)
        payload = await sync_to_async(_session_dict_with_unread)(service, fresh)
        return _text(payload)
    except (NotFoundError, ConflictError, ValueError, KeyError) as exc:
        return _error(str(exc))


async def _call_send_harness_message(api_key, org_id, args: dict) -> list[TextContent]:
    """Send a follow-up prompt without nested event loops."""
    import uuid as _uuid

    from asgiref.sync import sync_to_async
    from common.exceptions import ConflictError, NotFoundError

    session_id_str = args.get("session_id")
    prompt = (args.get("prompt") or "").strip()
    if not session_id_str or not prompt:
        return _error("session_id and prompt are required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    session, error = await sync_to_async(_owned_harness_session_or_error)(
        api_key, org_id, session_id
    )
    if error is not None:
        return error

    service = _get_harness_service()

    try:
        current = await sync_to_async(service.get_session)(session.id)
        await sync_to_async(service.ensure_user_promptable)(current)
        if not service.is_running(current.id):
            if args.get("mode"):
                current = await sync_to_async(service.set_mode)(
                    current.id, args["mode"]
                )
            if args.get("model"):
                current = await sync_to_async(service.set_model)(
                    current.id, args["model"]
                )
            if args.get("reasoning_effort"):
                current = await sync_to_async(service.set_reasoning_effort)(
                    current.id, args["reasoning_effort"]
                )
        await service.start_run(
            current,
            prompt,
            organization_id=org_id,
            workspace_id=str(current.workspace_id),
            user_id=api_key.user.id,
            skill_ids=list(args.get("skill_ids") or []) or None,
        )
        fresh = await sync_to_async(service.get_session)(current.id)
        payload = await sync_to_async(_session_dict_with_unread)(service, fresh)
        return _text(payload)
    except (NotFoundError, ConflictError, ValueError, KeyError) as exc:
        return _error(str(exc))


async def _call_fork_harness_session(api_key, org_id, args) -> list:  # type: ignore[no-untyped-def]
    """Fork a session, copying the message prefix (no run, no busy guard)."""
    import uuid as _uuid

    from asgiref.sync import sync_to_async
    from common.exceptions import ConflictError, NotFoundError

    session_id_str = args.get("session_id")
    if not session_id_str:
        return _error("session_id is required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    message_id = None
    if args.get("message_id"):
        try:
            message_id = _uuid.UUID(args["message_id"])
        except ValueError:
            return _error("Invalid message_id UUID")

    session, error = await sync_to_async(_owned_harness_session_or_error)(
        api_key, org_id, session_id
    )
    if error is not None:
        return error

    service = _get_harness_service()

    try:
        current = await sync_to_async(service.get_session)(session.id)
        await sync_to_async(service.ensure_user_promptable)(current)
        forked = await service.fork_session(current.id, message_id)
        payload = await sync_to_async(_session_dict_with_unread)(service, forked)
        return _text(payload)
    except (NotFoundError, ConflictError, ValueError, KeyError) as exc:
        return _error(str(exc))


async def _call_edit_harness_message(api_key, org_id, args) -> list:  # type: ignore[no-untyped-def]
    """Edit a user message, drop the suffix, and rerun with a new prompt."""
    import uuid as _uuid

    from asgiref.sync import sync_to_async
    from common.exceptions import ConflictError, NotFoundError

    session_id_str = args.get("session_id")
    message_id_str = args.get("message_id")
    prompt = (args.get("prompt") or "").strip()
    if not session_id_str or not message_id_str or not prompt:
        return _error("session_id, message_id and prompt are required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")
    try:
        message_id = _uuid.UUID(message_id_str)
    except ValueError:
        return _error("Invalid message_id UUID")

    session, error = await sync_to_async(_owned_harness_session_or_error)(
        api_key, org_id, session_id
    )
    if error is not None:
        return error

    service = _get_harness_service()
    mode = _optional_text(args.get("mode"))
    model = _optional_text(args.get("model"))
    effort = _optional_text(args.get("reasoning_effort"))

    try:
        current = await sync_to_async(service.get_session)(session.id)
        await sync_to_async(service.ensure_user_promptable)(current)
        await service.edit_user_message(
            current.id,
            message_id=message_id,
            prompt=prompt,
            mode=mode,
            model=model,
            reasoning_effort=effort,
            skill_ids=list(args.get("skill_ids") or []) or None,
            organization_id=org_id,
            workspace_id=str(current.workspace_id),
            user_id=api_key.user.id,
        )
        fresh = await sync_to_async(service.get_session)(current.id)
        payload = await sync_to_async(_session_dict_with_unread)(service, fresh)
        return _text(payload)
    except (NotFoundError, ConflictError, ValueError, KeyError) as exc:
        return _error(str(exc))


def _call_abort_harness_session(api_key, org_id, args: dict) -> list[TextContent]:
    import asyncio
    import uuid as _uuid

    session_id_str = args.get("session_id")
    if not session_id_str:
        return _error("session_id is required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = _get_harness_service()

    async def _abort():
        return await service.abort_run(session.id)

    loop = asyncio.new_event_loop()
    updated = loop.run_until_complete(_abort())
    loop.close()
    return _text(_session_dict_with_unread(service, updated))


def _call_take_desktop_control(api_key, org_id, args: dict) -> list[TextContent]:
    """Abort busy computer-use sessions so a human can drive the desktop."""
    import asyncio
    import uuid as _uuid

    from apps.accounts.models import APIKeyPermission

    if not api_key.has_permission(APIKeyPermission.TERMINAL_ACCESS):
        return _error(
            f"Permission denied: {APIKeyPermission.TERMINAL_ACCESS.value} required"
        )

    workspace_id_str = args.get("workspace_id")
    if not workspace_id_str:
        return _error("workspace_id is required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
    if error is not None:
        return error

    service = _get_harness_service()

    async def _abort():
        return await service.abort_busy_computeruse_for_workspace(workspace.id)

    loop = asyncio.new_event_loop()
    aborted = loop.run_until_complete(_abort())
    loop.close()
    return _text({"aborted_session_ids": [str(session.id) for session in aborted]})


def _call_list_harness_parts(api_key, org_id, args: dict) -> list[TextContent]:
    import uuid as _uuid

    session_id_str = args.get("session_id")
    if not session_id_str:
        return _error("session_id is required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = _get_harness_service()
    messages = service.list_messages(session.id)
    parts = service.list_parts(session.id)
    parts_by_message: dict[str, list] = {}
    for part in parts:
        parts_by_message.setdefault(str(part.message_id), []).append(
            {
                "id": str(part.id),
                "message_id": str(part.message_id),
                "type": part.type,
                "state": part.state,
                "call_id": part.call_id or "",
                "title": part.title or "",
                "output": part.output or "",
                "input": dict(part.input or {}),
                "meta": dict(part.meta or {}),
            }
        )
    return _text(
        {
            "session": _session_dict_with_unread(service, session),
            "messages": [
                {
                    "id": str(message.id),
                    "role": message.role,
                    "content": message.content or "",
                    "model": message.model or "",
                    "reasoning_effort": message.reasoning_effort or "",
                    "cost": float(message.cost or 0.0),
                    "tokens": dict(message.tokens or {}),
                    "finish": message.finish or "",
                    "error": message.error or "",
                    "created_at": message.created_at.isoformat(),
                    "completed_at": (
                        message.completed_at.isoformat()
                        if message.completed_at
                        else None
                    ),
                    "parts": parts_by_message.get(str(message.id), []),
                }
                for message in messages
            ],
            "permissions": service.list_pending_permissions(
                session.id, include_descendants=True
            ),
            "questions": service.list_pending_questions(
                session.id, include_descendants=True
            ),
        }
    )


def _call_list_harness_todos(api_key, org_id, args: dict) -> list[TextContent]:
    import uuid as _uuid

    session_id_str = args.get("session_id")
    if not session_id_str:
        return _error("session_id is required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = _get_harness_service()
    return _text(service.list_todos(session.id))


def _call_resolve_harness_permission(api_key, org_id, args: dict) -> list[TextContent]:
    import asyncio
    import uuid as _uuid

    from common.exceptions import NotFoundError

    session_id_str = args.get("session_id")
    request_id_str = args.get("request_id")
    response = (args.get("response") or "").strip()
    if not session_id_str or not request_id_str or not response:
        return _error("session_id, request_id and response are required")
    try:
        session_id = _uuid.UUID(session_id_str)
        request_id = _uuid.UUID(request_id_str)
    except ValueError:
        return _error("Invalid session_id or request_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = _get_harness_service()

    async def _resolve():
        return await service.resolve_permission(
            session=session,
            request_id=request_id,
            response=response,
        )

    try:
        loop = asyncio.new_event_loop()
        outcome = loop.run_until_complete(_resolve())
        loop.close()
        return _text(
            {
                "request_id": str(request_id),
                "decision": outcome["decision"],
                "remember": outcome["remember"],
            }
        )
    except (NotFoundError, ValueError, LookupError) as exc:
        return _error(str(exc))


def _call_patch_harness_session(api_key, org_id, args: dict) -> list[TextContent]:
    import uuid as _uuid

    session_id_str = args.get("session_id")
    title = (args.get("title") or "").strip()
    if not session_id_str or not title:
        return _error("session_id and title are required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = _get_harness_service()
    try:
        updated = service.update_title(session.id, title)
    except ValueError as exc:
        return _error(str(exc))
    return _text(_session_dict_with_unread(service, updated))


def _call_set_harness_session_mode(api_key, org_id, args: dict) -> list[TextContent]:
    import uuid as _uuid

    session_id_str = args.get("session_id")
    mode = (args.get("mode") or "").strip()
    if not session_id_str or not mode:
        return _error("session_id and mode are required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = _get_harness_service()
    if service.is_running(session.id):
        return _error(f"Harness session '{session.id}' already has an active run")

    try:
        updated = service.set_mode(session.id, mode)
    except ValueError as exc:
        return _error(str(exc))
    return _text(_session_dict_with_unread(service, updated))


def _call_delete_harness_session(api_key, org_id, args: dict) -> list[TextContent]:
    import asyncio
    import uuid as _uuid

    session_id_str = args.get("session_id")
    if not session_id_str:
        return _error("session_id is required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = _get_harness_service()

    async def _delete():
        await service.delete_session(session.id)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_delete())
    loop.close()
    return _text({"deleted": True, "session_id": str(session.id)})


def _call_list_harness_conversations(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.sio_server import get_runner_service

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    runner_service = get_runner_service()
    workspaces = runner_service.list_workspaces(
        organization_id=org_id,
        user=api_key.user,
    )
    service = _get_harness_service()
    return _text(
        service.list_conversations(
            organization_id=org_id,
            workspace_ids=[workspace.id for workspace in workspaces],
        )
    )


def _call_mark_harness_session_read(api_key, org_id, args: dict) -> list[TextContent]:
    import uuid as _uuid

    session_id_str = args.get("session_id")
    if not session_id_str:
        return _error("session_id is required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = _get_harness_service()
    service.mark_session_read(session.id)
    return _text({"session_id": str(session.id), "read": True})


def _call_mark_harness_session_unread(api_key, org_id, args: dict) -> list[TextContent]:
    import uuid as _uuid

    session_id_str = args.get("session_id")
    if not session_id_str:
        return _error("session_id is required")
    try:
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid session_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    service = _get_harness_service()
    service.mark_session_unread(session.id)
    return _text({"session_id": str(session.id), "unread": True})


# ---------------------------------------------------------------------------
# Git tools (productive git integration, whitelisted RPC parity)
# ---------------------------------------------------------------------------

#: Whitelisted git operations accepted by the ``git_operation`` MCP tool.
#: NOTE: ``_GIT_OPERATIONS`` is the full whitelist (reads + mutations) used
#: for argument validation and the per-call permission guard. The generic
#: ``git_operation`` *tool schema* intentionally exposes only
#: ``_GIT_MUTATION_OPERATIONS`` — dedicated ``get_git_*`` tools cover reads.
_GIT_OPERATIONS: tuple[str, ...] = (
    "list_repos",
    "repo_snapshot",
    "repo_history",
    "working_diff",
    "commit_details",
    "stage",
    "unstage",
    "discard",
    "commit",
    "fetch",
    "pull",
    "push",
    "sync",
    "checkout_branch",
    "checkout_commit",
    "checkout_remote_branch",
    "create_branch",
    "rename_branch",
    "delete_branch",
    "merge_into_current",
    "merge_current_into",
    "merge_abort",
)

#: Read-only git operations — callable via ``git_operation`` with only
#: ``workspaces:git_read`` (mutations always need ``workspaces:git_write``).
#: Kept as defense-in-depth even though the tool schema no longer advertises
#: reads (dedicated ``get_git_*`` tools cover them).
_GIT_READ_OPERATIONS = frozenset(
    {"list_repos", "repo_snapshot", "repo_history", "working_diff", "commit_details"}
)

#: Mutation-only operations advertised by the generic ``git_operation`` tool
#: schema. Dedicated ``get_git_state``/``get_git_diff``/``get_git_commit``
#: cover reads, so a write-only key never sees a read op as an option.
_GIT_MUTATION_OPERATIONS: tuple[str, ...] = tuple(
    op for op in _GIT_OPERATIONS if op not in _GIT_READ_OPERATIONS
)

_GIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

#: Allowed arg keys per git operation (excludes envelope keys
#: ``workspace_id``/``operation``/``repo_path`` handled separately).
#: Mirrors ``RunnerService._GIT_ALLOWED_ARGS``; the service re-validates,
#: so MCP and REST can never bypass the whitelist.
_GIT_MCP_ALLOWED_ARGS: dict[str, frozenset] = {
    "list_repos": frozenset(),
    "repo_snapshot": frozenset(),
    "repo_history": frozenset({"history_limit", "history_skip", "branch"}),
    "working_diff": frozenset(),
    "commit_details": frozenset({"commit"}),
    "stage": frozenset({"paths"}),
    "unstage": frozenset({"paths"}),
    "discard": frozenset({"paths"}),
    "commit": frozenset({"message"}),
    "fetch": frozenset({"remote"}),
    "pull": frozenset({"remote", "branch"}),
    "push": frozenset({"remote", "set_upstream"}),
    "sync": frozenset({"remote"}),
    "checkout_branch": frozenset({"branch"}),
    "checkout_commit": frozenset({"commit"}),
    "checkout_remote_branch": frozenset({"remote_ref", "local_name"}),
    "create_branch": frozenset({"branch", "start_point", "checkout"}),
    "rename_branch": frozenset({"new_branch", "old_branch"}),
    "delete_branch": frozenset({"branch"}),
    "merge_into_current": frozenset({"branch", "message"}),
    "merge_current_into": frozenset({"target", "message"}),
    "merge_abort": frozenset(),
}

#: Envelope keys accepted by ``git_operation`` (everything else rejected).
_GIT_MCP_ENVELOPE_KEYS = frozenset({"workspace_id", "operation", "repo_path"})

#: Rejected caller-controlled keys (defence in depth with explicit errors).
_GIT_MCP_FORBIDDEN_KEYS = frozenset(
    {"env", "author_name", "author_email", "args", "argv"}
)


def _parse_git_workspace(args: dict) -> tuple:
    """Parse workspace_id from MCP args or return an error."""
    raw = args.get("workspace_id")
    if not raw:
        return None, _error("workspace_id is required")
    try:
        return uuid.UUID(str(raw)), None
    except ValueError:
        return None, _error("Invalid workspace_id UUID")


def _validate_git_repo_path(repo_path: str | None) -> tuple[str | None, object]:
    """Basic backend check for absolute repo paths (runner re-validates)."""
    if repo_path is None:
        return None, None
    cleaned = str(repo_path).strip()
    if not cleaned:
        return None, _error("repo_path must be a non-empty string")
    if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
        return None, _error("Invalid repo_path")
    normalized = os.path.normpath(cleaned)
    if normalized != "/workspace" and not normalized.startswith("/workspace/"):
        return None, _error("repo_path must be under /workspace")
    if len(cleaned) > 512:
        return None, _error("repo_path too long (max 512 chars)")
    return cleaned, None


def _check_git_tool_permission(api_key, operation: str) -> object | None:
    """Enforce per-operation permissions for the generic git tool."""
    from apps.accounts.models import APIKeyPermission

    if operation in _GIT_READ_OPERATIONS:
        if not api_key.has_permission(APIKeyPermission.WORKSPACES_GIT_READ):
            return _error(
                "Permission denied: "
                f"{APIKeyPermission.WORKSPACES_GIT_READ.value} required"
            )
        return None
    if not api_key.has_permission(APIKeyPermission.WORKSPACES_GIT_WRITE):
        return _error(
            f"Permission denied: {APIKeyPermission.WORKSPACES_GIT_WRITE.value} required"
        )
    return None


def _git_mcp_args(operation: str, args: dict) -> tuple[dict | None, object]:
    """Build the service args dict field-by-field from MCP args.

    Only explicitly known fields per operation are forwarded — there is
    no ``args`` dict passthrough and no ``env`` field. Unknown keys
    (``env``, ``author_*``, ``args``/``argv``, aliases, typos) are
    rejected, never silently dropped. Returns ``(args, None)`` or
    ``(None, error)``. The webapp/API sends plain ``message`` only.
    """
    if operation not in _GIT_OPERATIONS:
        return None, _error(
            f"Unknown git operation: {operation!r}. "
            f"Must be one of: {', '.join(_GIT_OPERATIONS)}"
        )
    for forbidden in sorted(_GIT_MCP_FORBIDDEN_KEYS):
        if forbidden in args:
            if forbidden == "env":
                return None, _error("env is not accepted for git operations")
            if forbidden in {"author_name", "author_email"}:
                return None, _error(
                    "author_name/author_email are set server-side "
                    "from the API key account"
                )
            return None, _error(
                f"{forbidden} is not accepted for git operations"
            )
    allowed = _GIT_MCP_ALLOWED_ARGS.get(operation, frozenset())
    for key in args:
        if key in _GIT_MCP_ENVELOPE_KEYS:
            continue
        if key not in allowed:
            return None, _error(
                f"Unknown argument {key!r} for git operation {operation!r}"
            )
    out: dict = {}
    if operation in {"list_repos", "repo_snapshot"}:
        return {}, None
    if operation == "repo_history":
        if args.get("history_limit") is not None:
            try:
                limit = int(args["history_limit"])
            except (TypeError, ValueError):
                return None, _error("history_limit must be an integer (1-500)")
            if limit < 1 or limit > 500:
                return None, _error("history_limit must be an integer (1-500)")
            out["history_limit"] = limit
        if args.get("history_skip") is not None:
            try:
                skip = int(args["history_skip"])
            except (TypeError, ValueError):
                return None, _error("history_skip must be an integer (>= 0)")
            if skip < 0:
                return None, _error("history_skip must be >= 0")
            out["history_skip"] = skip
        if args.get("branch") not in (None, ""):
            branch = str(args["branch"]).strip()
            if not branch or len(branch) > 255:
                return None, _error("Invalid branch")
            out["branch"] = branch
        return out, None
    if operation in {"stage", "unstage", "discard"}:
        raw = args.get("paths")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            return None, _error(f"paths is required for operation {operation!r}")
        if len(raw) > 256:
            return None, _error("Too many paths (max 256)")
        for item in raw:
            text = str(item)
            if not text.strip() or len(text) > 256:
                return None, _error("Invalid path entry (must be 1-256 chars)")
            if "\x00" in text or "\n" in text or "\r" in text:
                return None, _error(f"Invalid path: {item!r}")
        out["paths"] = [str(item) for item in raw]
        return out, None
    if operation == "commit":
        message = str(args.get("message", "") or "")
        if not message.strip():
            return None, _error("message is required for commit")
        if len(message) > 10000:
            return None, _error("message too long (max 10000 chars)")
        out["message"] = message
        return out, None
    if operation == "commit_details":
        commit = str(args.get("commit", "") or "").strip()
        if not commit:
            return None, _error("commit is required for commit_details")
        if not _GIT_HASH_RE.match(commit.lower()):
            return None, _error("Invalid commit hash (must be 4-64 hex chars)")
        out["commit"] = commit
        return out, None
    if operation in {"fetch", "pull", "push", "sync"}:
        if args.get("remote") not in (None, ""):
            remote = str(args["remote"]).strip()
            if not remote or len(remote) > 255:
                return None, _error("Invalid remote")
            out["remote"] = remote
        if operation == "pull" and args.get("branch") not in (None, ""):
            if args.get("remote") in (None, ""):
                return None, _error("remote is required when branch is set for pull")
            branch = str(args["branch"]).strip()
            if not branch or len(branch) > 255:
                return None, _error("Invalid branch")
            out["branch"] = branch
        if operation == "push" and args.get("set_upstream") is not None:
            out["set_upstream"] = bool(args["set_upstream"])
        return out, None
    if operation == "checkout_branch":
        branch = str(args.get("branch", "") or "").strip()
        if not branch or len(branch) > 255:
            return None, _error("branch is required for checkout_branch")
        out["branch"] = branch
        return out, None
    if operation == "checkout_commit":
        commit = str(args.get("commit", "") or "").strip()
        if not commit:
            return None, _error("commit is required for checkout_commit")
        if not _GIT_HASH_RE.match(commit.lower()):
            return None, _error("Invalid commit hash (must be 4-64 hex chars)")
        out["commit"] = commit
        return out, None
    if operation == "checkout_remote_branch":
        remote_ref = str(args.get("remote_ref", "") or "").strip()
        if not remote_ref:
            return None, _error("remote_ref is required for checkout_remote_branch")
        if len(remote_ref) > 255:
            return None, _error("Invalid remote_ref")
        remote, sep, branch_part = remote_ref.partition("/")
        if not sep or not remote or not branch_part:
            return None, _error("Invalid remote_ref")
        out["remote_ref"] = remote_ref
        if args.get("local_name") not in (None, ""):
            local = str(args["local_name"]).strip()
            if not local or len(local) > 255:
                return None, _error("Invalid local_name")
            out["local_name"] = local
        return out, None
    if operation == "create_branch":
        branch = str(args.get("branch", "") or "").strip()
        if not branch or len(branch) > 255:
            return None, _error("branch is required for create_branch")
        out["branch"] = branch
        if args.get("start_point") not in (None, ""):
            start = str(args["start_point"]).strip()
            if not start or len(start) > 255:
                return None, _error("Invalid start_point")
            out["start_point"] = start
        if args.get("checkout") is not None:
            out["checkout"] = bool(args["checkout"])
        return out, None
    if operation == "rename_branch":
        new = str(args.get("new_branch", "") or "").strip()
        if not new or len(new) > 255:
            return None, _error("new_branch is required for rename_branch")
        out["new_branch"] = new
        old = str(args.get("old_branch", "") or "").strip()
        if old:
            if len(old) > 255:
                return None, _error("Invalid old_branch")
            out["old_branch"] = old
        return out, None
    if operation == "delete_branch":
        branch = str(args.get("branch", "") or "").strip()
        if not branch or len(branch) > 255:
            return None, _error("branch is required for delete_branch")
        out["branch"] = branch
        return out, None
    if operation == "merge_into_current":
        branch = str(args.get("branch", "") or "").strip()
        if not branch or len(branch) > 255:
            return None, _error("branch is required for merge_into_current")
        out["branch"] = branch
        if args.get("message") not in (None, ""):
            message = str(args["message"])
            if len(message) > 4096:
                return None, _error("message too long (max 4096 chars)")
            out["message"] = message
        return out, None
    if operation == "merge_current_into":
        target = str(args.get("target", "") or "").strip()
        if not target or len(target) > 255:
            return None, _error("target is required for merge_current_into")
        out["target"] = target
        if args.get("message") not in (None, ""):
            message = str(args["message"])
            if len(message) > 4096:
                return None, _error("message too long (max 4096 chars)")
            out["message"] = message
        return out, None
    if operation == "merge_abort":
        return {}, None
    if operation == "working_diff":
        return {}, None
    return None, _error(f"Unknown git operation: {operation!r}")


async def _run_git_operation_mcp(
    api_key, org_id, workspace_id, operation: str, repo_path, svc_args: dict
):
    """Validate ownership then dispatch one git operation via RunnerService.

    Commit identity always comes from ``api_key.user``; no caller sends
    ``author_*`` (rejected in :func:`_git_mcp_args` and the service).
    """
    from asgiref.sync import sync_to_async

    from apps.runners.exceptions import RunnerTimeoutError
    from common.exceptions import ConflictError, NotFoundError

    workspace, owned_error = await sync_to_async(_get_owned_workspace_or_error)(
        api_key, org_id, workspace_id
    )
    if owned_error is not None:
        return owned_error

    svc = _runner_service()
    try:
        result = await svc.run_git_operation(
            workspace.id,
            operation,
            repo_path=repo_path,
            args=svc_args,
            user=api_key.user,
        )
    except (NotFoundError, RunnerTimeoutError, ConflictError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc))
    return _text(result)


async def _call_get_git_state(api_key, org_id, args: dict) -> list[TextContent]:
    """List light git repo summaries under /workspace (lazy loading)."""
    workspace_id, error = _parse_git_workspace(args)
    if error is not None:
        return error
    svc_args, args_error = _git_mcp_args(
        "list_repos",
        {"workspace_id": args.get("workspace_id"), **args},
    )
    if args_error is not None:
        return args_error
    assert svc_args is not None
    return await _run_git_operation_mcp(
        api_key, org_id, workspace_id, "list_repos", None, svc_args
    )


async def _call_get_git_diff(api_key, org_id, args: dict) -> list[TextContent]:
    """Return staged/unstaged diff entries for one repo."""
    workspace_id, error = _parse_git_workspace(args)
    if error is not None:
        return error
    if not args.get("repo_path"):
        return _error("repo_path is required")
    repo_path, repo_error = _validate_git_repo_path(args.get("repo_path"))
    if repo_error is not None:
        return repo_error
    assert repo_path is not None
    return await _run_git_operation_mcp(
        api_key, org_id, workspace_id, "working_diff", repo_path, {}
    )


async def _call_get_git_commit(api_key, org_id, args: dict) -> list[TextContent]:
    """Return full details for one commit."""
    workspace_id, error = _parse_git_workspace(args)
    if error is not None:
        return error
    if not args.get("repo_path"):
        return _error("repo_path is required")
    repo_path, repo_error = _validate_git_repo_path(args.get("repo_path"))
    if repo_error is not None:
        return repo_error
    commit = str(args.get("commit", "") or "").strip()
    if not commit:
        return _error("commit is required")
    if not _GIT_HASH_RE.match(commit.lower()):
        return _error("Invalid commit hash (must be 4-64 hex chars)")
    assert repo_path is not None
    return await _run_git_operation_mcp(
        api_key, org_id, workspace_id, "commit_details", repo_path, {"commit": commit}
    )


async def _call_git_operation(api_key, org_id, args: dict) -> list[TextContent]:
    """Run one whitelisted git operation with per-operation permissions."""
    workspace_id, error = _parse_git_workspace(args)
    if error is not None:
        return error
    operation = str(args.get("operation", "") or "").strip()
    if not operation:
        return _error("operation is required")
    if operation not in _GIT_OPERATIONS:
        return _error(
            f"Unknown git operation: {operation!r}. "
            f"Must be one of: {', '.join(_GIT_OPERATIONS)}"
        )
    if operation in _GIT_READ_OPERATIONS:
        return _error(
            f"Unknown git operation: {operation!r}. "
            "Reads use get_git_state/get_git_diff/get_git_commit instead."
        )
    perm_error = _check_git_tool_permission(api_key, operation)
    if perm_error is not None:
        return perm_error
    repo_path = None
    if operation != "list_repos":
        if not args.get("repo_path"):
            return _error(f"repo_path is required for operation {operation!r}")
        repo_path, repo_error = _validate_git_repo_path(args.get("repo_path"))
        if repo_error is not None:
            return repo_error
    elif args.get("repo_path") not in (None, ""):
        repo_path, repo_error = _validate_git_repo_path(args.get("repo_path"))
        if repo_error is not None:
            return repo_error
    svc_args, args_error = _git_mcp_args(operation, args)
    if args_error is not None:
        return args_error
    assert svc_args is not None
    return await _run_git_operation_mcp(
        api_key, org_id, workspace_id, operation, repo_path, svc_args
    )


def _call_resolve_harness_question(api_key, org_id, args: dict) -> list[TextContent]:
    import asyncio
    import uuid as _uuid

    from common.exceptions import NotFoundError

    session_id_str = args.get("session_id")
    question_id_str = args.get("question_id")
    if not session_id_str or not question_id_str:
        return _error("session_id and question_id are required")
    try:
        session_id = _uuid.UUID(session_id_str)
        question_id = _uuid.UUID(question_id_str)
    except ValueError:
        return _error("Invalid session_id or question_id UUID")

    session, error = _owned_harness_session_or_error(api_key, org_id, session_id)
    if error is not None:
        return error

    answers = args.get("answers") or []
    reject = bool(args.get("reject", False))
    service = _get_harness_service()

    async def _resolve():
        return await service.resolve_question(
            session=session,
            question_id=question_id,
            answers=list(answers),
            reject=reject,
        )

    try:
        loop = asyncio.new_event_loop()
        outcome = loop.run_until_complete(_resolve())
        loop.close()
        return _text(outcome)
    except (NotFoundError, ValueError, LookupError) as exc:
        return _error(str(exc))


def _process_payload(process) -> dict:
    """Serialize a WorkspaceProcess ORM row for MCP responses."""
    started_at = getattr(process, "started_at", None)
    ended_at = getattr(process, "ended_at", None)
    updated_at = getattr(process, "updated_at", None)
    return {
        "id": str(getattr(process, "id", "")),
        "workspace_id": str(getattr(process, "workspace_id", "")),
        "name": getattr(process, "name", "") or "",
        "command": getattr(process, "command", "") or "",
        "workdir": getattr(process, "workdir", "") or "",
        "pid": getattr(process, "pid", None),
        "log_path": getattr(process, "log_path", "") or "",
        "status": str(getattr(process, "status", "") or ""),
        "exit_code": getattr(process, "exit_code", None),
        "run_count": getattr(process, "run_count", None),
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def _parse_process_args(args: dict, *required: str) -> tuple:
    """Parse workspace/process UUIDs from MCP args or return an error."""
    import uuid as _uuid

    values: dict[str, _uuid.UUID] = {}
    for key in required:
        raw = args.get(key)
        if not raw:
            return None, _error(f"{key} is required")
        try:
            values[key] = _uuid.UUID(str(raw))
        except ValueError:
            return None, _error(f"Invalid {key} UUID")
    return values, None


async def _call_list_processes(api_key, org_id, args: dict) -> list[TextContent]:
    """List background processes of an owned workspace (no nested loop)."""
    from asgiref.sync import sync_to_async

    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error

    workspace, owned_error = await sync_to_async(_get_owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    svc = _runner_service()
    try:
        processes = await svc.list_processes(workspace.id)
    except Exception as exc:
        return _error(str(exc))
    return _text([_process_payload(process) for process in processes])


async def _call_get_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Return one background process scoped to an owned workspace."""
    from asgiref.sync import sync_to_async

    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error
    process_id = args.get("process_id")
    if not process_id:
        return _error("process_id is required")

    workspace, owned_error = await sync_to_async(_get_owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    from common.exceptions import NotFoundError

    svc = _runner_service()
    try:
        process = await svc.get_process(workspace.id, str(process_id))
    except NotFoundError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc))
    return _text(_process_payload(process))


async def _call_start_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Start a detached background process in an owned workspace."""
    from asgiref.sync import sync_to_async

    command = (args.get("command") or "").strip()
    name = (args.get("name") or "").strip()
    if not args.get("workspace_id"):
        return _error("workspace_id is required")
    if not command:
        return _error("command is required")
    if not name:
        return _error("name is required")
    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error

    workspace, owned_error = await sync_to_async(_get_owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    from common.exceptions import ConflictError, NotFoundError

    svc = _runner_service()
    try:
        process = await svc.start_process(
            workspace.id,
            command,
            workdir=str(args.get("workdir") or "/workspace"),
            env=dict(args.get("env") or {}),
            name=name,
            user=api_key.user,
        )
    except (NotFoundError, ConflictError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc))
    return _text(_process_payload(process))


async def _call_stop_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Stop a background process in an owned workspace."""
    from asgiref.sync import sync_to_async

    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error
    process_id = args.get("process_id")
    if not process_id:
        return _error("process_id is required")

    workspace, owned_error = await sync_to_async(_get_owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    from common.exceptions import ConflictError, NotFoundError

    svc = _runner_service()
    try:
        process = await svc.stop_process(
            workspace.id,
            str(process_id),
        )
    except (NotFoundError, ConflictError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc))
    return _text(_process_payload(process))


async def _call_restart_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Restart a background process on the same row (new log)."""
    from asgiref.sync import sync_to_async

    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error
    process_id = args.get("process_id")
    if not process_id:
        return _error("process_id is required")

    workspace, owned_error = await sync_to_async(_get_owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    from common.exceptions import ConflictError, NotFoundError

    svc = _runner_service()
    try:
        process = await svc.restart_process(
            workspace.id,
            str(process_id),
            user=api_key.user,
        )
    except (NotFoundError, ConflictError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc))
    return _text(_process_payload(process))


async def _call_delete_process(api_key, org_id, args: dict) -> list[TextContent]:
    """Delete a background process (stops first if running)."""
    from asgiref.sync import sync_to_async

    parsed, error = _parse_process_args(args, "workspace_id")
    if error is not None:
        return error
    process_id = args.get("process_id")
    if not process_id:
        return _error("process_id is required")

    workspace, owned_error = await sync_to_async(_get_owned_workspace_or_error)(
        api_key, org_id, parsed["workspace_id"]
    )
    if owned_error is not None:
        return owned_error

    from common.exceptions import ConflictError, NotFoundError

    svc = _runner_service()
    try:
        deleted_id = await svc.delete_process(
            workspace.id,
            str(process_id),
        )
    except (NotFoundError, ConflictError, ValueError) as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(str(exc))
    return _text({"deleted": True, "process_id": str(deleted_id)})


def _call_list_org_credential_services(
    api_key, org_id, args: dict
) -> list[TextContent]:
    from apps.credentials.models import (
        CredentialService,
        OrgCredentialServiceActivation,
    )

    try:
        _require_org_admin(api_key.user, org_id)
    except PermissionError as exc:
        return _error(str(exc))

    activated_ids = set(
        OrgCredentialServiceActivation.objects.filter(
            organization_id=org_id
        ).values_list("credential_service_id", flat=True)
    )
    services = CredentialService.objects.all().order_by("name")
    return _text(
        [
            {
                "id": str(service.id),
                "name": service.name,
                "slug": service.slug,
                "description": service.description,
                "credential_type": str(service.credential_type),
                "env_var_name": service.env_var_name,
                "label": service.label,
                "is_active": service.id in activated_ids,
            }
            for service in services
        ]
    )


def _call_toggle_org_credential_service_activation(
    api_key, org_id, args: dict
) -> list[TextContent]:
    from apps.credentials.models import (
        CredentialService,
        OrgCredentialServiceActivation,
    )

    try:
        _require_org_admin(api_key.user, org_id)
        if not api_key.user.is_staff:
            return _error("Only staff users can modify credential service activation")
        service_id = _parse_uuid(args.get("service_id"), "service_id")
        if "active" not in args:
            raise ValueError("active is required")
        active = bool(args.get("active"))
        service = CredentialService.objects.filter(id=service_id).first()
        if service is None:
            return _error("Credential service not found")

        if active:
            OrgCredentialServiceActivation.objects.get_or_create(
                organization_id=org_id,
                credential_service=service,
            )
        else:
            OrgCredentialServiceActivation.objects.filter(
                organization_id=org_id,
                credential_service=service,
            ).delete()

        return _text(
            {
                "id": str(service.id),
                "name": service.name,
                "slug": service.slug,
                "description": service.description,
                "credential_type": str(service.credential_type),
                "env_var_name": service.env_var_name,
                "label": service.label,
                "is_active": active,
            }
        )
    except PermissionError as exc:
        return _error(str(exc))
    except ValueError as exc:
        return _error(str(exc))


# ---------------------------------------------------------------------------
# Tool dispatch table
# ---------------------------------------------------------------------------

_TOOL_HANDLERS = {
    "list_workspaces": _call_list_workspaces,
    "get_workspace": _call_get_workspace,
    "create_workspace": _call_create_workspace,
    "update_workspace": _call_update_workspace,
    "stop_workspace": _call_stop_workspace,
    "resume_workspace": _call_resume_workspace,
    "remove_workspace": _call_remove_workspace,
    "list_runners": _call_list_runners,
    "list_image_artifacts": _call_list_image_artifacts,
    "create_image_artifact": _call_create_image_artifact,
    "list_image_definitions": _call_list_image_definitions,
    "create_image_definition": _call_create_image_definition,
    "duplicate_image_definition": _call_duplicate_image_definition,
    "update_image_definition": _call_update_image_definition,
    "delete_image_definition": _call_delete_image_definition,
    "list_build_jobs": _call_list_build_jobs,
    "create_build_job": _call_create_build_job,
    "update_build_job": _call_update_build_job,
    "delete_build_job": _call_delete_build_job,
    "get_build_job_log": _call_get_build_job_log,
    "list_credentials": _call_list_credentials,
    "get_provider_config": _call_get_provider_config,
    "list_provider_models": _call_list_provider_models,
    "save_provider_config": _call_save_provider_config,
    "delete_provider_config": _call_delete_provider_config,
    "list_provider_connections": _call_list_provider_connections,
    "save_provider_connection": _call_save_provider_connection,
    "delete_provider_connection": _call_delete_provider_connection,
    "get_agent_s_config": _call_get_agent_s_config,
    "save_agent_s_config": _call_save_agent_s_config,
    "chatgpt_oauth_start": _call_chatgpt_oauth_start,
    "chatgpt_oauth_status": _call_chatgpt_oauth_status,
    "chatgpt_oauth_cancel": _call_chatgpt_oauth_cancel,
    "list_harness_sessions": _call_list_harness_sessions,
    "create_harness_session": _call_create_harness_session,
    "send_harness_message": _call_send_harness_message,
    "fork_harness_session": _call_fork_harness_session,
    "edit_harness_message": _call_edit_harness_message,
    "abort_harness_session": _call_abort_harness_session,
    "take_desktop_control": _call_take_desktop_control,
    "list_harness_parts": _call_list_harness_parts,
    "list_harness_todos": _call_list_harness_todos,
    "resolve_harness_permission": _call_resolve_harness_permission,
    "patch_harness_session": _call_patch_harness_session,
    "set_harness_session_mode": _call_set_harness_session_mode,
    "delete_harness_session": _call_delete_harness_session,
    "list_harness_conversations": _call_list_harness_conversations,
    "mark_harness_session_read": _call_mark_harness_session_read,
    "mark_harness_session_unread": _call_mark_harness_session_unread,
    "resolve_harness_question": _call_resolve_harness_question,
    "list_org_credential_services": _call_list_org_credential_services,
    "toggle_org_credential_service_activation": _call_toggle_org_credential_service_activation,
    "list_processes": _call_list_processes,
    "get_process": _call_get_process,
    "start_process": _call_start_process,
    "stop_process": _call_stop_process,
    "restart_process": _call_restart_process,
    "delete_process": _call_delete_process,
    "get_git_state": _call_get_git_state,
    "get_git_diff": _call_get_git_diff,
    "get_git_commit": _call_get_git_commit,
    "git_operation": _call_git_operation,
}


# ---------------------------------------------------------------------------
# Build the MCP Server factory (called once per SSE connection)
# ---------------------------------------------------------------------------


def create_mcp_server(api_key) -> Server:
    """
    Create a per-connection MCP Server instance bound to the given API key.

    Only tools for which the API key holds the required permission are exposed.
    """
    server = Server("opencuria")

    # Filter tools to those permitted by this API key
    allowed_tools = [
        tool for tool in _TOOLS if api_key.has_permission(_TOOL_PERMISSIONS[tool.name])
    ]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return allowed_tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        args = arguments or {}
        from asgiref.sync import sync_to_async

        # Permission check (double-check at call time)
        required_perm = _TOOL_PERMISSIONS.get(name)
        if required_perm and not api_key.has_permission(required_perm):
            return _error(f"Permission denied: {required_perm.value} required")

        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            return _error(f"Unknown tool: {name}")

        # Determine org_id from user's first org membership
        from apps.organizations.services import OrganizationService

        org_service = OrganizationService()
        orgs = await sync_to_async(org_service.list_user_organizations)(api_key.user)
        if not orgs:
            return _error("User is not a member of any organization")
        import uuid as _uuid

        org_id = _uuid.UUID(str(orgs[0]["id"]))

        try:
            import inspect

            if inspect.iscoroutinefunction(handler):
                result = await handler(api_key, org_id, args)
            else:
                result = await sync_to_async(handler)(api_key, org_id, args)
            return result
        except Exception as exc:
            logger.exception("MCP tool %s failed", name)
            return _error(str(exc))

    return server


# ---------------------------------------------------------------------------
# MCP transport helpers
# ---------------------------------------------------------------------------


class _BufferedReceive:
    """Replay a buffered HTTP request body for downstream ASGI handlers."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._sent = False

    async def __call__(self) -> dict[str, object]:
        if not self._sent:
            self._sent = True
            return {
                "type": "http.request",
                "body": self._body,
                "more_body": False,
            }
        return {"type": "http.disconnect"}


def _make_buffered_receive(body: bytes):
    return _BufferedReceive(body)


def _load_json_body(body: bytes):
    """Decode a buffered JSON body once so session routing can inspect it."""
    try:
        return json.loads(body), None
    except json.JSONDecodeError as exc:
        return None, exc


class _StreamableHTTPSession:
    """Owns one MCP streamable HTTP session and its background server task."""

    def __init__(self, session_id: str, api_key) -> None:
        self.session_id = session_id
        self.api_key = api_key
        self.transport = StreamableHTTPServerTransport(
            session_id,
            is_json_response_enabled=True,
        )
        self.server = create_mcp_server(api_key)
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(),
            name=f"opencuria-mcp-{self.session_id}",
        )
        await self._ready.wait()

    async def _run(self) -> None:
        try:
            async with self.transport.connect() as streams:
                self._ready.set()
                await self.server.run(
                    streams[0],
                    streams[1],
                    self.server.create_initialization_options(),
                )
        finally:
            self._ready.set()

    def add_done_callback(self, callback) -> None:
        if self._task is not None:
            self._task.add_done_callback(callback)

    def is_finished(self) -> bool:
        return self._task is not None and self._task.done()


class _StreamableHTTPSessionManager:
    """Tracks active MCP streamable HTTP sessions for authenticated clients."""

    def __init__(self) -> None:
        self._sessions: dict[str, _StreamableHTTPSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, api_key) -> _StreamableHTTPSession:
        async with self._lock:
            session_id = uuid.uuid4().hex
            session = _StreamableHTTPSession(session_id=session_id, api_key=api_key)
            await session.start()
            self._sessions[session_id] = session
            session.add_done_callback(
                lambda _task: asyncio.create_task(
                    self._remove_if_current(session_id, session)
                )
            )
            return session

    async def get_session(
        self, session_id: str | None
    ) -> _StreamableHTTPSession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.is_finished():
            await self._remove_if_current(session_id, session)
            return None
        return session

    async def _remove_if_current(
        self,
        session_id: str,
        session: _StreamableHTTPSession,
    ) -> None:
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is session:
                self._sessions.pop(session_id, None)


async def _authenticate_request(request: Request):
    token = request.headers.get("authorization", "").removeprefix(
        "Bearer "
    ).strip() or request.headers.get("x-api-key", "")

    from asgiref.sync import sync_to_async
    from .auth import authenticate_api_key

    return await sync_to_async(authenticate_api_key)(token)


def build_mcp_app() -> Starlette:
    """Build and return the Starlette ASGI app for MCP transports."""

    sse_transport = SseServerTransport("/mcp/messages/")
    session_manager = _StreamableHTTPSessionManager()

    async def handle_streamable_http(scope, receive, send) -> None:
        """Primary MCP endpoint implementing streamable HTTP on /mcp."""
        request = Request(scope, receive)
        api_key = await _authenticate_request(request)
        if api_key is None:
            response = JSONResponse(
                {
                    "error": (
                        "Invalid or missing API key. Ensure the key has the "
                        "mcp:access permission."
                    )
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        session_id = request.headers.get("mcp-session-id")
        request_receive = request.receive

        if request.method == "POST" and session_id is None:
            body = await request.body()
            request_receive = _make_buffered_receive(body)
            payload, parse_error = _load_json_body(body)

            if parse_error is not None:
                response = JSONResponse(
                    {"error": f"Invalid JSON-RPC body: {parse_error.msg}"},
                    status_code=400,
                )
                await response(scope, receive, send)
                return

            if not (
                isinstance(payload, dict)
                and payload.get("jsonrpc") == "2.0"
                and payload.get("method") == "initialize"
            ):
                response = JSONResponse(
                    {
                        "error": (
                            "Missing MCP session. Send initialize without "
                            "Mcp-Session-Id to start a new session."
                        )
                    },
                    status_code=400,
                )
                await response(scope, receive, send)
                return

            session = await session_manager.create_session(api_key)
        else:
            session = await session_manager.get_session(session_id)
            if session is None:
                if request.method == "GET" and session_id is None:
                    response = Response(status_code=405)
                    await response(scope, receive, send)
                    return

                response = JSONResponse(
                    {"error": "Invalid or expired MCP session."},
                    status_code=404 if session_id else 400,
                )
                await response(scope, receive, send)
                return

        await session.transport.handle_request(
            scope,
            request_receive,
            send,
        )

    async def handle_sse(scope, receive, send) -> None:
        """Legacy SSE endpoint kept for older MCP clients."""
        request = Request(scope, receive)
        api_key = await _authenticate_request(request)
        if api_key is None:
            response = JSONResponse(
                {
                    "error": "Invalid or missing API key. Ensure the key has the mcp:access permission."
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        mcp_server = create_mcp_server(api_key)

        async with sse_transport.connect_sse(scope, request.receive, send) as streams:
            await mcp_server.run(
                streams[0],
                streams[1],
                mcp_server.create_initialization_options(),
            )

    return Starlette(
        routes=[
            Mount("/mcp/sse", app=handle_sse),
            Mount("/mcp/messages/", app=sse_transport.handle_post_message),
            Mount("/mcp", app=handle_streamable_http),
        ],
    )


# Singleton — built lazily on first import from asgi.py
_mcp_app: Starlette | None = None


def get_mcp_app() -> Starlette:
    """Return (and lazily create) the singleton MCP Starlette application."""
    global _mcp_app
    if _mcp_app is None:
        _mcp_app = build_mcp_app()
    return _mcp_app
