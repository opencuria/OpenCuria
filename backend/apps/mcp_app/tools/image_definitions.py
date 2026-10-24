"""Image definition (recipe) and build job MCP tools."""

from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async
from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.runners.services.image_definitions import ImageDefinitionService
from common.exceptions import ServiceError

from .. import access
from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
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
                "base_distro": {"type": "string"},
                "packages": {"type": "array", "items": {"type": "string"}},
                "env_vars": {"type": "object"},
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
                "base_distro": {"type": "string"},
                "packages": {"type": "array", "items": {"type": "string"}},
                "env_vars": {"type": "object"},
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
                "message": {
                    "type": "string",
                    "description": "What changed in this version (optional).",
                },
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
                "message": {
                    "type": "string",
                    "description": "What changed in the rebuilt version (optional).",
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
]


PERMISSIONS: dict[str, APIKeyPermission] = {
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
}


def _image_definitions() -> ImageDefinitionService:
    """Return the image recipe service shared with the REST adapter."""
    return ImageDefinitionService(access.runner_service())


def _image_definition_payload(definition) -> dict:
    """Serialize an image definition the same way REST list responses do."""
    return {
        "id": str(definition.id),
        "organization_id": (
            str(definition.organization_id) if definition.organization_id else None
        ),
        "name": definition.name,
        "description": definition.description,
        "is_standard": definition.organization_id is None,
        "base_distro": definition.base_distro,
        "packages": list(definition.packages or []),
        "env_vars": dict(definition.env_vars or {}),
        "custom_init_script": definition.custom_init_script or "",
        "is_active": bool(definition.is_active),
        "status": getattr(definition, "status", "active"),
        "runner_build_summary": ImageDefinitionService.build_summary(definition),
        "delete_last_error": getattr(definition, "delete_last_error", "") or "",
        "created_at": definition.created_at.isoformat(),
        "updated_at": definition.updated_at.isoformat(),
    }


def _parse_definition_id(args: dict) -> tuple:
    """Parse ``definition_id`` from MCP args or return an MCP error."""
    raw = args.get("definition_id")
    if not raw:
        return None, error_result("definition_id is required")
    try:
        return uuid.UUID(raw), None
    except ValueError:
        return None, error_result("Invalid definition_id UUID")


def _parse_build_ids(args: dict) -> tuple:
    """Parse ``definition_id`` and ``runner_id`` or return an MCP error."""
    if not args.get("definition_id") or not args.get("runner_id"):
        return None, error_result("definition_id and runner_id are required")
    try:
        return (uuid.UUID(args["definition_id"]), uuid.UUID(args["runner_id"])), None
    except ValueError:
        return None, error_result("Invalid definition_id or runner_id UUID")


def _call_list_image_definitions(api_key, org_id, args: dict) -> list[TextContent]:
    definitions = _image_definitions().list_visible(api_key.user, org_id)
    return text_result(
        [_image_definition_payload(definition) for definition in definitions]
    )


def _call_create_image_definition(api_key, org_id, args: dict) -> list[TextContent]:
    name = (args.get("name") or "").strip()
    if not name:
        return error_result("name is required")
    recipe = {
        "name": name,
        "description": args.get("description") or "",
        "base_distro": args.get("base_distro") or "ubuntu:22.04",
        "packages": list(args.get("packages") or []),
        "env_vars": dict(args.get("env_vars") or {}),
        "custom_init_script": args.get("custom_init_script") or "",
        "is_active": bool(args.get("is_active", True)),
    }
    try:
        definition = _image_definitions().create(api_key.user, org_id, recipe)
    except ServiceError as exc:
        return error_result(exc.message)
    return text_result({"id": str(definition.id), "name": definition.name})


def _call_duplicate_image_definition(api_key, org_id, args: dict) -> list[TextContent]:
    definition_id, error = _parse_definition_id(args)
    if error is not None:
        return error
    name = args.get("name")
    try:
        copied = _image_definitions().duplicate(
            api_key.user,
            org_id,
            definition_id,
            None if name is None else str(name),
        )
    except ServiceError as exc:
        return error_result(exc.message)
    return text_result({"id": str(copied.id), "name": copied.name})


def _call_update_image_definition(api_key, org_id, args: dict) -> list[TextContent]:
    definition_id, error = _parse_definition_id(args)
    if error is not None:
        return error
    try:
        definition = _image_definitions().update(
            api_key.user, org_id, definition_id, args
        )
    except ServiceError as exc:
        return error_result(exc.message)
    return text_result({"id": str(definition.id), "name": definition.name})


async def _call_delete_image_definition(
    api_key, org_id, args: dict
) -> list[TextContent]:
    definition_id, error = _parse_definition_id(args)
    if error is not None:
        return error
    try:
        definition = await _image_definitions().delete(
            api_key.user, org_id, definition_id
        )
    except ServiceError as exc:
        return error_result(exc.message)
    except ValueError as exc:
        return error_result(str(exc))
    if definition is None or definition.status == "deleted":
        return text_result({"deleted": True, "status": "deleted"})
    return text_result(await sync_to_async(_image_definition_payload)(definition))


def _call_list_build_jobs(api_key, org_id, args: dict) -> list[TextContent]:
    definition_id, error = _parse_definition_id(args)
    if error is not None:
        return error
    try:
        builds = _image_definitions().list_builds(api_key.user, org_id, definition_id)
    except ServiceError as exc:
        return error_result(exc.message)
    return text_result(
        [
            {
                "image_path": (
                    build.image_instance.runner_ref
                    if getattr(build, "image_instance", None) is not None
                    else ""
                ),
                "id": str(build.id),
                "current_generation_id": str(build.current_generation_id)
                if build.current_generation_id
                else None,
                "pending_generation_id": str(build.pending_generation_id)
                if build.pending_generation_id
                else None,
                "image_definition_id": str(build.image_definition_id),
                "runner_id": str(build.runner_id),
                "status": build.status,
                # Size only: the full log is served by get_build_log and
                # would otherwise re-introduce the unbounded payload here.
                "build_log_size": int(getattr(build, "build_log_size", 0) or 0),
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


async def _call_create_build_job(api_key, org_id, args: dict) -> list[TextContent]:
    build_ids, error = _parse_build_ids(args)
    if error is not None:
        return error
    try:
        build = await _image_definitions().create_build(
            api_key.user,
            org_id,
            *build_ids,
            activate=bool(args.get("activate", True)),
            message=(args.get("message") or "")[:500],
        )
    except ServiceError as exc:
        return error_result(exc.message)
    return text_result({"id": str(build.id), "status": build.status})


async def _call_update_build_job(api_key, org_id, args: dict) -> list[TextContent]:
    action = (args.get("action") or "").strip().lower()
    if not args.get("definition_id") or not args.get("runner_id") or not action:
        return error_result("definition_id, runner_id and action are required")
    build_ids, error = _parse_build_ids(args)
    if error is not None:
        return error
    try:
        build = await _image_definitions().update_build(
            api_key.user,
            org_id,
            *build_ids,
            action,
            message=(args.get("message") or "")[:500],
        )
    except ServiceError as exc:
        return error_result(exc.message)
    return text_result({"id": str(build.id), "status": build.status})


async def _call_delete_build_job(api_key, org_id, args: dict) -> list[TextContent]:
    build_ids, error = _parse_build_ids(args)
    if error is not None:
        return error
    try:
        result = await _image_definitions().delete_build(
            api_key.user, org_id, *build_ids
        )
    except ServiceError as exc:
        return error_result(exc.message)
    except ValueError as exc:
        return error_result(str(exc))
    return text_result(result)


def _call_get_build_job_log(api_key, org_id, args: dict) -> list[TextContent]:
    build_ids, error = _parse_build_ids(args)
    if error is not None:
        return error
    try:
        build_log = _image_definitions().build_log(api_key.user, org_id, *build_ids)
    except ServiceError as exc:
        return error_result(exc.message)
    return text_result({"build_log": build_log})


HANDLERS: dict[str, ToolHandler] = {
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
}
