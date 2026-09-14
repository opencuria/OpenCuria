"""REST API endpoints for the plugins app.

Two routers:

- plugin_router      -> /api/v1/plugins/
- workspace_plugin_router -> /api/v1/workspaces/{workspace_id}/plugins/

Thin adapter layer — validates input, delegates to PluginService, and
formats responses. No business logic or direct ORM access here (outside
workspace lookups that mirror the existing workspace API behavior).
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.runners.schemas import ErrorOut
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .schemas import (
    PluginActivationIn,
    PluginCreateIn,
    PluginCredentialReadinessOut,
    PluginCredentialRequirementOut,
    PluginMcpServerOut,
    PluginOut,
    PluginSkillOut,
    PluginUpdateIn,
    WorkspacePluginOut,
    WorkspacePluginsUpdateIn,
)
from .services import PluginService


def _perm_denied(permission: APIKeyPermission):
    """Return a 403 error tuple for a denied API key permission."""
    return 403, ErrorOut(
        detail=f"API key lacks permission: {permission.value}",
        code="permission_denied",
    )


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


def _get_plugin_service() -> PluginService:
    return PluginService()


def _get_owned_workspace(
    request: HttpRequest, org_id: uuid.UUID, workspace_id: uuid.UUID
):
    """Mirror the workspace API: owner-scoped lookup (admin sees no more)."""
    from apps.runners.sio_server import get_runner_service

    service = get_runner_service()
    try:
        return service.get_workspace_for_user(
            workspace_id,
            user=request.user,
            organization_id=org_id,
        )
    except NotFoundError:
        raise NotFoundError("Workspace", str(workspace_id))


def _plugin_to_out(payload: dict) -> PluginOut:
    """Map a serialized plugin dict to its output schema."""
    return PluginOut(
        id=payload["id"],
        name=payload["name"],
        slug=payload["slug"],
        description=payload["description"],
        enabled=payload["enabled"],
        published=payload["published"],
        organization_id=payload["organization_id"],
        is_global=payload["is_global"],
        org_enabled=payload["org_enabled"],
        skills=[PluginSkillOut(**s) for s in payload["skills"]],
        mcp_servers=[PluginMcpServerOut(**m) for m in payload["mcp_servers"]],
        credential_requirements=[
            PluginCredentialRequirementOut(**r)
            for r in payload["credential_requirements"]
        ],
        credential_readiness=PluginCredentialReadinessOut(
            **payload["credential_readiness"]
        )
        if payload.get("credential_readiness")
        else None,
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
    )


def _workspace_plugin_to_out(payload: dict) -> WorkspacePluginOut:
    return WorkspacePluginOut(**payload)


# ===========================================================================
# Plugin Router — /api/v1/plugins/
# ===========================================================================

plugin_router = Router(tags=["plugins"])


@plugin_router.get(
    "/",
    response={200: list[PluginOut], 403: ErrorOut},
    summary="List plugins",
)
def list_plugins(request: HttpRequest):
    """Return visible global + org-owned plugins with readiness summary."""
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_READ):
        return _perm_denied(APIKeyPermission.PLUGINS_READ)
    org_id = _get_org_id(request)
    _get_org_service().require_membership(request.user, org_id)

    payloads = _get_plugin_service().list_visible(org_id=org_id)
    return [_plugin_to_out(p) for p in payloads]


@plugin_router.post(
    "/",
    response={201: PluginOut, 400: ErrorOut, 403: ErrorOut, 409: ErrorOut},
    summary="Create an org-owned plugin",
)
def create_plugin(request: HttpRequest, payload: PluginCreateIn):
    """Create an org-owned plugin with nested components (admin only)."""
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_WRITE):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    svc = _get_plugin_service()
    try:
        created = svc.create_org_plugin(
            org_id=org_id,
            user=request.user,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            enabled=payload.enabled,
            published=payload.published,
            skills=[s.model_dump() for s in payload.skills],
            mcp_servers=[m.model_dump() for m in payload.mcp_servers],
            credential_requirements=[
                r.model_dump() for r in payload.credential_requirements
            ],
        )
        return 201, _plugin_to_out(created)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@plugin_router.get(
    "/{plugin_id}/",
    response={200: PluginOut, 403: ErrorOut, 404: ErrorOut},
    summary="Get a plugin",
)
def get_plugin(request: HttpRequest, plugin_id: uuid.UUID):
    """Return a single visible plugin (foreign org -> 404)."""
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_READ):
        return _perm_denied(APIKeyPermission.PLUGINS_READ)
    org_id = _get_org_id(request)
    _get_org_service().require_membership(request.user, org_id)

    try:
        payload = _get_plugin_service().get_visible(plugin_id, org_id=org_id)
        return 200, _plugin_to_out(payload)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@plugin_router.patch(
    "/{plugin_id}/",
    response={
        200: PluginOut,
        400: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Update an org-owned plugin",
)
def update_plugin(request: HttpRequest, plugin_id: uuid.UUID, payload: PluginUpdateIn):
    """Replace metadata and/or full component lists (org-owned + admin)."""
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_WRITE):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    svc = _get_plugin_service()
    try:
        # Global plugins are never editable via REST (staff/Django admin only).
        plugin = svc.get_visible_model(plugin_id, org_id=org_id)
        if plugin.organization_id is None:
            return 403, ErrorOut(
                detail="Global plugins can only be edited by staff",
                code="forbidden",
            )
        updated = svc.update_org_plugin(
            plugin_id,
            org_id=org_id,
            user=request.user,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            enabled=payload.enabled,
            published=payload.published,
            skills=(
                [s.model_dump() for s in payload.skills]
                if payload.skills is not None
                else None
            ),
            mcp_servers=(
                [m.model_dump() for m in payload.mcp_servers]
                if payload.mcp_servers is not None
                else None
            ),
            credential_requirements=(
                [r.model_dump() for r in payload.credential_requirements]
                if payload.credential_requirements is not None
                else None
            ),
        )
        return 200, _plugin_to_out(updated)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@plugin_router.delete(
    "/{plugin_id}/",
    response={204: None, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Delete an org-owned plugin",
)
def delete_plugin(request: HttpRequest, plugin_id: uuid.UUID):
    """Delete an org-owned plugin (admin only)."""
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_WRITE):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    svc = _get_plugin_service()
    try:
        plugin = svc.get_visible_model(plugin_id, org_id=org_id)
        if plugin.organization_id is None:
            return 403, ErrorOut(
                detail="Global plugins can only be edited by staff",
                code="forbidden",
            )
        svc.delete_org_plugin(plugin_id, org_id=org_id)
        return 204, None
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


@plugin_router.post(
    "/{plugin_id}/activation/",
    response={
        200: PluginOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Toggle org activation of a plugin",
)
def toggle_plugin_activation(
    request: HttpRequest, plugin_id: uuid.UUID, payload: PluginActivationIn
):
    """Enable/disable a visible plugin org-wide (admin only).

    Enabling requires an enabled+published definition (409
    ``plugin_not_available`` otherwise); disabling always succeeds.
    """
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_WRITE):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    if org_service.get_user_role(request.user, org_id) != "admin":
        return 403, ErrorOut(detail="Admin role required", code="forbidden")

    try:
        updated = _get_plugin_service().set_org_activation(
            plugin_id,
            org_id=org_id,
            user=request.user,
            active=payload.active,
        )
        return 200, _plugin_to_out(updated)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)


# ===========================================================================
# Workspace Plugin Router — /api/v1/workspaces/{workspace_id}/plugins/
# ===========================================================================

workspace_plugin_router = Router(tags=["workspace-plugins"])


@workspace_plugin_router.get(
    "/{workspace_id}/plugins/",
    response={200: list[WorkspacePluginOut], 403: ErrorOut, 404: ErrorOut},
    summary="List org-enabled plugins for a workspace",
)
def list_workspace_plugins(request: HttpRequest, workspace_id: uuid.UUID):
    """List org-enabled plugins with workspace state and credential gaps."""
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_READ):
        return _perm_denied(APIKeyPermission.PLUGINS_READ)
    org_id = _get_org_id(request)
    _get_org_service().require_membership(request.user, org_id)
    try:
        workspace = _get_owned_workspace(request, org_id, workspace_id)
        payloads = _get_plugin_service().list_workspace_plugins(
            workspace=workspace, org_id=org_id
        )
        return 200, [_workspace_plugin_to_out(p) for p in payloads]
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)


@workspace_plugin_router.put(
    "/{workspace_id}/plugins/",
    response={
        200: list[WorkspacePluginOut],
        400: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Replace workspace plugin activations",
)
def update_workspace_plugins(
    request: HttpRequest, workspace_id: uuid.UUID, payload: WorkspacePluginsUpdateIn
):
    """Replace workspace activations atomically (any member may manage own ws)."""
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_WRITE):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    org_id = _get_org_id(request)
    _get_org_service().require_membership(request.user, org_id)
    try:
        workspace = _get_owned_workspace(request, org_id, workspace_id)
        payloads = _get_plugin_service().set_workspace_plugins(
            workspace=workspace,
            org_id=org_id,
            user=request.user,
            plugin_ids=payload.plugin_ids,
        )
        return 200, [_workspace_plugin_to_out(p) for p in payloads]
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


# ===========================================================================
# Workspace Plugin Router — /api/v1/workspaces/{workspace_id}/plugins/
# ===========================================================================
