"""Authenticated REST adapters for dictation and opt-in pointing recordings."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from asgiref.sync import sync_to_async
from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.runners.git_presenters import git_error_to_response
from apps.runners.services import get_runner_service
from common.request_context import get_organization_id
from common.schemas import ErrorOut

from .voice_provider import VoiceProviderError
from .voice_schemas import (
    VoiceConfig,
    VoiceRecordingIn,
    VoiceRecordingOut,
    VoiceTranscribeIn,
    VoiceTranscript,
)
from .voice_service import VoiceService

voice_router = Router(tags=["voice"])
voice_settings_router = Router(tags=["voice"])
_ERRORS = {code: ErrorOut for code in (400, 403, 404, 409, 429, 502, 504)}


async def _authorized(
    request: HttpRequest,
    workspace_id: uuid.UUID | None,
    operation: Callable[[uuid.UUID], Awaitable[Any]],
    *,
    write: bool = False,
) -> tuple[int, Any]:
    permissions = [APIKeyPermission.HARNESS_RUN]
    if write:
        permissions.append(APIKeyPermission.TERMINAL_ACCESS)
    for permission in permissions:
        if not check_api_key_permission(request, permission):
            return 403, ErrorOut(
                detail=f"API key lacks permission: {permission.value}",
                code="permission_denied",
            )
    try:
        org_id = get_organization_id(request)
        await sync_to_async(OrganizationService().require_membership)(
            request.user, org_id
        )
        if workspace_id is not None:
            await sync_to_async(
                get_runner_service().workspace_access.get_workspace_for_user
            )(workspace_id, user=request.user, organization_id=org_id)
        return 200, await operation(org_id)
    except VoiceProviderError as exc:
        return exc.status, ErrorOut(detail=str(exc), code="voice_provider_error")
    except Exception as exc:
        return git_error_to_response(exc)


@voice_settings_router.get("/config/", response={200: VoiceConfig, **_ERRORS})
async def voice_settings_config(
    request: HttpRequest,
) -> tuple[int, VoiceConfig | ErrorOut]:
    """Read org-level speech choices for Settings without requiring a workspace."""
    return await _authorized(
        request, None, lambda org: VoiceService().config(org, request.user.id)
    )


@voice_router.get(
    "/{workspace_id}/voice/config/", response={200: VoiceConfig, **_ERRORS}
)
async def voice_config(
    request: HttpRequest, workspace_id: uuid.UUID
) -> tuple[int, VoiceConfig | ErrorOut]:
    """Read selected speech providers and safe limits for this workspace owner."""
    return await _authorized(
        request, workspace_id, lambda org: VoiceService().config(org, request.user.id)
    )


@voice_router.post(
    "/{workspace_id}/voice/transcribe/", response={200: VoiceTranscript, **_ERRORS}
)
async def voice_transcribe(
    request: HttpRequest, workspace_id: uuid.UUID, payload: VoiceTranscribeIn
) -> tuple[int, VoiceTranscript | ErrorOut]:
    """Transcribe and clean dictation without modifying workspace or chat."""
    return await _authorized(
        request,
        workspace_id,
        lambda org: VoiceService().transcribe(org, request.user.id, payload),
    )


@voice_router.post(
    "/{workspace_id}/voice/recordings/", response={200: VoiceRecordingOut, **_ERRORS}
)
async def voice_recording(
    request: HttpRequest, workspace_id: uuid.UUID, payload: VoiceRecordingIn
) -> tuple[int, VoiceRecordingOut | ErrorOut]:
    """Persist pointing manifests with the existing workspace-write authority."""

    async def save(org_id: uuid.UUID) -> VoiceRecordingOut:
        return VoiceRecordingOut(
            path=await VoiceService().save_recording(workspace_id, payload)
        )

    return await _authorized(request, workspace_id, save, write=True)
