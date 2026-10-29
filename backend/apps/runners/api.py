"""
REST API endpoints for the runners app.

Thin adapter layer — validates input, delegates to RunnerService,
and formats responses. No business logic here.

All endpoints are protected by JWT auth (set globally on NinjaAPI).
Organization context is passed via X-Organization-Id header.

Split into separate routers to avoid URL path conflicts:
- runner_router    → /api/v1/runners/
- workspace_router → /api/v1/workspaces/
"""

from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async
from django.core.exceptions import ObjectDoesNotExist, SynchronousOnlyOperation
from django.http import HttpRequest
from ninja import Body, Router, Schema
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.credentials.services import CredentialSvc
from apps.organizations.services import OrganizationService
from apps.plugins.services import PluginService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError
from common.request_context import get_organization_id
from common.schemas import ErrorOut

from .exceptions import (
    AdminRoleRequiredError,
    ImageRecipeNotFoundError,
    ImageRecipeValidationError,
    RunnerOfflineError,
)
from .git_presenters import git_error_to_response
from .image_presenters import (
    captured_image_to_out,
    image_artifacts_to_out,
)
from .schemas import (
    CapturedImageOut,
    CapturedImageUpdateIn,
    DesktopClipboardReadIn,
    DesktopClipboardReadOut,
    DesktopClipboardWriteIn,
    DesktopRenewOut,
    DesktopStartOut,
    DesktopStatusOut,
    DesktopStopOut,
    DesktopTakeControlOut,
    DesktopViewerIntentIn,
    EditorFileWriteIn,
    GitHistoryQuery,
    GitOperationIn,
    GitRepoQuery,
    ImageArtifactCreateIn,
    ImageArtifactCreateOut,
    ImageBuildJobCreateIn,
    ImageBuildJobListOut,
    ImageBuildJobOut,
    ImageBuildJobUpdateIn,
    ImageDefinitionBuildSummaryOut,
    ImageDefinitionCreateIn,
    ImageDefinitionDuplicateIn,
    ImageDefinitionOut,
    ImageDefinitionUpdateIn,
    ProcessOut,
    RunnerCreateIn,
    RunnerCreateOut,
    RunnerOut,
    RunnerSystemMetricsOut,
    RunnerUpdateIn,
    TaskOut,
    TerminalStartIn,
    TerminalStartOut,
    WorkspaceCreateIn,
    WorkspaceCreateOut,
    WorkspaceFromImageArtifactIn,
    WorkspaceFromImageArtifactOut,
    WorkspaceOut,
    WorkspaceRecreateIn,
    WorkspaceRecreateOut,
    WorkspaceUpdateIn,
    WorkspaceUpdateOut,
    validate_commit_hash,
)
from .services import get_runner_service
from .services.image_definitions import ImageDefinitionService
from .services.workspace_reads import WorkspaceReadService
from .storage_schemas import RunnerStorageOut


def _perm_denied(permission: APIKeyPermission):
    """Return a 403 error tuple for a denied API key permission."""
    return 403, ErrorOut(
        detail=f"API key lacks permission: {permission.value}",
        code="permission_denied",
    )


def _get_service():
    """Get the RunnerService singleton."""
    return get_runner_service()


def _get_org_service() -> OrganizationService:
    return OrganizationService()


async def _get_org_admin_flag_async(request: HttpRequest, org_id: uuid.UUID) -> bool:
    """Validate membership and return whether the user is an org admin."""
    org_service = _get_org_service()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    role = await sync_to_async(org_service.get_user_role)(request.user, org_id)
    return role == "admin"


async def _require_org_membership_async(
    request: HttpRequest, org_id: uuid.UUID
) -> None:
    """Validate organization membership for async endpoints."""
    org_service = _get_org_service()
    await sync_to_async(org_service.require_membership)(request.user, org_id)


def _get_owned_workspace(
    request: HttpRequest, org_id: uuid.UUID, workspace_id: uuid.UUID
):
    """Return a workspace only when it belongs to the active org and owner."""
    service = _get_service()
    try:
        return service.get_workspace_for_user(
            workspace_id,
            user=request.user,
            organization_id=org_id,
        )
    except NotFoundError:
        raise NotFoundError("Workspace", str(workspace_id))


async def _get_owned_workspace_async(
    request: HttpRequest,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
):
    """Async wrapper around the owner-scoped workspace lookup."""
    return await sync_to_async(_get_owned_workspace)(request, org_id, workspace_id)


# ===========================================================================
# Runner Router — /api/v1/runners/
# ===========================================================================

runner_router = Router(tags=["runners"])


@runner_router.get("/operations/", response={200: dict, 403: ErrorOut})
def list_lifecycle_operations(request: HttpRequest):
    """List sanitized lifecycle state visible to the current owner/admin."""
    from .services.disposition import DispositionService

    if not check_api_key_permission(request, APIKeyPermission.RUNNERS_READ):
        return _perm_denied(APIKeyPermission.RUNNERS_READ)
    return {
        "operations": DispositionService.list_operations(
            request.user, get_organization_id(request)
        )
    }


@runner_router.get(
    "/", response={200: list[RunnerOut], 403: ErrorOut}, summary="List runners"
)
def list_runners(request: HttpRequest):
    """Return all runners for the user's active organization."""
    if not check_api_key_permission(request, APIKeyPermission.RUNNERS_READ):
        return _perm_denied(APIKeyPermission.RUNNERS_READ)
    org_id = get_organization_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    return 200, service.list_runners(organization_id=org_id)


@runner_router.post(
    "/", response={201: RunnerCreateOut, 403: ErrorOut}, summary="Register a new runner"
)
def create_runner(request: HttpRequest, payload: RunnerCreateIn):
    """
    Register a new runner. Requires admin role in the active organization.

    The token is shown only once — store it securely.
    """
    if not check_api_key_permission(request, APIKeyPermission.RUNNERS_CREATE):
        return _perm_denied(APIKeyPermission.RUNNERS_CREATE)
    org_id = get_organization_id(request)
    org_service = _get_org_service()

    try:
        org = org_service.require_admin(request.user, org_id)
    except AuthenticationError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)

    runner, token = _get_service().create_runner(name=payload.name, organization=org)

    return 201, RunnerCreateOut(
        id=runner.id,
        name=runner.name,
        api_token=token,
    )


@runner_router.patch(
    "/{runner_id}/",
    response={
        200: RunnerOut,
        400: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Update runner QEMU settings",
)
def update_runner(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerUpdateIn):
    """Update per-runner QEMU resource defaults and limits (admin only)."""
    org_id = get_organization_id(request)
    org_service = _get_org_service()
    try:
        org_service.require_admin(request.user, org_id)
    except AuthenticationError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)

    service = _get_service()
    try:
        runner = service.get_runner(runner_id)
        if runner.organization_id != org_id:
            raise NotFoundError("Runner", str(runner_id))
        updated_fields = payload.model_dump(exclude_unset=True)
        updated = service.update_runner_qemu_settings(runner_id, **updated_fields)
        return 200, updated
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")
    except RuntimeError as e:
        return 409, ErrorOut(detail=str(e), code="runner_call_failed")


@runner_router.get(
    "/{runner_id}/metrics/latest/",
    response={200: RunnerSystemMetricsOut, 404: ErrorOut},
    summary="Get latest system metrics for a runner",
)
def get_runner_metrics_latest(request: HttpRequest, runner_id: uuid.UUID):
    """Return the most recently recorded system metrics snapshot for a runner."""
    org_id = get_organization_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    runner = service.get_runner_or_none(runner_id)
    if runner is None or runner.organization_id != org_id:
        return 404, ErrorOut(detail="Runner not found", code="not_found")

    metrics = service.get_latest_runner_metrics(runner_id)
    if metrics is None:
        return 404, ErrorOut(detail="No metrics available yet", code="not_found")

    return 200, RunnerSystemMetricsOut(
        runner_id=metrics.runner_id,
        timestamp=metrics.timestamp,
        cpu_usage_percent=metrics.cpu_usage_percent,
        ram_used_bytes=metrics.ram_used_bytes,
        ram_total_bytes=metrics.ram_total_bytes,
        disk_used_bytes=metrics.disk_used_bytes,
        disk_total_bytes=metrics.disk_total_bytes,
        vm_metrics=metrics.vm_metrics,
    )


@runner_router.get(
    "/{runner_id}/metrics/history/",
    response={200: list[RunnerSystemMetricsOut], 404: ErrorOut},
    summary="Get system metrics history for a runner",
)
def get_runner_metrics_history(
    request: HttpRequest, runner_id: uuid.UUID, hours: int = 24
):
    """Return historical system metrics for a runner (default: last 24h)."""
    org_id = get_organization_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    runner = service.get_runner_or_none(runner_id)
    if runner is None or runner.organization_id != org_id:
        return 404, ErrorOut(detail="Runner not found", code="not_found")

    metrics = service.list_runner_metrics(runner_id, hours=hours)

    return 200, [
        RunnerSystemMetricsOut(
            runner_id=m.runner_id,
            timestamp=m.timestamp,
            cpu_usage_percent=m.cpu_usage_percent,
            ram_used_bytes=m.ram_used_bytes,
            ram_total_bytes=m.ram_total_bytes,
            disk_used_bytes=m.disk_used_bytes,
            disk_total_bytes=m.disk_total_bytes,
            vm_metrics=m.vm_metrics,
        )
        for m in metrics
    ]


# ===========================================================================
# Workspace Router — /api/v1/workspaces/
# ===========================================================================

workspace_router = Router(tags=["workspaces"])


@workspace_router.get(
    "/", response={200: list[WorkspaceOut], 403: ErrorOut}, summary="List workspaces"
)
def list_workspaces(request: HttpRequest, runner_id: uuid.UUID | None = None):
    """
    Return workspaces for the user in the active organization.
    """
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_READ)
    org_id = get_organization_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    workspaces = service.list_workspaces(
        runner_id=runner_id,
        organization_id=org_id,
        user=request.user,
    )
    reads = WorkspaceReadService()
    return 200, [WorkspaceOut(**data) for data in reads.describe_many(workspaces)]


@workspace_router.post(
    "/",
    response={202: WorkspaceCreateOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Create a workspace",
)
async def create_workspace(request: HttpRequest, payload: WorkspaceCreateIn):
    """Create a workspace — dispatches async task to a runner."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_CREATE):
        return _perm_denied(APIKeyPermission.WORKSPACES_CREATE)
    if payload.plugin_ids and not check_api_key_permission(
        request, APIKeyPermission.PLUGINS_WRITE
    ):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    if "credential_ids" in payload.model_fields_set and not check_api_key_permission(
        request, APIKeyPermission.CREDENTIALS_READ
    ):
        return _perm_denied(APIKeyPermission.CREDENTIALS_READ)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        if not payload.image_artifact_id:
            return 409, ErrorOut(
                detail="An image artifact must be selected to create a workspace",
                code="image_artifact_required",
            )
        workspace, task = await service.create_workspace(
            name=payload.name,
            repos=payload.repos,
            qemu_vcpus=payload.qemu_vcpus,
            qemu_memory_mb=payload.qemu_memory_mb,
            qemu_disk_size_gb=payload.qemu_disk_size_gb,
            desktop_width=payload.desktop_width,
            desktop_height=payload.desktop_height,
            credential_ids=payload.credential_ids,
            runner_id=payload.runner_id,
            image_artifact_id=payload.image_artifact_id,
            user=request.user,
            organization_id=org_id,
            plugin_ids=payload.plugin_ids,
        )
        plugin_ids = await sync_to_async(
            PluginService().list_workspace_plugin_ids_many
        )([workspace.id])
        return 202, WorkspaceCreateOut(
            workspace_id=workspace.id,
            task_id=task.id,
            status=workspace.status,
            plugin_ids=plugin_ids[workspace.id],
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(
            detail=e.message, code=e.code, gaps=getattr(e, "gaps", None)
        )
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="invalid_credentials")
    except RuntimeError as e:
        return 409, ErrorOut(detail=str(e), code="runner_call_failed")


@workspace_router.get(
    "/{workspace_id}/",
    response={200: WorkspaceOut, 403: ErrorOut, 404: ErrorOut},
)
def get_workspace(request: HttpRequest, workspace_id: uuid.UUID):
    """Return workspace details."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_READ)
    org_id = get_organization_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    try:
        workspace = _get_owned_workspace(request, org_id, workspace_id)
        return 200, WorkspaceOut(**WorkspaceReadService().describe(workspace))
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/terminal/",
    response={202: TerminalStartOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Start interactive terminal",
)
async def start_terminal(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    payload: TerminalStartIn = None,
):
    """Start an interactive PTY terminal in a workspace VM."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)

        cols = payload.cols if payload else 80
        rows = payload.rows if payload else 24
        task = await service.start_terminal(workspace_id, cols=cols, rows=rows)
        return 202, TerminalStartOut(task_id=task.id)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/desktop/",
    response={202: DesktopStartOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Start desktop session",
)
async def start_desktop(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    payload: DesktopViewerIntentIn,
):
    """Start a KasmVNC desktop session in a workspace VM."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = get_organization_id(request)
    service = _get_service()
    try:
        task = await service.start_desktop(
            workspace_id, user=request.user, organization_id=org_id,
            viewer_client_id=payload.viewer_client_id,
            intent_revision=payload.intent_revision)
        return 202, DesktopStartOut(task_id=task.id)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/desktop/stop/",
    response={202: DesktopStopOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Stop desktop session",
)
async def stop_desktop(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    payload: DesktopViewerIntentIn,
):
    """Release the viewer lease. The display stays up if computer-use holds it."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = get_organization_id(request)
    service = _get_service()
    try:
        task = await service.stop_desktop(
            workspace_id, user=request.user, organization_id=org_id,
            viewer_client_id=payload.viewer_client_id,
            intent_revision=payload.intent_revision)
        return 202, DesktopStopOut(task_id=task.id)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/desktop/renew/",
    response={200: DesktopRenewOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Renew existing viewer intent",
)
async def renew_desktop(request: HttpRequest, workspace_id: uuid.UUID,
                        payload: DesktopViewerIntentIn):
    """Renew only the authenticated tab's existing held lease."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    try:
        result = await _get_service().renew_desktop(
            workspace_id,
            user=request.user,
            organization_id=get_organization_id(request),
            viewer_client_id=payload.viewer_client_id,
            intent_revision=payload.intent_revision)
        return 200, DesktopRenewOut(**result)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/desktop/take-control/",
    response={200: DesktopTakeControlOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Take desktop control from computer-use",
)
async def take_desktop_control(
    request: HttpRequest,
    workspace_id: uuid.UUID,
):
    """Abort busy computer-use sessions so the viewer can drive the desktop."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _perm_denied(APIKeyPermission.HARNESS_RUN)
    org_id = get_organization_id(request)
    is_admin = await _get_org_admin_flag_async(request, org_id)

    service = _get_service()
    try:
        workspace = await sync_to_async(service.get_workspace)(workspace_id)
        if workspace.runner.organization_id != org_id:
            raise NotFoundError("Workspace", str(workspace_id))
        if not is_admin and workspace.created_by_id != request.user.id:
            raise NotFoundError("Workspace", str(workspace_id))

        aborted_ids = await service.take_desktop_control(workspace)
        return 200, DesktopTakeControlOut(aborted_session_ids=aborted_ids)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.get(
    "/{workspace_id}/desktop/status/",
    response={200: DesktopStatusOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Get desktop session status",
)
async def desktop_status(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    viewer_client_id: uuid.UUID | None = None,
):
    """Check whether a desktop session is active for the workspace."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = get_organization_id(request)
    service = _get_service()
    try:
        result = await service.viewer_desktop_status(
            workspace_id, user=request.user, organization_id=org_id,
            viewer_client_id=viewer_client_id)
        return 200, DesktopStatusOut(**result)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/desktop/clipboard/write/",
    response={
        200: DesktopClipboardReadOut,
        400: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Write text into desktop clipboard",
)
async def desktop_clipboard_write(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    payload: DesktopClipboardWriteIn,
):
    """Write plain text from the browser into the VM desktop clipboard."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = get_organization_id(request)
    is_admin = await _get_org_admin_flag_async(request, org_id)

    service = _get_service()
    try:
        workspace = await sync_to_async(service.get_workspace)(workspace_id)
        if workspace.runner.organization_id != org_id:
            raise NotFoundError("Workspace", str(workspace_id))
        if not is_admin and workspace.created_by_id != request.user.id:
            raise NotFoundError("Workspace", str(workspace_id))

        await service.write_desktop_clipboard(workspace_id, payload.text)
        text = await service.read_desktop_clipboard(workspace_id)
        return 200, DesktopClipboardReadOut(text=text)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@workspace_router.post(
    "/{workspace_id}/desktop/clipboard/read/",
    response={
        200: DesktopClipboardReadOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Read text from desktop clipboard",
)
async def desktop_clipboard_read(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    payload: DesktopClipboardReadIn | None = Body(None),
):
    """Read plain text from the VM desktop clipboard for local copy."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = get_organization_id(request)
    is_admin = await _get_org_admin_flag_async(request, org_id)

    service = _get_service()
    options = payload or DesktopClipboardReadIn()
    try:
        workspace = await sync_to_async(service.get_workspace)(workspace_id)
        if workspace.runner.organization_id != org_id:
            raise NotFoundError("Workspace", str(workspace_id))
        if not is_admin and workspace.created_by_id != request.user.id:
            raise NotFoundError("Workspace", str(workspace_id))

        if options.copy_selection:
            text = await service.read_desktop_clipboard(
                workspace_id,
                copy_selection=True,
                shift=options.shift,
            )
        else:
            text = await service.read_desktop_clipboard(workspace_id)
        return 200, DesktopClipboardReadOut(text=text)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except (RuntimeError, TimeoutError, SocketIOTimeoutError):
        if not options.copy_selection:
            raise
        return 409, ErrorOut(
            detail="Unable to copy the desktop selection. Try again.",
            code="conflict",
        )


@workspace_router.patch(
    "/{workspace_id}/",
    response={
        200: WorkspaceUpdateOut,
        400: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Update a workspace",
)
async def update_workspace(
    request: HttpRequest, workspace_id: uuid.UUID, payload: WorkspaceUpdateIn
):
    """Update mutable workspace metadata."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_UPDATE):
        return _perm_denied(APIKeyPermission.WORKSPACES_UPDATE)
    if payload.plugin_ids is not None and not check_api_key_permission(
        request, APIKeyPermission.PLUGINS_WRITE
    ):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    if payload.credential_ids is not None and not check_api_key_permission(
        request, APIKeyPermission.CREDENTIALS_READ
    ):
        return _perm_denied(APIKeyPermission.CREDENTIALS_READ)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)

        workspace = await service.update_workspace(
            workspace_id,
            name=payload.name,
            credential_ids=payload.credential_ids,
            qemu_vcpus=payload.qemu_vcpus,
            qemu_memory_mb=payload.qemu_memory_mb,
            qemu_disk_size_gb=payload.qemu_disk_size_gb,
            desktop_width=payload.desktop_width,
            desktop_height=payload.desktop_height,
            plugin_ids=payload.plugin_ids,
            user=request.user,
            organization_id=org_id,
        )
        persisted_plugin_ids = await sync_to_async(
            PluginService().list_workspace_plugin_ids_many
        )([workspace.id])
        credential_ids = await sync_to_async(
            CredentialSvc().workspace_credential_ids_many
        )([workspace.id])
        return 200, WorkspaceUpdateOut(
            id=workspace.id,
            name=workspace.name,
            updated_at=workspace.updated_at,
            active_operation=workspace.active_operation,
            intervention_required=workspace.intervention_required,
            lifecycle_diagnostic=workspace.lifecycle_diagnostic,
            credential_ids=credential_ids[workspace.id],
            plugin_ids=persisted_plugin_ids[workspace.id],
            credential_sync_status=getattr(
                workspace, "credential_sync_status", "not_required"
            ),
            credential_sync_detail=getattr(workspace, "credential_sync_detail", None),
            credentials_present=bool(workspace.credentials_present),
            qemu_vcpus=workspace.qemu_vcpus,
            qemu_memory_mb=workspace.qemu_memory_mb,
            qemu_disk_size_gb=workspace.qemu_disk_size_gb,
            desktop_width=workspace.desktop_width,
            desktop_height=workspace.desktop_height,
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(
            detail=e.message, code=e.code, gaps=getattr(e, "gaps", None)
        )
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@workspace_router.post(
    "/{workspace_id}/stop/",
    response={202: TaskOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Stop a workspace",
)
async def stop_workspace(request: HttpRequest, workspace_id: uuid.UUID):
    """Stop a running workspace."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_STOP):
        return _perm_denied(APIKeyPermission.WORKSPACES_STOP)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)

        task = await service.stop_workspace(workspace_id)
        return 202, task
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/resume/",
    response={202: TaskOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Resume a workspace",
)
async def resume_workspace(request: HttpRequest, workspace_id: uuid.UUID):
    """Resume a stopped workspace."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_RESUME):
        return _perm_denied(APIKeyPermission.WORKSPACES_RESUME)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)

        task = await service.resume_workspace(workspace_id)
        return 202, task
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.delete(
    "/{workspace_id}/",
    response={202: TaskOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Remove a workspace",
)
async def remove_workspace(request: HttpRequest, workspace_id: uuid.UUID):
    """Remove a workspace and its container."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_DELETE):
        return _perm_denied(APIKeyPermission.WORKSPACES_DELETE)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)

        task = await service.remove_workspace(workspace_id)
        return 202, task
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/recreate/",
    response={202: WorkspaceRecreateOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Reset or update a workspace (all workspace-only data is lost)",
)
async def recreate_workspace(
    request: HttpRequest, workspace_id: uuid.UUID, payload: WorkspaceRecreateIn
):
    """Recreate the workspace on its own version (reset) or the latest (update).

    The workspace keeps its id, chats, credentials, plugins and schedules.
    Requires the delete permission because workspace data is destroyed.
    """
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_DELETE):
        return _perm_denied(APIKeyPermission.WORKSPACES_DELETE)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        workspace, task = await service.recreate_workspace(
            workspace_id,
            payload.image_id,
            user=request.user,
            organization_id=org_id,
        )
        return 202, WorkspaceRecreateOut(
            workspace_id=workspace.id,
            task_id=task.id,
            active_operation=workspace.active_operation,
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except RunnerOfflineError as e:
        return 409, ErrorOut(detail=str(e), code="runner_offline")
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


# ===========================================================================
# Background process routers (workspace-bound, no auto-restart)
# ===========================================================================


def _process_to_out(process) -> ProcessOut:
    """Map a WorkspaceProcess ORM instance to ProcessOut."""
    return ProcessOut(
        id=process.id,
        workspace_id=process.workspace_id,
        name=process.name or "",
        command=process.command,
        workdir=process.workdir,
        pid=process.pid,
        log_path=process.log_path or "",
        status=process.status,
        exit_code=process.exit_code,
        run_count=int(getattr(process, "run_count", 0) or 0),
        kind=str(getattr(process, "kind", "") or "persistent"),
        started_at=process.started_at,
        ended_at=process.ended_at,
        updated_at=process.updated_at,
    )


def _user_visible_processes(processes) -> list:
    """Filter rows for the user list: all persistent + running temps.

    Finished temp rows stay in the DB (agent history) but vanish from
    the user list as soon as they stop — a stopped temp needs no user
    action, and temps can never be started/restarted/deleted by users.
    """
    visible = []
    for process in processes:
        if str(getattr(process, "kind", "") or "") != "temp":
            visible.append(process)
        elif str(getattr(process, "status", "") or "") == "running":
            visible.append(process)
    return visible


@workspace_router.get(
    "/{workspace_id}/processes/",
    response={200: list[ProcessOut], 403: ErrorOut, 404: ErrorOut},
    summary="List background processes",
)
async def list_processes(
    request: HttpRequest, workspace_id: uuid.UUID, live: bool = False
):
    """List background processes, using heartbeat-reconciled DB state by default.

    The runner heartbeat updates process state every 15 seconds, so a normal
    listing may reflect state up to one heartbeat interval ago. Pass
    ``?live=true`` to force a runner reconciliation (for explicit refreshes).
    Users see all persistent processes plus running temp processes; finished
    temps stay in the DB but are hidden here.
    """
    if not check_api_key_permission(
        request, APIKeyPermission.WORKSPACES_PROCESSES_READ
    ):
        return _perm_denied(APIKeyPermission.WORKSPACES_PROCESSES_READ)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        processes = await service.list_processes(workspace_id, live=live)
        visible = _user_visible_processes(processes)
        return 200, [_process_to_out(process) for process in visible]
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/processes/{process_id}/stop/",
    response={200: ProcessOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Stop a background process",
)
async def stop_process(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    process_id: str,
):
    """Stop a background process (SIGTERM, then SIGKILL after grace).

    ``process_id`` accepts a process UUID or the exact process name.
    Stopping is the only user action allowed on running temp processes;
    a stopped temp vanishes from the user list (its DB row is kept).
    """
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_PROCESSES_RUN):
        return _perm_denied(APIKeyPermission.WORKSPACES_PROCESSES_RUN)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        process = await service.stop_process(
            workspace_id,
            process_id,
        )
        return 200, _process_to_out(process)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/processes/{process_id}/restart/",
    response={
        200: ProcessOut,
        400: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Restart (same row, new log)",
)
async def restart_process(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    process_id: str,
):
    """Restart a background process on the same row (stable id, new log).

    Reuses the stored command/workdir; ``process_id`` accepts a process
    UUID or the exact process name. No request body required.
    Temporary processes cannot be restarted by users (400) — they live
    only for their agent session.
    """
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_PROCESSES_RUN):
        return _perm_denied(APIKeyPermission.WORKSPACES_PROCESSES_RUN)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        existing = await service.get_process(workspace_id, process_id)
        if str(getattr(existing, "kind", "") or "") == "temp":
            return 400, ErrorOut(
                detail=(
                    "Temporary processes cannot be restarted by users; "
                    "they live only for their agent session."
                ),
                code="validation_error",
            )
        process = await service.restart_process(
            workspace_id,
            process_id,
            user=request.user,
        )
        return 200, _process_to_out(process)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@workspace_router.delete(
    "/{workspace_id}/processes/{process_id}/",
    response={204: None, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Delete a background process",
)
async def delete_process(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    process_id: str,
):
    """Delete a background process row (stops it first if running).

    ``process_id`` accepts a process UUID or the exact process name.
    Temporary processes cannot be deleted by users (400) — stop them
    instead; finished temps vanish from the user list while their DB
    rows are kept.
    """
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_PROCESSES_RUN):
        return _perm_denied(APIKeyPermission.WORKSPACES_PROCESSES_RUN)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        existing = await service.get_process(workspace_id, process_id)
        if str(getattr(existing, "kind", "") or "") == "temp":
            return 400, ErrorOut(
                detail=(
                    "Temporary processes cannot be deleted by users; stop them instead."
                ),
                code="validation_error",
            )
        await service.delete_process(workspace_id, process_id)
        return 204, None
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


# ===========================================================================
# Git router — /api/v1/workspaces/{workspace_id}/git/ (productive integration)
# ===========================================================================


def _git_result_to_response(result: dict):
    """Map a runner git result dict to (status, payload) for Ninja.

    All runner payloads pass through intact as plain ``dict`` (the
    response map uses ``dict``/union for 200/400/409/504, so Ninja never
    re-validates/strips runner keys). Success (``ok`` truthy) → 200.
    Runner domain failures (``ok: False``) map by code: ``conflict``
    → 409 with the runner payload intact (incl. fresh ``snapshot``);
    ``timeout`` → 504 with ``code``/``message`` intact; everything
    else → 400 intact.
    """
    if not isinstance(result, dict):
        return 502, ErrorOut(
            detail="Git operation returned no result", code="runner_call_failed"
        )
    if result.get("ok"):
        return 200, result
    code = str(result.get("code") or "")
    if code in {"conflict", "stale_document"}:
        return 409, result
    if code == "timeout" or "timeout" in code:
        return 504, result
    return 400, result


#: Shared response map for the git endpoints. Runner payloads (success
#: and domain failures) pass through intact as plain ``dict`` so runner
#: keys (``snapshot``/``stderr``/``code``/``message``) are never stripped
#: by Ninja re-validation. Statuses that can carry *either* a runner
#: dict *or* a service-shaped ``ErrorOut`` (400/409/504) use a union so
#: both shapes validate: runner ``ok:false`` payloads stay intact while
#: service errors (timeout/offline/busy/validation) keep ``detail``/``code``.
_GIT_RESPONSES: dict = {
    200: dict,
    400: dict | ErrorOut,
    403: ErrorOut,
    404: ErrorOut,
    409: dict | ErrorOut,
    502: ErrorOut,
    504: dict | ErrorOut,
}


git_router = Router(tags=["workspaces-git"])


@workspace_router.get(
    "/{workspace_id}/files/content/", response=_GIT_RESPONSES,
    summary="Read complete editor text",
)
async def editor_file_content(
    request: HttpRequest, workspace_id: uuid.UUID, path: str,
):
    """Read text through owner-scoped workspace access."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_READ)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        result = await _get_service().editor_file_operation(
            workspace_id, "read", {"path": path}
        )
        return _git_result_to_response(result)
    except Exception as exc:
        return git_error_to_response(exc)


@workspace_router.put(
    "/{workspace_id}/files/content/", response=_GIT_RESPONSES,
    summary="Save editor text if its revision still matches",
)
async def save_editor_file_content(
    request: HttpRequest, workspace_id: uuid.UUID, payload: EditorFileWriteIn,
):
    """Save complete text through owner-scoped workspace access."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_UPDATE):
        return _perm_denied(APIKeyPermission.WORKSPACES_UPDATE)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        result = await _get_service().editor_file_operation(
            workspace_id, "write", payload.model_dump()
        )
        return _git_result_to_response(result)
    except Exception as exc:
        return git_error_to_response(exc)


@git_router.get(
    "/{workspace_id}/git/file-content/", response=_GIT_RESPONSES,
    summary="Read complete text from an index, worktree, or commit",
)
async def git_file_content(
    request: HttpRequest, workspace_id: uuid.UUID, repo_path: str,
    path: str, revision: str = "index",
):
    """Read an editor diff side with a token for stale-operation protection."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_GIT_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_GIT_READ)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        query = GitRepoQuery(repo_path=repo_path)
        result = await _get_service().run_git_operation(
            workspace_id, "file_content", repo_path=query.repo_path,
            args={"path": path, "revision": revision},
        )
        return _git_result_to_response(result)
    except Exception as exc:
        return git_error_to_response(exc)


@git_router.get(
    "/{workspace_id}/git/repos/",
    response=_GIT_RESPONSES,
    summary="List git repositories under /workspace",
)
async def git_repos(
    request: HttpRequest,
    workspace_id: uuid.UUID,
):
    """Return the discovered repos (id/name/path/branch/head) for a workspace."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_GIT_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_GIT_READ)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        result = await service.run_git_operation(
            workspace_id,
            "list_repos",
            args={},
        )
        return _git_result_to_response(result)
    except Exception as exc:
        return git_error_to_response(exc)


@git_router.get(
    "/{workspace_id}/git/repo/",
    response=_GIT_RESPONSES,
    summary="Get per-repo git snapshot",
)
async def git_repo(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    repo_path: str,
):
    """Return the full status, branches, remotes and recent history of one repo."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_GIT_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_GIT_READ)
    try:
        query = GitRepoQuery(repo_path=repo_path)
    except Exception as exc:
        return 400, ErrorOut(detail=str(exc), code="validation_error")
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        result = await service.run_git_operation(
            workspace_id,
            "repo_snapshot",
            repo_path=query.repo_path,
            args={},
        )
        return _git_result_to_response(result)
    except Exception as exc:
        return git_error_to_response(exc)


@git_router.get(
    "/{workspace_id}/git/history/",
    response=_GIT_RESPONSES,
    summary="Get paged commit history for one repo",
)
async def git_history(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    repo_path: str,
    history_limit: int = 50,
    history_skip: int = 0,
    branch: str | None = None,
):
    """Return paged commits (plus has_more cursors) for one repository.

    Query params page the history (1..500 / skip>=0); ``branch``
    optionally filters to one branch.
    """
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_GIT_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_GIT_READ)
    try:
        query = GitHistoryQuery(
            repo_path=repo_path,
            history_limit=history_limit,
            history_skip=history_skip,
            branch=branch,
        )
    except Exception as exc:
        return 400, ErrorOut(detail=str(exc), code="validation_error")
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        args: dict = {
            "history_limit": query.history_limit,
            "history_skip": query.history_skip,
        }
        if query.branch is not None:
            args["branch"] = query.branch
        result = await service.run_git_operation(
            workspace_id,
            "repo_history",
            repo_path=query.repo_path,
            args=args,
        )
        return _git_result_to_response(result)
    except Exception as exc:
        return git_error_to_response(exc)


@git_router.get(
    "/{workspace_id}/git/diff/",
    response=_GIT_RESPONSES,
    summary="Get working-tree diff for one repo",
)
async def git_diff(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    repo_path: str,
):
    """Return staged/unstaged diff entries for one repository."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_GIT_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_GIT_READ)
    try:
        query = GitRepoQuery(repo_path=repo_path)
    except Exception as exc:
        return 400, ErrorOut(detail=str(exc), code="validation_error")
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        result = await service.run_git_operation(
            workspace_id,
            "working_diff",
            repo_path=query.repo_path,
            args={},
        )
        return _git_result_to_response(result)
    except Exception as exc:
        return git_error_to_response(exc)


@git_router.get(
    "/{workspace_id}/git/commits/{commit_hash}/",
    response=_GIT_RESPONSES,
    summary="Get commit details",
)
async def git_commit_details(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    commit_hash: str,
    repo_path: str,
):
    """Return full details (message, authors, file changes) for one commit."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_GIT_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_GIT_READ)
    try:
        query = GitRepoQuery(repo_path=repo_path)
    except Exception as exc:
        return 400, ErrorOut(detail=str(exc), code="validation_error")
    try:
        commit = validate_commit_hash(commit_hash)
    except ValueError as exc:
        return 400, ErrorOut(detail=str(exc), code="validation_error")
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        result = await service.run_git_operation(
            workspace_id,
            "commit_details",
            repo_path=query.repo_path,
            args={"commit": commit},
        )
        return _git_result_to_response(result)
    except Exception as exc:
        return git_error_to_response(exc)


@git_router.post(
    "/{workspace_id}/git/operation/",
    response=_GIT_RESPONSES,
    summary="Run a typed Git mutation",
)
async def git_operation(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    payload: GitOperationIn,
):
    """Run one Git mutation with ``workspaces:git_write`` permission.

    Commit identity comes from the authenticated account; the schema
    accepts only declared mutation fields, never ``author_*`` or free args.
    """
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_GIT_WRITE):
        return _perm_denied(APIKeyPermission.WORKSPACES_GIT_WRITE)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        result = await service.run_git_operation(
            workspace_id,
            str(payload.operation),
            repo_path=payload.repo_path,
            args=payload.model_dump(
                exclude={"operation", "repo_path"},
                exclude_unset=True,
                exclude_none=True,
            ),
            user=request.user,
        )
        return _git_result_to_response(result)
    except Exception as exc:
        return git_error_to_response(exc)


# ===========================================================================
# Image artifact routers
# ===========================================================================

workspace_image_artifact_router = Router(tags=["image-artifacts"])
image_artifact_router = Router(tags=["image-artifacts"])


class ImageDeletionIn(Schema):
    target_type: str
    target_id: uuid.UUID
    mode: str = "deferred"
    fingerprint: str = ""


def _deletion_response(request, action, *args):
    from .services.deletion import DeletionService

    if not check_api_key_permission(request, APIKeyPermission.IMAGES_DELETE):
        return _perm_denied(APIKeyPermission.IMAGES_DELETE)
    try:
        return getattr(DeletionService(), action)(
            request.user, get_organization_id(request), *args
        )
    except AuthenticationError as exc:
        return 403, ErrorOut(detail=exc.message, code=exc.code)
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    except ConflictError as exc:
        return 409, ErrorOut(detail=exc.message, code=exc.code)


@image_artifact_router.post(
    "/deletions/preview/",
    response={200: dict, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
)
def preview_image_deletion(request: HttpRequest, payload: ImageDeletionIn):
    """Read-only structural cascade preview; no retirement or tasks."""
    return _deletion_response(
        request, "preview", payload.target_type, payload.target_id
    )


@image_artifact_router.post(
    "/deletions/", response={200: dict, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut}
)
def request_image_deletion(request: HttpRequest, payload: ImageDeletionIn):
    """Retire now and persist deferred or exact fingerprint-approved force intent."""
    return _deletion_response(
        request,
        "request",
        payload.target_type,
        payload.target_id,
        payload.mode,
        payload.fingerprint,
    )


@image_artifact_router.get(
    "/deletions/",
    response={200: list[dict], 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
)
def list_image_deletions(request: HttpRequest):
    return _deletion_response(request, "list")


@image_artifact_router.post(
    "/deletions/{request_id}/cancel/",
    response={200: dict, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
)
def cancel_image_deletion(request: HttpRequest, request_id: uuid.UUID):
    return _deletion_response(request, "cancel", request_id)


captured_image_router = Router(tags=["captured-images"])


def _captured_image_errors(exc: Exception):
    if isinstance(exc, NotFoundError):
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    if isinstance(exc, RunnerOfflineError):
        return 409, ErrorOut(detail=str(exc), code="runner_offline")
    if isinstance(exc, ConflictError):
        return 409, ErrorOut(
            detail=exc.message, code=exc.code, gaps=getattr(exc, "gaps", None)
        )
    raise exc


@captured_image_router.get(
    "/",
    response={200: list[CapturedImageOut], 403: ErrorOut},
    summary="List the current user's captured images with versions",
)
def list_captured_images(request: HttpRequest):
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_READ):
        return _perm_denied(APIKeyPermission.IMAGES_READ)
    org_id = get_organization_id(request)
    _get_org_service().require_membership(request.user, org_id)
    from .services.image_versions import ImageVersionService

    views = ImageVersionService().list_captured_images(request.user, org_id)
    return 200, [captured_image_to_out(view) for view in views]


@captured_image_router.patch(
    "/{captured_image_id}/",
    response={200: CapturedImageOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Rename a captured image",
)
def rename_captured_image(
    request: HttpRequest, captured_image_id: uuid.UUID, payload: CapturedImageUpdateIn
):
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_CREATE):
        return _perm_denied(APIKeyPermission.IMAGES_CREATE)
    org_id = get_organization_id(request)
    _get_org_service().require_membership(request.user, org_id)
    from .services.image_versions import ImageVersionService

    try:
        view = ImageVersionService().rename_captured_image(
            request.user, org_id, captured_image_id, payload.name
        )
        return 200, captured_image_to_out(view)
    except (NotFoundError, ConflictError) as exc:
        return _captured_image_errors(exc)


@captured_image_router.post(
    "/{captured_image_id}/workspaces/",
    response={
        202: WorkspaceFromImageArtifactOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Create a workspace from the latest version of a captured image",
)
async def create_workspace_from_captured_image(
    request: HttpRequest,
    captured_image_id: uuid.UUID,
    payload: WorkspaceFromImageArtifactIn,
):
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_CLONE):
        return _perm_denied(APIKeyPermission.IMAGES_CLONE)
    if payload.plugin_ids and not check_api_key_permission(
        request, APIKeyPermission.PLUGINS_WRITE
    ):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)
    from .services.image_versions import ImageVersionService

    service = _get_service()
    try:
        latest = await sync_to_async(ImageVersionService().latest_captured_version)(
            request.user, org_id, captured_image_id
        )
        workspace, task = await service.create_workspace_from_image_artifact(
            image_artifact_id=latest.id,
            name=payload.name,
            credential_ids=payload.credential_ids,
            user=request.user,
            organization_id=org_id,
            plugin_ids=payload.plugin_ids,
        )
        plugin_ids = await sync_to_async(
            PluginService().list_workspace_plugin_ids_many
        )([workspace.id])
        return 202, WorkspaceFromImageArtifactOut(
            workspace_id=workspace.id,
            task_id=task.id,
            status=workspace.status,
            plugin_ids=plugin_ids[workspace.id],
        )
    except (NotFoundError, ConflictError) as exc:
        return _captured_image_errors(exc)


@workspace_image_artifact_router.post(
    "/{workspace_id}/image-artifacts/",
    response={202: ImageArtifactCreateOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Create an image artifact from a workspace",
)
async def create_workspace_image_artifact(
    request: HttpRequest, workspace_id: uuid.UUID, payload: ImageArtifactCreateIn
):
    """Create an image artifact from a workspace."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_CREATE):
        return _perm_denied(APIKeyPermission.IMAGES_CREATE)
    org_id = get_organization_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        workspace, task = await service.create_image_artifact(
            workspace_id=workspace_id,
            name=payload.name,
            organization_id=org_id,
            captured_image_id=payload.captured_image_id,
            message=payload.message,
        )
        return 202, ImageArtifactCreateOut(task_id=task.id, workspace_id=workspace.id)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code="conflict")
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code="not_found")
    except ValueError as e:
        return 404, ErrorOut(detail=str(e), code="not_found")


# ===========================================================================
# Image Definitions Router — /api/v1/image-definitions/
# ===========================================================================

image_definition_router = Router(tags=["image-definitions"])

#: Service errors an image recipe endpoint maps to an HTTP status.
_IMAGE_RECIPE_ERRORS = (
    AdminRoleRequiredError,
    ImageRecipeNotFoundError,
    ImageRecipeValidationError,
    ConflictError,
)


def _image_definitions() -> ImageDefinitionService:
    return ImageDefinitionService(_get_service())


def _image_recipe_error(exc: Exception):
    """Map an ``_IMAGE_RECIPE_ERRORS`` exception to its REST error response."""
    if isinstance(exc, AdminRoleRequiredError):
        status = 403
    elif isinstance(exc, ImageRecipeNotFoundError):
        status = 404
    elif isinstance(exc, ImageRecipeValidationError):
        status = 400
    else:
        status = 409
    return status, ErrorOut(
        detail=exc.message, code=exc.code, gaps=getattr(exc, "gaps", None)
    )


def _image_definition_to_out(defn) -> ImageDefinitionOut:
    summary = ImageDefinitionService.build_summary(defn)
    return ImageDefinitionOut(
        id=defn.id,
        organization_id=defn.organization_id,
        created_by_id=defn.created_by_id,
        name=defn.name,
        description=defn.description,
        is_standard=defn.organization_id is None,
        base_distro=defn.base_distro,
        packages=list(defn.packages or []),
        env_vars=dict(defn.env_vars or {}),
        custom_init_script=defn.custom_init_script or "",
        is_active=defn.is_active,
        status=getattr(defn, "status", "active"),
        runner_build_summary=ImageDefinitionBuildSummaryOut(**summary),
        delete_requested_at=getattr(defn, "delete_requested_at", None),
        delete_started_at=getattr(defn, "delete_started_at", None),
        delete_confirmed_at=getattr(defn, "delete_confirmed_at", None),
        delete_last_error=getattr(defn, "delete_last_error", "") or "",
        delete_attempt_count=getattr(defn, "delete_attempt_count", 0) or 0,
        created_at=defn.created_at,
        updated_at=defn.updated_at,
    )


def _build_job_to_out(build) -> ImageBuildJobOut:
    artifact = None
    try:
        artifact = getattr(build, "image_instance", None)
    except (ObjectDoesNotExist, SynchronousOnlyOperation):
        artifact = None

    return ImageBuildJobOut(
        id=build.id,
        image_definition_id=build.image_definition_id,
        runner_id=build.runner_id,
        image_artifact_id=getattr(artifact, "id", None),
        current_generation_id=build.current_generation_id,
        pending_generation_id=build.pending_generation_id,
        status=build.status,
        build_log=getattr(build, "build_log", "") or "",
        build_task_id=build.build_task_id,
        built_at=build.built_at,
        deactivated_at=build.deactivated_at,
        delete_requested_at=getattr(build, "delete_requested_at", None),
        delete_confirmed_at=getattr(build, "delete_confirmed_at", None),
        delete_last_error=getattr(build, "delete_last_error", "") or "",
        created_at=build.created_at,
        updated_at=build.updated_at,
    )


def _build_job_to_list_out(build) -> ImageBuildJobListOut:
    """Convert a (build_log-deferred) build row to the polling-safe schema."""
    from .services.image_versions import ImageVersionService

    artifact = None
    try:
        artifact = getattr(build, "image_instance", None)
    except (ObjectDoesNotExist, SynchronousOnlyOperation):
        artifact = None

    return ImageBuildJobListOut(
        id=build.id,
        image_definition_id=build.image_definition_id,
        runner_id=build.runner_id,
        image_artifact_id=getattr(artifact, "id", None),
        current_generation_id=build.current_generation_id,
        pending_generation_id=build.pending_generation_id,
        status=build.status,
        build_log_size=int(getattr(build, "build_log_size", 0) or 0),
        build_task_id=build.build_task_id,
        built_at=build.built_at,
        deactivated_at=build.deactivated_at,
        delete_requested_at=getattr(build, "delete_requested_at", None),
        delete_confirmed_at=getattr(build, "delete_confirmed_at", None),
        delete_last_error=getattr(build, "delete_last_error", "") or "",
        created_at=build.created_at,
        updated_at=build.updated_at,
        versions=image_artifacts_to_out(
            ImageVersionService().definition_versions(build)
        ),
    )


@image_definition_router.get(
    "/",
    response={200: list[ImageDefinitionOut], 403: ErrorOut},
    summary="List image definitions",
)
def list_image_definitions(request: HttpRequest):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_READ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_READ)
    definitions = _image_definitions().list_visible(
        request.user, get_organization_id(request)
    )
    return 200, [_image_definition_to_out(d) for d in definitions]


@image_definition_router.post(
    "/",
    response={201: ImageDefinitionOut, 403: ErrorOut},
    summary="Create image definition",
)
def create_image_definition(request: HttpRequest, payload: ImageDefinitionCreateIn):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_WRITE)
    try:
        definition = _image_definitions().create(
            request.user, get_organization_id(request), payload.model_dump()
        )
    except _IMAGE_RECIPE_ERRORS as exc:
        return _image_recipe_error(exc)
    return 201, _image_definition_to_out(definition)


@image_definition_router.post(
    "/{definition_id}/duplicate/",
    response={201: ImageDefinitionOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut},
    summary="Duplicate an image definition into this organization",
)
def duplicate_image_definition(
    request: HttpRequest,
    definition_id: uuid.UUID,
    payload: ImageDefinitionDuplicateIn,
):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_WRITE)
    try:
        copied = _image_definitions().duplicate(
            request.user, get_organization_id(request), definition_id, payload.name
        )
    except _IMAGE_RECIPE_ERRORS as exc:
        return _image_recipe_error(exc)
    return 201, _image_definition_to_out(copied)


@image_definition_router.patch(
    "/{definition_id}/",
    response={200: ImageDefinitionOut, 403: ErrorOut, 404: ErrorOut},
    summary="Update image definition",
)
def update_image_definition(
    request: HttpRequest, definition_id: uuid.UUID, payload: ImageDefinitionUpdateIn
):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_WRITE)
    try:
        definition = _image_definitions().update(
            request.user,
            get_organization_id(request),
            definition_id,
            payload.model_dump(),
        )
    except _IMAGE_RECIPE_ERRORS as exc:
        return _image_recipe_error(exc)
    return 200, _image_definition_to_out(definition)


@image_definition_router.post(
    "/{definition_id}/activate/",
    response={200: ImageDefinitionOut, 404: ErrorOut, 403: ErrorOut, 409: ErrorOut},
    summary="Activate image definition",
)
async def activate_image_definition(request: HttpRequest, definition_id: uuid.UUID):
    """Re-activate a deactivated definition."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_WRITE)
    try:
        definition = await _image_definitions().activate(
            request.user, get_organization_id(request), definition_id
        )
    except _IMAGE_RECIPE_ERRORS as exc:
        return _image_recipe_error(exc)
    return 200, await sync_to_async(_image_definition_to_out)(definition)


@image_definition_router.get(
    "/{definition_id}/runner-builds/",
    response={200: list[ImageBuildJobListOut], 403: ErrorOut, 404: ErrorOut},
    summary="List runner builds for image definition",
)
def list_image_definition_runner_builds(request: HttpRequest, definition_id: uuid.UUID):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_READ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_READ)
    try:
        builds = _image_definitions().list_builds(
            request.user, get_organization_id(request), definition_id
        )
    except _IMAGE_RECIPE_ERRORS as exc:
        return _image_recipe_error(exc)
    return 200, [_build_job_to_list_out(b) for b in builds]


@image_definition_router.post(
    "/{definition_id}/runner-builds/",
    response={202: ImageBuildJobOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Assign runner and activate image definition",
)
async def create_image_definition_runner_build(
    request: HttpRequest, definition_id: uuid.UUID, payload: ImageBuildJobCreateIn
):
    if not check_api_key_permission(
        request, APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS
    ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS)
    try:
        build = await _image_definitions().create_build(
            request.user,
            get_organization_id(request),
            definition_id,
            payload.runner_id,
            activate=payload.activate,
            message=payload.message,
        )
    except _IMAGE_RECIPE_ERRORS as exc:
        return _image_recipe_error(exc)
    return 202, _build_job_to_out(build)


@image_definition_router.patch(
    "/{definition_id}/runner-builds/{runner_id}/",
    response={200: ImageBuildJobOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Update runner build status",
)
async def update_image_definition_runner_build(
    request: HttpRequest,
    definition_id: uuid.UUID,
    runner_id: uuid.UUID,
    payload: ImageBuildJobUpdateIn,
):
    if not check_api_key_permission(
        request, APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS
    ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS)
    try:
        build = await _image_definitions().update_build(
            request.user,
            get_organization_id(request),
            definition_id,
            runner_id,
            payload.action,
            message=payload.message,
        )
    except _IMAGE_RECIPE_ERRORS as exc:
        return _image_recipe_error(exc)
    return 200, _build_job_to_out(build)


@image_definition_router.get(
    "/{definition_id}/runner-builds/{runner_id}/log/",
    response={200: dict, 404: ErrorOut, 403: ErrorOut},
    summary="Get runner build log",
)
def get_image_definition_runner_build_log(
    request: HttpRequest, definition_id: uuid.UUID, runner_id: uuid.UUID
):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_READ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_READ)
    try:
        build_log = _image_definitions().build_log(
            request.user, get_organization_id(request), definition_id, runner_id
        )
    except _IMAGE_RECIPE_ERRORS as exc:
        return _image_recipe_error(exc)
    return 200, {"build_log": build_log}


@runner_router.get(
    "/{runner_id}/storage/",
    response={200: RunnerStorageOut, 403: ErrorOut, 404: ErrorOut},
)
def runner_storage(request: HttpRequest, runner_id: uuid.UUID):
    """Actual cached runner storage graph, including freshness and unknown sizes."""
    from .services.storage import StorageService

    if not check_api_key_permission(request, APIKeyPermission.RUNNERS_READ):
        return _perm_denied(APIKeyPermission.RUNNERS_READ)
    try:
        return StorageService().detail(
            request.user, get_organization_id(request), runner_id
        )
    except AuthenticationError as exc:
        return 403, ErrorOut(detail=exc.message, code=exc.code)
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)


@runner_router.post(
    "/{runner_id}/storage/refresh/",
    response={202: dict, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
)
async def refresh_runner_storage(request: HttpRequest, runner_id: uuid.UUID):
    """Explicitly request a full inventory scan; cached GET never dispatches."""
    from .services.storage import StorageService

    if not check_api_key_permission(request, APIKeyPermission.RUNNERS_READ):
        return _perm_denied(APIKeyPermission.RUNNERS_READ)
    try:
        result = await StorageService().refresh(
            _get_service(), request.user, get_organization_id(request), runner_id
        )
        return 202, result
    except AuthenticationError as exc:
        return 403, ErrorOut(detail=exc.message, code=exc.code)
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    except RunnerOfflineError as exc:
        return 409, ErrorOut(detail=str(exc), code="runner_offline")


@runner_router.get(
    "/operations/{operation_id}/", response={200: dict, 403: ErrorOut, 404: ErrorOut}
)
async def inspect_lifecycle_operation(request: HttpRequest, operation_id: uuid.UUID):
    """Read fresh journal evidence, or explicit unknown when offline."""
    from .services.disposition import DispositionService

    if not check_api_key_permission(request, APIKeyPermission.RUNNERS_READ):
        return _perm_denied(APIKeyPermission.RUNNERS_READ)
    try:
        return await DispositionService().inspect(
            _get_service(), request.user, get_organization_id(request), operation_id
        )
    except AuthenticationError as exc:
        return 403, ErrorOut(detail=exc.message, code=exc.code)
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)


@runner_router.post(
    "/operations/{operation_id}/{action}/",
    response={200: dict, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
)
async def dispose_lifecycle_operation(
    request: HttpRequest, operation_id: uuid.UUID, action: str
):
    """Reconcile, retry safe known failure, or admin acknowledge interruption."""
    from .services.disposition import DispositionService

    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_UPDATE):
        return _perm_denied(APIKeyPermission.WORKSPACES_UPDATE)
    try:
        if action == "retry":
            permission = await sync_to_async(DispositionService.retry_permission)(
                request.user, get_organization_id(request), operation_id
            )
            if permission is None or not check_api_key_permission(request, permission):
                return 403, ErrorOut(
                    detail="Operation-specific lifecycle scope required",
                    code="forbidden",
                )
        return await DispositionService().act(
            _get_service(),
            request.user,
            get_organization_id(request),
            operation_id,
            action,
        )
    except AuthenticationError as exc:
        return 403, ErrorOut(detail=exc.message, code=exc.code)
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    except ConflictError as exc:
        return 409, ErrorOut(detail=exc.message, code=exc.code)
