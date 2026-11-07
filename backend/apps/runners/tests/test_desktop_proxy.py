"""Regression tests for the desktop ASGI proxy."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.auth_backends import get_auth_backend
from apps.organizations.models import Membership, MembershipRole
from apps.runners.desktop_proxy import (
    _get_desktop_proxy_target,
    _proxy_websocket,
    _user_can_access_workspace,
    _ws_proxy_loop,
    apply_vnc_client_patches,
    build_vnc_redirect_url,
    desktop_proxy_app,
    inject_kasm_idle_guard,
)
from apps.runners.models import Runner
from apps.runners.services.infra.desktop_tunnels import (
    _WS_TUNNELS,
    push_runner_ws_closed,
    push_runner_ws_frame,
    register_ws_tunnel,
    unregister_ws_tunnel,
)
from apps.runners.voice_viewer_bridge import VOICE_VIEWER_BRIDGE
from common.utils import hash_token


@pytest.fixture(autouse=True)
def available_desktop_workspace(monkeypatch):
    """Existing isolated proxy tests model an available workspace."""
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._desktop_workspace_available",
        AsyncMock(return_value=True),
    )


async def _call_http(scope: dict) -> list[dict]:
    """Execute the proxy app for a single HTTP request and collect ASGI events."""
    events: list[dict] = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict):
        events.append(message)

    await desktop_proxy_app(scope, receive, send)
    return events


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_proxy_returns_not_found_for_member_without_workspace_access(
    organization,
    monkeypatch,
):
    owner_model = get_user_model()
    owner = owner_model.objects.create_user(
        email=f"desktop-owner-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    member = owner_model.objects.create_user(
        email=f"desktop-member-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(
        user=owner,
        organization=organization,
        role=MembershipRole.ADMIN,
    )
    Membership.objects.create(
        user=member,
        organization=organization,
        role=MembershipRole.MEMBER,
    )
    runner = Runner.objects.create(
        name="desktop-proxy-runner",
        api_token_hash=hash_token("runner-token"),
        status="online",
        organization=organization,
    )
    workspace = runner.workspaces.create(
        name="desktop-proxy-workspace",
        status="running",
        created_by=owner,
    )
    token = get_auth_backend().generate_tokens(member).access_token
    proxy_http = AsyncMock()
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._get_desktop_proxy_target",
        AsyncMock(
            return_value={
                "runner_sid": "runner-sid",
                "runner_id": str(runner.id),
                "desktop_info": {"port": 6901},
            }
        ),
    )
    monkeypatch.setattr("apps.runners.desktop_proxy._proxy_http", proxy_http)

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/ws/desktop/{workspace.id}/",
        "query_string": f"token={token}".encode(),
        "headers": [],
    }

    events = await _call_http(scope)

    assert events[0]["type"] == "http.response.start"
    assert events[0]["status"] == 404
    assert events[1]["type"] == "http.response.body"
    assert events[1]["body"] == b"Not Found"
    proxy_http.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_desktop_access_is_limited_to_owner_and_organization_admins(
    organization,
):
    user_model = get_user_model()
    owner, admin, member, outsider = (
        user_model.objects.create_user(
            email=f"desktop-{role}-{uuid.uuid4().hex[:8]}@example.com",
            password="secret",
        )
        for role in ("owner", "admin", "member", "outsider")
    )
    for user, role in (
        (owner, MembershipRole.MEMBER),
        (admin, MembershipRole.ADMIN),
        (member, MembershipRole.MEMBER),
    ):
        Membership.objects.create(user=user, organization=organization, role=role)
    runner = Runner.objects.create(
        name="desktop-access-runner",
        api_token_hash=hash_token(f"desktop-access-{uuid.uuid4().hex}"),
        status="online",
        organization=organization,
    )
    workspace = runner.workspaces.create(
        name="desktop-access-workspace", status="running", created_by=owner
    )
    workspace_id = str(workspace.id)

    assert await _user_can_access_workspace(str(owner.id), workspace_id) is True
    assert await _user_can_access_workspace(str(admin.id), workspace_id) is True
    assert await _user_can_access_workspace(str(member.id), workspace_id) is False
    assert await _user_can_access_workspace(str(outsider.id), workspace_id) is False
    assert await _user_can_access_workspace("not-a-user", workspace_id) is False
    assert await _user_can_access_workspace(str(owner.id), "not-a-uuid") is False
    assert await _user_can_access_workspace(str(owner.id), str(uuid.uuid4())) is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_proxy_target_needs_an_active_desktop_and_a_connected_runner(
    workspace, monkeypatch
):
    from apps.runners.services import get_runner_service

    workspace_id = str(workspace.id)
    assert await _get_desktop_proxy_target(workspace_id) is None

    desktops = dict(get_runner_service().session_store.state.desktops)
    desktops[workspace_id] = {"port": 6901}
    monkeypatch.setattr(
        get_runner_service().session_store.state, "desktops", desktops
    )
    assert await _get_desktop_proxy_target(workspace_id) == {
        "runner_id": str(workspace.runner_id),
        "runner_sid": workspace.runner.sid,
        "desktop_info": {"port": 6901},
    }

    Runner.objects.filter(pk=workspace.runner_id).update(sid="")
    assert await _get_desktop_proxy_target(workspace_id) is None
    assert await _get_desktop_proxy_target("not-a-uuid") is None


@pytest.mark.asyncio
async def test_proxy_http_fetches_asset_via_runner(monkeypatch):
    workspace_id = str(uuid.uuid4())
    sio = AsyncMock()
    sio.call = AsyncMock(
        return_value={
            "status": 200,
            "headers": [["Content-Type", "text/plain"]],
            "body": base64.b64encode(b"desktop asset").decode("ascii"),
            "body_encoding": "base64",
        }
    )
    monkeypatch.setattr(
        "common.realtime.get_sio_server", lambda: sio
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._validate_token",
        AsyncMock(return_value=SimpleNamespace(pk=1)),
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._user_can_access_workspace",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._get_desktop_proxy_target",
        AsyncMock(
            return_value={
                "runner_sid": "runner-sid",
                "runner_id": "runner-id",
                "desktop_info": {"port": 6901},
            }
        ),
    )

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/ws/desktop/{workspace_id}/dist/main.bundle.js",
        "query_string": b"token=test-token",
        "headers": [],
    }

    events = await _call_http(scope)

    sio.call.assert_awaited_once_with(
        "desktop:proxy_http_request",
        {
            "workspace_id": workspace_id,
            "path": "/dist/main.bundle.js",
            "query_string": "token=test-token",
            "method": "GET",
        },
        to="runner-sid",
        timeout=15,
    )
    assert events[0]["status"] == 200
    assert [b"Content-Type", b"text/plain"] in events[0]["headers"]
    assert events[1]["body"] == b"desktop asset"


@pytest.mark.asyncio
async def test_proxy_root_redirects_with_local_scale_not_remote_resize(
    monkeypatch,
):
    """Mini and fullscreen viewers must never ask KasmVNC to resize X11."""
    workspace_id = str(uuid.uuid4())
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._validate_token",
        AsyncMock(return_value=SimpleNamespace(pk=1)),
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._user_can_access_workspace",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._get_desktop_proxy_target",
        AsyncMock(
            return_value={
                "runner_sid": "runner-sid",
                "runner_id": "runner-id",
                "desktop_info": {"port": 6901},
            }
        ),
    )

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/ws/desktop/{workspace_id}/",
        "query_string": b"token=test-token&resize=remote",
        "headers": [],
    }

    events = await _call_http(scope)

    assert events[0]["type"] == "http.response.start"
    assert events[0]["status"] == 302
    location = dict(events[0]["headers"])[b"location"].decode()
    assert "vnc.html" in location
    assert "resize=scale" in location
    assert "resize=remote" not in location
    assert "autoconnect=true" in location
    redirect_query = parse_qs(urlsplit(location).query)
    assert redirect_query["clipboard_up"] == ["true"]
    assert redirect_query["clipboard_down"] == ["true"]
    assert redirect_query["clipboard_seamless"] == ["false"]
    assert redirect_query["enable_webrtc"] == ["false"]
    assert "reconnect=false" in location
    assert f"path=ws%2Fdesktop%2F{workspace_id}%2F%3Ftoken%3Dtest-token" in location
    assert f"path=ws/desktop/{workspace_id}/?token=" not in location


@pytest.mark.asyncio
async def test_runner_frames_are_forwarded_to_registered_websocket_tunnel():
    tunnel_id = uuid.uuid4().hex
    queue = register_ws_tunnel(
        tunnel_id,
        workspace_id=str(uuid.uuid4()),
        runner_id="runner-1",
    )
    try:
        await push_runner_ws_frame(
            tunnel_id,
            "runner-1",
            data=base64.b64encode(b"hello").decode("ascii"),
            encoding="base64",
        )
        await push_runner_ws_closed(tunnel_id, "runner-1", code=1001)

        first = await asyncio.wait_for(queue.get(), timeout=1)
        second = await asyncio.wait_for(queue.get(), timeout=1)

        assert first == {"type": "binary", "data": b"hello"}
        assert second == {"type": "close", "code": 1001}
    finally:
        unregister_ws_tunnel(tunnel_id)


@pytest.mark.asyncio
async def test_runner_disconnect_closes_only_its_desktop_transport(monkeypatch):
    """A vanished runner transport must not leave a seemingly connected viewer."""
    import socketio

    from apps.runners import sio_server

    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(server)
    service = Mock()
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    old_id, new_id = uuid.uuid4().hex, uuid.uuid4().hex
    old_queue = register_ws_tunnel(
        old_id, "workspace", "runner-1", runner_sid="old-sid"
    )
    new_queue = register_ws_tunnel(
        new_id, "workspace", "runner-1", runner_sid="replacement-sid"
    )
    try:
        await server.handlers["/"]["disconnect"]("old-sid")

        assert old_queue.get_nowait() == {"type": "close", "code": 1011}
        assert new_queue.empty()
        service.runner_lifecycle.unregister_runner.assert_called_once_with("old-sid")
    finally:
        unregister_ws_tunnel(old_id)
        unregister_ws_tunnel(new_id)


@pytest.mark.asyncio
async def test_runner_frames_from_wrong_runner_are_ignored():
    tunnel_id = uuid.uuid4().hex
    queue = register_ws_tunnel(
        tunnel_id,
        workspace_id=str(uuid.uuid4()),
        runner_id="runner-1",
    )
    try:
        await push_runner_ws_frame(
            tunnel_id,
            "runner-2",
            text="should-not-arrive",
        )

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)
    finally:
        unregister_ws_tunnel(tunnel_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_code", "expected_code"),
    [
        (1001, 1001),
        (1006, 1011),
        (1005, 1011),
        (1015, 1011),
        (999, 1011),
        (5000, 1011),
    ],
)
async def test_upstream_close_closes_runner_tunnel_and_sanitizes_code(
    monkeypatch,
    upstream_code,
    expected_code,
):
    sio = AsyncMock()
    monkeypatch.setattr(
        "common.realtime.get_sio_server", lambda: sio
    )
    tunnel_id = uuid.uuid4().hex
    queue = asyncio.Queue()
    await queue.put({"type": "close", "code": upstream_code})
    sent = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    await _ws_proxy_loop(
        receive,
        send,
        tunnel_id=tunnel_id,
        runner_sid="runner-sid",
        queue=queue,
    )

    sio.emit.assert_awaited_once_with(
        "desktop:proxy_ws_close",
        {"tunnel_id": tunnel_id},
        to="runner-sid",
    )
    assert sent == [{"type": "websocket.close", "code": expected_code}]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_side", ["send", "receive"])
async def test_proxy_loop_failures_close_runner_tunnel_once_without_reserved_code(
    monkeypatch,
    failure_side,
    caplog,
):
    sio = AsyncMock()
    monkeypatch.setattr(
        "common.realtime.get_sio_server", lambda: sio
    )
    tunnel_id = uuid.uuid4().hex
    queue = asyncio.Queue()
    if failure_side == "send":
        await queue.put({"type": "binary", "data": b"desktop-frame"})

    async def receive():
        if failure_side == "receive":
            raise RuntimeError("https://desktop.example/?token=secret-token")
        await asyncio.Event().wait()

    sent = []

    async def send(message):
        sent.append(message)
        if failure_side == "send":
            raise RuntimeError("https://desktop.example/?token=secret-token")

    await _ws_proxy_loop(
        receive,
        send,
        tunnel_id=tunnel_id,
        runner_sid="runner-sid",
        queue=queue,
    )

    sio.emit.assert_awaited_once_with(
        "desktop:proxy_ws_close",
        {"tunnel_id": tunnel_id},
        to="runner-sid",
    )
    assert not any(event.get("code") in {1005, 1006, 1015} for event in sent)
    assert "secret-token" not in caplog.text
    assert "desktop.example" not in caplog.text


@pytest.mark.asyncio
async def test_client_disconnect_closes_runner_tunnel(monkeypatch):
    sio = AsyncMock()
    monkeypatch.setattr(
        "common.realtime.get_sio_server", lambda: sio
    )
    tunnel_id = uuid.uuid4().hex

    async def receive():
        return {"type": "websocket.disconnect", "code": 1001}

    async def send(message):
        pass

    await _ws_proxy_loop(
        receive,
        send,
        tunnel_id=tunnel_id,
        runner_sid="runner-sid",
        queue=asyncio.Queue(),
    )

    sio.emit.assert_awaited_once_with(
        "desktop:proxy_ws_close",
        {"tunnel_id": tunnel_id},
        to="runner-sid",
    )


@pytest.mark.asyncio
async def test_proxy_websocket_unregisters_tunnel_after_loop(monkeypatch):
    sio = AsyncMock()
    sio.call = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(
        "common.realtime.get_sio_server", lambda: sio
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._ws_proxy_loop",
        AsyncMock(return_value=None),
    )
    sent = []
    registered_before = set(_WS_TUNNELS)

    async def send(message):
        sent.append(message)

    await _proxy_websocket(
        {"subprotocols": []},
        AsyncMock(),
        send,
        workspace_id=str(uuid.uuid4()),
        runner_sid="runner-sid",
        runner_id="runner-id",
        query_string="",
    )

    assert sent == [{"type": "websocket.accept"}]
    assert set(_WS_TUNNELS) == registered_before


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{"ok": False}, None, {}, {"ok": "true"}])
async def test_runner_rejected_handshake_never_accepts_browser(monkeypatch, response):
    """An upstream failure ACK must be handled immediately, without a fake accept."""
    sio = AsyncMock()
    sio.call.return_value = response
    monkeypatch.setattr(
        "common.realtime.get_sio_server", lambda: sio
    )
    send = AsyncMock()
    await _proxy_websocket(
        {"subprotocols": []},
        AsyncMock(),
        send,
        workspace_id=str(uuid.uuid4()),
        runner_sid="runner-sid",
        runner_id="runner-id",
        query_string="",
    )
    send.assert_awaited_once_with({"type": "websocket.close", "code": 1011})
    assert sio.emit.await_args.args[0] == "desktop:proxy_ws_close"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "error", "cancel", "accept"])
async def test_incomplete_websocket_handshake_closes_runner_tunnel(
    monkeypatch, failure, caplog
):
    """A missing ACK or browser must not leave an invisible VNC client alive."""
    from socketio.exceptions import TimeoutError as SocketIOTimeoutError

    exceptions = {
        "timeout": SocketIOTimeoutError(),
        "error": RuntimeError("https://desktop.example/?token=secret-token"),
        "cancel": asyncio.CancelledError(),
    }
    sio = AsyncMock()
    if failure in exceptions:
        sio.call.side_effect = exceptions[failure]
    else:
        sio.call.return_value = {"ok": True}
    monkeypatch.setattr(
        "common.realtime.get_sio_server", lambda: sio
    )
    sent = []
    registered_before = set(_WS_TUNNELS)

    async def send(message):
        if failure == "accept" and message["type"] == "websocket.accept":
            raise OSError("browser disconnected")
        sent.append(message)

    pending = _proxy_websocket(
        {"subprotocols": []},
        AsyncMock(),
        send,
        workspace_id=str(uuid.uuid4()),
        runner_sid="runner-sid",
        runner_id="runner-id",
        query_string="token=secret-token",
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        await pending

    tunnel_id = sio.call.await_args.args[1]["tunnel_id"]
    sio.emit.assert_awaited_once_with(
        "desktop:proxy_ws_close", {"tunnel_id": tunnel_id}, to="runner-sid"
    )
    assert set(_WS_TUNNELS) == registered_before
    assert "secret-token" not in caplog.text
    assert "desktop.example" not in caplog.text


def test_build_vnc_redirect_url_encodes_ws_path_and_disables_reconnect():
    workspace_id = "297de18f-3cc8-4e14-b64a-35c80856d51b"
    location = build_vnc_redirect_url(workspace_id, "tok+/=x")

    assert location.startswith(f"/ws/desktop/{workspace_id}/vnc.html?")
    assert "autoconnect=true" in location
    assert "resize=scale" in location
    assert "reconnect=false" in location
    assert "path=ws%2Fdesktop%2F" in location
    assert "%3Ftoken%3D" in location
    assert f"path=ws/desktop/{workspace_id}/?token=" not in location


def test_build_vnc_redirect_preserves_viewer_association_and_authentication():
    workspace_id = "297de18f-3cc8-4e14-b64a-35c80856d51b"
    client_id = "e6bd8acd-9977-4d3d-9b49-7c07adbd9eed"
    token = "authenticated-token"

    location = build_vnc_redirect_url(workspace_id, token, client_id, 12)
    query = parse_qs(urlsplit(location).query)
    ws_path_url = urlsplit(query["path"][0])
    ws_path = parse_qs(ws_path_url.query)

    assert query["token"] == [token]
    assert query["viewer_client_id"] == [client_id]
    assert query["intent_revision"] == ["12"]
    assert ws_path["token"] == [token]
    assert ws_path["viewer_client_id"] == [client_id]
    assert ws_path["intent_revision"] == ["12"]
    assert query["clipboard_up"] == ["true"]
    assert query["clipboard_down"] == ["true"]
    assert query["clipboard_seamless"] == ["false"]


def test_inject_kasm_idle_guard_inserts_into_head_and_is_idempotent():
    original = (
        b"<html><head lang='en'><title>KasmVNC</title></head><body></body></html>"
    )
    patched = inject_kasm_idle_guard(original)

    assert b"data-opencuria-kasm-idle-guard" in patched
    assert patched.startswith(b"<html><head lang='en'>")
    assert patched.count(b"data-opencuria-kasm-idle-guard") == 1
    assert inject_kasm_idle_guard(patched) == patched


def test_kasm_idle_guard_handles_only_last_active_exception():
    patched = inject_kasm_idle_guard(b"<html><head></head></html>")

    assert b"msg.indexOf('lastActiveAt')!==-1" in patched
    assert b"window.UI" not in patched
    assert b"throw err" in patched


def test_viewer_disables_public_stun_transport_before_loading_kasm():
    original = b'<html><head><script src="dist/main.bundle.js"></script></head></html>'
    patched = inject_kasm_idle_guard(original)
    assert patched.index(b"window.RTCPeerConnection=undefined;") < patched.index(
        b'<script src="dist/main.bundle.js"'
    )
    assert b"window.webkitRTCPeerConnection=undefined;" in patched


def test_voice_frame_bridge_accepts_only_embedding_parent_and_its_origin():
    """Execute the injected bridge to check its cross-origin security boundary."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the browser bridge")
    html = inject_kasm_idle_guard(b"<html><head></head></html>").decode()
    script = html.split("<script data-opencuria-kasm-idle-guard>", 1)[1].split(
        "</script>", 1
    )[0]
    check = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let message;
let captures = 0;
let time = 0;
const replies = [];
const listeners = {};
const parent = { postMessage: (...args) => replies.push(args) };
const attributes = () => ({
  attrs: new Set(),
  setAttribute(name) { this.attrs.add(name); },
  removeAttribute(name) { this.attrs.delete(name); }
});
const root = { ...attributes(), style: {
  setProperty() {}, removeProperty() {}
} };
let cursorStyle;
let pointerTick;
let timerCleared = false;
const window = { parent,
  setInterval: (handler) => { pointerTick = handler; return 1; },
  clearInterval: () => { timerCleared = true; },
  addEventListener: (type, handler) => { if (type === 'message') message = handler; } };
const framebuffer = { ...attributes(), width: 1920, height: 1080,
  getBoundingClientRect: () => ({ left: 40, top: 60, width: 960, height: 540 }) };
let hit = framebuffer;
const document = { referrer: 'https://app.example/chat',
  documentElement: root,
  head: { appendChild: (style) => { cursorStyle = style; } },
  elementFromPoint: () => hit,
  addEventListener: (type, handler, options) => {
    assert.equal(options.passive, true);
    assert.equal(options.capture, true);
    listeners[type] = handler;
  },
  querySelector: (selector) => {
    // The previous bridge captured the first arbitrary auxiliary canvas.
    assert.equal(selector, '#noVNC_container > div > canvas');
    return framebuffer;
  },
  createElement: (tag) => {
    if (tag === 'style') {
      return { textContent: '', remove() { cursorStyle = null; } };
    }
    assert.equal(tag, 'canvas');
    return { getContext: () => ({
        clearRect() {},
        drawImage: (source) => assert.equal(source, framebuffer),
        getImageData: () => ({ data: new Uint8ClampedArray([0, 0, 0, 255]) }) }),
      toBlob: (reply) => { captures++; reply('jpeg'); } };
  }
};
const performance = { timeOrigin: 1000, now: () => time };
vm.runInNewContext(SCRIPT, { window, document, URL, performance });
const data = { type: 'opencuria:voice-frame', requestId: 'request-1' };
message({ source: {}, origin: 'https://app.example', data });
message({ source: parent, origin: 'https://evil.example', data });
message({ source: parent, origin: 'https://app.example',
  data: { ...data, requestId: {} } });
assert.equal(captures, 0);
message({ source: parent, origin: 'https://app.example', data });
assert.equal(captures, 1);
assert.equal(replies[0][0].requestId, 'request-1');
assert.equal(replies[0][0].blob, 'jpeg');
assert.equal(replies[0][0].width, 1920);
assert.equal(replies[0][0].height, 1080);
assert.equal(replies[0][0].captured_at, 1000);
assert.equal(replies[0][1], 'https://app.example');
const pointer = { type: 'pointerdown', target: framebuffer, clientX: 200, clientY: 160,
  preventDefault: () => assert.fail('Recording must not block desktop input'),
  stopPropagation: () => assert.fail('Recording must not block desktop input') };
listeners.pointerdown(pointer);
assert.equal(replies.length, 1); // no observer outside an active recording
const sessionId = '11111111-1111-4111-8111-111111111111';
const observe = { type: 'opencuria:voice-observe', sessionId, enabled: true };
message({ source: parent, origin: 'https://evil.example', data: observe });
listeners.pointerdown(pointer);
assert.equal(replies.length, 1);
message({ source: parent, origin: 'https://app.example', data: observe });
listeners.pointerdown(pointer);
assert.equal(replies[1][0].type, 'opencuria:voice-pointer');
assert.equal(replies[1][0].sessionId, sessionId);
assert.equal(replies[1][0].x, 320);
assert.equal(replies[1][0].y, 200);
assert.equal(replies[1][0].event, 'down');
assert.equal(replies[1][0].observed_at, 1000);
assert.equal(framebuffer.attrs.has('data-voice-pointing-cursor'), false);
// Cursor feedback requires a same-size captured frame and a trusted parent.
const laser = { ...observe, cursor: 'url("data:image/svg+xml,laser") 12 12, crosshair',
  frameWidth: 1920, frameHeight: 1080 };
message({ source: parent, origin: 'https://evil.example', data: laser });
assert.equal(cursorStyle, undefined);
message({ source: parent, origin: 'https://app.example', data: laser });
assert.equal(framebuffer.attrs.has('data-voice-pointing-cursor'), true);
assert.ok(cursorStyle.textContent.includes('!important'));
assert.ok(cursorStyle.textContent.includes('body > canvas'));
// Resizing, letterboxing and viewer controls restore the normal cursor.
framebuffer.width = 1280;
pointerTick();
assert.equal(framebuffer.attrs.has('data-voice-pointing-cursor'), false);
framebuffer.width = 1920;
listeners.pointermove({ ...pointer, type: 'pointermove', clientX: 10 });
assert.equal(framebuffer.attrs.has('data-voice-pointing-cursor'), false);
listeners.pointerdown(pointer);
hit = {};
pointerTick();
assert.equal(framebuffer.attrs.has('data-voice-pointing-cursor'), false);
hit = framebuffer;
listeners.pointerdown(pointer);
assert.equal(framebuffer.attrs.has('data-voice-pointing-cursor'), true);
// Reproduce Kasm's real mouse capture sequence followed by a final move only
// 64ms after the previous one. It must survive throttling and remain sampled.
const captureElement = { ...attributes(), id: 'noVNC_mouse_capture_elem' };
time = 10;
listeners.pointermove({ ...pointer, type: 'pointermove' });
const beforeTransfer = replies.length;
time = 20;
listeners.pointerout({ ...pointer, type: 'pointerout',
  relatedTarget: captureElement });
assert.equal(replies.length, beforeTransfer);
time = 21;
hit = captureElement;
listeners.pointerup({ ...pointer, type: 'pointerup', target: captureElement });
assert.equal(replies[replies.length - 1][0].event, 'up');
assert.equal(captureElement.attrs.has('data-voice-pointing-cursor'), true);
time = 22;
hit = framebuffer;
listeners.pointerout({ ...pointer, type: 'pointerout', target: captureElement,
  relatedTarget: framebuffer });
assert.equal(replies[replies.length - 1][0].event, 'up');
time = 74;
const beforeFinalMove = replies.length;
listeners.pointermove({ ...pointer, type: 'pointermove',
  clientX: 440, clientY: 260 });
assert.equal(replies.length, beforeFinalMove); // immediate messages are throttled
for (const tick of [110, 210, 1210, 2210]) {
  time = tick;
  pointerTick();
  assert.equal(replies[replies.length - 1][0].x, 800);
  assert.equal(replies[replies.length - 1][0].y, 400);
  assert.equal(replies[replies.length - 1][0].event, 'move');
  assert.equal(replies[replies.length - 1][0].observed_at, 1000 + tick);
}
// Keepalive renewals retain the last position rather than starting over.
time = 2500;
message({ source: parent, origin: 'https://app.example', data: laser });
time = 3510;
pointerTick();
assert.equal(replies[replies.length - 1][0].x, 800);
const beforeExpiry = replies.length;
time = 5600;
pointerTick();
listeners.pointerdown(pointer);
assert.equal(replies.length, beforeExpiry);
assert.equal(timerCleared, true); // abandoned recordings expire in the viewer
assert.equal(cursorStyle, null);
assert.equal(framebuffer.attrs.has('data-voice-pointing-cursor'), false);
assert.equal(root.attrs.has('data-voice-pointing-active'), false);
// Explicit stop removes the feedback immediately, without waiting for expiry.
message({ source: parent, origin: 'https://app.example', data: laser });
listeners.pointerdown(pointer);
assert.equal(framebuffer.attrs.has('data-voice-pointing-cursor'), true);
message({ source: parent, origin: 'https://app.example',
  data: { ...observe, enabled: false } });
assert.equal(framebuffer.attrs.has('data-voice-pointing-cursor'), false);
assert.equal(cursorStyle, null);


"""
    subprocess.run(
        [node, "-e", "const SCRIPT=" + json.dumps(script) + ";" + check],
        check=True,
        capture_output=True,
        timeout=10,
    )


def test_voice_frame_bridge_encodes_only_changed_accepted_framebuffer_pixels():
    """Unchanged frames skip JPEG work without suppressing retries or new sessions."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the browser bridge")
    check = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let message;
let time = 0;
let captures = 0;
let failEncoding = false;
let failRead = false;
let deferEncoding = false;
let finishEncoding;
let pixels = new Uint8ClampedArray([0, 0, 0, 255, 0, 0, 0, 255]);
const replies = [];
const parent = { postMessage: (data) => replies.push(data) };
const framebuffer = { width: 2, height: 1 };
const window = { parent,
  addEventListener: (type, handler) => { if (type === 'message') message = handler; },
  setInterval: () => 1, clearInterval() {} };
const document = { referrer: 'https://app.example/chat',
  documentElement: { removeAttribute() {}, style: { removeProperty() {} } },
  addEventListener() {}, querySelector: () => framebuffer,
  createElement: () => {
    let frozen;
    return { getContext: () => ({
      clearRect() {},
      drawImage: () => { frozen = pixels.slice(); },
      getImageData: () => {
        if (failRead) throw Error('unreadable canvas');
        return { data: frozen };
      }
    }), toBlob: (reply) => {
      captures++;
      const finish = () => reply(failEncoding ? null : 'jpeg');
      if (deferEncoding) finishEncoding = finish;
      else finish();
    } };
  } };
vm.runInNewContext(SCRIPT, { window, document, URL,
  performance: { timeOrigin: 1000, now: () => time } });
const send = (data) => message({ source: parent, origin: 'https://app.example', data });
const capture = (previousFrameId) => {
  send({ type: 'opencuria:voice-frame', requestId: 'request', previousFrameId });
  return replies[replies.length - 1];
};
let first = capture();
assert.equal(captures, 1);
assert.equal(typeof first.frameId, 'string');
time = 500;
let same = capture(first.frameId);
assert.equal(captures, 1);
assert.equal(same.unchanged, true);
assert.equal(same.captured_at, 1500);
assert.equal(same.blob, undefined);
// One changed channel is enough, even if JPEG compression could hide it.
pixels[0] = 1;
let changed = capture(first.frameId);
assert.equal(captures, 2);
assert.notEqual(changed.frameId, first.frameId);
assert.equal(changed.blob, 'jpeg');
assert.equal(capture(changed.frameId).unchanged, true);
assert.equal(captures, 2);
// A stale/lost reply cannot falsely acknowledge the parent's older frame.
changed = capture(first.frameId);
assert.equal(captures, 3);
framebuffer.width = 3;
let resized = capture(changed.frameId);
assert.equal(captures, 4);
assert.equal(resized.width, 3);
// Failed reads/encoding preserve the last successful baseline for a retry.
pixels[1] = 2;
failEncoding = true;
assert.equal(capture(resized.frameId).blob, null);
assert.equal(captures, 5);
failEncoding = false;
failRead = true;
assert.equal(capture(resized.frameId).blob, null);
assert.equal(captures, 5);
failRead = false;
pixels[1] = 0;
assert.equal(capture(resized.frameId).unchanged, true);
assert.equal(captures, 5);
const observe = { type: 'opencuria:voice-observe',
  sessionId: '11111111-1111-4111-8111-111111111111', enabled: true };
send(observe);
let sessionFrame = capture(resized.frameId);
assert.equal(captures, 6); // entering a recording resets comparison state
send(observe);
assert.equal(capture(sessionFrame.frameId).unchanged, true);
assert.equal(captures, 6); // observation keepalives retain it
send({ ...observe, enabled: false });
send(observe);
sessionFrame = capture(sessionFrame.frameId);
assert.equal(captures, 7);
// An encoding that finishes after a session boundary cannot restore old pixels.
deferEncoding = true;
pixels[2] = 3;
capture(sessionFrame.frameId);
assert.equal(captures, 8);
send({ ...observe, enabled: false });
send(observe);
finishEncoding();
assert.equal(replies[replies.length - 1].frameId, undefined);
deferEncoding = false;
sessionFrame = capture(sessionFrame.frameId);
assert.equal(captures, 9);
assert.equal(capture(sessionFrame.frameId).unchanged, true);
"""
    subprocess.run(
        [
            node,
            "-e",
            "const SCRIPT=" + json.dumps(VOICE_VIEWER_BRIDGE.decode()) + ";" + check,
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )


def test_voice_frame_bridge_skips_idle_readback_and_tracks_rendering_lifecycle():
    """Dirty tracking avoids full-canvas work and fences async/render boundaries."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the browser bridge")
    check = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let message;
let readbacks = 0;
let snapshots = 0;
let encodes = 0;
let deferred;
let deferEncoding = false;
let failEncoding = false;
let failRead = false;
let pixels = new Uint8ClampedArray([0, 0, 0, 255]);
const replies = [];
const parent = { postMessage: (data) => replies.push(data) };
const methods = ['clearRect', 'drawImage', 'fill', 'fillRect', 'fillText',
  'putImageData', 'reset', 'stroke', 'strokeRect', 'strokeText'];
const context = Object.fromEntries(methods.map((name) => [name, function () {}]));
const originalFill = context.fillRect;
class Framebuffer {
  constructor() { this.w = 2; this.h = 1; }
  getContext() { return context; }
  get width() { return this.w; }
  set width(value) { this.w = value; pixels[0] = 0; }
  get height() { return this.h; }
  set height(value) { this.h = value; pixels[0] = 0; }
}
let framebuffer = new Framebuffer();
const window = { parent,
  addEventListener: (type, handler) => { if (type === 'message') message = handler; },
  setInterval: () => 1, clearInterval() {} };
const document = { referrer: 'https://app.example/chat',
  documentElement: { removeAttribute() {}, style: { removeProperty() {} } },
  addEventListener() {}, querySelector: () => framebuffer,
  createElement: () => {
    snapshots++;
    let frozen;
    return { getContext: () => ({
      clearRect() {},
      drawImage: () => { frozen = pixels.slice(); },
      getImageData: () => {
        readbacks++;
        if (failRead) throw Error('unreadable canvas');
        return { data: frozen };
      }
    }), toBlob: (reply) => {
      encodes++;
      const finish = () => reply(failEncoding ? null : 'jpeg');
      if (deferEncoding) deferred = finish;
      else finish();
    } };
  } };
vm.runInNewContext(SCRIPT, { window, document, URL,
  performance: { timeOrigin: 1000, now: () => 0 } });
const send = (data) => message({ source: parent, origin: 'https://app.example', data });
const capture = (previousFrameId) => {
  send({ type: 'opencuria:voice-frame', requestId: 'request', previousFrameId });
  return replies.at(-1);
};
const observe = { type: 'opencuria:voice-observe',
  sessionId: '11111111-1111-4111-8111-111111111111', enabled: true };
send(observe);
let frame = capture();
assert.equal(readbacks, 1);
assert.equal(encodes, 1);
assert.notEqual(context.fillRect, originalFill);
// Ten minutes' worth of idle 500 ms polls do no canvas work.
for (let i = 0; i < 1200; i++) {
  send(observe); // observation renewals must preserve the revision
  assert.equal(capture(frame.frameId).unchanged, true);
}
assert.equal(readbacks, 1);
assert.equal(encodes, 1);
assert.equal(snapshots, 1);
// Every drawing primitive invalidates the shortcut, including redundant draws.
for (const name of methods) {
  context[name]();
  const before = readbacks;
  assert.equal(capture(frame.frameId).unchanged, true);
  assert.equal(readbacks, before + 1);
  assert.equal(capture(frame.frameId).unchanged, true);
  assert.equal(readbacks, before + 1);
}
assert.equal(encodes, 1); // identical redraws still avoid encoding
pixels[0] = 1;
context.fillRect();
frame = capture(frame.frameId);
assert.equal(encodes, 2);
const accepted = frame;
pixels[0] = 2;
context.putImageData();
failRead = true;
assert.equal(capture(accepted.frameId).blob, null);
failRead = false;
failEncoding = true;
assert.equal(capture(accepted.frameId).blob, null);
failEncoding = false;
frame = capture(accepted.frameId);
assert.ok(frame.frameId);
// Reusing the snapshot never ties a successful frame to pixels drawn later.
pixels[0] = 3;
context.drawImage();
deferEncoding = true;
capture(frame.frameId);
pixels[0] = 4;
context.drawImage();
deferred();
frame = replies.at(-1);
deferEncoding = false;
const beforeLaterDraw = encodes;
frame = capture(frame.frameId);
assert.equal(encodes, beforeLaterDraw + 1);
assert.equal(capture(frame.frameId).unchanged, true);
// Same-size canvas resets and resize-away-and-back must not escape detection.
const beforeReset = readbacks;
framebuffer.width = framebuffer.width;
frame = capture(frame.frameId);
assert.equal(readbacks, beforeReset + 1);
assert.equal(frame.blob, 'jpeg');
framebuffer.height = 2;
framebuffer.height = 1;
assert.equal(capture(frame.frameId).unchanged, true);
assert.equal(readbacks, beforeReset + 2);
// A replacement of the same size still requires a new frame and restores hooks.
const old = framebuffer;
framebuffer = new Framebuffer();
const beforeReplacement = encodes;
frame = capture(frame.frameId);
assert.equal(encodes, beforeReplacement + 1);
assert.equal(Object.hasOwn(old, 'width'), false);
send({ ...observe, enabled: false });
assert.equal(context.fillRect, originalFill);
assert.equal(Object.hasOwn(framebuffer, 'width'), false);
assert.equal(Object.hasOwn(framebuffer, 'height'), false);
send(observe);
frame = capture(frame.frameId);
assert.equal(frame.blob, 'jpeg'); // new session cannot acknowledge an old image
send({ ...observe, enabled: false });
"""
    subprocess.run(
        [
            node,
            "-e",
            "const SCRIPT=" + json.dumps(VOICE_VIEWER_BRIDGE.decode()) + ";" + check,
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )


def test_inject_kasm_idle_guard_inserts_after_doctype_without_head():
    original = b'<!doctype html><html lang="en"><body>vnc</body></html>'

    patched = inject_kasm_idle_guard(original)

    assert patched.startswith(b'<!doctype html><html lang="en">')
    assert patched.index(b"<!doctype html>") < patched.index(
        b"data-opencuria-kasm-idle-guard"
    )
    assert patched.index(b"data-opencuria-kasm-idle-guard") < patched.index(b"<body>")


def test_apply_vnc_client_patches_rewrites_vnc_html_content_length():
    body = b"<html><head></head><body>ok</body></html>"
    headers = [
        [b"Content-Type", b"text/html"],
        [b"Content-Length", str(len(body)).encode()],
    ]

    next_headers, next_body = apply_vnc_client_patches("/vnc.html", headers, body)

    assert b"data-opencuria-kasm-idle-guard" in next_body
    assert next_body != body
    header_map = {key.lower(): value for key, value in next_headers}
    assert header_map[b"content-length"] == str(len(next_body)).encode()
    assert header_map[b"content-type"] == b"text/html"


def test_apply_vnc_client_patches_leaves_non_html_assets_unchanged():
    body = b"/* kasm bundle */ UI.rfb.lastActiveAt"
    headers = [[b"Content-Type", b"application/javascript"], [b"Content-Length", b"99"]]

    next_headers, next_body = apply_vnc_client_patches(
        "/dist/main.bundle.js",
        headers,
        body,
    )

    assert next_headers is headers
    assert next_body is body


@pytest.mark.asyncio
async def test_proxy_vnc_html_injects_idle_guard(monkeypatch):
    workspace_id = str(uuid.uuid4())
    original = b"<html><head></head><body>kasm</body></html>"
    sio = AsyncMock()
    sio.call = AsyncMock(
        return_value={
            "status": 200,
            "headers": [
                ["Content-Type", "text/html"],
                ["Content-Length", str(len(original))],
            ],
            "body": base64.b64encode(original).decode("ascii"),
            "body_encoding": "base64",
        }
    )
    monkeypatch.setattr(
        "common.realtime.get_sio_server", lambda: sio
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._validate_token",
        AsyncMock(return_value=SimpleNamespace(pk=1)),
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._user_can_access_workspace",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._get_desktop_proxy_target",
        AsyncMock(
            return_value={
                "runner_sid": "runner-sid",
                "runner_id": "runner-id",
                "desktop_info": {"port": 6901},
            }
        ),
    )

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/ws/desktop/{workspace_id}/vnc.html",
        "query_string": b"token=test-token",
        "headers": [],
    }

    events = await _call_http(scope)

    body = events[1]["body"]
    header_map = {key.lower(): value for key, value in events[0]["headers"]}
    assert events[0]["status"] == 200
    assert b"data-opencuria-kasm-idle-guard" in body
    assert header_map[b"content-length"] == str(len(body)).encode()
    assert header_map[b"content-type"] == b"text/html"


@pytest.mark.asyncio
async def test_manual_tunnel_renews_existing_lease_and_closes_without_release(
    monkeypatch,
):
    """Live tunnel renewal does not depend on visible-tab browser timers."""
    from apps.runners.services import get_runner_service

    workspace_id, client_id = str(uuid.uuid4()), str(uuid.uuid4())
    service = get_runner_service()
    renewed = asyncio.Event()
    viewer, organization_id = SimpleNamespace(id=1), uuid.uuid4()

    async def renew(*args, **kwargs):
        assert args == (uuid.UUID(workspace_id),)
        assert kwargs["user"] is viewer
        assert kwargs["organization_id"] == organization_id
        assert kwargs["viewer_client_id"] == uuid.UUID(client_id)
        assert kwargs["intent_revision"] == 3
        renewed.set()
        return {"viewer_lease_state": "held"}

    monkeypatch.setattr(service.interactive_sessions, "renew_desktop", renew)
    stop = AsyncMock()
    monkeypatch.setattr(service.interactive_sessions, "stop_desktop", stop)
    resolve = Mock(return_value=(viewer, organization_id))
    monkeypatch.setattr(service.workspace_access, "resolve_desktop_viewer", resolve)
    sio = SimpleNamespace(emit=AsyncMock())
    monkeypatch.setattr("common.realtime.get_sio_server", lambda: sio)

    async def receive():
        await renewed.wait()
        return {"type": "websocket.disconnect"}

    await asyncio.wait_for(
        _ws_proxy_loop(
            receive,
            AsyncMock(),
            tunnel_id="renew-tunnel",
            runner_sid="sid",
            queue=asyncio.Queue(),
            workspace_id=workspace_id,
            user_id="1",
            query_string=f"viewer_client_id={client_id}&intent_revision=3",
        ),
        2,
    )
    assert renewed.is_set()
    resolve.assert_called_once_with("1", workspace_id)
    stop.assert_not_awaited()
    assert sio.emit.await_args.args[0] == "desktop:proxy_ws_close"


@pytest.mark.asyncio
async def test_renew_lookup_failure_explicitly_closes_browser(monkeypatch):
    from apps.runners.exceptions import WorkspaceNotFoundError
    from apps.runners.services import get_runner_service

    service = get_runner_service()
    renew = AsyncMock()
    monkeypatch.setattr(service.interactive_sessions, "renew_desktop", renew)
    monkeypatch.setattr(
        service.workspace_access,
        "resolve_desktop_viewer",
        Mock(side_effect=WorkspaceNotFoundError("gone")),
    )
    sio = SimpleNamespace(emit=AsyncMock())
    monkeypatch.setattr("common.realtime.get_sio_server", lambda: sio)
    send = AsyncMock()

    async def receive():
        await asyncio.Event().wait()

    await asyncio.wait_for(
        _ws_proxy_loop(
            receive,
            send,
            tunnel_id="failed-renew",
            runner_sid="sid",
            queue=asyncio.Queue(),
            workspace_id=str(uuid.uuid4()),
            user_id="1",
            query_string=f"viewer_client_id={uuid.uuid4()}&intent_revision=1",
        ),
        2,
    )
    assert any(
        call.args[0]["type"] == "websocket.close" for call in send.await_args_list
    )
    assert sio.emit.await_args.args[0] == "desktop:proxy_ws_close"
    renew.assert_not_awaited()
