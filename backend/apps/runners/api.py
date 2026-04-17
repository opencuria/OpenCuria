"""
REST API endpoints for the runners app.

Thin adapter layer — validates input, delegates to RunnerService,
and formats responses. No business logic here.

All endpoints are protected by JWT auth (set globally on NinjaAPI).
Organization context is passed via X-Organization-Id header.

Split into separate routers to avoid URL path conflicts:
- runner_router    → /api/v1/runners/
- workspace_router → /api/v1/workspaces/
- agent_router     → /api/v1/agents/
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.core.exceptions import ObjectDoesNotExist, SynchronousOnlyOperation
from django.http import HttpRequest
from django.db.models import Q
from django.utils import timezone
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.credentials.services import CredentialSvc
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError
from common.utils import generate_api_token, hash_token

from .enums import RunnerStatus as RS, SessionStatus, WorkspaceStatus
from .exceptions import RunnerOfflineError
from .repositories import RunnerRepository, RunnerSystemMetricsRepository
from .schemas import (
    AgentCommandIn,
    AgentCommandOut,
    AgentCredentialRelationCommandIn,
    AgentCredentialRelationCommandOut,
    AgentCredentialRelationCreateIn,
    AgentCredentialRelationOut,
    AgentCredentialRelationUpdateIn,
    AgentOut,
    ChatCreateIn,
    ChatOut,
    ChatRenameIn,
    ConversationOut,
    ErrorOut,
    LastSessionOut,
    MarkConversationReadIn,
    MarkConversationUnreadIn,
    OrgAgentActivationToggleIn,
    OrgAgentDefinitionCreateIn,
    OrgAgentDefinitionDuplicateIn,
    OrgAgentDefinitionOut,
    OrgAgentDefinitionUpdateIn,
    ImageArtifactCreateIn,
    ImageArtifactCreateOut,
    ImageArtifactOut,
    ImageArtifactUpdateIn,
    ImageDefinitionCreateIn,
    ImageDefinitionDuplicateIn,
    ImageDefinitionOut,
    ImageDefinitionUpdateIn,
    PromptIn,
    PromptOut,
    RunnerCreateIn,
    RunnerCreateOut,
    RunnerOut,
    RunnerUpdateIn,
    RunnerSystemMetricsOut,
    SessionOut,
    SessionSkillOut,
    RunnerImageBuildCreateIn,
    RunnerImageBuildOut,
    RunnerImageBuildUpdateIn,
    TaskOut,
    TerminalStartIn,
    WorkspaceFromImageArtifactIn,
    WorkspaceFromImageArtifactOut,
    TerminalStartOut,
    DesktopStartOut,
    DesktopStopOut,
    DesktopStatusOut,
    DesktopClipboardWriteIn,
    DesktopClipboardReadOut,
    WorkspaceCreateIn,
    WorkspaceCreateOut,
    WorkspaceOut,
    WorkspaceUpdateIn,
    WorkspaceUpdateOut,
)


def _perm_denied(permission: APIKeyPermission):
    """Return a 403 error tuple for a denied API key permission."""
    return 403, ErrorOut(
        detail=f"API key lacks permission: {permission.value}",
        code="permission_denied",
    )


def _workspace_credential_ids(workspace) -> list[uuid.UUID]:
    """Return attached credential IDs for a workspace."""
    return [credential.id for credential in workspace.credentials.all()]


def _workspace_to_out(workspace) -> WorkspaceOut:
    """Map a Workspace ORM instance to WorkspaceOut."""
    # Determine runner online status — prefer cached attribute injected by the
    # queryset annotation, fall back to FK traversal.
    runner_online: bool = False
    if hasattr(workspace, "runner_is_online"):
        runner_online = bool(workspace.runner_is_online)
    elif hasattr(workspace, "runner") and workspace.runner is not None:
        from .enums import RunnerStatus as RS
        runner_online = workspace.runner.status == RS.ONLINE

    auto_stop_timeout_minutes = None
    auto_stop_at = None
    if (
        hasattr(workspace, "runner")
        and workspace.runner is not None
        and getattr(workspace.runner, "organization", None) is not None
    ):
        auto_stop_timeout_minutes = (
            workspace.runner.organization.workspace_auto_stop_timeout_minutes
        )
    if (
        auto_stop_timeout_minutes
        and workspace.status == WorkspaceStatus.RUNNING
        and workspace.last_activity_at is not None
    ):
        auto_stop_at = workspace.last_activity_at + timedelta(
            minutes=auto_stop_timeout_minutes
        )

    return WorkspaceOut(
        id=workspace.id,
        runner_id=workspace.runner_id,
        status=workspace.status,
        active_operation=workspace.active_operation,
        name=workspace.name,
        runtime_type=workspace.runtime_type,
        qemu_vcpus=workspace.qemu_vcpus,
        qemu_memory_mb=workspace.qemu_memory_mb,
        qemu_disk_size_gb=workspace.qemu_disk_size_gb,
        created_by_id=workspace.created_by_id,
        last_activity_at=workspace.last_activity_at,
        auto_stop_timeout_minutes=auto_stop_timeout_minutes,
        auto_stop_at=auto_stop_at,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
        has_active_session=bool(getattr(workspace, "has_active_session", False)),
        runner_online=runner_online,
        credential_ids=_workspace_credential_ids(workspace),
    )


def _get_service():
    """Get the RunnerService singleton (lazy import to avoid circular deps)."""
    from .sio_server import get_runner_service

    return get_runner_service()


def _get_org_id(request: HttpRequest) -> uuid.UUID:
    """Extract the organization ID from the X-Organization-Id header."""
    org_id_str = request.headers.get("X-Organization-Id")
    if not org_id_str:
        raise AuthenticationError("X-Organization-Id header is required")
    try:
        return uuid.UUID(org_id_str)
    except ValueError:
        raise AuthenticationError("Invalid X-Organization-Id header")


def _get_org_service() -> OrganizationService:
    return OrganizationService()


def _org_copy_name(base_name: str, org_id: uuid.UUID) -> str:
    """Return a unique copy name scoped to an organization."""
    from .models import AgentDefinition

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

    index = 2
    while True:
        suffix = f" (Copy {index})"
        candidate = f"{base[: 64 - len(suffix)]}{suffix}"
        if not AgentDefinition.objects.filter(organization_id=org_id, name=candidate).exists():
            return candidate
        index += 1


def _org_image_definition_copy_name(base_name: str, org_id: uuid.UUID) -> str:
    """Return a unique image definition copy name scoped to an organization."""
    from .models import ImageDefinition

    base = (base_name or "").strip() or "image"
    if len(base) > 255:
        base = base[:255]

    candidate = base
    if not ImageDefinition.objects.filter(organization_id=org_id, name=candidate).exists():
        return candidate

    suffix = " (Copy)"
    candidate = f"{base[: 255 - len(suffix)]}{suffix}"
    if not ImageDefinition.objects.filter(organization_id=org_id, name=candidate).exists():
        return candidate

    index = 2
    while True:
        suffix = f" (Copy {index})"
        candidate = f"{base[: 255 - len(suffix)]}{suffix}"
        if not ImageDefinition.objects.filter(organization_id=org_id, name=candidate).exists():
            return candidate
        index += 1


def _validate_image_definition_runtime(runtime_type: str, base_distro: str):
    """Validate runtime-specific image definition constraints."""
    if (runtime_type or "").strip().lower() == "qemu":
        if not (base_distro or "").strip().lower().startswith("ubuntu:"):
            return 409, ErrorOut(
                detail=(
                    "QEMU image definitions currently require an ubuntu:<version> "
                    "base distro"
                ),
                code="unsupported_base_distro",
            )
    return None


async def _get_org_admin_flag_async(request: HttpRequest, org_id: uuid.UUID) -> bool:
    """Validate membership and return whether the user is an org admin."""
    org_service = _get_org_service()
    await sync_to_async(org_service.require_membership)(request.user, org_id)
    role = await sync_to_async(org_service.get_user_role)(request.user, org_id)
    return role == "admin"


async def _require_org_membership_async(request: HttpRequest, org_id: uuid.UUID) -> None:
    """Validate organization membership for async endpoints."""
    org_service = _get_org_service()
    await sync_to_async(org_service.require_membership)(request.user, org_id)


def _is_org_admin(user, org_id: uuid.UUID) -> bool:
    """Return whether user is organization admin (membership required)."""
    org_service = _get_org_service()
    org_service.require_membership(user, org_id)
    return org_service.get_user_role(user, org_id) == "admin"


def _get_owned_workspace(request: HttpRequest, org_id: uuid.UUID, workspace_id: uuid.UUID):
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


async def _get_owned_workspace_artifact_async(
    request: HttpRequest,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    image_artifact_id: uuid.UUID,
):
    """Return a workspace-scoped image artifact only for the workspace owner."""
    service = _get_service()
    workspace = await _get_owned_workspace_async(request, org_id, workspace_id)
    artifact = await sync_to_async(service.image_artifacts.get_by_id)(image_artifact_id)
    if artifact is None:
        raise NotFoundError("ImageArtifact", str(image_artifact_id))
    if artifact.source_workspace_id != workspace.id:
        raise NotFoundError("ImageArtifact", str(image_artifact_id))
    if artifact.created_by_id != request.user.id:
        raise NotFoundError("ImageArtifact", str(image_artifact_id))
    return workspace, artifact


def _get_image_definition_for_org(org_id: uuid.UUID, definition_id: uuid.UUID):
    """Return a visible image definition for an organization."""
    from .models import ImageDefinition

    return ImageDefinition.objects.filter(id=definition_id).filter(
        Q(organization__isnull=True) | Q(organization_id=org_id)
    ).first()


def _get_runner_image_build_for_org(
    org_id: uuid.UUID,
    definition_id: uuid.UUID,
    runner_id: uuid.UUID,
):
    """Return a runner image build scoped to an organization."""
    from .models import RunnerImageBuild

    return (
        RunnerImageBuild.objects.filter(
            image_definition_id=definition_id,
            runner_id=runner_id,
            runner__organization_id=org_id,
        )
        .filter(
            Q(image_definition__organization_id=org_id)
            | Q(image_definition__organization__isnull=True)
        )
        .select_related("image_definition", "runner", "build_task")
        .first()
    )


def _session_to_out(session) -> SessionOut:
    """Map a Session ORM instance to SessionOut, including skill snapshots."""
    skills = [
        SessionSkillOut(
            id=ss.id,
            skill_id=ss.skill_id,
            name=ss.name,
            body=ss.body,
            created_at=ss.created_at,
        )
        for ss in session.session_skills.all()
    ]
    return SessionOut(
        id=session.id,
        chat_id=session.chat_id,
        prompt=session.prompt,
        agent_model=session.agent_model,
        agent_options=session.agent_options,
        output=session.output,
        error_message=session.error_message,
        status=session.status,
        read_at=session.read_at,
        created_at=session.created_at,
        completed_at=session.completed_at,
        skills=skills,
    )


# ===========================================================================
# Runner Router — /api/v1/runners/
# ===========================================================================

runner_router = Router(tags=["runners"])


@runner_router.get("/", response={200: list[RunnerOut], 403: ErrorOut}, summary="List runners")
def list_runners(request: HttpRequest):
    """Return all runners for the user's active organization."""
    if not check_api_key_permission(request, APIKeyPermission.RUNNERS_READ):
        return _perm_denied(APIKeyPermission.RUNNERS_READ)
    org_id = _get_org_id(request)
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
    org_id = _get_org_id(request)
    org_service = _get_org_service()

    try:
        org = org_service.require_admin(request.user, org_id)
    except AuthenticationError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)

    token = generate_api_token()
    token_hash = hash_token(token)

    runner = RunnerRepository.create(
        name=payload.name,
        api_token_hash=token_hash,
        organization=org,
    )

    return 201, RunnerCreateOut(
        id=runner.id,
        name=runner.name,
        api_token=token,
    )


@runner_router.get("/{runner_id}/", response={200: RunnerOut, 403: ErrorOut, 404: ErrorOut})
def get_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Return a runner by ID. User must be a member of the runner's organization."""
    if not check_api_key_permission(request, APIKeyPermission.RUNNERS_READ):
        return _perm_denied(APIKeyPermission.RUNNERS_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    try:
        runner = service.get_runner(runner_id)
        # Verify runner belongs to the user's active org
        if runner.organization_id != org_id:
            raise NotFoundError("Runner", str(runner_id))
        return 200, runner
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@runner_router.patch(
    "/{runner_id}/",
    response={200: RunnerOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Update runner QEMU settings",
)
def update_runner(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerUpdateIn):
    """Update per-runner QEMU resource defaults and limits (admin only)."""
    org_id = _get_org_id(request)
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
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    runner = RunnerRepository.get_by_id(runner_id)
    if runner is None or runner.organization_id != org_id:
        return 404, ErrorOut(detail="Runner not found", code="not_found")

    metrics = RunnerSystemMetricsRepository.get_latest(runner_id)
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
def get_runner_metrics_history(request: HttpRequest, runner_id: uuid.UUID, hours: int = 24):
    """Return historical system metrics for a runner (default: last 24h)."""
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    runner = RunnerRepository.get_by_id(runner_id)
    if runner is None or runner.organization_id != org_id:
        return 404, ErrorOut(detail="Runner not found", code="not_found")

    since = timezone.now() - timedelta(hours=hours)
    metrics = RunnerSystemMetricsRepository.get_history(runner_id, since)

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
        ) for m in metrics
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
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    workspaces = service.list_workspaces(
        runner_id=runner_id,
        organization_id=org_id,
        user=request.user,
    )
    return 200, [_workspace_to_out(workspace) for workspace in workspaces]


@workspace_router.post(
    "/",
    response={202: WorkspaceCreateOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Create a workspace",
)
async def create_workspace(request: HttpRequest, payload: WorkspaceCreateIn):
    """Create a workspace — dispatches async task to a runner."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_CREATE):
        return _perm_denied(APIKeyPermission.WORKSPACES_CREATE)
    org_id = _get_org_id(request)

    service = _get_service()
    try:
        if not payload.image_artifact_id:
            return 409, ErrorOut(
                detail="An image artifact must be selected to create a workspace",
                code="image_artifact_required",
            )
        # Resolve credential IDs to env_vars, files and ssh_keys
        credential_svc = CredentialSvc()
        resolved = await sync_to_async(credential_svc.resolve_credentials)(
            payload.credential_ids,
            org_id=org_id,
            user=request.user,
        )

        workspace, task = await service.create_workspace(
            name=payload.name,
            repos=payload.repos,
            runtime_type=payload.runtime_type,
            qemu_vcpus=payload.qemu_vcpus,
            qemu_memory_mb=payload.qemu_memory_mb,
            qemu_disk_size_gb=payload.qemu_disk_size_gb,
            env_vars=resolved.env_vars,
            files=resolved.files,
            ssh_keys=resolved.ssh_keys,
            credentials=resolved.credentials,
            runner_id=payload.runner_id,
            image_artifact_id=payload.image_artifact_id,
            user=request.user,
            organization_id=org_id,
        )
        return 202, WorkspaceCreateOut(
            workspace_id=workspace.id,
            task_id=task.id,
            status=workspace.status,
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except RuntimeError as e:
        return 409, ErrorOut(detail=str(e), code="runner_call_failed")


@workspace_router.get(
    "/{workspace_id}/",
    response={200: WorkspaceOut, 403: ErrorOut, 404: ErrorOut},
)
def get_workspace(request: HttpRequest, workspace_id: uuid.UUID):
    """Return workspace details without chat session history."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_READ):
        return _perm_denied(APIKeyPermission.WORKSPACES_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    try:
        workspace = _get_owned_workspace(request, org_id, workspace_id)
        from .enums import RunnerStatus as RS
        from .models import Session
        return 200, WorkspaceOut(
            id=workspace.id,
            runner_id=workspace.runner_id,
            status=workspace.status,
            active_operation=workspace.active_operation,
            name=workspace.name,
            runtime_type=workspace.runtime_type,
            qemu_vcpus=workspace.qemu_vcpus,
            qemu_memory_mb=workspace.qemu_memory_mb,
            qemu_disk_size_gb=workspace.qemu_disk_size_gb,
            created_by_id=workspace.created_by_id,
            last_activity_at=workspace.last_activity_at,
            auto_stop_timeout_minutes=workspace.runner.organization.workspace_auto_stop_timeout_minutes,
            auto_stop_at=(
                workspace.last_activity_at
                + timedelta(
                    minutes=workspace.runner.organization.workspace_auto_stop_timeout_minutes
                )
                if (
                    workspace.status == WorkspaceStatus.RUNNING
                    and workspace.last_activity_at is not None
                    and workspace.runner.organization.workspace_auto_stop_timeout_minutes
                )
                else None
            ),
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
            has_active_session=Session.objects.filter(
                chat__workspace_id=workspace_id,
                status__in=(SessionStatus.PENDING, SessionStatus.RUNNING),
            ).exists(),
            runner_online=workspace.runner.status == RS.ONLINE,
            credential_ids=_workspace_credential_ids(workspace),
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/prompt/",
    response={202: PromptOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Run a prompt",
)
async def run_prompt(request: HttpRequest, workspace_id: uuid.UUID, payload: PromptIn):
    """Run a prompt in a workspace — dispatches async task to the runner."""
    if not check_api_key_permission(request, APIKeyPermission.PROMPTS_RUN):
        return _perm_denied(APIKeyPermission.PROMPTS_RUN)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)

        session, task, chat = await service.run_prompt(
            workspace_id,
            payload.prompt,
            payload.agent_model,
            agent_options=payload.agent_options or {},
            chat_id=payload.chat_id,
            skill_ids=payload.skill_ids or [],
        )
        return 202, PromptOut(
            session_id=session.id,
            task_id=task.id,
            chat_id=chat.id,
            status=session.status,
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/sessions/{session_id}/cancel/",
    response={202: TaskOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Cancel a running session prompt",
)
async def cancel_session_prompt(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
):
    """Cancel a running prompt session without stopping the workspace."""
    if not check_api_key_permission(request, APIKeyPermission.PROMPTS_CANCEL):
        return _perm_denied(APIKeyPermission.PROMPTS_CANCEL)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)

        task = await service.cancel_session_prompt(workspace_id, session_id)
        return 202, task
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


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
    """Start an interactive PTY terminal in a workspace container."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = _get_org_id(request)
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
):
    """Start a KasmVNC desktop session in a workspace container."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = _get_org_id(request)
    is_admin = await _get_org_admin_flag_async(request, org_id)

    service = _get_service()
    try:
        workspace = await sync_to_async(service.get_workspace)(workspace_id)
        if workspace.runner.organization_id != org_id:
            raise NotFoundError("Workspace", str(workspace_id))
        if not is_admin and workspace.created_by_id != request.user.id:
            raise NotFoundError("Workspace", str(workspace_id))

        task = await service.start_desktop(workspace_id)
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
):
    """Stop the desktop session in a workspace container."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = _get_org_id(request)
    is_admin = await _get_org_admin_flag_async(request, org_id)

    service = _get_service()
    try:
        workspace = await sync_to_async(service.get_workspace)(workspace_id)
        if workspace.runner.organization_id != org_id:
            raise NotFoundError("Workspace", str(workspace_id))
        if not is_admin and workspace.created_by_id != request.user.id:
            raise NotFoundError("Workspace", str(workspace_id))

        task = await service.stop_desktop(workspace_id)
        return 202, DesktopStopOut(task_id=task.id)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.get(
    "/{workspace_id}/desktop/status/",
    response={200: DesktopStatusOut, 403: ErrorOut, 404: ErrorOut},
    summary="Get desktop session status",
)
async def desktop_status(
    request: HttpRequest,
    workspace_id: uuid.UUID,
):
    """Check whether a desktop session is active for the workspace."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = _get_org_id(request)
    is_admin = await _get_org_admin_flag_async(request, org_id)

    service = _get_service()
    try:
        workspace = await sync_to_async(service.get_workspace)(workspace_id)
        if workspace.runner.organization_id != org_id:
            raise NotFoundError("Workspace", str(workspace_id))
        if not is_admin and workspace.created_by_id != request.user.id:
            raise NotFoundError("Workspace", str(workspace_id))

        desktop_info = await sync_to_async(service.get_desktop_info)(str(workspace_id))
        is_active = desktop_info is not None
        proxy_url = f"/ws/desktop/{workspace_id}/" if is_active else None
        return 200, DesktopStatusOut(active=is_active, proxy_url=proxy_url)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/desktop/clipboard/write/",
    response={200: DesktopClipboardReadOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
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
    org_id = _get_org_id(request)
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
    response={200: DesktopClipboardReadOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Read text from desktop clipboard",
)
async def desktop_clipboard_read(
    request: HttpRequest,
    workspace_id: uuid.UUID,
):
    """Read plain text from the VM desktop clipboard for local copy."""
    if not check_api_key_permission(request, APIKeyPermission.TERMINAL_ACCESS):
        return _perm_denied(APIKeyPermission.TERMINAL_ACCESS)
    org_id = _get_org_id(request)
    is_admin = await _get_org_admin_flag_async(request, org_id)

    service = _get_service()
    try:
        workspace = await sync_to_async(service.get_workspace)(workspace_id)
        if workspace.runner.organization_id != org_id:
            raise NotFoundError("Workspace", str(workspace_id))
        if not is_admin and workspace.created_by_id != request.user.id:
            raise NotFoundError("Workspace", str(workspace_id))

        text = await service.read_desktop_clipboard(workspace_id)
        return 200, DesktopClipboardReadOut(text=text)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@workspace_router.patch(
    "/{workspace_id}/",
    response={200: WorkspaceUpdateOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Update a workspace",
)
async def update_workspace(request: HttpRequest, workspace_id: uuid.UUID, payload: WorkspaceUpdateIn):
    """Update mutable workspace metadata."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_UPDATE):
        return _perm_denied(APIKeyPermission.WORKSPACES_UPDATE)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)

        resolved_credentials = None
        if payload.credential_ids is not None:
            credential_svc = CredentialSvc()
            resolved_credentials = await sync_to_async(credential_svc.resolve_credentials)(
                payload.credential_ids,
                org_id=org_id,
                user=request.user,
            )

        workspace = await service.update_workspace(
            workspace_id,
            name=payload.name,
            credentials=(resolved_credentials.credentials if resolved_credentials is not None else None),
            qemu_vcpus=payload.qemu_vcpus,
            qemu_memory_mb=payload.qemu_memory_mb,
            qemu_disk_size_gb=payload.qemu_disk_size_gb,
        )
        return 200, WorkspaceUpdateOut(
            id=workspace.id,
            name=workspace.name,
            updated_at=workspace.updated_at,
            active_operation=workspace.active_operation,
            credential_ids=_workspace_credential_ids(workspace),
            qemu_vcpus=workspace.qemu_vcpus,
            qemu_memory_mb=workspace.qemu_memory_mb,
            qemu_disk_size_gb=workspace.qemu_disk_size_gb,
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
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
    org_id = _get_org_id(request)
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
    org_id = _get_org_id(request)
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
    org_id = _get_org_id(request)
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


@workspace_router.get(
    "/{workspace_id}/sessions/",
    response={200: list[SessionOut], 403: ErrorOut, 404: ErrorOut},
    summary="List sessions for a workspace",
)
def list_sessions(request: HttpRequest, workspace_id: uuid.UUID):
    """Return all sessions for a workspace."""
    if not check_api_key_permission(request, APIKeyPermission.CONVERSATIONS_READ):
        return _perm_denied(APIKeyPermission.CONVERSATIONS_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    try:
        _get_owned_workspace(request, org_id, workspace_id)
        service = _get_service()
        sessions = service.list_sessions(workspace_id)
        return [_session_to_out(s) for s in sessions]
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


# --- Chat endpoints ---


@workspace_router.get(
    "/{workspace_id}/chats/",
    response={200: list[ChatOut], 403: ErrorOut, 404: ErrorOut},
    summary="List chats for a workspace",
)
def list_chats(request: HttpRequest, workspace_id: uuid.UUID):
    """Return all chats for a workspace with session counts."""
    if not check_api_key_permission(request, APIKeyPermission.CONVERSATIONS_READ):
        return _perm_denied(APIKeyPermission.CONVERSATIONS_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    from django.db.models import Count

    try:
        _get_owned_workspace(request, org_id, workspace_id)
        from .models import Chat as ChatModel

        annotated = ChatModel.objects.filter(
            workspace_id=workspace_id,
        ).annotate(
            _session_count=Count("sessions"),
        ).order_by("-created_at")

        return [
            ChatOut(
                id=c.id,
                workspace_id=c.workspace_id,
                name=c.name,
                agent_definition_id=c.agent_definition_id,
                agent_type=c.agent_type,
                created_at=c.created_at,
                updated_at=c.updated_at,
                session_count=c._session_count,
            )
            for c in annotated
        ]
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@workspace_router.post(
    "/{workspace_id}/chats/",
    response={201: ChatOut, 400: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Create a chat",
)
def create_chat(request: HttpRequest, workspace_id: uuid.UUID, payload: ChatCreateIn):
    """Create a new chat within a workspace."""
    if not check_api_key_permission(request, APIKeyPermission.PROMPTS_RUN):
        return _perm_denied(APIKeyPermission.PROMPTS_RUN)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    try:
        _get_owned_workspace(request, org_id, workspace_id)

        chat = service.create_chat(
            workspace_id,
            payload.name,
            agent_definition_id=payload.agent_definition_id,
        )
        return 201, ChatOut(
            id=chat.id,
            workspace_id=chat.workspace_id,
            name=chat.name,
            agent_definition_id=chat.agent_definition_id,
            agent_type=chat.agent_type,
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            session_count=0,
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@workspace_router.patch(
    "/{workspace_id}/chats/{chat_id}/",
    response={200: ChatOut, 400: ErrorOut, 404: ErrorOut},
    summary="Rename a chat",
)
def rename_chat(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    chat_id: uuid.UUID,
    payload: ChatRenameIn,
):
    """Rename an existing chat."""
    if not check_api_key_permission(request, APIKeyPermission.PROMPTS_RUN):
        return _perm_denied(APIKeyPermission.PROMPTS_RUN)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    try:
        _get_owned_workspace(request, org_id, workspace_id)

        chat = service.rename_chat(chat_id, payload.name)
        session_count = chat.sessions.count()
        return 200, ChatOut(
            id=chat.id,
            workspace_id=chat.workspace_id,
            name=chat.name,
            agent_definition_id=chat.agent_definition_id,
            agent_type=chat.agent_type,
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            session_count=session_count,
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@workspace_router.delete(
    "/{workspace_id}/chats/{chat_id}/",
    response={204: None, 404: ErrorOut},
    summary="Delete a chat",
)
def delete_chat(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    chat_id: uuid.UUID,
):
    """Delete a chat and all its sessions."""
    if not check_api_key_permission(request, APIKeyPermission.PROMPTS_RUN):
        return _perm_denied(APIKeyPermission.PROMPTS_RUN)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    try:
        _get_owned_workspace(request, org_id, workspace_id)

        service.delete_chat(chat_id)
        return 204, None
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@workspace_router.get(
    "/{workspace_id}/chats/{chat_id}/sessions/",
    response={200: list[SessionOut], 403: ErrorOut, 404: ErrorOut},
    summary="List sessions for a chat",
)
def list_chat_sessions(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    chat_id: uuid.UUID,
):
    """Return all sessions for a specific chat."""
    if not check_api_key_permission(request, APIKeyPermission.CONVERSATIONS_READ):
        return _perm_denied(APIKeyPermission.CONVERSATIONS_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    try:
        _get_owned_workspace(request, org_id, workspace_id)
        service = _get_service()
        sessions = service.list_chat_sessions(chat_id)
        return [_session_to_out(s) for s in sessions]
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


# ===========================================================================
# Agent Router — /api/v1/agents/
# ===========================================================================

agent_router = Router(tags=["agents"])


@agent_router.get(
    "/",
    response={200: list[AgentOut], 403: ErrorOut, 404: ErrorOut},
    summary="List available agents",
)
def list_agents(request: HttpRequest, workspace_id: uuid.UUID | None = None):
    """Return agent definitions with org- or workspace-specific availability."""
    if not check_api_key_permission(request, APIKeyPermission.AGENTS_READ):
        return 403, ErrorOut(detail="API key lacks permission: agents:read", code="permission_denied")
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    workspace = None
    if workspace_id is not None:
        try:
            workspace = _get_owned_workspace(request, org_id, workspace_id)
        except NotFoundError as e:
            return 404, ErrorOut(detail=e.message, code=e.code)

    agents = service.get_available_agents(
        organization_id=org_id,
        user=request.user,
        workspace=workspace,
    )
    return 200, [
        AgentOut(
            id=a["id"],
            name=a["name"],
            description=a["description"],
            available_options=a["available_options"],
            supports_multi_chat=a["supports_multi_chat"],
            has_online_runner=a["has_online_runner"],
            required_credential_service_slugs=a["required_credential_service_slugs"],
            has_credentials=a["has_credentials"],
        )
        for a in agents
    ]


# ===========================================================================
# Conversation Router — /api/v1/conversations/
# ===========================================================================

conversation_router = Router(tags=["conversations"])


@conversation_router.get(
    "/", response={200: list[ConversationOut], 403: ErrorOut}, summary="List conversations"
)
def list_conversations(request: HttpRequest):
    """
    Return all conversations for the user, sorted by last activity DESC.

    Each Chat becomes one entry; workspaces without any chats appear as
    fallback entries. Visibility is always limited to the current user's workspaces.
    """
    if not check_api_key_permission(request, APIKeyPermission.CONVERSATIONS_READ):
        return 403, ErrorOut(detail="API key lacks permission: conversations:read", code="permission_denied")
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    from .repositories import ConversationRepository

    rows = ConversationRepository.list_for_user(org_id, request.user.id)
    return [
        ConversationOut(
            chat_id=r["chat_id"],
            workspace_id=r["workspace_id"],
            workspace_name=r["workspace_name"],
            workspace_status=r["workspace_status"],
            agent_definition_id=r["agent_definition_id"],
            agent_type=r["agent_type"],
            chat_name=r["chat_name"],
            last_session=(
                LastSessionOut(
                    id=r["last_session"]["id"],
                    prompt=r["last_session"]["prompt"],
                    status=r["last_session"]["status"],
                    created_at=r["last_session"]["created_at"],
                )
                if r["last_session"]
                else None
            ),
            session_count=r["session_count"],
            updated_at=r["updated_at"],
            is_read=r["is_read"],
        )
        for r in rows
    ]


@conversation_router.post(
    "/read/",
    response={204: None, 403: ErrorOut},
    summary="Mark a conversation as read",
)
def mark_conversation_read(request: HttpRequest, payload: MarkConversationReadIn):
    """
    Mark a conversation as read by setting the session read timestamp.

    Called by the frontend when the user opens a conversation. Only sessions
    with status COMPLETED or FAILED are updated; running sessions remain unread.
    The next call to GET /conversations/ will reflect ``is_read=true``.
    """
    if not check_api_key_permission(request, APIKeyPermission.CONVERSATIONS_READ):
        return _perm_denied(APIKeyPermission.CONVERSATIONS_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    service.mark_conversation_read(
        session_id=payload.session_id,
    )
    return 204, None


@conversation_router.post(
    "/unread/",
    response={204: None, 403: ErrorOut},
    summary="Mark a conversation as unread",
)
def mark_conversation_unread(request: HttpRequest, payload: MarkConversationUnreadIn):
    """
    Mark a conversation as unread by clearing the session read timestamp.

    Called by the frontend when the user explicitly wants a completed or failed
    reply to surface as unread again. Running sessions remain unchanged.
    """
    if not check_api_key_permission(request, APIKeyPermission.CONVERSATIONS_READ):
        return _perm_denied(APIKeyPermission.CONVERSATIONS_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    service.mark_conversation_unread(
        session_id=payload.session_id,
    )
    return 204, None


# ===========================================================================
# Image artifact routers
# ===========================================================================

workspace_image_artifact_router = Router(tags=["image-artifacts"])
image_artifact_router = Router(tags=["image-artifacts"])


def _image_artifact_to_out(artifact) -> ImageArtifactOut:
    """Map an ImageArtifact ORM instance to ImageArtifactOut."""
    runner_build = getattr(artifact, "runner_image_build", None)
    source_workspace = getattr(artifact, "source_workspace", None)
    runtime_type = getattr(source_workspace, "runtime_type", None)
    source_runner_id = getattr(source_workspace, "runner_id", None)
    source_runner_online = False
    workspace_runner = getattr(source_workspace, "runner", None)
    if workspace_runner is not None:
        source_runner_online = workspace_runner.status == RS.ONLINE
    source_definition_name = None
    is_deactivated = False
    if runner_build is not None:
        source_runner_id = getattr(runner_build, "runner_id", source_runner_id)
        runner = getattr(runner_build, "runner", None)
        if runner is not None:
            source_runner_online = runner.status == RS.ONLINE
        image_definition = getattr(runner_build, "image_definition", None)
        if image_definition is not None:
            source_definition_name = image_definition.name
            runtime_type = image_definition.runtime_type
        is_deactivated = getattr(runner_build, "status", "") == "deactivated"

    return ImageArtifactOut(
        id=artifact.id,
        source_workspace_id=artifact.source_workspace_id,
        runner_artifact_id=artifact.runner_artifact_id,
        name=artifact.name,
        size_bytes=artifact.size_bytes,
        status=artifact.status,
        artifact_kind=artifact.artifact_kind,
        runner_image_build_id=artifact.runner_image_build_id,
        source_definition_name=source_definition_name,
        source_runner_id=source_runner_id,
        runtime_type=runtime_type,
        is_deactivated=is_deactivated,
        source_runner_online=source_runner_online,
        created_at=artifact.created_at,
        created_by_id=artifact.created_by_id,
        credential_ids=[c.id for c in artifact.credentials.all()],
    )


@image_artifact_router.get(
    "/",
    response=list[ImageArtifactOut],
    summary="List image artifacts for the current user",
)
def list_image_artifacts(request: HttpRequest):
    """Return all image artifacts owned by the current user in the active organization."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_READ):
        return _perm_denied(APIKeyPermission.IMAGES_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    service = _get_service()
    service.image_artifacts.timeout_stale(timeout_hours=1)
    artifacts = service.list_image_artifacts_for_user(user=request.user)
    return [_image_artifact_to_out(artifact) for artifact in artifacts]


@image_artifact_router.post(
    "/",
    response={202: ImageArtifactCreateOut, 403: ErrorOut, 404: ErrorOut},
    summary="Create an image artifact from a workspace",
)
async def create_image_artifact_global(
    request: HttpRequest, payload: ImageArtifactCreateIn
):
    """Create an image artifact from a workspace using the global endpoint."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_CREATE):
        return _perm_denied(APIKeyPermission.IMAGES_CREATE)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    if not payload.workspace_id:
        return 404, ErrorOut(detail="workspace_id is required", code="validation_error")

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, payload.workspace_id)
        workspace, task = await service.create_image_artifact(
            workspace_id=payload.workspace_id,
            name=payload.name,
            organization_id=org_id,
        )
        return 202, ImageArtifactCreateOut(task_id=task.id, workspace_id=workspace.id)
    except (NotFoundError, ValueError) as e:
        return 404, ErrorOut(detail=str(e), code="not_found")


@image_artifact_router.patch(
    "/{image_artifact_id}/",
    response={200: ImageArtifactOut, 404: ErrorOut, 403: ErrorOut},
    summary="Rename an image artifact",
)
async def rename_image_artifact(
    request: HttpRequest,
    image_artifact_id: uuid.UUID,
    payload: ImageArtifactUpdateIn,
):
    """Rename an image artifact owned by the current user."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_CREATE):
        return _perm_denied(APIKeyPermission.IMAGES_CREATE)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    artifact = await sync_to_async(service.image_artifacts.get_by_id)(image_artifact_id)
    if artifact is None:
        return 404, ErrorOut(detail="Image artifact not found", code="not_found")
    if artifact.created_by != request.user:
        return 403, ErrorOut(
            detail="Not authorized to rename this image artifact",
            code="forbidden",
        )

    await sync_to_async(service.image_artifacts.update_name)(
        image_artifact_id, payload.name.strip()
    )
    updated = await sync_to_async(service.image_artifacts.get_by_id)(image_artifact_id)
    return 200, _image_artifact_to_out(updated)


@image_artifact_router.delete(
    "/{image_artifact_id}/",
    response={204: None, 404: ErrorOut, 403: ErrorOut},
    summary="Delete an image artifact",
)
async def delete_image_artifact_global(
    request: HttpRequest, image_artifact_id: uuid.UUID
):
    """Delete an image artifact owned by the current user."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_DELETE):
        return _perm_denied(APIKeyPermission.IMAGES_DELETE)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        artifact = await sync_to_async(service.image_artifacts.get_by_id)(image_artifact_id)
        if artifact is None:
            return 404, ErrorOut(detail="Image artifact not found", code="not_found")
        if artifact.created_by != request.user:
            return 403, ErrorOut(
                detail="Not authorized to delete this image artifact",
                code="forbidden",
            )
        await service.delete_image_artifact(image_artifact_id)
        return 204, None
    except ValueError as e:
        return 404, ErrorOut(detail=str(e), code="not_found")


@image_artifact_router.post(
    "/{image_artifact_id}/workspaces/",
    response={
        202: WorkspaceFromImageArtifactOut,
        404: ErrorOut,
        403: ErrorOut,
        409: ErrorOut,
    },
    summary="Create a workspace from an image artifact",
)
async def create_workspace_from_image_artifact_global(
    request: HttpRequest,
    image_artifact_id: uuid.UUID,
    payload: WorkspaceFromImageArtifactIn,
):
    """Create a workspace from an image artifact."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_CLONE):
        return _perm_denied(APIKeyPermission.IMAGES_CLONE)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        artifact = await sync_to_async(service.image_artifacts.get_by_id)(image_artifact_id)
        if artifact is None:
            return 404, ErrorOut(detail="Image artifact not found", code="not_found")
        if artifact.created_by != request.user:
            return 403, ErrorOut(
                detail="Not authorized to use this image artifact",
                code="forbidden",
            )

        workspace, task = await service.create_workspace_from_image_artifact(
            image_artifact_id=image_artifact_id,
            name=payload.name,
            user=request.user,
            organization_id=org_id,
        )
        return 202, WorkspaceFromImageArtifactOut(
            workspace_id=workspace.id,
            task_id=task.id,
            status=workspace.status,
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except RunnerOfflineError as e:
        return 409, ErrorOut(detail=str(e), code="runner_offline")
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 404, ErrorOut(detail=str(e), code="not_found")


@workspace_image_artifact_router.get(
    "/{workspace_id}/image-artifacts/",
    response={200: list[ImageArtifactOut], 404: ErrorOut},
    summary="List image artifacts for a workspace",
)
def list_workspace_image_artifacts(request: HttpRequest, workspace_id: uuid.UUID):
    """Return all image artifacts captured from a workspace."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_READ):
        return _perm_denied(APIKeyPermission.IMAGES_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    try:
        _get_owned_workspace(request, org_id, workspace_id)
        service = _get_service()
        artifacts = service.list_image_artifacts_for_workspace(workspace_id)
        return [_image_artifact_to_out(artifact) for artifact in artifacts]
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@workspace_image_artifact_router.post(
    "/{workspace_id}/image-artifacts/",
    response={202: ImageArtifactCreateOut, 404: ErrorOut},
    summary="Create an image artifact from a workspace",
)
async def create_workspace_image_artifact(
    request: HttpRequest, workspace_id: uuid.UUID, payload: ImageArtifactCreateIn
):
    """Create an image artifact from a workspace."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_CREATE):
        return _perm_denied(APIKeyPermission.IMAGES_CREATE)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_async(request, org_id, workspace_id)
        workspace, task = await service.create_image_artifact(
            workspace_id=workspace_id,
            name=payload.name,
            organization_id=org_id,
        )
        return 202, ImageArtifactCreateOut(task_id=task.id, workspace_id=workspace.id)
    except (NotFoundError, ValueError) as e:
        return 404, ErrorOut(detail=str(e), code="not_found")


@workspace_image_artifact_router.delete(
    "/{workspace_id}/image-artifacts/{image_artifact_id}/",
    response={204: None, 404: ErrorOut},
    summary="Delete an image artifact",
)
async def delete_workspace_image_artifact(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    image_artifact_id: uuid.UUID,
):
    """Delete an image artifact."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_DELETE):
        return _perm_denied(APIKeyPermission.IMAGES_DELETE)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_artifact_async(
            request,
            org_id,
            workspace_id,
            image_artifact_id,
        )
        await service.delete_image_artifact(image_artifact_id)
        return 204, None
    except (NotFoundError, ValueError) as e:
        detail = e.message if isinstance(e, NotFoundError) else str(e)
        return 404, ErrorOut(detail=detail, code="not_found")


@workspace_image_artifact_router.post(
    "/{workspace_id}/image-artifacts/{image_artifact_id}/workspaces/",
    response={202: WorkspaceFromImageArtifactOut, 404: ErrorOut, 409: ErrorOut},
    summary="Create a workspace from an image artifact",
)
async def create_workspace_from_workspace_image_artifact(
    request: HttpRequest,
    workspace_id: uuid.UUID,
    image_artifact_id: uuid.UUID,
    payload: WorkspaceFromImageArtifactIn,
):
    """Create a workspace from an image artifact."""
    if not check_api_key_permission(request, APIKeyPermission.IMAGES_CLONE):
        return _perm_denied(APIKeyPermission.IMAGES_CLONE)
    org_id = _get_org_id(request)
    await _require_org_membership_async(request, org_id)

    service = _get_service()
    try:
        await _get_owned_workspace_artifact_async(
            request,
            org_id,
            workspace_id,
            image_artifact_id,
        )
        workspace, task = await service.create_workspace_from_image_artifact(
            image_artifact_id=image_artifact_id,
            name=payload.name,
            user=request.user,
            organization_id=org_id,
        )
        return 202, WorkspaceFromImageArtifactOut(
            workspace_id=workspace.id,
            task_id=task.id,
            status=workspace.status,
        )
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except RunnerOfflineError as e:
        return 409, ErrorOut(detail=str(e), code="runner_offline")
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 404, ErrorOut(detail=str(e), code="not_found")


# ===========================================================================
# Image Definitions Router — /api/v1/image-definitions/
# ===========================================================================

image_definition_router = Router(tags=["image-definitions"])


def _image_definition_to_out(defn) -> ImageDefinitionOut:
    return ImageDefinitionOut(
        id=defn.id,
        organization_id=defn.organization_id,
        created_by_id=defn.created_by_id,
        name=defn.name,
        description=defn.description,
        is_standard=defn.organization_id is None,
        runtime_type=defn.runtime_type,
        base_distro=defn.base_distro,
        packages=list(defn.packages or []),
        env_vars=dict(defn.env_vars or {}),
        custom_dockerfile=defn.custom_dockerfile or "",
        custom_init_script=defn.custom_init_script or "",
        is_active=defn.is_active,
        created_at=defn.created_at,
        updated_at=defn.updated_at,
    )


def _runner_image_build_to_out(build) -> RunnerImageBuildOut:
    artifact = None
    try:
        artifact = getattr(build, "artifact", None)
    except (ObjectDoesNotExist, SynchronousOnlyOperation):
        artifact = None
    if (
        artifact is None
        and getattr(build, "status", "") == "active"
        and (build.image_tag or build.image_path)
    ):
        from .models import ImageArtifact

        artifact = ImageArtifact.objects.create(
            source_workspace=None,
            created_by=None,
            artifact_kind=ImageArtifact.ArtifactKind.BUILT,
            runner_image_build=build,
            runner_artifact_id=build.image_tag or build.image_path,
            name=f"{build.image_definition.name} ({build.runner.name})",
            status=ImageArtifact.ArtifactStatus.READY,
        )

    return RunnerImageBuildOut(
        id=build.id,
        image_definition_id=build.image_definition_id,
        runner_id=build.runner_id,
        image_artifact_id=getattr(artifact, "id", None),
        status=build.status,
        image_tag=build.image_tag,
        image_path=build.image_path,
        build_log=build.build_log,
        build_task_id=build.build_task_id,
        built_at=build.built_at,
        deactivated_at=build.deactivated_at,
        created_at=build.created_at,
        updated_at=build.updated_at,
    )


@image_definition_router.get(
    "/",
    response={200: list[ImageDefinitionOut], 403: ErrorOut},
    summary="List image definitions",
)
def list_image_definitions(request: HttpRequest):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_READ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    service = _get_service()
    return 200, [_image_definition_to_out(d) for d in service.list_image_definitions(org_id)]


@image_definition_router.post(
    "/",
    response={201: ImageDefinitionOut, 403: ErrorOut},
    summary="Create image definition",
)
def create_image_definition(request: HttpRequest, payload: ImageDefinitionCreateIn):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    if not _is_org_admin(request.user, org_id):
        return 403, ErrorOut(detail="Admin role required", code="forbidden")
    from .models import ImageDefinition

    validation_error = _validate_image_definition_runtime(
        payload.runtime_type,
        payload.base_distro,
    )
    if validation_error is not None:
        return validation_error

    definition = ImageDefinition.objects.create(
        organization_id=org_id,
        created_by=request.user,
        name=payload.name,
        description=payload.description,
        runtime_type=payload.runtime_type,
        base_distro=payload.base_distro,
        packages=payload.packages,
        env_vars=payload.env_vars,
        custom_dockerfile=payload.custom_dockerfile,
        custom_init_script=payload.custom_init_script,
        is_active=payload.is_active,
    )
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
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from .models import ImageDefinition

    source = ImageDefinition.objects.filter(id=definition_id).filter(
        Q(organization__isnull=True) | Q(organization_id=org_id)
    ).first()
    if source is None:
        return 404, ErrorOut(detail="Image definition not found", code="not_found")

    if payload.name is not None and not payload.name.strip():
        return 400, ErrorOut(detail="name cannot be empty", code="validation_error")

    target_name = _org_image_definition_copy_name(payload.name or source.name, org_id)
    copied = ImageDefinition.objects.create(
        organization_id=org_id,
        created_by=request.user,
        name=target_name,
        description=source.description,
        runtime_type=source.runtime_type,
        base_distro=source.base_distro,
        packages=list(source.packages or []),
        env_vars=dict(source.env_vars or {}),
        custom_dockerfile=source.custom_dockerfile,
        custom_init_script=source.custom_init_script,
        is_active=source.is_active,
    )
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
    org_id = _get_org_id(request)
    if not _is_org_admin(request.user, org_id):
        return 403, ErrorOut(detail="Admin role required", code="forbidden")
    from .models import ImageDefinition

    definition = ImageDefinition.objects.filter(
        id=definition_id, organization_id=org_id
    ).first()
    if definition is None:
        return 404, ErrorOut(detail="Image definition not found", code="not_found")

    validation_error = _validate_image_definition_runtime(
        payload.runtime_type or definition.runtime_type,
        payload.base_distro or definition.base_distro,
    )
    if validation_error is not None:
        return validation_error

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
        value = getattr(payload, field, None)
        if value is not None:
            setattr(definition, field, value)
    definition.save()
    return 200, _image_definition_to_out(definition)


@image_definition_router.delete(
    "/{definition_id}/",
    response={204: None, 404: ErrorOut, 403: ErrorOut},
    summary="Delete image definition",
)
def delete_image_definition(request: HttpRequest, definition_id: uuid.UUID):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    if not _is_org_admin(request.user, org_id):
        return 403, ErrorOut(detail="Admin role required", code="forbidden")
    from .models import ImageDefinition

    definition = ImageDefinition.objects.filter(
        id=definition_id, organization_id=org_id
    ).first()
    if definition is None:
        return 404, ErrorOut(detail="Image definition not found", code="not_found")
    definition.delete()
    return 204, None


@image_definition_router.get(
    "/{definition_id}/runner-builds/",
    response={200: list[RunnerImageBuildOut], 403: ErrorOut, 404: ErrorOut},
    summary="List runner builds for image definition",
)
def list_image_definition_runner_builds(request: HttpRequest, definition_id: uuid.UUID):
    if not check_api_key_permission(request, APIKeyPermission.IMAGE_DEFINITIONS_READ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    service = _get_service()
    if _get_image_definition_for_org(org_id, definition_id) is None:
        return 404, ErrorOut(detail="Image definition not found", code="not_found")
    return 200, [
        _runner_image_build_to_out(b)
        for b in service.list_runner_image_builds(definition_id, org_id)
    ]


@image_definition_router.post(
    "/{definition_id}/runner-builds/",
    response={202: RunnerImageBuildOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Assign runner and activate image definition",
)
async def create_image_definition_runner_build(
    request: HttpRequest, definition_id: uuid.UUID, payload: RunnerImageBuildCreateIn
):
    if not check_api_key_permission(
        request, APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS
    ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS)
    org_id = _get_org_id(request)
    if not await _get_org_admin_flag_async(request, org_id):
        return 403, ErrorOut(detail="Admin role required", code="forbidden")
    definition = await sync_to_async(_get_image_definition_for_org)(org_id, definition_id)
    if definition is None:
        return 404, ErrorOut(detail="Image definition not found", code="not_found")
    runner = await sync_to_async(RunnerRepository.get_by_id)(payload.runner_id)
    if runner is None or runner.organization_id != org_id:
        return 404, ErrorOut(detail="Runner not found", code="not_found")

    service = _get_service()
    try:
        build = await service.trigger_runner_image_build(
            image_definition=definition,
            runner=runner,
            activate=payload.activate,
            created_by=request.user,
        )
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    return 202, _runner_image_build_to_out(build)


@image_definition_router.patch(
    "/{definition_id}/runner-builds/{runner_id}/",
    response={200: RunnerImageBuildOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Update runner build status",
)
async def update_image_definition_runner_build(
    request: HttpRequest,
    definition_id: uuid.UUID,
    runner_id: uuid.UUID,
    payload: RunnerImageBuildUpdateIn,
):
    if not check_api_key_permission(
        request, APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS
    ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS)
    org_id = _get_org_id(request)
    if not await _get_org_admin_flag_async(request, org_id):
        return 403, ErrorOut(detail="Admin role required", code="forbidden")
    from django.utils import timezone
    from .models import RunnerImageBuild

    build = await sync_to_async(_get_runner_image_build_for_org)(
        org_id,
        definition_id,
        runner_id,
    )
    if build is None:
        return 404, ErrorOut(detail="Runner image build not found", code="not_found")

    action = payload.action.strip().lower()
    if action == "deactivate":
        build.status = RunnerImageBuild.Status.DEACTIVATED
        build.deactivated_at = timezone.now()
        await sync_to_async(build.save)(update_fields=["status", "deactivated_at", "updated_at"])
        return 200, _runner_image_build_to_out(build)

    service = _get_service()
    definition = build.image_definition
    runner = build.runner
    try:
        rebuilt = await service.trigger_runner_image_build(
            image_definition=definition,
            runner=runner,
            activate=True,
            created_by=request.user,
        )
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    return 200, _runner_image_build_to_out(rebuilt)


@image_definition_router.delete(
    "/{definition_id}/runner-builds/{runner_id}/",
    response={204: None, 404: ErrorOut, 403: ErrorOut},
    summary="Remove runner assignment from image definition",
)
def delete_image_definition_runner_build(
    request: HttpRequest, definition_id: uuid.UUID, runner_id: uuid.UUID
):
    if not check_api_key_permission(
        request, APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS
    ):
        return _perm_denied(APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS)
    org_id = _get_org_id(request)
    if not _is_org_admin(request.user, org_id):
        return 403, ErrorOut(detail="Admin role required", code="forbidden")
    deleted = _get_service().runner_image_builds.delete_for_org(
        definition_id,
        runner_id,
        org_id,
    )
    if not deleted:
        return 404, ErrorOut(detail="Runner image build not found", code="not_found")
    return 204, None


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
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if _get_image_definition_for_org(org_id, definition_id) is None:
        return 404, ErrorOut(detail="Image definition not found", code="not_found")

    build = _get_runner_image_build_for_org(org_id, definition_id, runner_id)
    if build is None:
        return 404, ErrorOut(detail="Runner image build not found", code="not_found")
    return 200, {"build_log": build.build_log}


# ===========================================================================
# Org Agent Definitions Router — /api/v1/org-agent-definitions/
# ===========================================================================

org_agent_def_router = Router(tags=["org-agent-definitions"])


def _agent_def_to_out(agent, org_id: uuid.UUID, activated_ids: set) -> OrgAgentDefinitionOut:
    """Map an AgentDefinition ORM instance to OrgAgentDefinitionOut."""
    commands = [
        AgentCommandOut(
            id=cmd.id,
            phase=cmd.phase,
            args=list(cmd.args or []),
            workdir=cmd.workdir,
            env=dict(cmd.env or {}),
            description=cmd.description,
            order=cmd.order,
        )
        for cmd in agent.commands.all().order_by("phase", "order")
    ]
    required_ids = [svc.id for svc in agent.required_credential_services.all()]
    return OrgAgentDefinitionOut(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        is_standard=agent.organization_id is None,
        organization_id=agent.organization_id,
        available_options=list(agent.available_options or []),
        default_env=dict(agent.default_env or {}),
        supports_multi_chat=agent.supports_multi_chat,
        required_credential_service_ids=required_ids,
        commands=commands,
        is_active=agent.id in activated_ids,
    )


@org_agent_def_router.get(
    "/",
    response={200: list[OrgAgentDefinitionOut], 403: ErrorOut},
    summary="List all agent definitions for the org (admin only)",
)
def list_org_agent_definitions(request: HttpRequest):
    """Return all standard and org-specific agent definitions with activation status."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_READ):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_READ)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org = org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from django.db.models import Q
    from .models import AgentDefinition, OrgAgentDefinitionActivation

    # All standard + org-specific definitions
    definitions = list(
        AgentDefinition.objects.filter(
            Q(organization__isnull=True) | Q(organization_id=org_id)
        )
        .prefetch_related("commands", "required_credential_services")
        .order_by("name")
    )

    activated_ids = set(
        OrgAgentDefinitionActivation.objects.filter(
            organization_id=org_id
        ).values_list("agent_definition_id", flat=True)
    )

    return 200, [_agent_def_to_out(a, org_id, activated_ids) for a in definitions]


@org_agent_def_router.post(
    "/{agent_id}/duplicate/",
    response={201: OrgAgentDefinitionOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut},
    summary="Duplicate an agent definition into this organization",
)
def duplicate_org_agent_definition(
    request: HttpRequest,
    agent_id: uuid.UUID,
    payload: OrgAgentDefinitionDuplicateIn,
):
    """Create an org-owned copy of a visible standard or org-owned definition."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from django.db import connection, transaction
    from django.db.models import Q
    from .models import (
        AgentCommand,
        AgentCredentialRelationCommand,
        AgentDefinition,
        AgentDefinitionCredentialRelation,
        OrgAgentDefinitionActivation,
    )

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
        return 404, ErrorOut(detail="Agent definition not found", code="not_found")

    if payload.name is not None and not payload.name.strip():
        return 400, ErrorOut(detail="name cannot be empty", code="validation_error")

    target_name = _org_copy_name(payload.name or source.name, org_id)

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

        if payload.activate:
            OrgAgentDefinitionActivation.objects.get_or_create(
                organization_id=org_id,
                agent_definition=copied,
            )

    copied = AgentDefinition.objects.prefetch_related(
        "commands",
        "required_credential_services",
    ).get(id=copied.id)
    activated_ids = {copied.id} if payload.activate else set()
    return 201, _agent_def_to_out(copied, org_id, activated_ids)


@org_agent_def_router.post(
    "/",
    response={201: OrgAgentDefinitionOut, 400: ErrorOut, 403: ErrorOut, 409: ErrorOut},
    summary="Create an org-specific agent definition",
)
def create_org_agent_definition(request: HttpRequest, payload: OrgAgentDefinitionCreateIn):
    """Create a new agent definition owned by the organization."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org = org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from django.db import IntegrityError
    from apps.credentials.models import CredentialService
    from .models import AgentCommand, AgentDefinition, OrgAgentDefinitionActivation

    # Validate commands: must have exactly one run command
    run_commands = [c for c in payload.commands if c.phase == "run"]
    if len(run_commands) != 1:
        return 400, ErrorOut(
            detail="Exactly one 'run' command is required", code="validation_error"
        )

    try:
        agent = AgentDefinition.objects.create(
            organization_id=org_id,
            name=payload.name,
            description=payload.description,
            available_options=payload.available_options,
            default_env=payload.default_env,
            supports_multi_chat=payload.supports_multi_chat,
        )
    except IntegrityError:
        return 409, ErrorOut(
            detail=f"Agent definition '{payload.name}' already exists in this organization",
            code="conflict",
        )

    # Set required credential services
    if payload.required_credential_service_ids:
        services = CredentialService.objects.filter(
            id__in=payload.required_credential_service_ids
        )
        agent.required_credential_services.set(services)

    # Create commands
    for cmd in payload.commands:
        AgentCommand.objects.create(
            agent=agent,
            phase=cmd.phase,
            args=cmd.args,
            workdir=cmd.workdir,
            env=cmd.env,
            description=cmd.description,
            order=cmd.order,
        )

    # Auto-activate for this org
    OrgAgentDefinitionActivation.objects.create(
        organization_id=org_id, agent_definition=agent
    )

    # Reload with prefetch
    agent = AgentDefinition.objects.prefetch_related(
        "commands", "required_credential_services"
    ).get(id=agent.id)

    return 201, _agent_def_to_out(agent, org_id, {agent.id})


@org_agent_def_router.patch(
    "/{agent_id}/",
    response={200: OrgAgentDefinitionOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut},
    summary="Update an org-specific agent definition",
)
def update_org_agent_definition(
    request: HttpRequest,
    agent_id: uuid.UUID,
    payload: OrgAgentDefinitionUpdateIn,
):
    """Update an org-specific agent definition. Standard definitions cannot be modified."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from apps.credentials.models import CredentialService
    from .models import AgentCommand, AgentDefinition, OrgAgentDefinitionActivation

    agent = AgentDefinition.objects.filter(id=agent_id, organization_id=org_id).first()
    if agent is None:
        return 404, ErrorOut(detail="Agent definition not found or is not org-owned", code="not_found")

    if payload.name is not None:
        agent.name = payload.name
    if payload.description is not None:
        agent.description = payload.description
    if payload.available_options is not None:
        agent.available_options = payload.available_options
    if payload.default_env is not None:
        agent.default_env = payload.default_env
    if payload.supports_multi_chat is not None:
        agent.supports_multi_chat = payload.supports_multi_chat
    agent.save()

    if payload.required_credential_service_ids is not None:
        services = CredentialService.objects.filter(
            id__in=payload.required_credential_service_ids
        )
        agent.required_credential_services.set(services)

    if payload.commands is not None:
        # Validate: exactly one run command
        run_commands = [c for c in payload.commands if c.phase == "run"]
        if len(run_commands) != 1:
            return 400, ErrorOut(
                detail="Exactly one 'run' command is required", code="validation_error"
            )
        # Replace all commands
        AgentCommand.objects.filter(agent=agent).delete()
        for cmd in payload.commands:
            AgentCommand.objects.create(
                agent=agent,
                phase=cmd.phase,
                args=cmd.args,
                workdir=cmd.workdir,
                env=cmd.env,
                description=cmd.description,
                order=cmd.order,
            )

    agent = AgentDefinition.objects.prefetch_related(
        "commands", "required_credential_services"
    ).get(id=agent.id)

    activated_ids = set(
        OrgAgentDefinitionActivation.objects.filter(
            organization_id=org_id
        ).values_list("agent_definition_id", flat=True)
    )

    return 200, _agent_def_to_out(agent, org_id, activated_ids)


@org_agent_def_router.delete(
    "/{agent_id}/",
    response={204: None, 403: ErrorOut, 404: ErrorOut},
    summary="Delete an org-specific agent definition",
)
def delete_org_agent_definition(request: HttpRequest, agent_id: uuid.UUID):
    """Delete an org-specific agent definition. Standard definitions cannot be deleted."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from .models import AgentDefinition

    agent = AgentDefinition.objects.filter(id=agent_id, organization_id=org_id).first()
    if agent is None:
        return 404, ErrorOut(detail="Agent definition not found or is not org-owned", code="not_found")

    agent.delete()
    return 204, None


@org_agent_def_router.post(
    "/{agent_id}/activation/",
    response={200: OrgAgentDefinitionOut, 403: ErrorOut, 404: ErrorOut},
    summary="Toggle activation of an agent definition for the org",
)
def toggle_org_agent_activation(
    request: HttpRequest,
    agent_id: uuid.UUID,
    payload: OrgAgentActivationToggleIn,
):
    """Activate or deactivate an agent definition for the organization."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from django.db.models import Q
    from .models import AgentDefinition, OrgAgentDefinitionActivation

    # The definition must be standard OR owned by this org
    agent = AgentDefinition.objects.prefetch_related(
        "commands", "required_credential_services"
    ).filter(
        Q(organization__isnull=True) | Q(organization_id=org_id),
        id=agent_id,
    ).first()

    if agent is None:
        return 404, ErrorOut(detail="Agent definition not found", code="not_found")

    if agent.organization_id is None and not request.user.is_staff:
        return 403, ErrorOut(
            detail="Only staff users can modify standard agent definition activation",
            code="forbidden",
        )

    if payload.active:
        OrgAgentDefinitionActivation.objects.get_or_create(
            organization_id=org_id, agent_definition=agent
        )
        activated_ids = {agent.id}
    else:
        OrgAgentDefinitionActivation.objects.filter(
            organization_id=org_id, agent_definition=agent
        ).delete()
        activated_ids = set()

    return 200, _agent_def_to_out(agent, org_id, activated_ids)


# ---------------------------------------------------------------------------
# Org Agent Credential Relations — /api/v1/org-agent-definitions/{agent_id}/credential-relations/
# ---------------------------------------------------------------------------


def _relation_to_out(relation) -> AgentCredentialRelationOut:
    """Map an AgentDefinitionCredentialRelation ORM instance to schema."""
    return AgentCredentialRelationOut(
        id=relation.id,
        credential_service_id=relation.credential_service_id,
        credential_service_name=relation.credential_service.name,
        default_env=relation.default_env or {},
        commands=[
            AgentCredentialRelationCommandOut(
                id=cmd.id,
                phase=cmd.phase,
                args=cmd.args,
                workdir=cmd.workdir,
                env=cmd.env or {},
                description=cmd.description,
                order=cmd.order,
            )
            for cmd in relation.commands.all().order_by("phase", "order")
        ],
    )


@org_agent_def_router.get(
    "/{agent_id}/credential-relations/",
    response={200: list[AgentCredentialRelationOut], 403: ErrorOut, 404: ErrorOut},
)
def list_agent_credential_relations(request: HttpRequest, agent_id: uuid.UUID):
    """List all credential relations for an agent definition."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_READ):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_READ)
    org_id = _get_org_id(request)
    org_service = OrganizationService()
    org_service.require_membership(request.user, org_id)

    from django.db.models import Q
    from .models import AgentDefinition, AgentDefinitionCredentialRelation

    agent = AgentDefinition.objects.filter(
        Q(organization__isnull=True) | Q(organization_id=org_id),
        id=agent_id,
    ).first()
    if agent is None:
        return 404, ErrorOut(detail="Agent definition not found", code="not_found")

    relations = AgentDefinitionCredentialRelation.objects.select_related(
        "credential_service"
    ).prefetch_related("commands").filter(agent_definition=agent)

    return 200, [_relation_to_out(r) for r in relations]


@org_agent_def_router.post(
    "/{agent_id}/credential-relations/",
    response={201: AgentCredentialRelationOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
)
def create_agent_credential_relation(
    request: HttpRequest,
    agent_id: uuid.UUID,
    payload: AgentCredentialRelationCreateIn,
):
    """Create a credential relation for an agent definition."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    org_service = OrganizationService()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from django.db import IntegrityError
    from django.db.models import Q
    from apps.credentials.models import CredentialService
    from .models import AgentDefinition, AgentDefinitionCredentialRelation, AgentCredentialRelationCommand

    agent = AgentDefinition.objects.filter(
        Q(organization__isnull=True) | Q(organization_id=org_id),
        id=agent_id,
    ).first()
    if agent is None:
        return 404, ErrorOut(detail="Agent definition not found", code="not_found")

    svc = CredentialService.objects.filter(id=payload.credential_service_id).first()
    if svc is None:
        return 404, ErrorOut(detail="Credential service not found", code="not_found")

    try:
        relation = AgentDefinitionCredentialRelation.objects.create(
            agent_definition=agent,
            credential_service=svc,
            default_env=payload.default_env or {},
        )
    except IntegrityError:
        return 409, ErrorOut(
            detail="Relation already exists for this credential service",
            code="conflict",
        )

    for i, cmd in enumerate(payload.commands):
        AgentCredentialRelationCommand.objects.create(
            relation=relation,
            phase=cmd.phase,
            args=cmd.args,
            workdir=cmd.workdir,
            env=cmd.env or {},
            description=cmd.description,
            order=cmd.order if cmd.order is not None else i,
        )

    relation = AgentDefinitionCredentialRelation.objects.select_related(
        "credential_service"
    ).prefetch_related("commands").get(id=relation.id)

    return 201, _relation_to_out(relation)


@org_agent_def_router.patch(
    "/{agent_id}/credential-relations/{relation_id}/",
    response={200: AgentCredentialRelationOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut},
)
def update_agent_credential_relation(
    request: HttpRequest,
    agent_id: uuid.UUID,
    relation_id: uuid.UUID,
    payload: AgentCredentialRelationUpdateIn,
):
    """Update a credential relation (default_env and/or commands)."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    org_service = OrganizationService()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from django.db.models import Q
    from .models import AgentDefinition, AgentDefinitionCredentialRelation, AgentCredentialRelationCommand

    agent = AgentDefinition.objects.filter(
        Q(organization__isnull=True) | Q(organization_id=org_id),
        id=agent_id,
    ).first()
    if agent is None:
        return 404, ErrorOut(detail="Agent definition not found", code="not_found")

    relation = AgentDefinitionCredentialRelation.objects.select_related(
        "credential_service"
    ).filter(agent_definition=agent, id=relation_id).first()
    if relation is None:
        return 404, ErrorOut(detail="Credential relation not found", code="not_found")

    if payload.default_env is not None:
        relation.default_env = payload.default_env
        relation.save(update_fields=["default_env", "updated_at"])

    if payload.commands is not None:
        relation.commands.all().delete()
        for i, cmd in enumerate(payload.commands):
            AgentCredentialRelationCommand.objects.create(
                relation=relation,
                phase=cmd.phase,
                args=cmd.args,
                workdir=cmd.workdir,
                env=cmd.env or {},
                description=cmd.description,
                order=cmd.order if cmd.order is not None else i,
            )

    relation = AgentDefinitionCredentialRelation.objects.select_related(
        "credential_service"
    ).prefetch_related("commands").get(id=relation.id)

    return 200, _relation_to_out(relation)


@org_agent_def_router.delete(
    "/{agent_id}/credential-relations/{relation_id}/",
    response={204: None, 403: ErrorOut, 404: ErrorOut},
)
def delete_agent_credential_relation(
    request: HttpRequest,
    agent_id: uuid.UUID,
    relation_id: uuid.UUID,
):
    """Delete a credential relation from an agent definition."""
    if not check_api_key_permission(request, APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE):
        return _perm_denied(APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE)
    org_id = _get_org_id(request)
    org_service = OrganizationService()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    from django.db.models import Q
    from .models import AgentDefinition, AgentDefinitionCredentialRelation

    agent = AgentDefinition.objects.filter(
        Q(organization__isnull=True) | Q(organization_id=org_id),
        id=agent_id,
    ).first()
    if agent is None:
        return 404, ErrorOut(detail="Agent definition not found", code="not_found")

    deleted, _ = AgentDefinitionCredentialRelation.objects.filter(
        agent_definition=agent, id=relation_id
    ).delete()
    if not deleted:
        return 404, ErrorOut(detail="Credential relation not found", code="not_found")

    return 204, None
