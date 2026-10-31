"""ASGI reverse proxy for KasmVNC desktop sessions.

Routes ``/ws/desktop/{workspace_id}/`` to the KasmVNC server running
inside the workspace VM.  Handles both HTTP requests (for the
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
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode

from asgiref.sync import sync_to_async
from django.conf import settings
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from .services import get_runner_service
from .services.infra.desktop_tunnels import register_ws_tunnel, unregister_ws_tunnel

logger = logging.getLogger(__name__)

# Match /ws/desktop/<uuid>/<rest>
_PATH_RE = re.compile(r"^/ws/desktop/(?P<workspace_id>[0-9a-f\-]{36})(?P<rest>/.*)$")

_COOKIE_NAME = "desktop_auth"
_COOKIE_MAX_AGE = 3600  # 1 hour

# KasmVNC 1.3.3 treats any iframe as Kasm VDI and starts a 5s idle timer
# that reads ``UI.rfb.lastActiveAt`` with no null check. After a websocket
# drop the RFB object is cleared but the interval keeps running, which
# surfaces as the "KasmVNC encountered an error" overlay. This script
# wraps ``setInterval`` before the client bundle runs.
_KASM_IDLE_GUARD_SCRIPT = (
    b"<script data-opencuria-kasm-idle-guard>"
    # The viewer uses our WebSocket proxy exclusively. KasmVNC 1.3.3 otherwise
    # creates a WebRTC peer with a hardcoded public Google STUN server on connect.
    # Disable that optional transport in this document before its bundle loads.
    b"window.RTCPeerConnection=undefined;"
    b"window.webkitRTCPeerConnection=undefined;"
    b"(function(){"
    b"var n=window.setInterval.bind(window);"
    b"window.setInterval=function(handler,timeout){"
    b"if(typeof handler!=='function'){return n.apply(this,arguments);}"
    b"var wrapped=function(){try{return handler.apply(this,arguments);}"
    b"catch(err){var msg=String((err&&err.message)||err);"
    b"if(msg.indexOf('lastActiveAt')!==-1){return;}"
    b"throw err;}};"
    b"var args=Array.prototype.slice.call(arguments);args[0]=wrapped;"
    b"return n.apply(this,args);};"
    b"})();"
    b"</script>"
)


def _viewer_query(query_string: str | bytes) -> dict:
    """Extract a validated manual-viewer association; observers have none."""
    query = parse_qs(
        query_string.decode() if isinstance(query_string, bytes) else query_string
    )
    try:
        client = str(uuid.UUID(query["viewer_client_id"][0]))
        revision = int(query["intent_revision"][0])
        if revision < 1:
            return {}
        return {"viewer_client_id": client, "intent_revision": revision}
    except (KeyError, ValueError, TypeError, IndexError):
        return {}


def build_vnc_redirect_url(
    workspace_id: str,
    token: str | None,
    viewer_client_id: str | None = None,
    intent_revision: int | None = None,
) -> str:
    """Return the Location for the KasmVNC client, with a safe WebSocket path.

    ``path`` is query-encoded so ``?token=`` stays inside the KasmVNC
    WebSocket path instead of being parsed as another ``vnc.html``
    parameter. ``reconnect=false`` disables KasmVNC's iframe VDI
    auto-reconnect, which races the idle timer against a torn-down
    ``UI.rfb``.
    """
    token_value = token or ""
    ws_path = f"ws/desktop/{workspace_id}/?token={token_value}"
    association = {}
    if viewer_client_id is not None and intent_revision is not None:
        association = {
            "viewer_client_id": viewer_client_id,
            "intent_revision": intent_revision,
        }
        ws_path += "&" + urlencode(association)
    query = urlencode(
        {
            "token": token_value,
            "autoconnect": "true",
            "resize": "scale",
            "reconnect": "false",
            "enable_webrtc": "false",
            "clipboard_up": "true",
            "clipboard_down": "true",
            # OpenCuria owns trusted copy/paste, not Kasm's background reads.
            "clipboard_seamless": "false",
            "path": ws_path,
            **association,
        }
    )
    return f"/ws/desktop/{workspace_id}/vnc.html?{query}"


def inject_kasm_idle_guard(html: bytes) -> bytes:
    """Enforce WebSocket-only transport and guard the iframe's idle timer."""
    if b"data-opencuria-kasm-idle-guard" in html:
        return html
    lower = html.lower()
    head_idx = lower.find(b"<head")
    if head_idx != -1:
        gt = html.find(b">", head_idx)
        if gt != -1:
            insert_at = gt + 1
            return html[:insert_at] + _KASM_IDLE_GUARD_SCRIPT + html[insert_at:]
    close_idx = lower.find(b"</head>")
    if close_idx != -1:
        return html[:close_idx] + _KASM_IDLE_GUARD_SCRIPT + html[close_idx:]

    # vnc.html may omit an explicit head. Keep any standards-mode doctype
    # first, and place the script inside the root element when available.
    html_idx = lower.find(b"<html")
    if html_idx != -1:
        gt = html.find(b">", html_idx)
        if gt != -1:
            insert_at = gt + 1
            return html[:insert_at] + _KASM_IDLE_GUARD_SCRIPT + html[insert_at:]
    doctype_idx = lower.find(b"<!doctype")
    if doctype_idx != -1:
        gt = html.find(b">", doctype_idx)
        if gt != -1:
            insert_at = gt + 1
            return html[:insert_at] + _KASM_IDLE_GUARD_SCRIPT + html[insert_at:]
    return _KASM_IDLE_GUARD_SCRIPT + html


def _is_vnc_html_path(rest_path: str) -> bool:
    """Return whether this proxied path is the KasmVNC client page."""
    return rest_path.split("?", 1)[0] == "/vnc.html"


def apply_vnc_client_patches(
    rest_path: str,
    headers: list[list[bytes]],
    body: bytes,
) -> tuple[list[list[bytes]], bytes]:
    """Inject the idle-timer guard into vnc.html and fix Content-Length."""
    if not _is_vnc_html_path(rest_path):
        return headers, body
    patched = inject_kasm_idle_guard(body)
    if patched == body:
        return headers, body
    headers = [item for item in headers if item[0].lower() != b"content-length"]
    headers.append([b"content-length", str(len(patched)).encode()])
    return headers, patched


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
    return get_runner_service().can_user_view_workspace_desktop(user_id, workspace_id)


@sync_to_async
def _desktop_workspace_available(workspace_id: str) -> bool:
    """Check the shared lifecycle gate without changing desktop authorization."""
    return get_runner_service().is_workspace_available(uuid.UUID(workspace_id))


async def _reject_unavailable_desktop(scope, send) -> None:
    """Reject live desktop traffic while a lifecycle hold owns the workspace."""
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 4009})
    else:
        await send({"type": "http.response.start", "status": 409, "headers": []})
        await send({"type": "http.response.body", "body": b"Workspace unavailable"})


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

    if not await _desktop_workspace_available(workspace_id):
        await _reject_unavailable_desktop(scope, send)
        return

    proxy_target = await _get_desktop_proxy_target(workspace_id)
    if proxy_target is None:
        logger.warning(
            "Desktop proxy: no active session for workspace %s", workspace_id
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4004})
        else:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send(
                {
                    "type": "http.response.body",
                    "body": b"No active desktop session",
                }
            )
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
            user_id=user_id_str,
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
    if not await _desktop_workspace_available(workspace_id):
        await _reject_unavailable_desktop(scope, send)
        return

    # If rest_path is just "/" redirect to the vnc.html page
    if rest_path == "/":
        # Always scale locally. ``resize=remote`` asks KasmVNC to send
        # SetDesktopSize and would shrink/grow the real X11 framebuffer
        # when switching between the chat mini viewer and fullscreen.
        association = _viewer_query(query_string)
        redirect_url = build_vnc_redirect_url(workspace_id, token, **association)
        resp_headers = [[b"location", redirect_url.encode()]]
        if cookie_header:
            resp_headers.append([b"set-cookie", cookie_header.encode()])
        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": resp_headers,
            }
        )
        await send({"type": "http.response.body", "body": b""})
        return

    try:
        from .services.infra.realtime import get_sio_server

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
            [key.encode(), value.encode()] for key, value in response.get("headers", [])
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

        headers, body_bytes = apply_vnc_client_patches(rest_path, headers, body_bytes)

        await send(
            {
                "type": "http.response.start",
                "status": int(response.get("status", 200)),
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body_bytes,
            }
        )
    except SocketIOTimeoutError:
        logger.error("Desktop HTTP proxy via runner timed out")
        await send({"type": "http.response.start", "status": 504, "headers": []})
        await send({"type": "http.response.body", "body": b"Gateway Timeout"})
    except Exception:
        logger.error("Desktop HTTP proxy via runner failed")
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
    user_id=None,
):
    """Reverse-proxy WebSocket connections to KasmVNC through the runner."""
    if not await _desktop_workspace_available(workspace_id):
        await _reject_unavailable_desktop(scope, send)
        return

    # Negotiate subprotocol — KasmVNC uses "binary"
    client_protocols = [
        p.decode() if isinstance(p, bytes) else p for p in scope.get("subprotocols", [])
    ]
    tunnel_id = uuid.uuid4().hex
    queue = register_ws_tunnel(
        tunnel_id, workspace_id, runner_id, runner_sid=runner_sid
    )
    loop_owns_cleanup = False

    try:
        from .services.infra.realtime import get_sio_server

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

        if not isinstance(response, dict) or response.get("ok") is not True:
            await send({"type": "websocket.close", "code": 1011})
            return

        accept_msg = {"type": "websocket.accept"}
        chosen_protocol = response.get("subprotocol")
        if chosen_protocol:
            accept_msg["subprotocol"] = chosen_protocol
        await send(accept_msg)
        loop_owns_cleanup = True
        await _ws_proxy_loop(
            receive,
            send,
            tunnel_id=tunnel_id,
            runner_sid=runner_sid,
            queue=queue,
            workspace_id=workspace_id,
            query_string=query_string,
            user_id=user_id,
        )
    except SocketIOTimeoutError:
        logger.error("Desktop WebSocket proxy via runner timed out")
        try:
            await send({"type": "websocket.close", "code": 1011})
        except Exception:
            pass
    except Exception:
        # Do not include upstream exception text or tracebacks: they can contain
        # request details from the proxied desktop session.
        logger.error("Desktop WebSocket proxy unexpected error")
        try:
            await send({"type": "websocket.close", "code": 1011})
        except Exception:
            pass
    finally:
        unregister_ws_tunnel(tunnel_id)
        if not loop_owns_cleanup:
            # A timed-out open may still complete on the runner. Retire its ID
            # even if no ACK arrived or the browser vanished before accept.
            cleanup_task = asyncio.create_task(
                _close_runner_tunnel(runner_sid, tunnel_id)
            )
            await asyncio.shield(cleanup_task)


async def _close_runner_tunnel(runner_sid: str, tunnel_id: str) -> None:
    """Retire a runner tunnel without exposing upstream request details."""
    from .services.infra.realtime import get_sio_server

    try:
        await get_sio_server().emit(
            "desktop:proxy_ws_close",
            {"tunnel_id": tunnel_id},
            to=runner_sid,
        )
    except Exception:
        logger.error("Desktop WebSocket runner tunnel close failed")


def _sanitize_websocket_close_code(value) -> int:
    """Return a close code that is valid to send in an ASGI WebSocket frame."""
    try:
        code = int(value)
    except (OverflowError, TypeError, ValueError):
        return 1011
    if code < 1000 or code >= 5000 or code in {1004, 1005, 1006, 1015}:
        return 1011
    return code


async def _ws_proxy_loop(
    receive,
    send,
    *,
    tunnel_id,
    runner_sid,
    queue,
    workspace_id=None,
    query_string="",
    user_id=None,
):
    """Bidirectional proxy between client ASGI WebSocket and the runner tunnel."""

    from .services.infra.realtime import get_sio_server

    runner_close_task: asyncio.Task | None = None
    unavailable_close_sent = False

    async def close_runner_tunnel():
        nonlocal runner_close_task
        if runner_close_task is None:
            runner_close_task = asyncio.create_task(
                _close_runner_tunnel(runner_sid, tunnel_id)
            )
        # Shield the close emit from cancellation of either forwarding task.
        await asyncio.shield(runner_close_task)

    async def close_unavailable_client():
        nonlocal unavailable_close_sent
        if not unavailable_close_sent:
            unavailable_close_sent = True
            await send({"type": "websocket.close", "code": 4009})

    async def client_to_upstream():
        """Forward messages from the browser to KasmVNC."""
        try:
            while True:
                message = await receive()
                msg_type = message.get("type", "")

                if msg_type == "websocket.receive":
                    if (
                        workspace_id is not None
                        and not await _desktop_workspace_available(workspace_id)
                    ):
                        await close_unavailable_client()
                        return
                    if "bytes" in message and message["bytes"]:
                        await get_sio_server().emit(
                            "desktop:proxy_ws_send",
                            {
                                "tunnel_id": tunnel_id,
                                "data": base64.b64encode(message["bytes"]).decode(
                                    "ascii"
                                ),
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
                    await close_runner_tunnel()
                    return
        except Exception:
            logger.error("Desktop WebSocket client-to-runner proxy failed")

    async def upstream_to_client():
        """Forward messages from KasmVNC to the browser."""
        try:
            while True:
                msg = await queue.get()
                if msg["type"] == "binary":
                    await send(
                        {
                            "type": "websocket.send",
                            "bytes": msg["data"],
                        }
                    )
                elif msg["type"] == "text":
                    await send(
                        {
                            "type": "websocket.send",
                            "text": msg["data"],
                        }
                    )
                elif msg["type"] == "close":
                    try:
                        await close_runner_tunnel()
                    except Exception:
                        logger.error("Desktop WebSocket runner tunnel close failed")
                    code = _sanitize_websocket_close_code(msg.get("code", 1000))
                    await send({"type": "websocket.close", "code": code})
                    break
        except Exception:
            logger.error("Desktop WebSocket runner-to-client proxy failed")

    async def monitor_workspace():
        """Retire an idle tunnel promptly when Capture acquires the workspace."""
        try:
            while await _desktop_workspace_available(workspace_id):
                await asyncio.sleep(0.1)
        except Exception:
            # A failed lifecycle check must never leave an input tunnel usable.
            logger.error("Desktop WebSocket lifecycle check failed")
        await close_unavailable_client()

    async def renew_viewer():
        """Tunnel liveness extends existing ownership, independent of tab visibility."""
        association = _viewer_query(query_string)
        if not association or not user_id:
            return
        service = get_runner_service()
        try:
            user, organization_id = await sync_to_async(service.resolve_desktop_viewer)(
                user_id, workspace_id
            )
            while True:
                await service.renew_desktop(
                    uuid.UUID(workspace_id),
                    user=user,
                    organization_id=organization_id,
                    viewer_client_id=uuid.UUID(association["viewer_client_id"]),
                    intent_revision=association["intent_revision"],
                )
                await asyncio.sleep(45)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Failure closes only this tunnel; never acquire or release here.
            await close_unavailable_client()

    # Run both directions concurrently and always await/cancel the other side.
    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    if workspace_id is not None:
        tasks.append(asyncio.create_task(monitor_workspace()))

    if user_id and _viewer_query(query_string):
        tasks.append(asyncio.create_task(renew_viewer()))

    async def cleanup():
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A forwarding task may have failed before initiating the close.
        await close_runner_tunnel()

    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # Preserve cleanup if this proxy task is cancelled during teardown.
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # A second cancellation must not cancel the close emit either.
                pass
            raise


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
    try:
        return get_runner_service().get_desktop_proxy_target(workspace_id)
    except Exception:
        return None
