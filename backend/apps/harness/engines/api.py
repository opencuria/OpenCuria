"""REST API for harness engines and personal Claude connections."""

from __future__ import annotations

import uuid
from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, NotFoundError

from .catalog import ENGINE_IDS, list_claude_models
from .connections import EngineConnectionService

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


def _connection_view(
    organization_id: uuid.UUID, user_id: object
) -> dict[str, Any] | None:
    """Return the user's one connection without any credential material."""
    connections = EngineConnectionService().list_connections(organization_id, user_id)
    if not connections:
        return None
    connection = connections[0]
    return {
        "id": connection["id"],
        "auth_type": connection["auth_type"],
        "label": connection["label"],
        "connected": connection["connected"],
    }


class HarnessEngineOut(Schema):
    """Public engine availability and connection state."""

    id: str
    name: str
    modes: list[str]
    connected: bool


class EngineConnectionOut(Schema):
    """Safe public metadata for one personal Claude connection."""

    id: str
    auth_type: str
    label: str
    connected: bool


class EngineConnectionIn(Schema):
    """Personal Claude auth upsert payload; blank token preserves same mode."""

    auth_type: str
    token: str = ""
    label: str = "Claude"


class ClaudeModelOut(Schema):
    """Claude engine model using the shared picker model shape."""

    id: str
    name: str
    provider: str = "claude"
    reasoning_efforts: list[str] = []
    default_effort: str = ""
    supports_tools: bool = True
    context_length: int = 0
    max_output_tokens: int = 0


def _model_payload(model) -> ClaudeModelOut:
    """Convert the shared provider-model shape into this engine's API schema."""
    return ClaudeModelOut(
        id=model.id,
        name=model.name,
        provider=model.provider,
        reasoning_efforts=list(model.reasoning_efforts),
        default_effort=model.default_effort,
        supports_tools=model.supports_tools,
        context_length=model.context_length,
        max_output_tokens=model.max_output_tokens,
    )


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
        claude_connection = _connection_view(organization_id, request.user.id)
        rows = [
            HarnessEngineOut(
                id="native",
                name="OpenCuria",
                modes=["build", "plan"],
                connected=True,
            ),
            HarnessEngineOut(
                id="claude",
                name="Claude Agent",
                modes=["build", "plan"],
                connected=bool(claude_connection and claude_connection["connected"]),
            ),
        ]
        return 200, [row for row in rows if row.id in ENGINE_IDS]
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
        return 200, _connection_view(organization_id, request.user.id)
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
        )
        return 200, EngineConnectionOut(
            id=str(connection.id),
            auth_type=connection.auth_type,
            label=connection.label,
            connected=connection.credential_id is not None,
        )
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
def delete_claude_connection(request: HttpRequest):
    """Delete the caller's personal Claude authorization and stored token."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_PROVIDERS):
        return _permission_denied(APIKeyPermission.HARNESS_PROVIDERS)
    try:
        organization_id = _require_membership(request)
        EngineConnectionService().delete_connection(organization_id, request.user.id)
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
        return 200, [_model_payload(model) for model in list_claude_models()]
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
