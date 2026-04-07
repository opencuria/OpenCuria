"""REST API endpoints for the skills app.

skill_router → /api/v1/skills/
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.runners.schemas import ErrorOut
from common.exceptions import AuthenticationError, NotFoundError

from .schemas import SkillCreateIn, SkillOut, SkillUpdateIn
from .services import SkillService

skill_router = Router(tags=["skills"])


def _get_org_id(request: HttpRequest) -> uuid.UUID:
    """Extract and validate the organization ID from the request header."""
    org_id_str = request.headers.get("X-Organization-Id")
    if not org_id_str:
        raise AuthenticationError("X-Organization-Id header is required")
    try:
        return uuid.UUID(org_id_str)
    except ValueError:
        raise AuthenticationError("Invalid X-Organization-Id header")


def _get_org_service() -> OrganizationService:
    return OrganizationService()


def _skill_to_out(skill) -> SkillOut:
    """Map a Skill model instance to its output schema."""
    scope = "organization" if skill.organization_id else "personal"
    created_by_email = skill.created_by.email if skill.created_by else None
    return SkillOut(
        id=skill.id,
        name=skill.name,
        body=skill.body,
        scope=scope,
        created_by_email=created_by_email,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


@skill_router.get(
    "/",
    response={200: list[SkillOut], 403: ErrorOut},
    summary="List skills",
)
def list_skills(request: HttpRequest):
    """Return all skills visible to the user in the active organization.

    Includes personal skills (owned by the user) and org-shared skills.
    """
    if not check_api_key_permission(request, APIKeyPermission.SKILLS_READ):
        return 403, ErrorOut(
            detail="API key lacks permission: skills:read",
            code="permission_denied",
        )
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    svc = SkillService()
    skills = svc.list_skills(request.user, org_id)
    return [_skill_to_out(s) for s in skills]


@skill_router.post(
    "/",
    response={201: SkillOut, 403: ErrorOut},
    summary="Create a skill",
)
def create_skill(request: HttpRequest, payload: SkillCreateIn):
    """Create a personal or organization skill.

    Organization skills require admin role.
    """
    if not check_api_key_permission(request, APIKeyPermission.SKILLS_WRITE):
        return 403, ErrorOut(
            detail="API key lacks permission: skills:write",
            code="permission_denied",
        )
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)

    svc = SkillService()
    try:
        if payload.organization_skill:
            is_admin = org_service.get_user_role(request.user, org_id) == "admin"
            if not is_admin:
                return 403, ErrorOut(
                    detail="Only org admins can create organization skills",
                    code="forbidden",
                )
            skill = svc.create_org_skill(
                name=payload.name,
                body=payload.body,
                org_id=org_id,
                user=request.user,
            )
        else:
            skill = svc.create_personal_skill(
                name=payload.name,
                body=payload.body,
                user=request.user,
            )
        return 201, _skill_to_out(skill)
    except ValueError as e:
        return 403, ErrorOut(detail=str(e), code="validation_error")


@skill_router.patch(
    "/{skill_id}/",
    response={200: SkillOut, 403: ErrorOut, 404: ErrorOut},
    summary="Update a skill",
)
def update_skill(request: HttpRequest, skill_id: uuid.UUID, payload: SkillUpdateIn):
    """Update a skill's name and/or body.

    Personal skills: only owner may edit.
    Org skills: only org admins may edit.
    """
    if not check_api_key_permission(request, APIKeyPermission.SKILLS_WRITE):
        return 403, ErrorOut(
            detail="API key lacks permission: skills:write",
            code="permission_denied",
        )
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    is_admin = org_service.get_user_role(request.user, org_id) == "admin"

    svc = SkillService()
    try:
        skill = svc.update_skill(
            skill_id,
            name=payload.name,
            body=payload.body,
            user=request.user,
            org_id=org_id,
            is_admin=is_admin,
        )
        return 200, _skill_to_out(skill)
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except AuthenticationError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)
    except ValueError as e:
        return 403, ErrorOut(detail=str(e), code="validation_error")


@skill_router.delete(
    "/{skill_id}/",
    response={204: None, 403: ErrorOut, 404: ErrorOut},
    summary="Delete a skill",
)
def delete_skill(request: HttpRequest, skill_id: uuid.UUID):
    """Delete a skill.

    Personal skills: only owner may delete.
    Org skills: only org admins may delete.
    """
    if not check_api_key_permission(request, APIKeyPermission.SKILLS_WRITE):
        return 403, ErrorOut(
            detail="API key lacks permission: skills:write",
            code="permission_denied",
        )
    org_id = _get_org_id(request)
    org_service = _get_org_service()
    org_service.require_membership(request.user, org_id)
    is_admin = org_service.get_user_role(request.user, org_id) == "admin"

    svc = SkillService()
    try:
        svc.delete_skill(
            skill_id, user=request.user, org_id=org_id, is_admin=is_admin
        )
        return 204, None
    except NotFoundError as e:
        return 404, ErrorOut(detail=e.message, code=e.code)
    except AuthenticationError as e:
        return 403, ErrorOut(detail=e.message, code=e.code)
