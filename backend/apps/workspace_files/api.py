"""REST adapter: sign links to workspace files and single-file uploads.

The bytes themselves move through :mod:`.http`, outside Django.
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from common.request_context import get_organization_id
from common.schemas import ErrorOut

from .schemas import FileLinkOut, FileLinksIn, FileLinksOut, FileUploadIn, FileUploadOut
from .services import FileAccessError, get_workspace_file_service

workspace_file_router = Router(tags=["workspace-files"])
_ERRORS = {code: ErrorOut for code in (400, 403, 404, 409, 413, 503)}


def _denied(permission: APIKeyPermission) -> tuple[int, ErrorOut]:
    return 403, ErrorOut(
        detail=f"API key lacks permission: {permission.value}",
        code="permission_denied",
    )


def _refused(error: FileAccessError) -> tuple[int, ErrorOut]:
    status = error.status if error.status in _ERRORS else 503
    return status, ErrorOut(detail=error.message, code=error.code)


@workspace_file_router.post(
    "/{workspace_id}/files/links/",
    response={200: FileLinksOut, **_ERRORS},
    summary="Sign links to workspace files",
)
async def create_file_links(
    request: HttpRequest, workspace_id: uuid.UUID, payload: FileLinksIn
):
    """Describe paths and return signed URLs to stream, download or preview them."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_READ):
        return _denied(APIKeyPermission.WORKSPACES_READ)
    try:
        links = await get_workspace_file_service().links(
            workspace_id,
            payload.paths,
            user=request.user,
            organization_id=get_organization_id(request),
        )
    except FileAccessError as exc:
        return _refused(exc)
    return 200, FileLinksOut(
        files=[FileLinkOut(**link.__dict__) for link in links.files],
        expires_at=links.expires_at,
    )


@workspace_file_router.post(
    "/{workspace_id}/files/uploads/",
    response={200: FileUploadOut, **_ERRORS},
    summary="Sign the upload of one workspace file",
)
async def create_file_upload(
    request: HttpRequest, workspace_id: uuid.UUID, payload: FileUploadIn
):
    """Return a URL that accepts exactly ``size`` bytes for the target file."""
    if not check_api_key_permission(request, APIKeyPermission.WORKSPACES_UPDATE):
        return _denied(APIKeyPermission.WORKSPACES_UPDATE)
    try:
        link = await get_workspace_file_service().create_upload(
            workspace_id,
            directory=payload.directory,
            filename=payload.filename,
            size=payload.size,
            conflict=payload.conflict,
            user=request.user,
            organization_id=get_organization_id(request),
        )
    except FileAccessError as exc:
        return _refused(exc)
    return 200, FileUploadOut(
        upload_url=link.upload_url, expires_at=link.expires_at, max_bytes=link.max_bytes
    )
