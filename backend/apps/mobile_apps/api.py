"""REST API for the apps agents published to the signed-in user.

Publishing happens through the ``app_publish`` MCP tool; clients list apps,
fetch a version's install manifest and download its content-addressed files.
"""

from __future__ import annotations

import uuid

from django.http import FileResponse, HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import NotFoundError
from common.request_context import get_organization_id
from common.schemas import ErrorOut

from .models import AppVersion
from .schemas import (
    AppAssetOut,
    AppDetailOut,
    AppFileOut,
    AppManifestOut,
    AppOut,
    AppVersionOut,
)
from .services import AppListing, AppManifest, AppService

app_router = Router(tags=["apps"])

#: Version files never change; devices may cache them for good.
IMMUTABLE_CACHE = "private, max-age=31536000, immutable"


def _service() -> AppService:
    return AppService()


def _denied(permission: APIKeyPermission) -> tuple[int, ErrorOut]:
    return 403, ErrorOut(
        detail=f"API key lacks permission: {permission.value}",
        code="permission_denied",
    )


def _scope(request: HttpRequest) -> uuid.UUID:
    """The request's organization after checking membership."""
    org_id = get_organization_id(request)
    OrganizationService().require_membership(request.user, org_id)
    return org_id


def _version_out(listing: AppListing, version: AppVersion) -> AppVersionOut:
    return AppVersionOut(
        id=version.id,
        number=version.number,
        version_name=version.version_name,
        sdk_version=version.sdk_version,
        notes=version.notes,
        created_at=version.created_at,
        icon_sha256=version.icon_id,
        platforms=listing.platforms(version),
    )


def _app_fields(listing: AppListing) -> dict:
    app, latest = listing.app, listing.latest
    return {
        "id": app.id,
        "name": app.name,
        "slug": app.slug,
        "workspace_id": app.workspace_id,
        "workspace_name": app.workspace.name if app.workspace else "",
        "created_at": app.created_at,
        "updated_at": app.updated_at,
        "latest_version": _version_out(listing, latest) if latest else None,
    }


def _manifest_out(manifest: AppManifest) -> AppManifestOut:
    version, icon = manifest.version, manifest.version.icon
    return AppManifestOut(
        app_id=manifest.app.id,
        version_id=version.id,
        number=version.number,
        name=manifest.app.name,
        slug=manifest.app.slug,
        version_name=version.version_name,
        sdk_version=version.sdk_version,
        platform=manifest.platform,
        expo_config=version.expo_config,
        bundle=AppFileOut(**manifest.bundle.__dict__),
        assets=[AppAssetOut(**asset.__dict__) for asset in manifest.assets],
        icon=(
            AppFileOut(sha256=icon.sha256, md5=icon.md5, size=icon.size)
            if icon
            else None
        ),
        created_at=version.created_at,
    )


@app_router.get("/", response={200: list[AppOut], 403: ErrorOut, 404: ErrorOut})
def list_apps(request: HttpRequest):
    """Apps agents published to the signed-in user, newest activity first."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _scope(request)
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    listings = _service().list_apps(organization_id=org_id, owner_id=request.user.id)
    return 200, [AppOut(**_app_fields(listing)) for listing in listings]


@app_router.get(
    "/{app_id}/", response={200: AppDetailOut, 403: ErrorOut, 404: ErrorOut}
)
def get_app(request: HttpRequest, app_id: uuid.UUID):
    """One app with its retained versions."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _scope(request)
        listing = _service().app_detail(
            app_id, organization_id=org_id, owner_id=request.user.id
        )
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    return 200, AppDetailOut(
        **_app_fields(listing),
        versions=[_version_out(listing, version) for version in listing.versions],
    )


@app_router.delete("/{app_id}/", response={204: None, 403: ErrorOut, 404: ErrorOut})
def delete_app(request: HttpRequest, app_id: uuid.UUID):
    """Delete an app from the server; installed copies stay on devices."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_RUN):
        return _denied(APIKeyPermission.HARNESS_RUN)
    try:
        org_id = _scope(request)
        _service().delete_app(app_id, organization_id=org_id, owner_id=request.user.id)
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    return 204, None


@app_router.get(
    "/{app_id}/versions/{version_id}/manifest/",
    response={200: AppManifestOut, 400: ErrorOut, 403: ErrorOut, 404: ErrorOut},
)
def get_manifest(
    request: HttpRequest, app_id: uuid.UUID, version_id: uuid.UUID, platform: str
):
    """Install manifest of a version for ``platform`` (``android`` or ``ios``)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _scope(request)
        manifest = _service().manifest(
            app_id,
            version_id,
            platform,
            organization_id=org_id,
            owner_id=request.user.id,
        )
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    except ValueError as exc:
        return 400, ErrorOut(detail=str(exc), code="validation_error")
    return 200, _manifest_out(manifest)


@app_router.get(
    "/{app_id}/versions/{version_id}/files/{sha256}/",
    response={403: ErrorOut, 404: ErrorOut},
)
def download_file(
    request: HttpRequest, app_id: uuid.UUID, version_id: uuid.UUID, sha256: str
):
    """Download a bundle, asset or icon of a version by its SHA-256."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _denied(APIKeyPermission.HARNESS_READ)
    try:
        org_id = _scope(request)
        blob = _service().open_file(
            app_id,
            version_id,
            sha256.lower(),
            organization_id=org_id,
            owner_id=request.user.id,
        )
        handle = blob.file.open("rb")
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    except FileNotFoundError:
        return 404, ErrorOut(detail="App file not found", code="not_found")
    response = FileResponse(handle, content_type="application/octet-stream")
    response["Content-Length"] = str(blob.size)
    response["Cache-Control"] = IMMUTABLE_CACHE
    response["ETag"] = f'"{blob.sha256}"'
    return response
