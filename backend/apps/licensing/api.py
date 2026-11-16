"""Admin-only REST endpoints for Windows licensing, mounted at ``/licensing/``.

Every endpoint checks the API-key scope (organizations read/write) and the
organization admin role; members get 403.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from common.api_errors import api_key_permission_denied
from common.exceptions import AuthenticationError, ConflictError, NotFoundError
from common.schemas import ErrorOut

from .schemas import (
    WindowsLicensingOut,
    WindowsLicensingUpdateIn,
    WindowsProductKeyCreateIn,
    WindowsProductKeyOut,
    WindowsProductKeyUpdateIn,
)
from .services import LicensingValidationError, WindowsLicenseService

licensing_router = Router(tags=["licensing"])

_ERRORS = {400: ErrorOut, 403: ErrorOut, 404: ErrorOut, 409: ErrorOut}


def _handle(call: Callable[[], Any]) -> tuple[int, Any] | Any:
    """Run a service call and map its errors to ``{detail, code}`` responses."""
    try:
        return call()
    except LicensingValidationError as exc:
        return 400, ErrorOut(detail=exc.message, code=exc.code)
    except AuthenticationError as exc:
        return 403, ErrorOut(detail=exc.message, code=exc.code)
    except NotFoundError as exc:
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    except ConflictError as exc:
        return 409, ErrorOut(detail=exc.message, code=exc.code)


def _licensing(view: Any) -> tuple[int, WindowsLicensingOut]:
    return 200, WindowsLicensingOut(**asdict(view))


@licensing_router.get(
    "/organizations/{org_id}/windows/",
    response={200: WindowsLicensingOut, **_ERRORS},
    summary="Get Windows licensing",
)
def get_windows_licensing(request: HttpRequest, org_id: uuid.UUID):
    """Return the license model, product keys, licensed members and usage."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_READ):
        return api_key_permission_denied(APIKeyPermission.ORGANIZATIONS_READ)
    return _handle(
        lambda: _licensing(
            WindowsLicenseService().licensing(org_id=org_id, user=request.user)
        )
    )


@licensing_router.patch(
    "/organizations/{org_id}/windows/",
    response={200: WindowsLicensingOut, **_ERRORS},
    summary="Update Windows licensing",
)
def update_windows_licensing(
    request: HttpRequest, org_id: uuid.UUID, payload: WindowsLicensingUpdateIn
):
    """Change the license model, KMS host, user license seats or running cap."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_WRITE):
        return api_key_permission_denied(APIKeyPermission.ORGANIZATIONS_WRITE)
    return _handle(
        lambda: _licensing(
            WindowsLicenseService().update_policy(
                org_id=org_id,
                user=request.user,
                changes=payload.dict(exclude_unset=True),
            )
        )
    )


@licensing_router.post(
    "/organizations/{org_id}/windows/keys/",
    response={201: WindowsProductKeyOut, **_ERRORS},
    summary="Add a Windows product key",
)
def add_windows_product_key(
    request: HttpRequest, org_id: uuid.UUID, payload: WindowsProductKeyCreateIn
):
    """Add a Retail or MAK key; it is encrypted and never returned."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_WRITE):
        return api_key_permission_denied(APIKeyPermission.ORGANIZATIONS_WRITE)
    return _handle(
        lambda: (
            201,
            WindowsProductKeyOut(
                **asdict(
                    WindowsLicenseService().add_key(
                        org_id=org_id,
                        user=request.user,
                        product_key=payload.product_key.get_secret_value(),
                        label=payload.label,
                        seats=payload.seats,
                    )
                )
            ),
        )
    )


@licensing_router.patch(
    "/organizations/{org_id}/windows/keys/{key_id}/",
    response={200: WindowsProductKeyOut, **_ERRORS},
    summary="Update a Windows product key",
)
def update_windows_product_key(
    request: HttpRequest,
    org_id: uuid.UUID,
    key_id: uuid.UUID,
    payload: WindowsProductKeyUpdateIn,
):
    """Change a key's label or seats; seats cannot drop below used seats."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_WRITE):
        return api_key_permission_denied(APIKeyPermission.ORGANIZATIONS_WRITE)
    return _handle(
        lambda: (
            200,
            WindowsProductKeyOut(
                **asdict(
                    WindowsLicenseService().update_key(
                        org_id=org_id,
                        user=request.user,
                        key_id=key_id,
                        changes=payload.dict(exclude_unset=True),
                    )
                )
            ),
        )
    )


@licensing_router.delete(
    "/organizations/{org_id}/windows/keys/{key_id}/",
    response={204: None, **_ERRORS},
    summary="Delete a Windows product key",
)
def delete_windows_product_key(
    request: HttpRequest, org_id: uuid.UUID, key_id: uuid.UUID
):
    """Delete a key that no workspace uses."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_WRITE):
        return api_key_permission_denied(APIKeyPermission.ORGANIZATIONS_WRITE)

    def delete() -> tuple[int, None]:
        WindowsLicenseService().delete_key(
            org_id=org_id, user=request.user, key_id=key_id
        )
        return 204, None

    return _handle(delete)


@licensing_router.put(
    "/organizations/{org_id}/windows/users/{user_id}/",
    response={200: WindowsLicensingOut, **_ERRORS},
    summary="License a member for Windows",
)
def grant_windows_user_license(request: HttpRequest, org_id: uuid.UUID, user_id: int):
    """Grant a Windows user license to an active member."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_WRITE):
        return api_key_permission_denied(APIKeyPermission.ORGANIZATIONS_WRITE)
    return _handle(
        lambda: _licensing(
            WindowsLicenseService().grant_user(
                org_id=org_id, user=request.user, user_id=user_id
            )
        )
    )


@licensing_router.delete(
    "/organizations/{org_id}/windows/users/{user_id}/",
    response={200: WindowsLicensingOut, **_ERRORS},
    summary="Revoke a Windows user license",
)
def revoke_windows_user_license(
    request: HttpRequest, org_id: uuid.UUID, user_id: int
):
    """Revoke a user license; running workspaces keep running."""
    if not check_api_key_permission(request, APIKeyPermission.ORGANIZATIONS_WRITE):
        return api_key_permission_denied(APIKeyPermission.ORGANIZATIONS_WRITE)
    return _handle(
        lambda: _licensing(
            WindowsLicenseService().revoke_user(
                org_id=org_id, user=request.user, user_id=user_id
            )
        )
    )
