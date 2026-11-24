"""HTTP API: apps register native tokens, backends send encrypted messages.

The proxy never sees notification content. Every provider payload is built
here from a fixed template plus the backend's ciphertext, so a backend cannot
make the proxy show plaintext either.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import structlog
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Settings, load_settings
from .encoding import decode_server_key, server_key_thumbprint
from .handles import Destination, HandleSealer
from .providers import APNsClient, FCMClient, ProviderResult
from .ratelimit import RateLimiter
from .schemas import PushIn, RegistrationIn, RegistrationOut
from .vapid import VapidError, authenticate

log = structlog.get_logger(__name__)

REGISTRATION_BODY_LIMIT = 8 * 1024
PUSH_BODY_LIMIT = 16 * 1024
APNS_PAYLOAD_LIMIT = 4096
FCM_DATA_LIMIT = 4000
# Shown only if the app cannot decrypt in time (for example before first unlock).
ALERT_FALLBACK = {"title": "OpenCuria", "body": "New agent activity"}
# The app's Live Activity (expo-widgets) decodes {"name", "props"} content state.
LIVE_ACTIVITY_NAME = "OpenCuriaAgentActivity"
LIVE_ACTIVITY_COLLAPSE_ID = "opencuria-agent-activity"


class ApiError(Exception):
    """A JSON error response with a stable machine-readable code."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str | None = None,
        *,
        retry_after: int | None = None,
        provider_code: str = "",
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail
        self.retry_after = retry_after
        self.provider_code = provider_code

    def response(self) -> JSONResponse:
        """Render without echoing any request data."""
        body: dict[str, Any] = {"error": self.code}
        if self.detail:
            body["detail"] = self.detail
        if self.provider_code:
            body["provider_code"] = self.provider_code
        headers = {"retry-after": str(self.retry_after)} if self.retry_after else None
        return JSONResponse(body, status_code=self.status, headers=headers)


def _validation_detail(error: ValidationError) -> str:
    """Field paths and messages only; pydantic inputs may contain tokens."""
    first = error.errors(include_input=False, include_url=False)[0]
    location = ".".join(str(part) for part in first["loc"]) or "body"
    return f"{location}: {first['msg']}"


async def _read_json(request: Request, limit: int) -> Any:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "payload_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise ApiError(413, "payload_too_large")
    try:
        return json.loads(body)
    except ValueError:
        raise ApiError(400, "invalid_request", "Body must be JSON") from None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


class PushProxy:
    """Registration and delivery rules, independent of the HTTP framework."""

    def __init__(
        self,
        settings: Settings,
        *,
        apns: APNsClient | None,
        fcm: FCMClient | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.sealer = HandleSealer(settings.handle_keys)
        self.apns = apns
        self.fcm = fcm
        self.clock = clock
        # Generous per-IP limit: mobile carriers put many phones behind one IP.
        self.registrations = RateLimiter(rate=2, burst=60)
        self.senders = RateLimiter(rate=50, burst=500)
        self.handles = RateLimiter(rate=1, burst=30)

    def register(self, data: Any, client_ip: str) -> RegistrationOut:
        """Seal a native token for the backend whose key the app supplies."""
        retry_after = self.registrations.acquire(client_ip)
        if retry_after:
            raise ApiError(429, "rate_limited", retry_after=retry_after)
        try:
            registration = RegistrationIn.model_validate(data)
        except ValidationError as error:
            raise ApiError(400, "invalid_request", _validation_detail(error)) from None
        if registration.app_id not in self.settings.app_ids(registration.platform):
            raise ApiError(
                422,
                "unsupported_app",
                f"This push proxy does not serve {registration.app_id} "
                f"on {registration.platform}",
            )
        handle = self.sealer.seal(
            Destination(
                platform=registration.platform,
                app_id=registration.app_id,
                environment=registration.environment,
                kind=registration.kind,
                token=registration.token,
                server=server_key_thumbprint(
                    decode_server_key(registration.server_key)
                ),
            )
        )
        log.debug(
            "push_registration",
            platform=registration.platform,
            kind=registration.kind,
        )
        return RegistrationOut(handle=handle)

    async def push(self, data: Any, authorization: str | None) -> None:
        """Deliver one ciphertext; raise ApiError for anything but success."""
        try:
            sender = authenticate(authorization, self.settings.public_url, self.clock())
        except VapidError as error:
            raise ApiError(401, "unauthorized", str(error)) from None
        try:
            message = PushIn.model_validate(data)
        except ValidationError as error:
            raise ApiError(400, "invalid_request", _validation_detail(error)) from None
        retry_after = self.senders.acquire(sender.thumbprint)
        if retry_after:
            raise ApiError(429, "rate_limited", retry_after=retry_after)
        destination = self.sealer.open(message.handle)
        # A foreign handle looks exactly like a forged one: no existence oracle.
        if destination is None or destination.server != sender.thumbprint:
            raise ApiError(410, "invalid_handle")
        if (destination.kind == "alert") != (message.type == "alert"):
            raise ApiError(422, "wrong_handle_kind")
        if destination.app_id not in self.settings.app_ids(destination.platform):
            raise ApiError(422, "unsupported_app")
        retry_after = self.handles.acquire(_hash(message.handle))
        if retry_after:
            raise ApiError(429, "rate_limited", retry_after=retry_after)
        try:
            result = await self._send(destination, message)
        except httpx.TransportError:
            result = ProviderResult("retry", "provider_unreachable")
        log_event = log.debug if result.status == "sent" else log.warning
        log_event(
            "push_delivery",
            platform=destination.platform,
            type=message.type,
            status=result.status,
            code=result.code,
            sender=sender.thumbprint[:8],
        )
        if result.status == "sent":
            return
        if result.status == "unregistered":
            raise ApiError(410, "unregistered", provider_code=result.code)
        if result.status == "retry":
            raise ApiError(
                503,
                "provider_unavailable",
                retry_after=result.retry_after or 30,
                provider_code=result.code,
            )
        raise ApiError(502, "provider_rejected", provider_code=result.code)

    async def _send(self, destination: Destination, message: PushIn) -> ProviderResult:
        now = int(self.clock())
        if destination.platform == "android":
            if self.fcm is None:
                raise ApiError(422, "unsupported_app")
            data = {"enc": message.payload}
            if len(json.dumps(data, separators=(",", ":"))) > FCM_DATA_LIMIT:
                raise ApiError(413, "payload_too_large")
            return await self.fcm.send(
                token=destination.token,
                app_id=destination.app_id,
                data=data,
                ttl=message.ttl,
            )
        if self.apns is None:
            raise ApiError(422, "unsupported_app")
        if message.type == "alert":
            body: dict[str, Any] = {
                "aps": {
                    "alert": ALERT_FALLBACK,
                    "mutable-content": 1,
                    "sound": "default",
                },
                "enc": message.payload,
            }
            topic = destination.app_id
            push_type = "alert"
            priority = 10
            collapse_id = message.collapse_id
        else:
            aps: dict[str, Any] = {
                "timestamp": message.timestamp,
                "event": "end" if message.type == "activity_end" else "update",
                "content-state": {"name": LIVE_ACTIVITY_NAME, "props": message.payload},
            }
            if message.stale_date is not None:
                aps["stale-date"] = message.stale_date
            if message.dismissal_date is not None:
                aps["dismissal-date"] = message.dismissal_date
            body = {"aps": aps}
            topic = f"{destination.app_id}.push-type.liveactivity"
            push_type = "liveactivity"
            # Updates use the low-priority budget; ending must not be throttled.
            priority = 10 if message.type == "activity_end" else 5
            collapse_id = LIVE_ACTIVITY_COLLAPSE_ID
        payload = json.dumps(body, separators=(",", ":")).encode()
        if len(payload) > APNS_PAYLOAD_LIMIT:
            raise ApiError(413, "payload_too_large")
        return await self.apns.send(
            token=destination.token,
            environment=destination.environment,
            topic=topic,
            push_type=push_type,
            priority=priority,
            expiration=now + message.ttl if message.ttl else 0,
            payload=payload,
            collapse_id=collapse_id,
        )


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _register(request: Request) -> JSONResponse:
    proxy: PushProxy = request.app.state.proxy
    try:
        data = await _read_json(request, REGISTRATION_BODY_LIMIT)
        client_ip = request.client.host if request.client else "unknown"
        result = proxy.register(data, client_ip)
    except ApiError as error:
        return error.response()
    return JSONResponse(result.model_dump(), status_code=201)


async def _push(request: Request) -> JSONResponse:
    proxy: PushProxy = request.app.state.proxy
    try:
        data = await _read_json(request, PUSH_BODY_LIMIT)
        await proxy.push(data, request.headers.get("authorization"))
    except ApiError as error:
        return error.response()
    except Exception:
        log.error("push_delivery_crashed")
        return ApiError(500, "internal_error").response()
    return JSONResponse({"status": "sent"})


def create_app(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.time,
) -> Starlette:
    """Build the ASGI app; tests inject a mock provider HTTP client."""

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        http = client or httpx.AsyncClient(
            http2=True, timeout=httpx.Timeout(15.0, connect=5.0)
        )
        app.state.proxy = PushProxy(
            settings,
            apns=APNsClient(settings.apns, http) if settings.apns else None,
            fcm=FCMClient(settings.fcm, http) if settings.fcm else None,
            clock=clock,
        )
        try:
            yield
        finally:
            if client is None:
                await http.aclose()

    return Starlette(
        routes=[
            Route("/healthz", _health, methods=["GET"]),
            Route("/v1/registrations", _register, methods=["POST"]),
            Route("/v1/push", _push, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def create_app_from_env() -> Starlette:
    """uvicorn factory: fail fast on invalid settings."""
    return create_app(load_settings(os.environ))
