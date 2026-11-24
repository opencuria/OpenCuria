"""REST API endpoints for the plugins app.

``/api/v1/plugins/`` manages plugins; ``/api/v1/skills/`` lists the skills of
those plugins that the user can select for chats and scheduled tasks.

Thin adapter layer — validates input, delegates to the services, and
formats responses. No business logic or direct ORM access here. The
workspace-scoped plugin listing lives with the other workspace routers in
``apps.runners.api``.
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import ConflictError, NotFoundError
from common.request_context import get_organization_id
from common.schemas import ErrorOut

from .schemas import (
    PluginActivationIn,
    PluginCreateIn,
    PluginCredentialRequirementOut,
    PluginMcpServerOut,
    PluginOut,
    PluginSkillOut,
    PluginUpdateIn,
    SkillOut,
)
from .services import PluginPermissionError, PluginService
from .skills import PluginSkillService


def _perm_denied(permission: APIKeyPermission):
    """Return a 403 error tuple for a denied API key permission."""
    return 403, ErrorOut(
        detail=f"API key lacks permission: {permission.value}",
        code="permission_denied",
    )


def _get_org_service() -> OrganizationService:
    return OrganizationService()


def _get_plugin_service() -> PluginService:
    return PluginService()


def _stores_credentials(requirements) -> bool:
    """Whether a payload creates credential services or credential values."""
    return any(
        item.value is not None or item.new_service is not None
        for item in requirements or []
    )


def _caller(request: HttpRequest) -> tuple[uuid.UUID, bool]:
    """Return the active organization and whether the caller administers it."""
    org_id = get_organization_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    return org_id, org_service.get_user_role(request.user, org_id) == "admin"


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
        scope=payload["scope"],
        is_global=payload["is_global"],
        org_enabled=payload["org_enabled"],
        available=payload["available"],
        editable=payload["editable"],
        skills=[PluginSkillOut(**s) for s in payload["skills"]],
        mcp_servers=[PluginMcpServerOut(**m) for m in payload["mcp_servers"]],
        credential_requirements=[
            PluginCredentialRequirementOut(**r)
            for r in payload["credential_requirements"]
        ],
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
    )


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
    """Return built-in, organization and the caller's personal plugins.

    Members get only built-in and organization plugins released for the
    organization, without their MCP server configuration.
    """
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_READ):
        return _perm_denied(APIKeyPermission.PLUGINS_READ)
    org_id, admin = _caller(request)
    payloads = _get_plugin_service().list_visible(
        org_id=org_id, user=request.user, admin=admin
    )
    return [_plugin_to_out(p) for p in payloads]


@plugin_router.post(
    "/",
    response={201: PluginOut, 400: ErrorOut, 403: ErrorOut, 409: ErrorOut},
    summary="Create a plugin",
)
def create_plugin(request: HttpRequest, payload: PluginCreateIn):
    """Create a personal plugin, or (admins only) an organization plugin."""
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_WRITE):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    if _stores_credentials(payload.credential_requirements) and (
        not check_api_key_permission(request, APIKeyPermission.CREDENTIALS_WRITE)
    ):
        return _perm_denied(APIKeyPermission.CREDENTIALS_WRITE)
    org_id, admin = _caller(request)
    try:
        created = _get_plugin_service().create_plugin(
            org_id=org_id,
            user=request.user,
            admin=admin,
            scope=payload.scope,
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
    except PluginPermissionError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except NotFoundError as e:
        return 400, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@plugin_router.patch(
    "/{plugin_id}/",
    response={
        200: PluginOut,
        400: ErrorOut,
        403: ErrorOut,
        404: ErrorOut,
        409: ErrorOut,
    },
    summary="Update a plugin",
)
def update_plugin(request: HttpRequest, plugin_id: uuid.UUID, payload: PluginUpdateIn):
    """Replace metadata and/or full component lists.

    Owners change personal plugins, admins organization plugins; built-in
    plugins are read-only.
    """
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_WRITE):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    if _stores_credentials(payload.credential_requirements) and (
        not check_api_key_permission(request, APIKeyPermission.CREDENTIALS_WRITE)
    ):
        return _perm_denied(APIKeyPermission.CREDENTIALS_WRITE)
    org_id, admin = _caller(request)
    try:
        updated = _get_plugin_service().update_plugin(
            plugin_id,
            org_id=org_id,
            user=request.user,
            admin=admin,
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
    except PluginPermissionError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except ConflictError as e:
        return 409, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 400, ErrorOut(detail=str(e), code="validation_error")


@plugin_router.delete(
    "/{plugin_id}/",
    response={204: None, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut},
    summary="Delete a plugin",
)
def delete_plugin(request: HttpRequest, plugin_id: uuid.UUID):
    """Delete a personal (owner) or organization (admin) plugin."""
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_WRITE):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    org_id, admin = _caller(request)
    try:
        _get_plugin_service().delete_plugin(
            plugin_id, org_id=org_id, user=request.user, admin=admin
        )
        return 204, None
    except PluginPermissionError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)
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
    summary="Release a plugin for the organization",
)
def toggle_plugin_activation(
    request: HttpRequest, plugin_id: uuid.UUID, payload: PluginActivationIn
):
    """Release or withdraw a built-in/organization plugin (admin only).

    Enabling requires an enabled+published definition (409
    ``plugin_not_available`` otherwise); disabling always succeeds.
    """
    if not check_api_key_permission(request, APIKeyPermission.PLUGINS_WRITE):
        return _perm_denied(APIKeyPermission.PLUGINS_WRITE)
    org_id, admin = _caller(request)
    if not admin:
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
# Skill Router — /api/v1/skills/
# ===========================================================================

skill_router = Router(tags=["skills"])


@skill_router.get(
    "/",
    response={200: list[SkillOut], 403: ErrorOut},
    summary="List selectable skills",
)
def list_skills(request: HttpRequest):
    """Return the skills of the plugins the user can use, by plugin name.

    Skills are edited as part of their plugin (``/api/v1/plugins/``).
    """
    if not check_api_key_permission(request, APIKeyPermission.SKILLS_READ):
        return _perm_denied(APIKeyPermission.SKILLS_READ)
    org_id, _ = _caller(request)
    return [
        SkillOut(
            id=skill.id,
            name=skill.name,
            description=skill.description,
            plugin_id=skill.plugin_id,
            plugin_name=skill.plugin.name,
            scope=skill.plugin.scope,
        )
        for skill in PluginSkillService().list_usable(
            user_id=request.user.id, organization_id=org_id
        )
    ]
