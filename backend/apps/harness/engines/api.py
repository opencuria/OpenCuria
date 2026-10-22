"""REST API for harness engine availability and model catalogs."""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, NotFoundError

from .schemas import (
    EngineModelOut,
    HarnessEngineOut,
    engine_model_to_out,
)
from .service import EngineCatalogService

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
    """Return available harnesses for the active organization and user."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _permission_denied(APIKeyPermission.HARNESS_READ)
    try:
        organization_id = _require_membership(request)
        rows = EngineCatalogService().list_engines(organization_id, request.user.id)
        return 200, [HarnessEngineOut(**row) for row in rows]
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}


@engines_router.get(
    "/harness/engines/{engine_id}/models/",
    response={
        200: list[EngineModelOut],
        400: dict,
        401: dict,
        403: dict,
        404: dict,
        502: dict,
    },
    summary="List models supported by a harness engine",
)
def list_engine_models(request: HttpRequest, engine_id: str):
    """Return the selected-account-scoped engine catalog (also used by MCP)."""
    if not check_api_key_permission(request, APIKeyPermission.HARNESS_READ):
        return _permission_denied(APIKeyPermission.HARNESS_READ)
    try:
        organization_id = _require_membership(request)
        models = EngineCatalogService().list_models(
            engine_id, organization_id, request.user.id
        )
        return 200, [engine_model_to_out(model) for model in models]
    except AuthenticationError as exc:
        return 401, {"detail": exc.message, "code": exc.code}
    except NotFoundError as exc:
        return 404, {"detail": exc.message, "code": exc.code}
    except ValueError as exc:
        return 400, {"detail": str(exc), "code": "validation_error"}
    except Exception as exc:
        from ..providers.base import (
            ProviderAuthError,
            ProviderResponseError,
            ProviderTimeoutError,
        )

        if isinstance(
            exc, (ProviderAuthError, ProviderResponseError, ProviderTimeoutError)
        ):
            # Provider responses can echo credential-bearing request context.
            return 502, {
                "detail": "Engine model catalog is temporarily unavailable",
                "code": "provider_error",
            }
        raise
