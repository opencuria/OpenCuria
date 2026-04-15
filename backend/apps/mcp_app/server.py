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
- stop_workspace         → workspaces:stop
- resume_workspace       → workspaces:resume
- remove_workspace       → workspaces:delete
- run_prompt             → prompts:run
- cancel_prompt          → prompts:cancel
- list_runners           → runners:read
- list_agents            → agents:read
- list_conversations     → conversations:read
- list_image_artifacts   → images:read
- create_image_artifact  → images:create
- list_image_definitions → image_definitions:read
- create_image_definition → image_definitions:write
- update_image_definition → image_definitions:write
- delete_image_definition → image_definitions:write
- list_runner_image_builds → image_definitions:read
- create_runner_image_build → image_definitions:manage_runners
- update_runner_image_build → image_definitions:manage_runners
- delete_runner_image_build → image_definitions:manage_runners
- get_runner_image_build_log → image_definitions:read
- list_credentials       → credentials:read
- list_org_agent_definitions         → org_agent_definitions:read
- create_org_agent_definition        → org_agent_definitions:write
- update_org_agent_definition        → org_agent_definitions:write
- delete_org_agent_definition        → org_agent_definitions:write
- duplicate_org_agent_definition     → org_agent_definitions:write
- toggle_org_agent_definition_activation → org_agent_definitions:write
- list_org_credential_services       → org_credential_services:read
- toggle_org_credential_service_activation → org_credential_services:write
"""

from __future__ import annotations

import json
import logging

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import (
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

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
        description="Get details of a single workspace including sessions.",
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
                "runner_id": {"type": "string", "description": "Runner UUID to host the workspace."},
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
            },
            "required": ["name"],
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
        name="run_prompt",
        description="Run a prompt / send a message to an AI agent in a workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "prompt": {"type": "string", "description": "The prompt / message to send."},
                "agent_model": {
                    "type": "string",
                    "description": "Agent model override (optional).",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Existing chat UUID to continue (optional).",
                },
            },
            "required": ["workspace_id", "prompt"],
        },
    ),
    Tool(
        name="cancel_prompt",
        description="Cancel a running prompt session in a workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace UUID."},
                "session_id": {
                    "type": "string",
                    "description": "Session UUID to cancel.",
                },
            },
            "required": ["workspace_id", "session_id"],
        },
    ),
    Tool(
        name="list_runners",
        description="List runners in the active organization.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_agents",
        description="List available agent definitions.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Filter by workspace UUID (optional).",
                }
            },
        },
    ),
    Tool(
        name="list_conversations",
        description="List conversations (chats) for the current user.",
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
        name="list_runner_image_builds",
        description="List runner image builds for an image definition.",
        inputSchema={
            "type": "object",
            "properties": {"definition_id": {"type": "string"}},
            "required": ["definition_id"],
        },
    ),
    Tool(
        name="create_runner_image_build",
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
        name="update_runner_image_build",
        description="Update runner image build state (deactivate, activate, rebuild) (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "definition_id": {"type": "string"},
                "runner_id": {"type": "string"},
                "action": {"type": "string", "enum": ["deactivate", "activate", "rebuild"]},
            },
            "required": ["definition_id", "runner_id", "action"],
        },
    ),
    Tool(
        name="delete_runner_image_build",
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
        name="get_runner_image_build_log",
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
        name="list_org_agent_definitions",
        description="List standard and organization-specific agent definitions with activation status (admin).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="create_org_agent_definition",
        description="Create an organization-owned agent definition (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "available_options": {"type": "array", "items": {"type": "object"}},
                "default_env": {"type": "object"},
                "supports_multi_chat": {"type": "boolean"},
                "required_credential_service_ids": {"type": "array", "items": {"type": "string"}},
                "commands": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["name", "commands"],
        },
    ),
    Tool(
        name="update_org_agent_definition",
        description="Update an organization-owned agent definition (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "available_options": {"type": "array", "items": {"type": "object"}},
                "default_env": {"type": "object"},
                "supports_multi_chat": {"type": "boolean"},
                "required_credential_service_ids": {"type": "array", "items": {"type": "string"}},
                "commands": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["agent_id"],
        },
    ),
    Tool(
        name="delete_org_agent_definition",
        description="Delete an organization-owned agent definition (admin).",
        inputSchema={
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    ),
    Tool(
        name="duplicate_org_agent_definition",
        description="Duplicate a visible agent definition into the current organization (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "name": {"type": "string"},
                "activate": {"type": "boolean"},
            },
            "required": ["agent_id"],
        },
    ),
    Tool(
        name="toggle_org_agent_definition_activation",
        description="Activate or deactivate an agent definition for the organization (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "active": {"type": "boolean"},
            },
            "required": ["agent_id", "active"],
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
]

# Map tool name → required permission
_TOOL_PERMISSIONS: dict[str, APIKeyPermission] = {
    "list_workspaces": APIKeyPermission.WORKSPACES_READ,
    "get_workspace": APIKeyPermission.WORKSPACES_READ,
    "create_workspace": APIKeyPermission.WORKSPACES_CREATE,
    "stop_workspace": APIKeyPermission.WORKSPACES_STOP,
    "resume_workspace": APIKeyPermission.WORKSPACES_RESUME,
    "remove_workspace": APIKeyPermission.WORKSPACES_DELETE,
    "run_prompt": APIKeyPermission.PROMPTS_RUN,
    "cancel_prompt": APIKeyPermission.PROMPTS_CANCEL,
    "list_runners": APIKeyPermission.RUNNERS_READ,
    "list_agents": APIKeyPermission.AGENTS_READ,
    "list_conversations": APIKeyPermission.CONVERSATIONS_READ,
    "list_image_artifacts": APIKeyPermission.IMAGES_READ,
    "create_image_artifact": APIKeyPermission.IMAGES_CREATE,
    "list_image_definitions": APIKeyPermission.IMAGE_DEFINITIONS_READ,
    "create_image_definition": APIKeyPermission.IMAGE_DEFINITIONS_WRITE,
    "duplicate_image_definition": APIKeyPermission.IMAGE_DEFINITIONS_WRITE,
    "update_image_definition": APIKeyPermission.IMAGE_DEFINITIONS_WRITE,
    "delete_image_definition": APIKeyPermission.IMAGE_DEFINITIONS_WRITE,
    "list_runner_image_builds": APIKeyPermission.IMAGE_DEFINITIONS_READ,
    "create_runner_image_build": APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS,
    "update_runner_image_build": APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS,
    "delete_runner_image_build": APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS,
    "get_runner_image_build_log": APIKeyPermission.IMAGE_DEFINITIONS_READ,
    "list_credentials": APIKeyPermission.CREDENTIALS_READ,
    "list_org_agent_definitions": APIKeyPermission.ORG_AGENT_DEFINITIONS_READ,
    "create_org_agent_definition": APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE,
    "update_org_agent_definition": APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE,
    "delete_org_agent_definition": APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE,
    "duplicate_org_agent_definition": APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE,
    "toggle_org_agent_definition_activation": APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE,
    "list_org_credential_services": APIKeyPermission.ORG_CREDENTIAL_SERVICES_READ,
    "toggle_org_credential_service_activation": APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE,
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
    return [TextContent(type="text", text=json.dumps(_serialise(data), indent=2, default=str))]


def _error(msg: str) -> list[TextContent]:
    """Return an error as a text MCP response."""
    return [TextContent(type="text", text=f"Error: {msg}")]


def _get_owned_workspace_or_error(api_key, org_id, workspace_id):
    """Return an owned workspace or an MCP-formatted error payload."""
    from apps.runners.sio_server import get_runner_service
    from apps.organizations.services import OrganizationService
    from common.exceptions import NotFoundError

    svc = get_runner_service()
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

    from apps.runners.sio_server import get_runner_service

    svc = get_runner_service()
    sessions = list(svc.list_sessions(workspace_id))
    result = {
        "id": str(workspace.id),
        "name": workspace.name,
        "status": str(workspace.status),
        "runner_id": str(workspace.runner_id),
        "runtime_type": str(workspace.runtime_type),
        "created_at": workspace.created_at.isoformat(),
        "sessions": [
            {
                "id": str(s.id),
                "prompt": s.prompt,
                "status": str(s.status),
                "output": s.output,
                "created_at": s.created_at.isoformat(),
            }
            for s in sessions
        ],
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
            user=api_key.user,
            organization_id=org_id,
        )
        return workspace, task

    try:
        loop = asyncio.new_event_loop()
        workspace, task = loop.run_until_complete(_create())
        loop.close()
        return _text({
            "workspace_id": str(workspace.id),
            "task_id": str(task.id),
            "status": str(workspace.status),
            "message": "Workspace creation started. Use get_workspace to check status.",
        })
    except (NotFoundError, ConflictError) as e:
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


def _call_run_prompt(api_key, org_id, args: dict) -> list[TextContent]:
    from common.exceptions import NotFoundError, ConflictError

    import asyncio
    import uuid as _uuid

    workspace_id_str = args.get("workspace_id")
    prompt = args.get("prompt")
    if not workspace_id_str or not prompt:
        return _error("workspace_id and prompt are required")
    try:
        workspace_id = _uuid.UUID(workspace_id_str)
    except ValueError:
        return _error("Invalid workspace_id UUID")

    agent_model = args.get("agent_model")
    chat_id_str = args.get("chat_id")
    chat_id = None
    if chat_id_str:
        try:
            chat_id = _uuid.UUID(chat_id_str)
        except ValueError:
            return _error("Invalid chat_id UUID")

    try:
        _workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
        if error is not None:
            return error

        from apps.runners.sio_server import get_runner_service

        svc = get_runner_service()

        async def _run():
            return await svc.run_prompt(
                workspace_id,
                prompt,
                agent_model,
                agent_options={},
                chat_id=chat_id,
                skill_ids=[],
            )

        loop = asyncio.new_event_loop()
        session, task, chat = loop.run_until_complete(_run())
        loop.close()
        return _text({
            "session_id": str(session.id),
            "task_id": str(task.id),
            "chat_id": str(chat.id),
            "status": str(session.status),
            "message": "Prompt dispatched. Use get_workspace to see output.",
        })
    except (NotFoundError, ConflictError) as e:
        return _error(str(e))


def _call_cancel_prompt(api_key, org_id, args: dict) -> list[TextContent]:
    from common.exceptions import NotFoundError, ConflictError

    import asyncio
    import uuid as _uuid

    workspace_id_str = args.get("workspace_id")
    session_id_str = args.get("session_id")
    if not workspace_id_str or not session_id_str:
        return _error("workspace_id and session_id are required")

    try:
        workspace_id = _uuid.UUID(workspace_id_str)
        session_id = _uuid.UUID(session_id_str)
    except ValueError:
        return _error("Invalid workspace_id or session_id UUID")

    try:
        _workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
        if error is not None:
            return error

        from apps.runners.sio_server import get_runner_service

        svc = get_runner_service()

        async def _cancel():
            return await svc.cancel_session_prompt(workspace_id, session_id)

        loop = asyncio.new_event_loop()
        task = loop.run_until_complete(_cancel())
        loop.close()
        return _text(
            {
                "task_id": str(task.id),
                "message": "Cancellation task dispatched.",
            }
        )
    except (NotFoundError, ConflictError, ValueError) as e:
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


def _call_list_agents(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.sio_server import get_runner_service

    import uuid as _uuid

    svc = get_runner_service()
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)

    workspace = None
    if args.get("workspace_id"):
        try:
            workspace_id = _uuid.UUID(args["workspace_id"])
        except ValueError:
            return _error("Invalid workspace_id UUID")
        workspace, error = _get_owned_workspace_or_error(api_key, org_id, workspace_id)
        if error is not None:
            return error

    agents = svc.get_available_agents(
        organization_id=org_id,
        user=api_key.user,
        workspace=workspace,
    )
    return _text(agents)


def _call_list_conversations(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.runners.repositories import ConversationRepository
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    rows = ConversationRepository.list_for_user(org_id, api_key.user.id)
    return _text(rows)


def _call_list_image_artifacts(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.runners.sio_server import get_runner_service
    from apps.organizations.services import OrganizationService

    svc = get_runner_service()
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    svc.image_artifacts.timeout_stale(timeout_hours=1)
    artifacts = svc.list_image_artifacts_for_user(user=api_key.user)
    result = [
        {
            "id": str(s.id),
            "source_workspace_id": (
                str(s.source_workspace_id) if s.source_workspace_id else None
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


def _call_list_image_definitions(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.sio_server import get_runner_service

    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    svc = get_runner_service()
    definitions = svc.list_image_definitions(org_id)
    return _text(
        [
            {
                "id": str(definition.id),
                "organization_id": (
                    str(definition.organization_id)
                    if definition.organization_id
                    else None
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
                "created_at": definition.created_at.isoformat(),
                "updated_at": definition.updated_at.isoformat(),
            }
            for definition in definitions
        ]
    )


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

    source = ImageDefinition.objects.filter(id=definition_id).filter(
        Q(organization__isnull=True) | Q(organization_id=org_id)
    ).first()
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
    definition.delete()
    return _text({"deleted": True})


def _call_list_runner_image_builds(api_key, org_id, args: dict) -> list[TextContent]:
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
    builds = svc.list_runner_image_builds(definition_id, org_id)
    return _text(
        [
            {
                "id": str(build.id),
                "image_definition_id": str(build.image_definition_id),
                "runner_id": str(build.runner_id),
                "status": build.status,
                "image_tag": build.image_tag,
                "image_path": build.image_path,
                "build_log": build.build_log,
                "build_task_id": str(build.build_task_id) if build.build_task_id else None,
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


def _call_create_runner_image_build(api_key, org_id, args: dict) -> list[TextContent]:
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
        return await svc.trigger_runner_image_build(
            image_definition=definition,
            runner=runner,
            activate=bool(args.get("activate", True)),
            created_by=api_key.user,
        )

    loop = asyncio.new_event_loop()
    build = loop.run_until_complete(_create())
    loop.close()
    return _text({"id": str(build.id), "status": build.status})


def _call_update_runner_image_build(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.models import RunnerImageBuild
    from apps.runners.repositories import RunnerImageBuildRepository
    from apps.runners.sio_server import get_runner_service
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

    build = RunnerImageBuildRepository.get_for_org(definition_id, runner_id, org_id)
    if build is None:
        return _error("Runner image build not found")

    if action == "deactivate":
        build.status = RunnerImageBuild.Status.DEACTIVATED
        build.deactivated_at = timezone.now()
        build.save(update_fields=["status", "deactivated_at", "updated_at"])
        return _text({"id": str(build.id), "status": build.status})

    if action not in {"activate", "rebuild"}:
        return _error("action must be one of: deactivate, activate, rebuild")

    svc = get_runner_service()

    async def _rebuild():
        return await svc.trigger_runner_image_build(
            image_definition=build.image_definition,
            runner=build.runner,
            activate=True,
            created_by=api_key.user,
        )

    loop = asyncio.new_event_loop()
    updated = loop.run_until_complete(_rebuild())
    loop.close()
    return _text({"id": str(updated.id), "status": updated.status})


def _call_delete_runner_image_build(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.repositories import RunnerImageBuildRepository

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

    deleted = RunnerImageBuildRepository.delete_for_org(definition_id, runner_id, org_id)
    if not deleted:
        return _error("Runner image build not found")
    return _text({"deleted": True})


def _call_get_runner_image_build_log(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.organizations.services import OrganizationService
    from apps.runners.repositories import RunnerImageBuildRepository

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

    build = RunnerImageBuildRepository.get_for_org(definition_id, runner_id, org_id)
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


def _require_org_admin(user, org_id):
    from apps.organizations.services import OrganizationService

    org_service = OrganizationService()
    org_service.require_membership(user, org_id)
    if org_service.get_user_role(user, org_id) != "admin":
        raise PermissionError("Admin role required")


def _org_agent_to_dict(agent, activated_ids: set) -> dict:
    commands = [
        {
            "id": str(cmd.id),
            "phase": cmd.phase,
            "args": list(cmd.args or []),
            "workdir": cmd.workdir,
            "env": dict(cmd.env or {}),
            "description": cmd.description,
            "order": cmd.order,
        }
        for cmd in agent.commands.all().order_by("phase", "order")
    ]
    required_ids = [str(svc.id) for svc in agent.required_credential_services.all()]
    return {
        "id": str(agent.id),
        "name": agent.name,
        "description": agent.description,
        "is_standard": agent.organization_id is None,
        "organization_id": str(agent.organization_id) if agent.organization_id else None,
        "available_options": list(agent.available_options or []),
        "default_env": dict(agent.default_env or {}),
        "supports_multi_chat": bool(agent.supports_multi_chat),
        "required_credential_service_ids": required_ids,
        "commands": commands,
        "is_active": agent.id in activated_ids,
    }


def _parse_uuid(value: str | None, field_name: str):
    import uuid as _uuid

    if not value:
        raise ValueError(f"{field_name} is required")
    try:
        return _uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name} UUID") from exc


def _validate_run_commands(commands: list[dict]) -> None:
    run_commands = [c for c in commands if c.get("phase") == "run"]
    if len(run_commands) != 1:
        raise ValueError("Exactly one 'run' command is required")


def _call_list_org_agent_definitions(api_key, org_id, args: dict) -> list[TextContent]:
    from django.db.models import Q

    from apps.runners.models import AgentDefinition, OrgAgentDefinitionActivation

    try:
        _require_org_admin(api_key.user, org_id)
    except PermissionError as exc:
        return _error(str(exc))

    definitions = list(
        AgentDefinition.objects.filter(Q(organization__isnull=True) | Q(organization_id=org_id))
        .prefetch_related("commands", "required_credential_services")
        .order_by("name")
    )
    activated_ids = set(
        OrgAgentDefinitionActivation.objects.filter(organization_id=org_id).values_list(
            "agent_definition_id", flat=True
        )
    )
    return _text([_org_agent_to_dict(agent, activated_ids) for agent in definitions])


def _call_create_org_agent_definition(api_key, org_id, args: dict) -> list[TextContent]:
    from django.db import IntegrityError

    from apps.credentials.models import CredentialService
    from apps.runners.models import AgentCommand, AgentDefinition, OrgAgentDefinitionActivation

    try:
        _require_org_admin(api_key.user, org_id)
        name = args.get("name")
        if not name:
            raise ValueError("name is required")
        commands = args.get("commands") or []
        if not isinstance(commands, list):
            raise ValueError("commands must be a list")
        _validate_run_commands(commands)

        agent = AgentDefinition.objects.create(
            organization_id=org_id,
            name=name,
            description=args.get("description", ""),
            available_options=args.get("available_options") or [],
            default_env=args.get("default_env") or {},
            supports_multi_chat=bool(args.get("supports_multi_chat", False)),
        )

        required_ids = args.get("required_credential_service_ids") or []
        if required_ids:
            service_ids = [_parse_uuid(value, "required_credential_service_id") for value in required_ids]
            services = CredentialService.objects.filter(id__in=service_ids)
            agent.required_credential_services.set(services)

        for cmd in commands:
            AgentCommand.objects.create(
                agent=agent,
                phase=cmd.get("phase"),
                args=cmd.get("args") or [],
                workdir=cmd.get("workdir"),
                env=cmd.get("env") or {},
                description=cmd.get("description", ""),
                order=int(cmd.get("order", 0)),
            )

        OrgAgentDefinitionActivation.objects.get_or_create(
            organization_id=org_id,
            agent_definition=agent,
        )
        agent = AgentDefinition.objects.prefetch_related(
            "commands", "required_credential_services"
        ).get(id=agent.id)
        return _text(_org_agent_to_dict(agent, {agent.id}))
    except PermissionError as exc:
        return _error(str(exc))
    except IntegrityError:
        return _error(f"Agent definition '{args.get('name')}' already exists in this organization")
    except ValueError as exc:
        return _error(str(exc))


def _call_update_org_agent_definition(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.credentials.models import CredentialService
    from apps.runners.models import AgentCommand, AgentDefinition, OrgAgentDefinitionActivation

    try:
        _require_org_admin(api_key.user, org_id)
        agent_id = _parse_uuid(args.get("agent_id"), "agent_id")
        agent = AgentDefinition.objects.filter(id=agent_id, organization_id=org_id).first()
        if agent is None:
            return _error("Agent definition not found or is not org-owned")

        if "name" in args:
            agent.name = args.get("name")
        if "description" in args:
            agent.description = args.get("description", "")
        if "available_options" in args:
            agent.available_options = args.get("available_options") or []
        if "default_env" in args:
            agent.default_env = args.get("default_env") or {}
        if "supports_multi_chat" in args:
            agent.supports_multi_chat = bool(args.get("supports_multi_chat"))
        agent.save()

        if "required_credential_service_ids" in args:
            required_ids = args.get("required_credential_service_ids") or []
            service_ids = [_parse_uuid(value, "required_credential_service_id") for value in required_ids]
            services = CredentialService.objects.filter(id__in=service_ids)
            agent.required_credential_services.set(services)

        if "commands" in args:
            commands = args.get("commands") or []
            if not isinstance(commands, list):
                raise ValueError("commands must be a list")
            _validate_run_commands(commands)
            AgentCommand.objects.filter(agent=agent).delete()
            for cmd in commands:
                AgentCommand.objects.create(
                    agent=agent,
                    phase=cmd.get("phase"),
                    args=cmd.get("args") or [],
                    workdir=cmd.get("workdir"),
                    env=cmd.get("env") or {},
                    description=cmd.get("description", ""),
                    order=int(cmd.get("order", 0)),
                )

        agent = AgentDefinition.objects.prefetch_related(
            "commands", "required_credential_services"
        ).get(id=agent.id)
        activated_ids = set(
            OrgAgentDefinitionActivation.objects.filter(organization_id=org_id).values_list(
                "agent_definition_id", flat=True
            )
        )
        return _text(_org_agent_to_dict(agent, activated_ids))
    except PermissionError as exc:
        return _error(str(exc))
    except ValueError as exc:
        return _error(str(exc))


def _call_delete_org_agent_definition(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.runners.models import AgentDefinition

    try:
        _require_org_admin(api_key.user, org_id)
        agent_id = _parse_uuid(args.get("agent_id"), "agent_id")
        agent = AgentDefinition.objects.filter(id=agent_id, organization_id=org_id).first()
        if agent is None:
            return _error("Agent definition not found or is not org-owned")
        agent.delete()
        return _text({"deleted": True, "agent_id": str(agent_id)})
    except PermissionError as exc:
        return _error(str(exc))
    except ValueError as exc:
        return _error(str(exc))


def _call_toggle_org_agent_definition_activation(api_key, org_id, args: dict) -> list[TextContent]:
    from django.db.models import Q

    from apps.runners.models import AgentDefinition, OrgAgentDefinitionActivation

    try:
        _require_org_admin(api_key.user, org_id)
        agent_id = _parse_uuid(args.get("agent_id"), "agent_id")
        if "active" not in args:
            raise ValueError("active is required")
        active = bool(args.get("active"))

        agent = AgentDefinition.objects.prefetch_related(
            "commands", "required_credential_services"
        ).filter(Q(organization__isnull=True) | Q(organization_id=org_id), id=agent_id).first()
        if agent is None:
            return _error("Agent definition not found")
        if agent.organization_id is None and not api_key.user.is_staff:
            return _error("Only staff users can modify standard agent definition activation")

        if active:
            OrgAgentDefinitionActivation.objects.get_or_create(
                organization_id=org_id,
                agent_definition=agent,
            )
            activated_ids = {agent.id}
        else:
            OrgAgentDefinitionActivation.objects.filter(
                organization_id=org_id,
                agent_definition=agent,
            ).delete()
            activated_ids = set()
        return _text(_org_agent_to_dict(agent, activated_ids))
    except PermissionError as exc:
        return _error(str(exc))
    except ValueError as exc:
        return _error(str(exc))


def _call_list_org_credential_services(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.credentials.models import CredentialService, OrgCredentialServiceActivation

    try:
        _require_org_admin(api_key.user, org_id)
    except PermissionError as exc:
        return _error(str(exc))

    activated_ids = set(
        OrgCredentialServiceActivation.objects.filter(organization_id=org_id).values_list(
            "credential_service_id", flat=True
        )
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


def _call_toggle_org_credential_service_activation(api_key, org_id, args: dict) -> list[TextContent]:
    from apps.credentials.models import CredentialService, OrgCredentialServiceActivation

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


def _call_duplicate_org_agent_definition(api_key, org_id, args: dict) -> list[TextContent]:
    from django.db import connection, transaction
    from django.db.models import Q

    from apps.runners.models import (
        AgentCommand,
        AgentCredentialRelationCommand,
        AgentDefinition,
        AgentDefinitionCredentialRelation,
        OrgAgentDefinitionActivation,
    )

    def _copy_name(base_name: str) -> str:
        base = (base_name or "").strip() or "agent"
        if len(base) > 64:
            base = base[:64]
        candidate = base
        if not AgentDefinition.objects.filter(organization_id=org_id, name=candidate).exists():
            return candidate

        suffix = " (Copy)"
        candidate = f"{base[: 64 - len(suffix)]}{suffix}"
        if not AgentDefinition.objects.filter(organization_id=org_id, name=candidate).exists():
            return candidate

        idx = 2
        while True:
            suffix = f" (Copy {idx})"
            candidate = f"{base[: 64 - len(suffix)]}{suffix}"
            if not AgentDefinition.objects.filter(organization_id=org_id, name=candidate).exists():
                return candidate
            idx += 1

    try:
        _require_org_admin(api_key.user, org_id)
        agent_id = _parse_uuid(args.get("agent_id"), "agent_id")
        requested_name = args.get("name")
        if requested_name is not None and not str(requested_name).strip():
            raise ValueError("name cannot be empty")
        active = bool(args.get("activate", True))

        relation_table = AgentDefinitionCredentialRelation._meta.db_table
        has_relation_table = relation_table in connection.introspection.table_names()
        prefetches = ["commands", "required_credential_services"]
        if has_relation_table:
            prefetches.append("credential_relations__commands")

        source = (
            AgentDefinition.objects.filter(
                Q(organization__isnull=True) | Q(organization_id=org_id),
                id=agent_id,
            )
            .prefetch_related(*prefetches)
            .first()
        )
        if source is None:
            return _error("Agent definition not found")

        target_name = _copy_name(str(requested_name or source.name))

        with transaction.atomic():
            copied = AgentDefinition.objects.create(
                organization_id=org_id,
                name=target_name,
                description=source.description,
                available_options=list(source.available_options or []),
                default_env=dict(source.default_env or {}),
                supports_multi_chat=source.supports_multi_chat,
            )
            copied.required_credential_services.set(source.required_credential_services.all())

            for cmd in source.commands.all():
                AgentCommand.objects.create(
                    agent=copied,
                    phase=cmd.phase,
                    args=list(cmd.args or []),
                    workdir=cmd.workdir,
                    env=dict(cmd.env or {}),
                    description=cmd.description,
                    order=cmd.order,
                )

            if has_relation_table:
                for relation in source.credential_relations.all():
                    copied_relation = AgentDefinitionCredentialRelation.objects.create(
                        agent_definition=copied,
                        credential_service=relation.credential_service,
                        default_env=dict(relation.default_env or {}),
                    )
                    for rel_cmd in relation.commands.all():
                        AgentCredentialRelationCommand.objects.create(
                            relation=copied_relation,
                            phase=rel_cmd.phase,
                            args=list(rel_cmd.args or []),
                            workdir=rel_cmd.workdir,
                            env=dict(rel_cmd.env or {}),
                            description=rel_cmd.description,
                            order=rel_cmd.order,
                        )

            if active:
                OrgAgentDefinitionActivation.objects.get_or_create(
                    organization_id=org_id,
                    agent_definition=copied,
                )

        copied = AgentDefinition.objects.prefetch_related(
            "commands", "required_credential_services"
        ).get(id=copied.id)
        activated_ids = {copied.id} if active else set()
        return _text(_org_agent_to_dict(copied, activated_ids))
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
    "stop_workspace": _call_stop_workspace,
    "resume_workspace": _call_resume_workspace,
    "remove_workspace": _call_remove_workspace,
    "run_prompt": _call_run_prompt,
    "cancel_prompt": _call_cancel_prompt,
    "list_runners": _call_list_runners,
    "list_agents": _call_list_agents,
    "list_conversations": _call_list_conversations,
    "list_image_artifacts": _call_list_image_artifacts,
    "create_image_artifact": _call_create_image_artifact,
    "list_image_definitions": _call_list_image_definitions,
    "create_image_definition": _call_create_image_definition,
    "duplicate_image_definition": _call_duplicate_image_definition,
    "update_image_definition": _call_update_image_definition,
    "delete_image_definition": _call_delete_image_definition,
    "list_runner_image_builds": _call_list_runner_image_builds,
    "create_runner_image_build": _call_create_runner_image_build,
    "update_runner_image_build": _call_update_runner_image_build,
    "delete_runner_image_build": _call_delete_runner_image_build,
    "get_runner_image_build_log": _call_get_runner_image_build_log,
    "list_credentials": _call_list_credentials,
    "list_org_agent_definitions": _call_list_org_agent_definitions,
    "create_org_agent_definition": _call_create_org_agent_definition,
    "update_org_agent_definition": _call_update_org_agent_definition,
    "delete_org_agent_definition": _call_delete_org_agent_definition,
    "duplicate_org_agent_definition": _call_duplicate_org_agent_definition,
    "toggle_org_agent_definition_activation": _call_toggle_org_agent_definition_activation,
    "list_org_credential_services": _call_list_org_credential_services,
    "toggle_org_credential_service_activation": _call_toggle_org_credential_service_activation,
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
        tool for tool in _TOOLS
        if api_key.has_permission(_TOOL_PERMISSIONS[tool.name])
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
            result = await sync_to_async(handler)(api_key, org_id, args)
            return result
        except Exception as exc:
            logger.exception("MCP tool %s failed", name)
            return _error(str(exc))

    return server


# ---------------------------------------------------------------------------
# Starlette ASGI application for MCP via SSE
# ---------------------------------------------------------------------------

def _extract_token(scope) -> str | None:
    """Extract API key token from ASGI scope headers."""
    headers = dict(scope.get("headers", []))
    auth = headers.get(b"authorization", b"").decode()
    if auth:
        return auth.removeprefix("Bearer ").strip()
    return headers.get(b"x-api-key", b"").decode() or None


def build_mcp_app() -> Starlette:
    """Build and return the Starlette ASGI app for MCP SSE transport."""

    sse_transport = SseServerTransport("/mcp/messages/")

    async def handle_sse(request: Request) -> Response:
        """SSE endpoint — one persistent connection per MCP client."""
        token = (
            request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            or request.headers.get("x-api-key", "")
        )

        from asgiref.sync import sync_to_async
        from .auth import authenticate_api_key

        api_key = await sync_to_async(authenticate_api_key)(token)
        if api_key is None:
            return JSONResponse(
                {"error": "Invalid or missing API key. Ensure the key has the mcp:access permission."},
                status_code=401,
            )

        mcp_server = create_mcp_server(api_key)

        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0],
                streams[1],
                mcp_server.create_initialization_options(),
            )

        # connect_sse sends the response itself; return a dummy to satisfy type checker
        return Response()

    return Starlette(
        routes=[
            Route("/mcp/sse", endpoint=handle_sse),
            Mount("/mcp/messages/", app=sse_transport.handle_post_message),
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
