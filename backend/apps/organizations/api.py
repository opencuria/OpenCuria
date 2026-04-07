"""
REST API endpoints for organizations.

All endpoints require authentication (global JWT auth on NinjaAPI).
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .schemas import (
    OrganizationCreateIn,
    OrganizationOut,
    OrganizationWorkspacePolicyUpdateIn,
)
from .services import OrganizationService

org_router = Router(tags=["organizations"])


def _get_service() -> OrganizationService:
    return OrganizationService()


@org_router.get(
    "/",
    response={200: list[OrganizationOut], 403: dict},
    summary="List user's organizations",
)
def list_organizations(request: HttpRequest):
    """Return all organizations the current user belongs to."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_READ):
        return 403, {"detail": "API key lacks permission: organizations:read", "code": "permission_denied"}
    service = _get_service()
    org_list = service.list_user_organizations(request.user)
    return [
        OrganizationOut(
            id=o["id"],
            name=o["name"],
            slug=o["slug"],
            role=o["role"],
            workspace_auto_stop_timeout_minutes=o.get(
                "workspace_auto_stop_timeout_minutes"
            ),
            created_at=o["created_at"],
        )
        for o in org_list
    ]


@org_router.post(
    "/",
    response={201: OrganizationOut, 403: dict, 409: dict},
    summary="Create an organization",
)
def create_organization(request: HttpRequest, payload: OrganizationCreateIn):
    """Create a new organization. The current user becomes admin."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_WRITE):
        return 403, {"detail": "API key lacks permission: organizations:write", "code": "permission_denied"}
    service = _get_service()
    try:
        org = service.create_organization(
            name=payload.name,
            user=request.user,
        )
    except ConflictError as e:
        return 409, {"detail": e.message, "code": e.code}

    return 201, OrganizationOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        role="admin",
        workspace_auto_stop_timeout_minutes=org.workspace_auto_stop_timeout_minutes,
        created_at=org.created_at,
    )


@org_router.get(
    "/{org_id}/",
    response={200: OrganizationOut, 403: dict, 404: dict},
    summary="Get organization detail",
)
def get_organization(request: HttpRequest, org_id: uuid.UUID):
    """Return an organization by ID. User must be a member."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_READ):
        return 403, {"detail": "API key lacks permission: organizations:read", "code": "permission_denied"}
    service = _get_service()
    try:
        org = service.get_organization(org_id, request.user)
        role = service.get_user_role(request.user, org_id) or "member"
    except NotFoundError as e:
        return 404, {"detail": e.message, "code": e.code}

    return 200, OrganizationOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        role=role,
        workspace_auto_stop_timeout_minutes=org.workspace_auto_stop_timeout_minutes,
        created_at=org.created_at,
    )


@org_router.patch(
    "/{org_id}/workspace-policy/",
    response={200: OrganizationOut, 400: dict, 403: dict, 404: dict},
    summary="Update organization workspace policy",
)
def update_workspace_policy(
    request: HttpRequest,
    org_id: uuid.UUID,
    payload: OrganizationWorkspacePolicyUpdateIn,
):
    """Update org-wide workspace inactivity settings."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_WRITE):
        return 403, {
            "detail": "API key lacks permission: organizations:write",
            "code": "permission_denied",
        }

    service = _get_service()
    try:
        org = service.update_workspace_policy(
            org_id=org_id,
            user=request.user,
            workspace_auto_stop_timeout_minutes=payload.workspace_auto_stop_timeout_minutes,
        )
        role = service.get_user_role(request.user, org_id) or "member"
    except NotFoundError as e:
        return 404, {"detail": e.message, "code": e.code}
    except ValueError as e:
        return 400, {"detail": str(e), "code": "validation_error"}
    except AuthenticationError as e:
        return 403, {"detail": e.message, "code": e.code}

    return 200, OrganizationOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        role=role,
        workspace_auto_stop_timeout_minutes=org.workspace_auto_stop_timeout_minutes,
        created_at=org.created_at,
    )
