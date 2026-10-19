"""REST API for harness engines and personal Claude connections."""

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
    EngineConnectionIn,
    EngineConnectionOut,
    HarnessEngineOut,
    claude_model_to_out,
    engine_connection_to_out,
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
        claude_connection = EngineConnectionService().connection_view(
            organization_id, request.user.id
        )
        rows = list_engines(
            claude_connected=bool(claude_connection and claude_connection["connected"])
        )
        return 200, [HarnessEngineOut(**row) for row in rows]
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@engines_router.get(
    "/harness/engines/claude/connection/",
    response={200: EngineConnectionOut | None, 401: dict, 403: dict, 404: dict},
    summary="Get the current user's Claude connection status",
)
def get_claude_connection(request: HttpRequest):
    """Return personal Claude connection metadata or null when disconnected."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _permission_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        organization_id = _require_membership(request)
        return 200, EngineConnectionService().connection_view(
            organization_id, request.user.id
        )
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@engines_router.put(
    "/harness/engines/claude/connection/",
    response={
        200: EngineConnectionOut,
        400: dict,
        401: dict,
        403: dict,
        404: dict,
    },
    summary="Save the current user's Claude connection",
)
def save_claude_connection(request: HttpRequest, payload: EngineConnectionIn):
    """Create or update only the caller's personal Claude authorization."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _permission_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        organization_id = _require_membership(request)
        connection = EngineConnectionService().save_connection(
            organization_id=organization_id,
            user=request.user,
            auth_type=payload.auth_type,
            token=payload.token,
            label=payload.label,
            connection_id=payload.connection_id,
        )
        return 200, engine_connection_to_out(connection)
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except (ValueError, TypeError) as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}


@engines_router.delete(
    "/harness/engines/claude/connection/",
    response={204: None, 401: dict, 403: dict, 404: dict, 409: dict},
    summary="Delete the current user's Claude connection",
)
def delete_claude_connection(
    request: HttpRequest, connection_id: uuid.UUID | None = None
):
    """Delete an explicitly scoped Claude account or the active compatibility one."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _permission_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        organization_id = _require_membership(request)
        EngineConnectionService().delete_connection(
            organization_id, request.user.id, connection_id
        )
        return 204, None
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except Exception as exc:
        from common.exceptions import ConflictError

        if isinstance(exc, ConflictError):
            return 409, {"detail": exc.message, "code": exc.code}
        raise


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
