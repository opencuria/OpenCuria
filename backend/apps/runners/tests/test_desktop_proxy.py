"""Regression tests for the desktop ASGI proxy."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.auth_backends import get_auth_backend
from apps.organizations.models import Membership, MembershipRole
from apps.runners.desktop_proxy import (
    _WS_TUNNELS,
    _proxy_websocket,
    _register_ws_tunnel,
    _unregister_ws_tunnel,
    _ws_proxy_loop,
    apply_vnc_client_patches,
    build_vnc_redirect_url,
    desktop_proxy_app,
    inject_kasm_idle_guard,
    patch_kasm_133_clipboard_bundle,
    push_runner_ws_closed,
    push_runner_ws_frame,
)
from apps.runners.models import Runner
from common.utils import hash_token


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
        available_runtimes=["docker"],
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
    monkeypatch.setattr("apps.runners.sio_server.get_sio_server", lambda: sio)
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
        "path": f"/ws/desktop/{workspace_id}/dist/style.bundle.css",
        "query_string": b"token=test-token",
        "headers": [],
    }

    events = await _call_http(scope)

    sio.call.assert_awaited_once_with(
        "desktop:proxy_http_request",
        {
            "workspace_id": workspace_id,
            "path": "/dist/style.bundle.css",
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
    assert "reconnect=false" in location
    assert f"path=ws%2Fdesktop%2F{workspace_id}%2F%3Ftoken%3Dtest-token" in location
    assert f"path=ws/desktop/{workspace_id}/?token=" not in location


@pytest.mark.asyncio
async def test_runner_frames_are_forwarded_to_registered_websocket_tunnel():
    tunnel_id = uuid.uuid4().hex
    queue = _register_ws_tunnel(
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
        _unregister_ws_tunnel(tunnel_id)


@pytest.mark.asyncio
async def test_runner_frames_from_wrong_runner_are_ignored():
    tunnel_id = uuid.uuid4().hex
    queue = _register_ws_tunnel(
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
        _unregister_ws_tunnel(tunnel_id)


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
    monkeypatch.setattr("apps.runners.sio_server.get_sio_server", lambda: sio)
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
    monkeypatch.setattr("apps.runners.sio_server.get_sio_server", lambda: sio)
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
    monkeypatch.setattr("apps.runners.sio_server.get_sio_server", lambda: sio)
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
    sio.call = AsyncMock(return_value={})
    monkeypatch.setattr("apps.runners.sio_server.get_sio_server", lambda: sio)
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


def test_build_vnc_redirect_url_encodes_ws_path_and_disables_reconnect():
    workspace_id = "297de18f-3cc8-4e14-b64a-35c80856d51b"
    location = build_vnc_redirect_url(workspace_id, "tok+/=x")

    assert location.startswith(f"/ws/desktop/{workspace_id}/vnc.html?")
    assert "autoconnect=true" in location
    assert "resize=scale" in location
    assert "reconnect=false" in location
    assert "clipboard_up=true" in location
    assert "clipboard_down=true" in location
    assert "clipboard_seamless=true" in location
    assert "path=ws%2Fdesktop%2F" in location
    assert "%3Ftoken%3D" in location
    assert f"path=ws/desktop/{workspace_id}/?token=" not in location


def test_provisioned_xvnc_start_sets_native_clipboard_mimes_and_qemu_scripts_match():
    root = Path(__file__).parents[4]
    qemu = (root / "backend/apps/runners/scripts/qemu_desktop_session.sh").read_bytes()
    packer = (root / "runner/packer/desktop-session.sh").read_bytes()
    assert qemu == packer
    assert b"-DLP_ClipTypes text/plain,text/html,image/png" in qemu

    lifecycle = (
        root / "backend/apps/runners/services/domains/image_lifecycle.py"
    ).read_text()
    start_line = next(
        line for line in lifecycle.splitlines() if "/usr/bin/Xvnc :1" in line
    )
    assert "-DLP_ClipTypes text/plain,text/html,image/png" in start_line


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


def test_apply_vnc_client_patches_pins_native_clipboard_patch_and_headers(monkeypatch):
    source = Path(__file__).with_name("fixtures") / "kasm_133_clipboard_excerpt.js"
    source = source.read_bytes()
    monkeypatch.setattr("apps.runners.desktop_proxy._KASM_133_BUNDLE_SIZE", len(source))
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._KASM_133_BUNDLE_SHA256",
        hashlib.sha256(source).hexdigest(),
    )
    headers = [
        [b"Content-Type", b"application/javascript"],
        [b"Content-Length", str(len(source)).encode()],
        [b"Content-Encoding", b"gzip"],
        [b"ETag", b"old"],
        [b"Content-MD5", b"old"],
    ]

    next_headers, patched = apply_vnc_client_patches(
        "/dist/main.bundle.js", headers, source
    )

    monkeypatch.setattr(
        "apps.runners.desktop_proxy._KASM_133_PATCHED_SHA256",
        hashlib.sha256(patched).hexdigest(),
    )
    monkeypatch.setattr(
        "apps.runners.desktop_proxy._KASM_133_PATCHED_BUNDLE_SIZE", len(patched)
    )

    # Generator-local var declaration survives the async regenerator resumes.
    paste_start = patched.index(b'key: "clipboardPasteDataFrom"')
    paste_end = patched.index(b'key: "requestBottleneckStats"', paste_start)
    generator = patched[paste_start:paste_end]
    assert b"var _opencuriaGeneration;" in generator
    assert b"_opencuriaGeneration = window.__opencuriaClipboardCapture();" in generator
    assert generator.index(b"var _opencuriaGeneration;") < generator.index(b"case 0:")
    assert (
        generator.count(
            b"window.__opencuriaClipboardAllowed(this, _opencuriaGeneration)"
        )
        == 2
    )
    assert b"navigator.clipboard.read().then(function (items)" in patched
    assert b"window.__opencuriaClipboardCanReceive(UI.rfb)" in patched
    assert (
        b"navigator.clipboard.write([new ClipboardItem(clipItemData)]).then("
        b"function () {" in patched
    )
    assert b"window.__opencuriaClipboardFallback(\n            'permission'" in patched
    assert (
        b"window.__opencuriaClipboardFallback(\n                'unavailable'"
        in patched
    )
    assert b'return _context.abrupt("return", dataset.length > 0)' in generator
    assert b"blob.size === 0" in generator
    assert b"message.kind === 'context'" in patched
    assert b"__opencuriaSuppressNextNativeRead" not in patched
    assert b"__opencuriaLastClipboardSentGeneration" not in patched
    assert b"sendBinaryClipboard" in generator

    header_map = {key.lower(): value for key, value in next_headers}
    assert header_map[b"content-length"] == str(len(patched)).encode()
    assert b"content-encoding" not in header_map
    assert b"etag" not in header_map
    assert b"content-md5" not in header_map
    assert header_map[b"content-type"] == b"application/javascript"

    with pytest.raises(ValueError, match="Unsupported KasmVNC"):
        patch_kasm_133_clipboard_bundle(source + b"unknown")
    with pytest.raises(ValueError, match="Unsupported KasmVNC"):
        patch_kasm_133_clipboard_bundle(b"UI.rfb = unknown")
    # The exact output of this trusted patch is idempotent, unknown markers are not.
    patch_kasm_133_clipboard_bundle.cache_clear()
    assert patch_kasm_133_clipboard_bundle(patched) == patched
    with pytest.raises(ValueError, match="Unsupported KasmVNC"):
        patch_kasm_133_clipboard_bundle(patched.replace(b"opencuria", b"attacker", 1))


def test_apply_vnc_client_patches_leaves_non_html_assets_unchanged():
    body = b"/* kasm bundle */ UI.rfb.lastActiveAt"
    headers = [[b"Content-Type", b"application/javascript"], [b"Content-Length", b"99"]]

    next_headers, next_body = apply_vnc_client_patches(
        "/dist/style.bundle.css",
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
    monkeypatch.setattr("apps.runners.sio_server.get_sio_server", lambda: sio)
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
