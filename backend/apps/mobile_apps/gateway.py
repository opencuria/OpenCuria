"""ASGI gateway from published apps to the processes of their workspace.

``/ws/app-backends/<app_id>/<label>/<port>/<path>`` carries what the app sent
to ``http://<label>.opencuria.local:<port>/<path>``: the OpenCuria mobile app
rewrites those requests and adds the app session token. HTTP requests and
WebSockets are authorized and resolved by :class:`AppBackendService` and
forwarded by :mod:`.proxy`.
"""

from __future__ import annotations

import functools
import re

import structlog

from common.exceptions import ServiceError

from .backends import BackendError, get_app_backend_service
from .gateway_protocol import (
    GATEWAY_PREFIX,
    SESSION_HEADER,
    gateway_path,
    parse_address,
)
from .proxy import (
    ASGIReceive,
    ASGISend,
    Upstream,
    UpstreamError,
    forward_http,
    forward_websocket,
    send_error,
)

log = structlog.get_logger(__name__)

_PATH = re.compile(
    rb"^" + re.escape(GATEWAY_PREFIX.encode()) + rb"/(?P<app>[0-9a-fA-F-]{36})"
    rb"/(?P<label>[^/]+)/(?P<port>[^/]+)(?P<rest>/.*)?$"
)
_SESSION_HEADER = SESSION_HEADER.encode()


def _header(scope: dict, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


async def app_backend_gateway(
    scope: dict, receive: ASGIReceive, send: ASGISend
) -> None:
    """Forward one app request (HTTP or WebSocket) to its workspace process."""
    if scope["type"] not in ("http", "websocket"):
        return
    websocket = scope["type"] == "websocket"
    if websocket and (await receive())["type"] != "websocket.connect":
        return
    match = _PATH.match(scope.get("raw_path") or scope.get("path", "").encode())
    try:
        if match is None:
            raise ValueError("Unknown app backend path.")
        app_id = match["app"].decode()
        address = parse_address(match["label"].decode(), match["port"].decode())
    except ValueError as exc:
        await send_error(scope, send, 404, str(exc), "backend_not_found")
        return

    service = get_app_backend_service()
    try:
        claims = service.verify_session(_header(scope, _SESSION_HEADER), app_id)
        target = await service.resolve(claims, address)
        upstream = Upstream(
            open_stream=functools.partial(
                service.open_stream, target, websocket=websocket
            ),
            port=target.port,
            address=address,
            app_path=gateway_path(app_id),
        )
        path = (match["rest"] or b"/").decode("latin-1")
        forward = forward_websocket if websocket else forward_http
        async with service.keep_active(target.workspace_id):
            await forward(scope, receive, send, upstream, path)
    except BackendError as exc:
        log.info("app_backend_refused", app_id=app_id, host=address.host, code=exc.code)
        await send_error(
            scope, send, exc.status, exc.message, exc.code, retry_after=exc.retry_after
        )
    except UpstreamError as exc:
        service.forget(target)
        log.info("app_backend_unreachable", app_id=app_id, host=address.host)
        await send_error(
            scope,
            send,
            502,
            f"Process '{target.process_name}' did not answer on port "
            f"{target.port}: {exc}",
            "backend_unreachable",
        )
    except ServiceError as exc:
        log.warning(
            "app_backend_failed", app_id=app_id, host=address.host, code=exc.code
        )
        await send_error(scope, send, 503, exc.message, exc.code, retry_after=5)
