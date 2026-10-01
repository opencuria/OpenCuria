"""REST API for owner-scoped personal scheduled tasks."""

from __future__ import annotations

import uuid
from datetime import datetime

from asgiref.sync import sync_to_async
from django.http import HttpRequest
from ninja import Router, Schema

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import NotFoundError

from .models import ScheduledTask, ScheduledTaskRun
from .services import ScheduledTaskService

scheduled_task_router = Router(tags=["scheduled-tasks"])


def _service() -> ScheduledTaskService:
    return ScheduledTaskService()


def _permission_denied(permission: APIKeyPermission) -> tuple[int, dict[str, str]]:
    return 403, {
        "detail": f"API key lacks permission: {permission.value}",
        "code": "permission_denied",
    }


def _org_id(request: HttpRequest) -> uuid.UUID:
    raw = request.headers.get("X-Organization-Id")
    if not raw:
        raise ValueError("X-Organization-Id header is required")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ValueError("Invalid X-Organization-Id header") from exc


def _require_membership(request: HttpRequest, org_id: uuid.UUID) -> None:
    """Validate org scope and convert missing/foreign orgs into API 404s."""
    try:
        OrganizationService().require_membership(request.user, org_id)
    except NotFoundError as exc:
        raise _OrganizationNotFound(exc.message) from exc


class _OrganizationNotFound(Exception):
    """Internal signal for an inaccessible organization scope."""


class ScheduledTaskIn(Schema):
    name: str
    workspace_id: uuid.UUID
    prompt: str
    mode: str = "build"
    model: str = ""
    reasoning_effort: str = ""
    skill_ids: list[str] = []
    recurrence: str = "daily"
    weekdays: list[int] = []
    local_time: str = "09:00"
    timezone_name: str = "UTC"
    enabled: bool = True


class ScheduledTaskPatchIn(Schema):
    name: str | None = None
    prompt: str | None = None
    mode: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    skill_ids: list[str] | None = None
    recurrence: str | None = None
    weekdays: list[int] | None = None
    local_time: str | None = None
    timezone_name: str | None = None
    enabled: bool | None = None


class ScheduledTaskOut(Schema):
    id: uuid.UUID
    name: str
    workspace_id: uuid.UUID
    prompt: str
    mode: str
    model: str
    reasoning_effort: str
    skill_ids: list[str]
    recurrence: str
    weekdays: list[int]
    local_time: str
    timezone_name: str
    enabled: bool
    next_run_at: datetime
    created_at: datetime
    updated_at: datetime


class ScheduledTaskRunOut(Schema):
    id: uuid.UUID
    scheduled_for: datetime
    trigger: str
    configuration_snapshot: dict = {}
    status: str
    session_id: uuid.UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    reason: str = ""
    assistant_finish: str = ""
    assistant_error: str = ""


def _task_out(task: ScheduledTask) -> ScheduledTaskOut:
    return ScheduledTaskOut(
        id=task.id,
        name=task.name,
        workspace_id=task.workspace_id,
        prompt=task.prompt,
        mode=task.mode,
        model=task.model,
        reasoning_effort=task.reasoning_effort,
        skill_ids=list(task.skill_ids or []),
        recurrence=task.recurrence,
        weekdays=list(task.weekdays or []),
        local_time=task.local_time.strftime("%H:%M"),
        timezone_name=task.timezone_name,
        enabled=task.enabled,
        next_run_at=task.next_run_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _run_out(run: ScheduledTaskRun) -> ScheduledTaskRunOut:
    return ScheduledTaskRunOut(
        id=run.id,
        scheduled_for=run.scheduled_for,
        trigger=run.trigger,
        configuration_snapshot=run.configuration_snapshot or {},
        status=run.status,
        session_id=run.session_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        reason=run.reason,
        assistant_finish=(
            run.assistant_message.finish if run.assistant_message else ""
        ),
        assistant_error=(run.assistant_message.error if run.assistant_message else ""),
    )


async def _owned_workspace(
    request: HttpRequest, org_id: uuid.UUID, workspace_id: uuid.UUID
):
    from apps.runners.sio_server import get_runner_service

    return await sync_to_async(get_runner_service().get_workspace_for_user)(
        workspace_id, user=request.user, organization_id=org_id
    )


@scheduled_task_router.get(
    "/scheduled-tasks/",
    response={200: list[ScheduledTaskOut], 400: dict, 403: dict, 404: dict},
)
def list_scheduled_tasks(request: HttpRequest):
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _permission_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _org_id(request)
        _require_membership(request, org_id)
        return 200, [
            _task_out(task)
            for task in _service().list(
                organization_id=org_id, owner_id=request.user.id
            )
        ]
    except _OrganizationNotFound as exc:
        return 404, {"detail": str(exc), "code": "not_found"}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@scheduled_task_router.post(
    "/scheduled-tasks/",
    response={201: ScheduledTaskOut, 400: dict, 403: dict, 404: dict},
)
async def create_scheduled_task(request: HttpRequest, payload: ScheduledTaskIn):
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _permission_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _org_id(request)
        await sync_to_async(_require_membership)(request, org_id)
        workspace = await _owned_workspace(request, org_id, payload.workspace_id)
        task = await sync_to_async(_service().create)(
            organization_id=org_id,
            owner_id=request.user.id,
            workspace=workspace,
            values=payload.model_dump(exclude={"workspace_id"}),
        )
        return 201, _task_out(task)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except _OrganizationNotFound as exc:
        return 404, {"detail": str(exc), "code": "not_found"}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@scheduled_task_router.get(
    "/scheduled-tasks/{task_id}/",
    response={200: ScheduledTaskOut, 400: dict, 403: dict, 404: dict},
)
def get_scheduled_task(request: HttpRequest, task_id: uuid.UUID):
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _permission_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _org_id(request)
        _require_membership(request, org_id)
    except _OrganizationNotFound as exc:
        return 404, {"detail": str(exc), "code": "not_found"}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    try:
        return 200, _task_out(
            _service().get(task_id, organization_id=org_id, owner_id=request.user.id)
        )
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@scheduled_task_router.patch(
    "/scheduled-tasks/{task_id}/",
    response={200: ScheduledTaskOut, 400: dict, 403: dict, 404: dict},
)
async def patch_scheduled_task(
    request: HttpRequest, task_id: uuid.UUID, payload: ScheduledTaskPatchIn
):
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _permission_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _org_id(request)
        await sync_to_async(_require_membership)(request, org_id)
    except _OrganizationNotFound as exc:
        return 404, {"detail": str(exc), "code": "not_found"}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    try:
        task = await sync_to_async(_service().get)(
            task_id, organization_id=org_id, owner_id=request.user.id
        )
        updated = await sync_to_async(_service().update)(
            task, payload.model_dump(exclude_unset=True, exclude_none=True)
        )
        return 200, _task_out(updated)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@scheduled_task_router.delete(
    "/scheduled-tasks/{task_id}/",
    response={204: None, 400: dict, 403: dict, 404: dict},
)
def delete_scheduled_task(request: HttpRequest, task_id: uuid.UUID):
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _permission_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _org_id(request)
        _require_membership(request, org_id)
    except _OrganizationNotFound as exc:
        return 404, {"detail": str(exc), "code": "not_found"}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    try:
        service = _service()
        service.delete(
            service.get(task_id, organization_id=org_id, owner_id=request.user.id)
        )
        return 204, None
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@scheduled_task_router.post(
    "/scheduled-tasks/{task_id}/run/",
    response={202: ScheduledTaskRunOut, 400: dict, 403: dict, 404: dict},
)
async def run_scheduled_task(request: HttpRequest, task_id: uuid.UUID):
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _permission_denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _org_id(request)
        await sync_to_async(_require_membership)(request, org_id)
    except _OrganizationNotFound as exc:
        return 404, {"detail": str(exc), "code": "not_found"}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    try:
        service = _service()
        task = await sync_to_async(service.get)(
            task_id, organization_id=org_id, owner_id=request.user.id
        )
        run = await service.run_now(task)
        return 202, _run_out(run)
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@scheduled_task_router.get(
    "/scheduled-tasks/{task_id}/runs/",
    response={200: list[ScheduledTaskRunOut], 400: dict, 403: dict, 404: dict},
)
def list_scheduled_task_runs(request: HttpRequest, task_id: uuid.UUID):
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _permission_denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _org_id(request)
        _require_membership(request, org_id)
    except _OrganizationNotFound as exc:
        return 404, {"detail": str(exc), "code": "not_found"}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    try:
        service = _service()
        task = service.get(
            task_id,
            organization_id=org_id,
            owner_id=request.user.id,
            include_deleted=True,
        )
        return 200, [_run_out(run) for run in service.list_runs(task)]
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
