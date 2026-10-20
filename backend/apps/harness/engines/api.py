"""REST API for harness engine availability and model catalogs."""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, NotFoundError

from .catalog import list_claude_models, list_engines
from .connections import EngineConnectionService
from .schemas import (
    ClaudeModelOut,
    HarnessEngineOut,
    claude_model_to_out,
)

engines_router = Router(tags=["harness-engines"])


def _permission_denied(permission: APIKeyPermission) -> tuple[int, dict[str, str]]:
    """Return the standard API-key permission error response."""
    return 403, {
        "detail": f"API key lacks permission: {permission.value}",
        "code": "permission_denied",
    }


def _organization_id(request: HttpRequest) -> uuid.UUID:
    """Read and validate the active organization request header."""
    raw_value = request.headers.get("X-Organization-Id", "")
    if not raw_value:
        raise AuthenticationError("X-Organization-Id header is required")
    try:
        return uuid.UUID(raw_value)
    except (TypeError, ValueError, AttributeError):
        raise AuthenticationError("Invalid X-Organization-Id header") from None


def _require_membership(request: HttpRequest) -> uuid.UUID:
    """Return the active organization ID after membership validation."""
    organization_id = _organization_id(request)
    OrganizationService().require_membership(request.user, organization_id)
    return organization_id


@engines_router.get(
    "/harness/engines/",
    response={200: list[HarnessEngineOut], 401: dict, 403: dict, 404: dict},
    summary="List harness engines and personal connection status",
)
def list_harness_engines(request: HttpRequest):
    """Return native and Claude harness engines for the active organization."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _permission_denied(APIKeyPermission.HARNESS_READ)
    try:
        organization_id = _require_membership(request)
        rows = list_engines(
            claude_connected=EngineConnectionService().is_connected(
                organization_id, request.user.id
            )
        )
        return 200, [HarnessEngineOut(**row) for row in rows]
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@engines_router.get(
    "/harness/engines/claude/models/",
    response={200: list[ClaudeModelOut], 401: dict, 403: dict, 404: dict},
    summary="List the Claude engine model catalog",
)
def list_claude_engine_models(request: HttpRequest):
    """Return constrained Claude models independently of provider adapters."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _permission_denied(APIKeyPermission.HARNESS_READ)
    try:
        _require_membership(request)
        return 200, [claude_model_to_out(model) for model in list_claude_models()]
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
