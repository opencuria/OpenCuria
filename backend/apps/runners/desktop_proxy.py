"""ASGI reverse proxy for KasmVNC desktop sessions.

Routes ``/ws/desktop/{workspace_id}/`` to the KasmVNC server running
inside the workspace VM. HTTP requests serve the pinned KasmVNC web client
(``kasmvnc_client``): the viewer runs on the platform origin, so a workspace
can supply only byte-identical copies of its files, never its own HTML,
scripts, headers or redirects. WebSocket connections carry the VNC stream.

The viewer opens ``/ws/desktop/{workspace_id}/?ticket=…`` with a single-use
ticket (``desktop_tickets``). Redeeming it sets the path-scoped HttpOnly
``desktop_auth`` cookie that authenticates every later request. Access
tokens are never accepted in URLs, and query strings never reach the guest.
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

from common import realtime

from .desktop_tickets import redeem_desktop_ticket
from .kasmvnc_client import (
    ClientFile,
    accept_client_file,
    cached_client_file,
    lookup_client_file,
)
from .services import get_runner_service
from .services.infra.desktop_tunnels import register_ws_tunnel, unregister_ws_tunnel
from .voice_viewer_bridge import VOICE_VIEWER_BRIDGE

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
    + VOICE_VIEWER_BRIDGE
    + b"</script>"
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
    viewer_client_id: str | None = None,
    intent_revision: int | None = None,
) -> str:
    """Return the Location for the KasmVNC client, with a safe WebSocket path.

    The URL carries no credentials; the ``desktop_auth`` cookie does. ``path``
    is query-encoded so the viewer association stays inside the KasmVNC
    WebSocket path instead of being parsed as another ``vnc.html``
    parameter. ``reconnect=false`` disables KasmVNC's iframe VDI
    auto-reconnect, which races the idle timer against a torn-down
    ``UI.rfb``.
    """
    ws_path = f"ws/desktop/{workspace_id}/"
    association = {}
    if viewer_client_id is not None and intent_revision is not None:
        association = {
            "viewer_client_id": viewer_client_id,
            "intent_revision": intent_revision,
        }
        ws_path += "?" + urlencode(association)
    query = urlencode(
        {
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


def client_file_response(file: ClientFile, body: bytes) -> tuple[list, bytes]:
    """Return headers and body for a verified client file, never upstream's.

    Only vnc.html is patched (idle guard and voice bridge) and never cached,
    so a reload always picks up the current bridge.
    """
    if file.path == "vnc.html":
        body = inject_kasm_idle_guard(body)
        cache_control = b"no-store"
    else:
        cache_control = b"private, max-age=3600"
    headers = [
        [b"content-type", file.content_type.encode()],
        [b"content-length", str(len(body)).encode()],
        [b"cache-control", cache_control],
        [b"x-content-type-options", b"nosniff"],
    ]
    return headers, body


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
    return get_runner_service().workspace_access.can_user_view_workspace_desktop(
        user_id, workspace_id
    )


@sync_to_async
def _desktop_workspace_available(workspace_id: str) -> bool:
    """Check the shared lifecycle gate without changing desktop authorization."""
    return get_runner_service().ownership.is_workspace_available(
        uuid.UUID(workspace_id)
    )


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

    # --- Authenticate via a single-use ticket (entry page) OR signed cookie ---
    query_string = scope.get("query_string", b"").decode("utf-8", errors="replace")
    params = parse_qs(query_string)
    ticket = (params.get("ticket") or [None])[0]

    user_id_str = ""
    set_cookie = False  # Whether we need to set the auth cookie

    if ticket and scope["type"] == "http" and rest_path == "/":
        user_id = await _redeem_ticket(ticket, workspace_id)
        if user_id is not None:
            user_id_str = str(user_id)
            set_cookie = True
    if not user_id_str:
        # A reloaded entry page keeps working while its cookie is valid.
        cookie_value = _get_cookie_from_scope(scope, _COOKIE_NAME)
        if cookie_value:
            result = _verify_cookie(cookie_value)
            if result and result[0] == workspace_id:
                user_id_str = result[1]

    if not user_id_str:
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
        secure = "; Secure" if scope.get("scheme") == "https" else ""
        cookie_header = (
            f"{_COOKIE_NAME}={cookie_value}; "
            f"Path={cookie_path}; "
            f"Max-Age={_COOKIE_MAX_AGE}; "
            f"HttpOnly; SameSite=Lax{secure}"
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
            query_string,
            cookie_header,
        )
    else:
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b"Bad Request"})


async def _send_plain(send, status: int, body: bytes) -> None:
    """Send a short text response."""
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"text/plain"]],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _proxy_http(
    scope,
    receive,
    send,
    runner_sid,
    rest_path,
    workspace_id,
    query_string,
    cookie_header,
):
    """Serve the entry redirect and the pinned KasmVNC client files."""
    if not await _desktop_workspace_available(workspace_id):
        await _reject_unavailable_desktop(scope, send)
        return

    # If rest_path is just "/" redirect to the vnc.html page
    if rest_path == "/":
        # Always scale locally. ``resize=remote`` asks KasmVNC to send
        # SetDesktopSize and would shrink/grow the real X11 framebuffer
        # when switching between the chat mini viewer and fullscreen.
        association = _viewer_query(query_string)
        redirect_url = build_vnc_redirect_url(workspace_id, **association)
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

    if scope.get("method", "GET") != "GET":
        await _send_plain(send, 405, b"Method Not Allowed")
        return
    file = lookup_client_file(rest_path)
    if file is None:
        await _send_plain(send, 404, b"Not Found")
        return

    body = cached_client_file(file)
    if body is None:
        try:
            body = await _fetch_client_file(runner_sid, workspace_id, file)
        except SocketIOTimeoutError:
            logger.error("Desktop HTTP proxy via runner timed out")
            await _send_plain(send, 504, b"Gateway Timeout")
            return
        except Exception:
            logger.error("Desktop HTTP proxy via runner failed")
            await _send_plain(send, 502, b"Bad Gateway")
            return
        if body is None:
            logger.warning(
                "Desktop viewer file failed its integrity check "
                "(workspace=%s, file=%s)",
                workspace_id,
                file.path,
            )
            await _send_plain(send, 502, b"Bad Gateway")
            return

    headers, body = client_file_response(file, body)
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _fetch_client_file(
    runner_sid: str, workspace_id: str, file: ClientFile
) -> bytes | None:
    """Read *file* from the workspace; None unless it is the pinned file."""
    response = await realtime.get_sio_server().call(
        "desktop:proxy_http_request",
        {"workspace_id": workspace_id, "path": f"/{file.path}"},
        to=runner_sid,
        timeout=15,
    )
    if not isinstance(response, dict) or response.get("status") != 200:
        return None
    body = response.get("body", "")
    if response.get("body_encoding") == "base64":
        body_bytes = base64.b64decode(body)
    elif isinstance(body, str):
        body_bytes = body.encode()
    else:
        body_bytes = bytes(body)
    return body_bytes if accept_client_file(file, body_bytes) else None


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
        response = await realtime.get_sio_server().call(
            "desktop:proxy_ws_open",
            {
                "workspace_id": workspace_id,
                "tunnel_id": tunnel_id,
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
    try:
        await realtime.get_sio_server().emit(
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
                        await realtime.get_sio_server().emit(
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
                        await realtime.get_sio_server().emit(
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
            user, organization_id = await sync_to_async(
                service.workspace_access.resolve_desktop_viewer
            )(user_id, workspace_id)
            while True:
                await service.interactive_sessions.renew_desktop(
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


@sync_to_async
def _redeem_ticket(ticket: str, workspace_id: str) -> int | None:
    """Redeem a single-use desktop ticket and return its user ID."""
    return redeem_desktop_ticket(ticket, workspace_id)


@sync_to_async
def _get_desktop_proxy_target(workspace_id: str) -> dict | None:
    """Resolve the active desktop state and online runner socket for a workspace."""
    try:
        return get_runner_service().interactive_sessions.get_desktop_proxy_target(
            workspace_id
        )
    except Exception:
        return None
