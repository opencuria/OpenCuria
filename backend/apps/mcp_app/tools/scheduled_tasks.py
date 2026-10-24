"""Scheduled task MCP tools."""

from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async
from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.scheduled_tasks.services import ScheduledTaskService
from common.exceptions import NotFoundError

from .. import access
from ..results import ToolHandler, error_result, text_result

CRON_EXPRESSION_HELP = (
    "Required when recurrence is 'cron'. Five-field cron "
    "(minute hour day-of-month month day-of-week, Sunday=0) evaluated in "
    "timezone_name, e.g. '*/30 8-17 * * 1-5'. Lists, ranges, steps, names "
    "and @hourly/@daily/@weekly/@monthly are supported; separate several "
    "expressions with ';'."
)


TOOLS: list[Tool] = [
    Tool(
        name="list_scheduled_tasks",
        description="List your scheduled tasks.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="create_scheduled_task",
        description=(
            "Create a personal recurring prompt schedule. Use recurrence "
            "daily/weekly with local_time, or cron with cron_expression for "
            "multiple runs per day and other cron cadences."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string"},
                "name": {"type": "string"},
                "prompt": {"type": "string"},
                "harness_id": {"type": "string", "enum": ["native", "claude", "codex"]},
                "mode": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "skill_ids": {"type": "array", "items": {"type": "string"}},
                "recurrence": {"type": "string", "enum": ["daily", "weekly", "cron"]},
                "weekdays": {"type": "array", "items": {"type": "integer"}},
                "cron_expression": {
                    "type": "string",
                    "description": CRON_EXPRESSION_HELP,
                },
                "local_time": {"type": "string"},
                "timezone_name": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["workspace_id", "name", "prompt", "timezone_name"],
        },
    ),
    Tool(
        name="update_scheduled_task",
        description="Update or pause a personal scheduled task.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "workspace_id": {"type": "string"},
                "name": {"type": "string"},
                "prompt": {"type": "string"},
                "harness_id": {"type": "string", "enum": ["native", "claude", "codex"]},
                "mode": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "skill_ids": {"type": "array", "items": {"type": "string"}},
                "recurrence": {"type": "string", "enum": ["daily", "weekly", "cron"]},
                "weekdays": {"type": "array", "items": {"type": "integer"}},
                "cron_expression": {
                    "type": "string",
                    "description": CRON_EXPRESSION_HELP,
                },
                "local_time": {"type": "string"},
                "timezone_name": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="delete_scheduled_task",
        description="Delete a personal scheduled task.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    ),
    Tool(
        name="run_scheduled_task_now",
        description="Run a scheduled task immediately without changing its schedule.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    ),
    Tool(
        name="get_scheduled_task",
        description="Get one scheduled task you own.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    ),
    Tool(
        name="list_scheduled_task_runs",
        description="List history for one scheduled task.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "list_scheduled_tasks": APIKeyPermission.HARNESS_READ,
    "get_scheduled_task": APIKeyPermission.HARNESS_READ,
    "list_scheduled_task_runs": APIKeyPermission.HARNESS_READ,
    "create_scheduled_task": APIKeyPermission.HARNESS_RUN,
    "update_scheduled_task": APIKeyPermission.HARNESS_RUN,
    "delete_scheduled_task": APIKeyPermission.HARNESS_RUN,
    "run_scheduled_task_now": APIKeyPermission.HARNESS_RUN,
}


def _call_list_scheduled_tasks(api_key, org_id, args: dict) -> list[TextContent]:
    rows = ScheduledTaskService().list(organization_id=org_id, owner_id=api_key.user.id)
    return text_result([_scheduled_task_dict(row) for row in rows])


def _scheduled_task_dict(task) -> dict:
    return {
        "id": str(task.id),
        "name": task.name,
        "workspace_id": str(task.workspace_id),
        "prompt": task.prompt,
        "mode": task.mode,
        "harness_id": getattr(task, "harness_id", "native"),
        "model": task.model,
        "reasoning_effort": task.reasoning_effort,
        "skill_ids": list(task.skill_ids or []),
        "recurrence": task.recurrence,
        "weekdays": list(task.weekdays or []),
        "cron_expression": getattr(task, "cron_expression", ""),
        "local_time": task.local_time.strftime("%H:%M"),
        "timezone_name": task.timezone_name,
        "enabled": task.enabled,
        "next_run_at": task.next_run_at.isoformat(),
    }


async def _call_create_scheduled_task(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        await sync_to_async(OrganizationService().require_membership)(
            api_key.user, org_id
        )
        workspace_id = uuid.UUID(str(args.get("workspace_id", "")))
    except ValueError:
        return error_result("Invalid workspace_id UUID")
    except Exception as exc:
        return error_result(str(exc))
    workspace, error = await sync_to_async(access.owned_workspace_or_error)(
        api_key, org_id, workspace_id
    )
    if error:
        return error
    try:
        task = await sync_to_async(ScheduledTaskService().create)(
            organization_id=org_id,
            owner_id=api_key.user.id,
            workspace=workspace,
            values=args,
        )
        return text_result(_scheduled_task_dict(task))
    except (ValueError, KeyError) as exc:
        return error_result(str(exc))


async def _call_update_scheduled_task(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        await sync_to_async(OrganizationService().require_membership)(
            api_key.user, org_id
        )
        service = ScheduledTaskService()
        task = await sync_to_async(service.get)(
            uuid.UUID(str(args.get("task_id", ""))),
            organization_id=org_id,
            owner_id=api_key.user.id,
        )
        values = {
            key: value
            for key, value in args.items()
            if key not in {"task_id", "workspace_id"}
        }
        workspace = None
        if args.get("workspace_id"):
            workspace, error = await sync_to_async(access.owned_workspace_or_error)(
                api_key, org_id, uuid.UUID(str(args["workspace_id"]))
            )
            if error:
                return error
        task = await sync_to_async(service.update)(task, values, workspace=workspace)
        return text_result(_scheduled_task_dict(task))
    except (ValueError, NotFoundError) as exc:
        return error_result(str(exc))


async def _call_delete_scheduled_task(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        await sync_to_async(OrganizationService().require_membership)(
            api_key.user, org_id
        )
        service = ScheduledTaskService()
        task = await sync_to_async(service.get)(
            uuid.UUID(str(args.get("task_id", ""))),
            organization_id=org_id,
            owner_id=api_key.user.id,
        )
        await sync_to_async(service.delete)(task)
        return text_result({"deleted": True})
    except (ValueError, NotFoundError) as exc:
        return error_result(str(exc))


async def _call_run_scheduled_task_now(
    api_key, org_id, args: dict
) -> list[TextContent]:
    try:
        await sync_to_async(OrganizationService().require_membership)(
            api_key.user, org_id
        )
        service = ScheduledTaskService()
        task = await sync_to_async(service.get)(
            uuid.UUID(str(args.get("task_id", ""))),
            organization_id=org_id,
            owner_id=api_key.user.id,
        )
        run = await service.run_now(task)
        return text_result(_scheduled_run_dict(run))
    except (ValueError, NotFoundError) as exc:
        return error_result(str(exc))


def _scheduled_run_dict(run) -> dict:
    return {
        "id": str(run.id),
        "scheduled_for": run.scheduled_for.isoformat(),
        "trigger": run.trigger,
        "configuration_snapshot": run.configuration_snapshot or {},
        "status": run.status,
        "session_id": str(run.session_id) if run.session_id else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "reason": run.reason,
        "assistant_finish": run.assistant_message.finish
        if run.assistant_message
        else "",
        "assistant_error": run.assistant_message.error if run.assistant_message else "",
    }


def _call_get_scheduled_task(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        OrganizationService().require_membership(api_key.user, org_id)
        task = ScheduledTaskService().get(
            uuid.UUID(str(args.get("task_id", ""))),
            organization_id=org_id,
            owner_id=api_key.user.id,
        )
        return text_result(_scheduled_task_dict(task))
    except (ValueError, NotFoundError) as exc:
        return error_result(str(exc))


def _call_list_scheduled_task_runs(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        OrganizationService().require_membership(api_key.user, org_id)
        service = ScheduledTaskService()
        task = service.get(
            uuid.UUID(str(args.get("task_id", ""))),
            organization_id=org_id,
            owner_id=api_key.user.id,
            include_deleted=True,
        )
        return text_result(
            [_scheduled_run_dict(run) for run in service.list_runs(task)]
        )
    except (ValueError, NotFoundError) as exc:
        return error_result(str(exc))


HANDLERS: dict[str, ToolHandler] = {
    "list_scheduled_tasks": _call_list_scheduled_tasks,
    "get_scheduled_task": _call_get_scheduled_task,
    "list_scheduled_task_runs": _call_list_scheduled_task_runs,
    "create_scheduled_task": _call_create_scheduled_task,
    "update_scheduled_task": _call_update_scheduled_task,
    "delete_scheduled_task": _call_delete_scheduled_task,
    "run_scheduled_task_now": _call_run_scheduled_task_now,
}
