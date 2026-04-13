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
import hashlib
import hmac
import logging
import re
import time
import uuid
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

import aiohttp
from asgiref.sync import sync_to_async
from django.conf import settings

logger = logging.getLogger(__name__)

# Match /ws/desktop/<uuid>/<rest>
_PATH_RE = re.compile(r"^/ws/desktop/(?P<workspace_id>[0-9a-f\-]{36})(?P<rest>/.*)$")

_COOKIE_NAME = "desktop_auth"
_COOKIE_MAX_AGE = 3600  # 1 hour


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

    desktop_info = await _get_desktop_info(workspace_id)
    if desktop_info is None:
        logger.warning("Desktop proxy: no active session for workspace %s", workspace_id)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4004})
        else:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"No active desktop session"})
        return

    container_ip = desktop_info["container_ip"]
    port = desktop_info["port"]

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
        await _proxy_websocket(scope, receive, send, container_ip, port, rest_path, query_string)
    elif scope["type"] == "http":
        await _proxy_http(scope, receive, send, container_ip, port, rest_path, workspace_id, token, cookie_header)
    else:
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b"Bad Request"})


async def _proxy_http(scope, receive, send, container_ip, port, rest_path, workspace_id, token, cookie_header):
    """Reverse-proxy HTTP requests to KasmVNC's built-in web server."""
    upstream_url = f"http://{container_ip}:{port}{rest_path}"

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
        async with aiohttp.ClientSession() as session:
            async with session.get(upstream_url) as resp:
                # Build response headers
                headers = []
                for key, value in resp.headers.items():
                    lower = key.lower()
                    # Skip hop-by-hop headers
                    if lower in ("transfer-encoding", "connection", "keep-alive"):
                        continue
                    headers.append([key.encode(), value.encode()])

                if cookie_header:
                    headers.append([b"set-cookie", cookie_header.encode()])

                await send({
                    "type": "http.response.start",
                    "status": resp.status,
                    "headers": headers,
                })

                # Stream body
                body = await resp.read()
                await send({
                    "type": "http.response.body",
                    "body": body,
                })
    except aiohttp.ClientError as exc:
        logger.error("Desktop HTTP proxy upstream failed: %s", exc)
        await send({"type": "http.response.start", "status": 502, "headers": []})
        await send({"type": "http.response.body", "body": b"Bad Gateway"})
    except Exception:
        logger.exception("Desktop HTTP proxy unexpected error")
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b"Internal Server Error"})


async def _proxy_websocket(scope, receive, send, container_ip, port, rest_path, query_string):
    """Reverse-proxy WebSocket connections to KasmVNC."""
    # KasmVNC exposes the VNC WebSocket at /websockify and requires an Origin header
    upstream_url = f"ws://{container_ip}:{port}/websockify"
    upstream_origin = f"http://{container_ip}:{port}"

    # Negotiate subprotocol — KasmVNC uses "binary"
    client_protocols = [
        p.decode() if isinstance(p, bytes) else p
        for p in scope.get("subprotocols", [])
    ]
    chosen_protocol = "binary" if "binary" in client_protocols else None

    # Accept the client WebSocket with the matching subprotocol
    accept_msg = {"type": "websocket.accept"}
    if chosen_protocol:
        accept_msg["subprotocol"] = chosen_protocol
    await send(accept_msg)

    try:
        async with aiohttp.ClientSession() as session:
            protocols = ["binary"] if chosen_protocol else None
            async with session.ws_connect(
                upstream_url,
                protocols=protocols,
                max_msg_size=16 * 1024 * 1024,
                headers={"Origin": upstream_origin},
            ) as upstream_ws:
                await _ws_proxy_loop(receive, send, upstream_ws)
    except aiohttp.ClientError as exc:
        logger.error("Desktop proxy upstream connection failed: %s", exc)
        try:
            await send({"type": "websocket.close", "code": 1011})
        except Exception:
            pass
    except Exception:
        logger.exception("Desktop proxy unexpected error")
        try:
            await send({"type": "websocket.close", "code": 1011})
        except Exception:
            pass


async def _ws_proxy_loop(receive, send, upstream_ws):
    """Bidirectional proxy between client ASGI WebSocket and upstream aiohttp WS."""

    client_closed = asyncio.Event()

    async def client_to_upstream():
        """Forward messages from the browser to KasmVNC."""
        try:
            while True:
                message = await receive()
                msg_type = message.get("type", "")

                if msg_type == "websocket.receive":
                    if "bytes" in message and message["bytes"]:
                        await upstream_ws.send_bytes(message["bytes"])
                    elif "text" in message and message["text"]:
                        await upstream_ws.send_str(message["text"])
                elif msg_type == "websocket.disconnect":
                    await upstream_ws.close()
                    return
        except Exception:
            pass
        finally:
            client_closed.set()

    async def upstream_to_client():
        """Forward messages from KasmVNC to the browser."""
        try:
            async for msg in upstream_ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await send({
                        "type": "websocket.send",
                        "bytes": msg.data,
                    })
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    await send({
                        "type": "websocket.send",
                        "text": msg.data,
                    })
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except Exception:
            pass
        finally:
            try:
                await send({"type": "websocket.close", "code": 1000})
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
def _get_desktop_info(workspace_id: str) -> dict | None:
    """Get desktop session info from the RunnerService singleton."""
    from .sio_server import get_runner_service
    try:
        service = get_runner_service()
        return service.get_desktop_info(workspace_id)
    except Exception:
        return None
