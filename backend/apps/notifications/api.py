"""JWT-only mobile registration and server-wide push capabilities."""

from __future__ import annotations

import uuid
from typing import Any

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import JWTAuth
from common.exceptions import AuthenticationError, NotFoundError
from common.schemas import ErrorOut

from .schemas import (
    ActivityRegistrationIn,
    ActivitySnapshotOut,
    DeviceOut,
    DeviceRegistrationIn,
    PushCapabilitiesOut,
)
from .services import NotificationService

notification_router = Router(tags=["mobile notifications"], auth=JWTAuth())


def _org_id(request: HttpRequest) -> uuid.UUID:
    try:
        return uuid.UUID(request.headers.get("X-Organization-Id", ""))
    except ValueError:
        raise ValueError("A valid X-Organization-Id header is required") from None


def _error(exc: Exception) -> tuple[int, ErrorOut]:
    if isinstance(exc, NotFoundError):
        return 404, ErrorOut(detail=exc.message, code=exc.code)
    if isinstance(exc, AuthenticationError):
        return 403, ErrorOut(detail=exc.message, code=exc.code)
    return 400, ErrorOut(detail=str(exc), code="invalid_notification_configuration")


ERRORS = {400: ErrorOut, 403: ErrorOut, 404: ErrorOut}


@notification_router.get(
    "/capabilities/", response={200: PushCapabilitiesOut, **ERRORS}
)
def capabilities(request: HttpRequest) -> Any:
    """Tell native settings which providers this server has configured."""
    try:
        return NotificationService().capabilities(request.user, _org_id(request))
    except (NotFoundError, AuthenticationError, ValueError) as exc:
        return _error(exc)


@notification_router.put("/devices/{device_id}/", response={200: DeviceOut, **ERRORS})
def register_device(
    request: HttpRequest, device_id: uuid.UUID, payload: DeviceRegistrationIn
) -> Any:
    """Save this installation's opt-in and current native token."""
    try:
        return NotificationService().register(
            request.user, _org_id(request), device_id, payload
        )
    except (NotFoundError, AuthenticationError, ValueError) as exc:
        return _error(exc)


@notification_router.delete("/devices/{device_id}/", response={204: None, **ERRORS})
def unregister_device(request: HttpRequest, device_id: uuid.UUID) -> Any:
    """Revoke a device and its pending deliveries; repeated deletion is safe."""
    try:
        NotificationService().unregister(request.user, _org_id(request), device_id)
        return 204, None
    except (NotFoundError, AuthenticationError, ValueError) as exc:
        return _error(exc)


@notification_router.put(
    "/devices/{device_id}/activity/", response={204: None, **ERRORS}
)
def register_activity(
    request: HttpRequest, device_id: uuid.UUID, payload: ActivityRegistrationIn
) -> Any:
    """Register a Live Activity token and immediately reconcile its current state."""
    try:
        NotificationService().register_activity(
            request.user, _org_id(request), device_id, payload
        )
        return 204, None
    except (NotFoundError, AuthenticationError, ValueError) as exc:
        return _error(exc)


@notification_router.get(
    "/devices/{device_id}/activity/", response={200: ActivitySnapshotOut, **ERRORS}
)
def activity_snapshot(request: HttpRequest, device_id: uuid.UUID) -> Any:
    """Hydrate the lock-screen state after reconnect and foreground activation."""
    try:
        return NotificationService().snapshot(request.user, _org_id(request), device_id)
    except (NotFoundError, AuthenticationError, ValueError) as exc:
        return _error(exc)
