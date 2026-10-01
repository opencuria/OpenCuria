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
from functools import lru_cache
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from asgiref.sync import sync_to_async
from django.conf import settings
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

logger = logging.getLogger(__name__)

# Match /ws/desktop/<uuid>/<rest>
_PATH_RE = re.compile(r"^/ws/desktop/(?P<workspace_id>[0-9a-f\-]{36})(?P<rest>/.*)$")

_COOKIE_NAME = "desktop_auth"
_COOKIE_MAX_AGE = 3600  # 1 hour
_KASM_133_BUNDLE_SHA256 = (
    "3f2d6ca7c7d12944441bd6450b5d74b312abd86b51ef7e04cefa9617205caa50"
)
_KASM_133_BUNDLE_SIZE = 777428
_KASM_133_PATCHED_SHA256 = (
    "0925de9391b292719772328ab070408efa0b2a6be1454121c10fcf7918e69d3c"
)
_KASM_133_PATCHED_BUNDLE_SIZE = 800631
_KASM_PATCH_ANCHOR = b"}; // Set up translations"
_KASM_RFB_FOCUS = b'    key: "_focusCanvas",\n    value: function _focusCanvas(event) {'
_KASM_PASTE_DATA_START = (
    b"                dataset = [];\n                mimes = [];\n"
    b"                h = 0;"
)
_KASM_PRIMARY_SEND = (
    b"                    RFB.messages.sendBinaryClipboard(this._sock, dataset, mimes);"
)
_KASM_SECONDARY_SEND = (
    b"                    this._proxyRFBMessage('sendBinaryClipboard', "
    b"[dataset, mimes]);"
)
_CLIPBOARD_BRIDGE = Path(__file__).with_name("assets") / "native_kasm_clipboard.js"

# KasmVNC 1.3.3 treats any iframe as Kasm VDI and starts a 5s idle timer
# that reads ``UI.rfb.lastActiveAt`` with no null check. After a websocket
# drop the RFB object is cleared but the interval keeps running, which
# surfaces as the "KasmVNC encountered an error" overlay. This script
# wraps ``setInterval`` before the client bundle runs.
_KASM_IDLE_GUARD_SCRIPT = (
    b"<script data-opencuria-kasm-idle-guard>"
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


def build_vnc_redirect_url(workspace_id: str, token: str | None) -> str:
    """Return the Location for the KasmVNC client, with a safe WebSocket path.

    ``path`` is query-encoded so ``?token=`` stays inside the KasmVNC
    WebSocket path instead of being parsed as another ``vnc.html``
    parameter. ``reconnect=false`` disables KasmVNC's iframe VDI
    auto-reconnect, which races the idle timer against a torn-down
    ``UI.rfb``.
    """
    token_value = token or ""
    ws_path = f"ws/desktop/{workspace_id}/?token={token_value}"
    query = urlencode(
        {
            "token": token_value,
            "autoconnect": "true",
            "resize": "scale",
            "reconnect": "false",
            "path": ws_path,
            "clipboard_up": "true",
            "clipboard_down": "true",
            "clipboard_seamless": "true",
        }
    )
    return f"/ws/desktop/{workspace_id}/vnc.html?{query}"


def inject_kasm_idle_guard(html: bytes) -> bytes:
    """Insert the idle-timer guard so it runs before KasmVNC scripts."""
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


def _is_kasm_bundle_path(rest_path: str) -> bool:
    """Return whether this is the version-pinned KasmVNC main bundle."""
    return rest_path.split("?", 1)[0] == "/dist/main.bundle.js"


def _patch_headers_for_body(
    headers: list[list[bytes]], body: bytes
) -> list[list[bytes]]:
    """Keep validators and framing valid after editing a proxied resource."""
    headers = [
        item
        for item in headers
        if item[0].lower()
        not in {b"content-length", b"content-encoding", b"etag", b"content-md5"}
    ]
    headers.append([b"content-length", str(len(body)).encode()])
    return headers


@lru_cache(maxsize=1)
def patch_kasm_133_clipboard_bundle(body: bytes) -> bytes:
    """Patch only the authenticated, exact KasmVNC 1.3.3 bundle."""
    digest = hashlib.sha256(body).hexdigest()
    if (
        digest == _KASM_133_PATCHED_SHA256
        and len(body) == _KASM_133_PATCHED_BUNDLE_SIZE
    ):
        return body
    if len(body) != _KASM_133_BUNDLE_SIZE or digest != _KASM_133_BUNDLE_SHA256:
        raise ValueError("Unsupported KasmVNC main bundle")

    required = (
        _KASM_PATCH_ANCHOR,
        _KASM_RFB_FOCUS,
        _KASM_PASTE_DATA_START,
        _KASM_PRIMARY_SEND,
        _KASM_SECONDARY_SEND,
    )
    if any(body.count(anchor) != 1 for anchor in required):
        raise ValueError("KasmVNC clipboard patch anchor mismatch")

    bridge = _CLIPBOARD_BRIDGE.read_bytes()
    paste_start = body.index(b'key: "clipboardPasteDataFrom"')
    paste_end = body.index(b'key: "requestBottleneckStats"', paste_start)
    paste = body[paste_start:paste_end]
    if paste.count(_KASM_PRIMARY_SEND) != 1 or paste.count(_KASM_SECONDARY_SEND) != 1:
        raise ValueError("KasmVNC clipboard send anchor mismatch")

    # Keep the generation in the async-generator closure, not the regenerator
    # state-machine handler: that handler is re-entered after every await.
    declaration = b"var dataset, mimes, h, i, ti, mime, blob, buff, data, _i2, _i3;"
    if paste.count(declaration) != 1:
        raise ValueError("KasmVNC clipboard generator declaration mismatch")
    paste = paste.replace(
        declaration,
        declaration + b"\n        var _opencuriaGeneration;",
        1,
    )
    case_zero = b"              case 0:\n                if (!(this._rfbConnectionState"
    if paste.count(case_zero) != 1:
        raise ValueError("KasmVNC clipboard generator entry mismatch")
    paste = paste.replace(
        case_zero,
        b"              case 0:\n"
        b"                _opencuriaGeneration = window.__opencuriaClipboardCapture();"
        b"\n                if (!(this._rfbConnectionState",
        1,
    )
    send_guard = (
        b"                    if (!window.__opencuriaClipboardAllowed(this, "
        b"_opencuriaGeneration)) { this._clipHash = 0; "
        b"this._resendClipboardNextUserDrivenEvent = true; return _context.stop(); }\n"
    )
    paste = paste.replace(_KASM_PRIMARY_SEND, send_guard + _KASM_PRIMARY_SEND, 1)
    paste = paste.replace(_KASM_SECONDARY_SEND, send_guard + _KASM_SECONDARY_SEND, 1)
    unchanged_anchor = (
        b"                Debug('No clipboard changes');\n"
        b'                return _context.abrupt("return");'
    )
    if paste.count(unchanged_anchor) != 1:
        raise ValueError("KasmVNC unchanged clipboard outcome anchor mismatch")
    paste = paste.replace(
        unchanged_anchor,
        b"                Debug('No clipboard changes');\n"
        b'                return _context.abrupt("return", true);',
        1,
    )
    empty_blob_anchor = b"              case 18:\n                _context.next = 20;"
    if paste.count(empty_blob_anchor) != 1:
        raise ValueError("KasmVNC empty clipboard MIME anchor mismatch")
    paste = paste.replace(
        empty_blob_anchor,
        b"              case 18:\n"
        b'                if (blob.size === 0) return _context.abrupt("continue", 37);'
        b"\n                _context.next = 20;",
        1,
    )
    send_completion = b'              case 45:\n              case "end":'
    if paste.count(send_completion) != 1:
        raise ValueError("KasmVNC clipboard outcome completion anchor mismatch")
    paste = paste.replace(
        send_completion,
        b'                return _context.abrupt("return", dataset.length > 0);\n'
        + send_completion,
        1,
    )

    patched = body[:paste_start] + paste + body[paste_end:]
    focus_start = patched.index(_KASM_RFB_FOCUS)
    focus_end = patched.index(b'key: "_setDesktopName"', focus_start)
    focus = patched[focus_start:focus_end]
    resend_marker = b"      if (this._resendClipboardNextUserDrivenEvent) {"
    if focus.count(resend_marker) != 1:
        raise ValueError("KasmVNC user-focus clipboard anchor mismatch")
    focus = focus.replace(
        resend_marker,
        b"      if (this._resendClipboardNextUserDrivenEvent && "
        b"window.__opencuriaClipboardCanRead()) {",
        1,
    )
    patched = patched[:focus_start] + focus + patched[focus_end:]

    receive_anchor = b"  clipboardReceive: function clipboardReceive(e) {"
    receive_start = patched.index(receive_anchor)
    receive_end = patched.index(
        b"  bottleneckStatsRecieve: function bottleneckStatsRecieve(e) {", receive_start
    )
    receive = patched[receive_start:receive_end]
    if receive.count(receive_anchor) != 1:
        raise ValueError("KasmVNC clipboard receive anchor mismatch")
    receive_gate = (
        b"      if (!window.__opencuriaClipboardCanReceive(UI.rfb)) return;\n"
    )
    patched = (
        patched[:receive_start]
        + receive.replace(receive_anchor, receive_anchor + b"\n" + receive_gate, 1)
        + patched[receive_end:]
    )

    # Guard every delayed browser write, preserving Kasm's native MIME handling.
    writer_key = b'key: "_write_binary_clipboard"'
    writer_start = patched.index(writer_key)
    writer_end = patched.index(b'key: "_handle_server_stats_msg"', writer_start)
    writer = patched[writer_start:writer_end]
    write_anchor = (
        b"    value: function _write_binary_clipboard(clipItemData, textdata) {\n"
        b"      var _this8 = this;"
    )
    if writer.count(write_anchor) != 1:
        raise ValueError("KasmVNC clipboard write anchor mismatch")
    writer = writer.replace(
        write_anchor,
        write_anchor
        + (
            b"\n      var _opencuriaWriteGeneration = "
            b"window.__opencuriaClipboardCapture();"
            b"\n      if (!window.__opencuriaClipboardCanWrite(this, "
            b"_opencuriaWriteGeneration)) return;"
        ),
        1,
    )
    write_marker = (
        b"      navigator.clipboard.write([new ClipboardItem(clipItemData)])"
        b".then(function () {"
    )
    writer_end_marker = b"\n    }\n  }, {\n    "
    if writer.count(write_marker) != 1 or writer.count(writer_end_marker) != 1:
        raise ValueError("KasmVNC clipboard writer await anchor mismatch")
    write_start = writer.index(write_marker)
    write_end = writer.index(writer_end_marker, write_start)
    writer_body = b"""      try {
        navigator.clipboard.write([new ClipboardItem(clipItemData)]).then(function () {
          var current = window.__opencuriaClipboardCanWrite(
            _this8, _opencuriaWriteGeneration);
          if (!current) return;
          if (textdata) {
            current = window.__opencuriaClipboardCanWrite(
              _this8, _opencuriaWriteGeneration);
            if (!current) return;
            _this8._clipHash = hashUInt8Array(textdata);
          }
        }, function (err) {
          var current = window.__opencuriaClipboardCanWrite(
            _this8, _opencuriaWriteGeneration);
          if (!current) return;
          logging_Error(\"Error writing to client clipboard: \" + err);
          window.__opencuriaClipboardFallback(
            'permission', _this8, _opencuriaWriteGeneration);
          if (textdata.length > 0) {
            try {
              navigator.clipboard.writeText(textdata).then(function () {
                var current = window.__opencuriaClipboardCanWrite(
                  _this8, _opencuriaWriteGeneration);
                if (!current) return;
                _this8._clipHash = hashUInt8Array(textdata);
              }, function (err) {
                var current = window.__opencuriaClipboardCanWrite(
                  _this8, _opencuriaWriteGeneration);
                if (!current) return;
                logging_Error(\"Error writing text to client clipboard: \" + err);
                window.__opencuriaClipboardFallback(
                  'permission', _this8, _opencuriaWriteGeneration);
              });
            } catch (err) {
              window.__opencuriaClipboardFallback(
                'unavailable', _this8, _opencuriaWriteGeneration);
            }
          }
        });
      } catch (err) {
        window.__opencuriaClipboardFallback(
          'unavailable', _this8, _opencuriaWriteGeneration);
      }"""
    writer = writer[:write_start] + writer_body + writer[write_end:]
    patched = patched[:writer_start] + writer + patched[writer_end:]
    return patched.replace(_KASM_PATCH_ANCHOR, _KASM_PATCH_ANCHOR + b"\n" + bridge, 1)


def apply_vnc_client_patches(
    rest_path: str,
    headers: list[list[bytes]],
    body: bytes,
) -> tuple[list[list[bytes]], bytes]:
    """Patch the VNC page and pinned native clipboard bridge bundle."""
    if _is_vnc_html_path(rest_path):
        patched = inject_kasm_idle_guard(body)
    elif _is_kasm_bundle_path(rest_path):
        patched = patch_kasm_133_clipboard_bundle(body)
    else:
        return headers, body
    if patched == body:
        return headers, body
    return _patch_headers_for_body(headers, patched), patched


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
        # Always scale locally. ``resize=remote`` asks KasmVNC to send
        # SetDesktopSize and would shrink/grow the real X11 framebuffer
        # when switching between the chat mini viewer and fullscreen.
        redirect_url = build_vnc_redirect_url(workspace_id, token)
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
):
    """Reverse-proxy WebSocket connections to KasmVNC through the runner."""
    # Negotiate subprotocol — KasmVNC uses "binary"
    client_protocols = [
        p.decode() if isinstance(p, bytes) else p for p in scope.get("subprotocols", [])
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
        # Do not include upstream exception text or tracebacks: they can contain
        # request details from the proxied desktop session.
        logger.error("Desktop WebSocket proxy unexpected error")
        try:
            await send({"type": "websocket.close", "code": 1011})
        except Exception:
            pass
    finally:
        _unregister_ws_tunnel(tunnel_id)


def _sanitize_websocket_close_code(value) -> int:
    """Return a close code that is valid to send in an ASGI WebSocket frame."""
    try:
        code = int(value)
    except (OverflowError, TypeError, ValueError):
        return 1011
    if code < 1000 or code >= 5000 or code in {1004, 1005, 1006, 1015}:
        return 1011
    return code


async def _ws_proxy_loop(receive, send, *, tunnel_id, runner_sid, queue):
    """Bidirectional proxy between client ASGI WebSocket and the runner tunnel."""

    from .sio_server import get_sio_server

    runner_close_task: asyncio.Task | None = None

    async def emit_runner_tunnel_close():
        try:
            await get_sio_server().emit(
                "desktop:proxy_ws_close",
                {"tunnel_id": tunnel_id},
                to=runner_sid,
            )
        except Exception:
            # Avoid logging exception details, which may include session data.
            logger.error("Desktop WebSocket runner tunnel close failed")

    async def close_runner_tunnel():
        nonlocal runner_close_task
        if runner_close_task is None:
            runner_close_task = asyncio.create_task(emit_runner_tunnel_close())
        # Shield the close emit from cancellation of either forwarding task.
        await asyncio.shield(runner_close_task)

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

    # Run both directions concurrently and always await/cancel the other side.
    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]

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
