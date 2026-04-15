"""ASGI reverse proxy for KasmVNC desktop sessions.

Routes ``/ws/desktop/{workspace_id}/`` to the KasmVNC server running
inside the workspace container.  Handles both HTTP requests (for the
KasmVNC web client static files) and WebSocket connections (for the
VNC data stream).

Authentication is enforced via JWT ``token`` query-parameter or a
``desktop_auth`` cookie (set automatically after the first
authenticated request so that KasmVNC sub-resources load without
needing the token on every URL).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import time
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from asgiref.sync import sync_to_async
from django.conf import settings
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

logger = logging.getLogger(__name__)

# Match /ws/desktop/<uuid>/<rest>
_PATH_RE = re.compile(r"^/ws/desktop/(?P<workspace_id>[0-9a-f\-]{36})(?P<rest>/.*)$")

_COOKIE_NAME = "desktop_auth"
_COOKIE_MAX_AGE = 3600  # 1 hour


@dataclass
class _RunnerWebSocketTunnel:
    """Backend-side state for a browser<->runner desktop tunnel."""

    workspace_id: str
    runner_id: str
    queue: asyncio.Queue[dict]


_WS_TUNNELS: dict[str, _RunnerWebSocketTunnel] = {}


def _sign_cookie(workspace_id: str, user_id: str, ts: int) -> str:
    """Create an HMAC-signed cookie value."""
    secret = settings.SECRET_KEY.encode()
    payload = f"{workspace_id}:{user_id}:{ts}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{sig}"


def _verify_cookie(value: str) -> tuple[str, str] | None:
    """Verify a signed cookie.  Returns (workspace_id, user_id) or None."""
    parts = value.split(":")
    if len(parts) != 4:
        return None
    workspace_id, user_id, ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    if time.time() - ts > _COOKIE_MAX_AGE:
        return None
    secret = settings.SECRET_KEY.encode()
    payload = f"{workspace_id}:{user_id}:{ts}"
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    return workspace_id, user_id


@sync_to_async
def _user_can_access_workspace(user_id: str, workspace_id: str) -> bool:
    """Return whether the authenticated user may access the workspace desktop."""
    from apps.organizations.models import Membership, MembershipRole
    from .repositories import WorkspaceRepository

    try:
        workspace_uuid = uuid.UUID(workspace_id)
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        return False

    workspace = WorkspaceRepository.get_by_id(workspace_uuid)
    if workspace is None:
        return False

    membership = Membership.objects.filter(
        user_id=user_id_int,
        organization_id=workspace.runner.organization_id,
    ).first()
    if membership is None:
        return False

    if membership.role == MembershipRole.ADMIN:
        return True

    return workspace.created_by_id == user_id_int


async def desktop_proxy_app(scope, receive, send):
    """ASGI application that proxies HTTP and WebSocket traffic to KasmVNC."""
    path = scope.get("path", "")
    match = _PATH_RE.match(path)
    if not match:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4004})
        else:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"Not Found"})
        return

    workspace_id = match.group("workspace_id")
    rest_path = match.group("rest")

    # --- Authenticate via JWT query parameter OR signed cookie ---
    query_string = scope.get("query_string", b"").decode("utf-8", errors="replace")
    params = parse_qs(query_string)
    token = (params.get("token") or [None])[0]

    user = None
    user_id_str = ""
    set_cookie = False  # Whether we need to set the auth cookie

    if token:
        user = await _validate_token(token)
        if user:
            user_id_str = str(user.pk)
            set_cookie = True
    else:
        # Try cookie-based auth
        cookie_value = _get_cookie_from_scope(scope, _COOKIE_NAME)
        if cookie_value:
            result = _verify_cookie(cookie_value)
            if result and result[0] == workspace_id:
                user_id_str = result[1]
                user = True  # Cookie is valid — no need to fetch user object

    if not user:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4001})
        else:
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b"Unauthorized"})
        return

    # --- Check workspace access and desktop availability ---
    if not await _user_can_access_workspace(user_id_str, workspace_id):
        logger.warning(
            "Desktop proxy denied workspace access (user=%s, workspace=%s)",
            user_id_str,
            workspace_id,
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4004})
        else:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"Not Found"})
        return

    proxy_target = await _get_desktop_proxy_target(workspace_id)
    if proxy_target is None:
        logger.warning("Desktop proxy: no active session for workspace %s", workspace_id)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4004})
        else:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"No active desktop session"})
        return

    cookie_header = None
    if set_cookie:
        cookie_value = _sign_cookie(workspace_id, user_id_str, int(time.time()))
        cookie_path = f"/ws/desktop/{workspace_id}/"
        cookie_header = (
            f"{_COOKIE_NAME}={cookie_value}; "
            f"Path={cookie_path}; "
            f"Max-Age={_COOKIE_MAX_AGE}; "
            f"HttpOnly; SameSite=Lax"
        )

    if scope["type"] == "websocket":
        await _proxy_websocket(
            scope,
            receive,
            send,
            workspace_id,
            proxy_target["runner_sid"],
            proxy_target["runner_id"],
            query_string,
        )
    elif scope["type"] == "http":
        await _proxy_http(
            scope,
            receive,
            send,
            proxy_target["runner_sid"],
            rest_path,
            workspace_id,
            token,
            query_string,
            cookie_header,
        )
    else:
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b"Bad Request"})


async def _proxy_http(
    scope,
    receive,
    send,
    runner_sid,
    rest_path,
    workspace_id,
    token,
    query_string,
    cookie_header,
):
    """Reverse-proxy HTTP requests to KasmVNC through the runner."""
    # If rest_path is just "/" redirect to the vnc.html page
    if rest_path == "/":
        base_path = f"/ws/desktop/{workspace_id}"
        # path= tells KasmVNC client where to open the WebSocket.
        # KasmVNC serves WS at root, so we proxy to / on the upstream.
        ws_path = f"ws/desktop/{workspace_id}/?token={token}"
        redirect_url = f"{base_path}/vnc.html?token={token}&autoconnect=true&resize=remote&path={ws_path}"
        resp_headers = [[b"location", redirect_url.encode()]]
        if cookie_header:
            resp_headers.append([b"set-cookie", cookie_header.encode()])
        await send({
            "type": "http.response.start",
            "status": 302,
            "headers": resp_headers,
        })
        await send({"type": "http.response.body", "body": b""})
        return

    try:
        from .sio_server import get_sio_server

        response = await get_sio_server().call(
            "desktop:proxy_http_request",
            {
                "workspace_id": workspace_id,
                "path": rest_path,
                "query_string": query_string,
                "method": scope.get("method", "GET"),
            },
            to=runner_sid,
            timeout=15,
        )

        headers = [
            [key.encode(), value.encode()]
            for key, value in response.get("headers", [])
        ]
        if cookie_header:
            headers.append([b"set-cookie", cookie_header.encode()])

        body = response.get("body", "")
        if response.get("body_encoding") == "base64":
            body_bytes = base64.b64decode(body)
        elif isinstance(body, str):
            body_bytes = body.encode()
        else:
            body_bytes = bytes(body)

        await send({
            "type": "http.response.start",
            "status": int(response.get("status", 200)),
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": body_bytes,
        })
    except SocketIOTimeoutError:
        logger.error("Desktop HTTP proxy via runner timed out")
        await send({"type": "http.response.start", "status": 504, "headers": []})
        await send({"type": "http.response.body", "body": b"Gateway Timeout"})
    except Exception as exc:
        logger.error("Desktop HTTP proxy via runner failed: %s", exc)
        await send({"type": "http.response.start", "status": 502, "headers": []})
        await send({"type": "http.response.body", "body": b"Bad Gateway"})


async def _proxy_websocket(
    scope,
    receive,
    send,
    workspace_id,
    runner_sid,
    runner_id,
    query_string,
):
    """Reverse-proxy WebSocket connections to KasmVNC through the runner."""
    # Negotiate subprotocol — KasmVNC uses "binary"
    client_protocols = [
        p.decode() if isinstance(p, bytes) else p
        for p in scope.get("subprotocols", [])
    ]
    tunnel_id = uuid.uuid4().hex
    queue = _register_ws_tunnel(tunnel_id, workspace_id, runner_id)

    try:
        from .sio_server import get_sio_server

        response = await get_sio_server().call(
            "desktop:proxy_ws_open",
            {
                "workspace_id": workspace_id,
                "tunnel_id": tunnel_id,
                "query_string": query_string,
                "subprotocols": client_protocols,
            },
            to=runner_sid,
            timeout=15,
        )

        accept_msg = {"type": "websocket.accept"}
        chosen_protocol = response.get("subprotocol")
        if chosen_protocol:
            accept_msg["subprotocol"] = chosen_protocol
        await send(accept_msg)
        await _ws_proxy_loop(
            receive,
            send,
            tunnel_id=tunnel_id,
            runner_sid=runner_sid,
            queue=queue,
        )
    except SocketIOTimeoutError:
        logger.error("Desktop WebSocket proxy via runner timed out")
        try:
            await send({"type": "websocket.close", "code": 1011})
        except Exception:
            pass
    except Exception:
        logger.exception("Desktop WebSocket proxy unexpected error")
        try:
            await send({"type": "websocket.close", "code": 1011})
        except Exception:
            pass
    finally:
        _unregister_ws_tunnel(tunnel_id)


async def _ws_proxy_loop(receive, send, *, tunnel_id, runner_sid, queue):
    """Bidirectional proxy between client ASGI WebSocket and the runner tunnel."""

    from .sio_server import get_sio_server

    async def client_to_upstream():
        """Forward messages from the browser to KasmVNC."""
        try:
            while True:
                message = await receive()
                msg_type = message.get("type", "")

                if msg_type == "websocket.receive":
                    if "bytes" in message and message["bytes"]:
                        await get_sio_server().emit(
                            "desktop:proxy_ws_send",
                            {
                                "tunnel_id": tunnel_id,
                                "data": base64.b64encode(message["bytes"]).decode("ascii"),
                                "encoding": "base64",
                            },
                            to=runner_sid,
                        )
                    elif "text" in message and message["text"]:
                        await get_sio_server().emit(
                            "desktop:proxy_ws_send",
                            {
                                "tunnel_id": tunnel_id,
                                "text": message["text"],
                            },
                            to=runner_sid,
                        )
                elif msg_type == "websocket.disconnect":
                    await get_sio_server().emit(
                        "desktop:proxy_ws_close",
                        {"tunnel_id": tunnel_id},
                        to=runner_sid,
                    )
                    return
        except Exception:
            pass

    async def upstream_to_client():
        """Forward messages from KasmVNC to the browser."""
        try:
            while True:
                msg = await queue.get()
                if msg["type"] == "binary":
                    await send({
                        "type": "websocket.send",
                        "bytes": msg["data"],
                    })
                elif msg["type"] == "text":
                    await send({
                        "type": "websocket.send",
                        "text": msg["data"],
                    })
                elif msg["type"] == "close":
                    code = int(msg.get("code", 1000))
                    if code < 1000:
                        code = 1000
                    if code >= 5000:
                        code = 1011
                    await send({"type": "websocket.close", "code": code})
                    break
        except Exception:
            pass

    # Run both directions concurrently
    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        # Await cancelled tasks to suppress warnings
        for t in pending:
            try:
                await t
            except asyncio.CancelledError:
                pass
    except Exception:
        for t in tasks:
            t.cancel()


def _get_cookie_from_scope(scope: dict, name: str) -> str | None:
    """Extract a cookie value from ASGI scope headers."""
    for header_name, header_value in scope.get("headers", []):
        if header_name == b"cookie":
            cookie = SimpleCookie()
            cookie.load(header_value.decode("utf-8", errors="replace"))
            if name in cookie:
                return cookie[name].value
    return None


async def _validate_token(token: str):
    """Validate a JWT token and return the user, or None."""
    from asgiref.sync import sync_to_async
    from apps.accounts.auth_backends import get_auth_backend

    try:
        backend = get_auth_backend()
        user = await sync_to_async(backend.validate_access_token)(token)
        return user
    except Exception:
        return None


@sync_to_async
def _get_desktop_proxy_target(workspace_id: str) -> dict | None:
    """Resolve the active desktop state and online runner socket for a workspace."""
    from .repositories import WorkspaceRepository
    from .sio_server import get_runner_service

    try:
        service = get_runner_service()
        desktop_info = service.get_desktop_info(workspace_id)
        if desktop_info is None:
            return None

        workspace = WorkspaceRepository.get_by_id(uuid.UUID(workspace_id))
        if workspace is None or not workspace.runner.sid:
            return None

        return {
            "runner_id": str(workspace.runner_id),
            "runner_sid": workspace.runner.sid,
            "desktop_info": desktop_info,
        }
    except Exception:
        return None


def _register_ws_tunnel(
    tunnel_id: str,
    workspace_id: str,
    runner_id: str,
) -> asyncio.Queue[dict]:
    """Register a backend-side queue for a live desktop WebSocket tunnel."""
    queue: asyncio.Queue[dict] = asyncio.Queue()
    _WS_TUNNELS[tunnel_id] = _RunnerWebSocketTunnel(
        workspace_id=workspace_id,
        runner_id=runner_id,
        queue=queue,
    )
    return queue


def _unregister_ws_tunnel(tunnel_id: str) -> None:
    """Drop backend-side state for a desktop WebSocket tunnel."""
    _WS_TUNNELS.pop(tunnel_id, None)


async def push_runner_ws_frame(
    tunnel_id: str,
    runner_id: str,
    *,
    text: str | None = None,
    data: str | None = None,
    encoding: str | None = None,
) -> None:
    """Deliver a runner-emitted desktop frame into the browser-facing queue."""
    tunnel = _WS_TUNNELS.get(tunnel_id)
    if tunnel is None or tunnel.runner_id != runner_id:
        return

    if text is not None:
        await tunnel.queue.put({"type": "text", "data": text})
        return

    if data is None:
        return

    payload = base64.b64decode(data) if encoding == "base64" else data.encode()
    await tunnel.queue.put({"type": "binary", "data": payload})


async def push_runner_ws_closed(
    tunnel_id: str,
    runner_id: str,
    *,
    code: int = 1000,
) -> None:
    """Notify the browser-facing side that the runner tunnel closed."""
    tunnel = _WS_TUNNELS.get(tunnel_id)
    if tunnel is None or tunnel.runner_id != runner_id:
        return
    await tunnel.queue.put({"type": "close", "code": code})
