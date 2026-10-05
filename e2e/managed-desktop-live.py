"""Opt-in real QEMU/SSH/headed Playwright smoke, NOT a pytest gate.

Run with backend/.venv/bin/python e2e/managed-desktop-live.py --base-image PATH
--run-live. Requires libvirt/KVM, cloud-init tools and the runner dependencies.
Real loopback Socket.IO WebSockets connect killable runner/backend children.
Production MCP SDK, accessor, handlers, managers, SQLite and QEMU run unchanged.
This is not deployed control-plane authentication or database integration.
Artifacts and run-owned disks live outside the repository. No existing guest,
backend database or credentials are used. The base image is read-only backing.
Run against a quiescent libvirt control plane: an unrelated connected runner's
unknown-workspace reconciler otherwise deletes this unregistered fixture.
Provisioning downloads Node 22 and the current MCP browser in the guest only.
The second MCP connection tests independent ownership, not a second browser.
Backend and runner children are actually SIGKILLed. Backend expiry injects an
overdue SQLite timestamp for speed; runner restart uses fresh managers/epoch
and verifies old browser group death, not transparent adoption.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import signal
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "backend"), str(REPO / "runner")]
# Same Python ABI required. Backend environment owns MCP/Django; runner adds
# libvirt and runtime dependencies without installing production dependencies.
sys.path[:0] = [
    str(p) for p in (REPO / "runner/.venv/lib").glob("python*/site-packages")
]
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


async def smoke(args: argparse.Namespace) -> None:
    """Provision one disposable guest and exercise actual desktop owners."""
    import django
    import structlog

    django.setup()
    from src.config import RunnerSettings
    from src.interfaces.websocket import WebSocketInterface
    from src.models import WorkspaceInfo
    from src.runtime.base import WorkspaceConfig
    from src.runtime.qemu_runtime import QemuRuntime
    from src.service import WorkspaceService

    from apps.harness.access import runner_accessor as routes
    from apps.harness.desktop_leases import DesktopLease
    from apps.harness.mcp_client.runtime import McpRuntime
    from apps.plugins.runtime_snapshot import (
        DesktopResourceSnapshot,
        EffectivePluginSnapshot,
        PluginMcpServerSnapshot,
        PluginResourcesSnapshot,
        PreparedPluginRuntime,
        WorkspacePluginSnapshot,
    )

    root = args.artifacts.resolve() / str(uuid.uuid4())
    if root.is_relative_to(REPO):
        raise ValueError("Artifacts must be outside the repository")
    root.mkdir(parents=True, mode=0o755)
    for directory in ("images", "disks", "snapshots"):
        (root / directory).mkdir(mode=0o755)
    identity = uuid.uuid4()
    report: dict[str, Any] = {
        "workspace_id": str(identity),
        "transport": "in-process Socket.IO bridge",
        "real_components": [
            "McpRuntime",
            "McpServerConnection",
            "DesktopLease",
            "RunnerWorkspaceAccessor",
            "WebSocketInterface",
            "WorkspaceService managers",
            "QemuRuntime",
            "asyncssh",
        ],
        "limitations": [
            "loopback test server bypasses deployed authentication/DB",
            "backend crash expiry uses injected overdue timestamp",
            "second plugin connection holds display without a second browser",
        ],
        "checks": [],
        "passed": False,
        "cleanup": False,
    }

    def save() -> None:
        (root / "results.json").write_text(json.dumps(report, indent=2))

    settings = RunnerSettings(
        _env_file=None,
        api_token="",
        state_dir=str(root / "state"),
        enabled_runtimes="qemu",
        qemu_image_cache_dir=str(root / "images"),
        qemu_disk_dir=str(root / "disks"),
        qemu_snapshot_dir=str(root / "snapshots"),
        qemu_ssh_key_path=str(root / "runner_key"),
        qemu_ssh_timeout=180,
        qemu_memory_mb=args.memory_mb,
        desktop_lease_interval=1,
        qemu_vcpus=1,
        qemu_disk_size_gb=20,
    )
    runtime = QemuRuntime(settings)
    service = WorkspaceService({"qemu": runtime}, settings)
    interface = WebSocketInterface(service, settings)

    import socketio
    from aiohttp import web

    socket_server = socketio.AsyncServer(async_mode="aiohttp")
    app = web.Application()
    socket_server.attach(app)
    runner_sid = ""
    backend_sids: set[str] = set()
    drop_hold_result = False
    registered = asyncio.Event()

    @socket_server.on("runner:register")
    async def register(sid: str, data: dict) -> None:
        nonlocal runner_sid
        runner_sid = sid
        report["runner_epoch"] = data["inventory_epoch"]
        registered.set()

    @socket_server.on("relay")
    async def relay(sid: str, data: dict) -> dict:
        backend_sids.add(sid)
        return await socket_server.call(
            data["event"], data["data"], to=runner_sid, timeout=180
        )

    @socket_server.on("*")
    async def incoming(event: str, sid: str, data: dict) -> dict:
        nonlocal drop_hold_result
        if (
            drop_hold_result
            and event == "harness:desktop_action_result"
            and data.get("lease_state") == "held"
        ):
            drop_hold_result = False
            return {"ok": True}  # lose result after real side effect
        if event == "workspace:stream_output":
            accepted = routes.route_stream_output(data)
        elif event == "workspace:stream_closed":
            accepted = routes.route_stream_closed(data)
        elif event == "harness:read_file_chunk":
            accepted = routes.route_harness_file_chunk(data)
        elif event.startswith("harness:") and event.endswith("_result"):
            accepted = routes.route_harness_result(data)
        else:
            accepted = True
        for backend_sid in backend_sids:
            await socket_server.emit(event, data, to=backend_sid)
        return {"ok": accepted or bool(backend_sids)}

    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    report["transport"] = (
        "real loopback Socket.IO WebSocket server/client; no deployed DB/auth"
    )
    children: list[asyncio.subprocess.Process] = []
    child_logs: list[Any] = []
    config_path = root / "child-config.json"
    config_path.write_text(
        json.dumps(
            {"settings": settings.model_dump(), "identity": str(identity), "url": url}
        )
    )

    async def spawn(role: str) -> asyncio.subprocess.Process:
        log_file = (root / f"{role}-child.log").open("wb")
        child_logs.append(log_file)
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            role,
            "--child-config",
            str(config_path),
            stdout=log_file,
            stderr=log_file,
        )
        children.append(child)
        return child

    async def dispatch(event: str, data: dict) -> None:
        await socket_server.emit(event, data, to=runner_sid)

    async def call(event: str, data: dict, timeout: float | None) -> dict:
        return await socket_server.call(
            event, data, to=runner_sid, timeout=timeout or 180
        )

    accessor = routes.RunnerWorkspaceAccessor(
        str(identity),
        emit=dispatch,
        call=call,
        default_timeout=180,
    )
    runtimes: list[McpRuntime] = []
    viewers: list[DesktopLease] = []

    async def guest(command: str) -> str:
        rc, output = await runtime.exec_command_wait(
            str(identity), ["bash", "-lc", command], workdir="/workspace"
        )
        if rc:
            raise RuntimeError(f"Guest command failed (exit {rc}): {output[-2000:]}")
        return output

    async def alive(expected: bool) -> None:
        rc, output = await runtime.exec_command_wait(
            str(identity),
            ["bash", "-lc", ("pgrep -a Xvnc || true; test -S /tmp/.X11-unix/X1")],
            workdir="/workspace",
        )
        assert (rc == 0) is expected, (expected, output)
        if expected:
            assert "Xvnc" in output, output
        report["checks"].append({"xvnc_alive": expected, "probe": output.strip()})
        save()

    async def open_mcp(slug: str) -> McpRuntime:
        server = PluginMcpServerSnapshot(
            id=uuid.uuid4(),
            name=slug,
            slug=slug,
            transport="stdio",
            command="npx",
            args=(
                "-y",
                "@playwright/mcp@latest",
                "--browser",
                "chromium",
                "--no-sandbox",
                "--isolated",
            ),
            resources=PluginResourcesSnapshot(DesktopResourceSnapshot("first_tool")),
            startup_timeout_seconds=180,
            request_timeout_seconds=120,
        )
        plugin = EffectivePluginSnapshot(
            id=uuid.uuid4(),
            name=slug,
            slug=slug,
            description="Live smoke",
            organization_id=None,
            is_global=True,
            mcp_servers=(server,),
        )
        mcp = McpRuntime()
        runtimes.append(mcp)
        await mcp.setup(
            workspace=None,
            organization_id=None,
            accessor=accessor,
            snapshot=PreparedPluginRuntime(
                snapshot=WorkspacePluginSnapshot(identity, uuid.uuid4(), (plugin,))
            ),
        )
        assert len(mcp.connections) == 1, mcp.skipped
        return mcp

    save()
    structlog.get_logger().info("live_smoke_artifacts", path=str(root))
    try:
        await runtime.create_workspace(
            WorkspaceConfig(
                workspace_id=str(identity),
                image=str(args.base_image.resolve()),
                env_vars={},
                qemu_vcpus=1,
                qemu_memory_mb=args.memory_mb,
                qemu_disk_size_gb=20,
            )
        )
        service._cache[identity] = WorkspaceInfo(
            identity, str(identity), "running", runtime_type="qemu"
        )
        await guest(
            "curl -fsSL https://nodejs.org/dist/v22.22.0/"
            "node-v22.22.0-linux-x64.tar.xz -o /tmp/node.tar.xz && "
            "tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1"
        )
        report["guest_versions"] = await guest(
            "mkdir -p /workspace; node --version; npm --version; "
            "command -v Xvnc; command -v ffmpeg"
        )
        # Installation occurs exclusively in disposable guest, never host/base.
        report["mcp_version"] = await guest("npx -y @playwright/mcp@latest --version")
        await guest("npx -y @playwright/mcp@latest install-browser chrome-for-testing")
        interface._operations.journal.close()
        runner_child = await spawn("runner")
        await asyncio.wait_for(registered.wait(), 60)
        await alive(False)
        first = await open_mcp("live-one")
        connection = first.connections[0]
        assert any(t.name == "browser_navigate" for t in connection.tools)
        await alive(False)  # initialize + tools/list reserve but never start Xvnc
        report["checks"].append("initialize_and_list_without_xvnc")
        # Real HTTP fixture served by guest process, no external browser site.
        await guest(
            "printf '%s' '<html><body style=\"background:#15253d;"
            'color:white;font:36px sans-serif"><h1>Managed desktop LIVE</h1>'
            "<p>Real Chromium / QEMU / MCP</p></body></html>' "
            "> /workspace/live.html; nohup python3 -m http.server 8765 "
            "--bind 127.0.0.1 >/tmp/live-http.log 2>&1 </dev/null &"
        )
        navigation = await connection.call_tool(
            "browser_navigate", {"url": "http://127.0.0.1:8765/live.html"}
        )
        report["navigation"] = str(navigation)
        body = await connection.call_tool(
            "browser_evaluate", {"function": "() => document.body.innerText"}
        )
        assert "Managed desktop LIVE" in str(body), body
        await alive(True)
        await connection.call_tool(
            "browser_take_screenshot",
            {"filename": "/workspace/guest-browser.png", "fullPage": True},
        )
        image = await accessor.read_file("/workspace/guest-browser.png")
        (root / "guest-browser.png").write_bytes(image.content)
        framebuffer = await accessor.desktop_action("screenshot", {"format": "png"})
        (root / "framebuffer.png").write_bytes(
            base64.b64decode(framebuffer["image_b64"])
        )
        report["checks"].append("headed_browser_and_framebuffer_captured")
        second = await open_mcp("live-two")
        assert second.connections[0].env["DISPLAY"] == connection.env["DISPLAY"]
        await second.connections[0].desktop_lease.hold()
        for _ in range(2):
            lease = DesktopLease(accessor, kind="viewer")
            viewers.append(lease)
            await lease.reserve()
            await lease.hold()
        # MCP SDK stacks must unwind LIFO on this owner task.
        await second.aclose()
        await alive(True)
        await viewers[0].aclose()
        await alive(True)
        await viewers[1].aclose()
        await alive(True)
        await first.aclose()
        await alive(False)
        report["checks"].append("independent_mcp_and_viewer_owners_last_close_stops")
        lost_ack = DesktopLease(accessor, kind="mcp")
        viewers.append(lost_ack)
        await lost_ack.reserve()
        drop_hold_result = True
        try:
            await asyncio.wait_for(lost_ack.hold(), 2)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Dropped hold reply unexpectedly acknowledged")
        assert (await service.desktop.lease_store.get(lost_ack.lease_id))[
            "state"
        ] == "held"
        await alive(True)
        await lost_ack.aclose()
        await alive(False)
        report["checks"].append("lost_hold_reply_after_side_effect_release_compensates")
        # Kill an actual backend MCP owner process while the separate runner
        # stays alive. Production TTL remains 180s; accelerate only its clock.
        backend_child = await spawn("backend")
        backend_ready = root / "backend-ready.json"
        for _ in range(2400):
            if backend_ready.exists():
                break
            if backend_child.returncode is not None:
                raise RuntimeError("Backend child exited before readiness")
            await asyncio.sleep(0.1)
        assert backend_ready.exists(), "Backend child readiness timed out"
        backend_owner = json.loads(backend_ready.read_text())
        await alive(True)
        backend_child.kill()
        assert await backend_child.wait() == -signal.SIGKILL
        report["checks"].append(
            {
                "backend_sigkill": backend_child.pid,
                "runner_still_alive": runner_child.returncode is None,
            }
        )
        with sqlite3.connect(service.desktop.lease_store.path) as db:
            db.execute(
                "UPDATE desktop_leases SET expires_at=0 WHERE lease_id=?",
                (backend_owner["lease_id"],),
            )
        # Actual runner's background lease maintenance loop (one second cadence).
        for _ in range(120):
            row = await service.desktop.lease_store.get(backend_owner["lease_id"])
            if row["state"] == "expired":
                break
            await asyncio.sleep(0.5)
        assert row["state"] == "expired", row
        stream_rows = await service.streams.intent_store.owner_rows(
            backend_owner["lease_id"]
        )
        assert stream_rows and all(r["state"] == "closed" for r in stream_rows), (
            stream_rows
        )
        report["backend_crash_streams"] = stream_rows
        await alive(False)
        await guest("! pgrep -f '/[c]hrome-linux64/chrome' >/dev/null")
        report["checks"].append(
            "backend_os_crash_expired_clock_accelerated_browser_group_dead"
        )
        backend_sids.clear()
        # Second backend owns a real MCP/browser when the runner is SIGKILLed.
        backend_ready.unlink()
        backend_child = await spawn("backend")
        for _ in range(2400):
            if backend_ready.exists():
                break
            if backend_child.returncode is not None:
                raise RuntimeError("Backend child exited before runner crash")
            await asyncio.sleep(0.1)
        assert backend_ready.exists()
        stale = json.loads(backend_ready.read_text())
        await alive(True)
        runner_child.kill()
        assert await runner_child.wait() == -signal.SIGKILL
        # Do not allow backend SDK timeout/renewal to race recovery assertions.
        backend_child.kill()
        await backend_child.wait()
        backend_sids.clear()
        restarted = WorkspaceService({"qemu": runtime}, settings)
        restarted._cache[identity] = service._cache[identity]
        replacement = WebSocketInterface(restarted, settings)
        assert replacement._inventory_epoch != stale["epoch"]
        await restarted.desktop.recover()
        assert (await restarted.desktop.lease_store.get(stale["lease_id"]))[
            "state"
        ] == "expired"
        recovered_streams = await restarted.streams.intent_store.owner_rows(
            stale["lease_id"]
        )
        assert recovered_streams and all(
            r["state"] == "closed" for r in recovered_streams
        ), recovered_streams
        report["runner_crash_streams"] = recovered_streams
        await alive(False)
        await guest("! pgrep -f '/[c]hrome-linux64/chrome' >/dev/null")
        rejected = False
        try:
            await restarted.desktop.desktop_action(identity, "renew", stale)
        except ValueError:
            rejected = True
        assert rejected, "Stale owner adopted by new epoch"
        # Real public lifecycle stop/resume, with a launch prepared before stop.
        # Capture expected token now, never let spawn silently refresh it later.
        old_token = await runtime.probe_managed_token(str(identity))
        lease_id = str(uuid.uuid4())
        lifecycle_owner = {
            "lease_id": lease_id,
            "epoch": replacement._inventory_epoch,
            "kind": "mcp",
            "owner_id": str(uuid.uuid4()),
            "revision": 1,
        }
        await restarted.desktop.desktop_action(identity, "reserve", lifecycle_owner)
        await restarted.desktop.desktop_action(identity, "hold", lifecycle_owner)
        lifecycle_stream_id = uuid.uuid4().hex
        await restarted.streams.stream_start_process(
            identity,
            lifecycle_stream_id,
            ["sleep", "600"],
            owner={"lease_id": lease_id, "epoch": replacement._inventory_epoch},
        )
        await alive(True)
        assert await restarted.stop_workspace(identity) is False
        stopped_status = await runtime.get_workspace_status(str(identity))
        assert stopped_status.status in {"exited", "stopped"}, stopped_status
        assert restarted._cache[identity].credentials_present is False
        stopped_owner = await restarted.desktop.lease_store.get(lease_id)
        assert stopped_owner["state"] in {"released", "expired"}, stopped_owner
        stopped_stream = await restarted.streams.intent_store.get(lifecycle_stream_id)
        assert stopped_stream["state"] == "closed", stopped_stream
        report["public_stop"] = {
            "runtime_status": stopped_status.status,
            "credentials_present": False,
            "lease": stopped_owner,
            "stream": stopped_stream,
        }
        await restarted.resume_workspace(
            identity,
            qemu_vcpus=1,
            qemu_memory_mb=args.memory_mb,
            qemu_disk_size_gb=20,
            env_vars={},
            files=[],
            ssh_keys=[],
        )
        new_token = await runtime.probe_managed_token(str(identity))
        assert new_token != old_token, (old_token, new_token)
        await alive(False)
        delayed_path = f"/var/lib/opencuria/streams/live-delayed-{uuid.uuid4().hex}"
        delayed = await runtime.spawn_process(
            str(identity),
            ["bash", "-lc", "touch /workspace/delayed-launch-ran"],
            workdir="/workspace",
            control_path=delayed_path,
            expected_token=old_token,
        )
        delayed_exit = await asyncio.wait_for(runtime.process_wait(delayed), 30)
        assert delayed_exit == 125, delayed_exit
        await runtime.process_close(delayed)
        await guest("test ! -e /workspace/delayed-launch-ran")
        report["checks"].append(
            {
                "public_stop_resume": True,
                "old_token": old_token,
                "new_token": new_token,
                "delayed_old_token_launch_exit": delayed_exit,
                "no_delayed_command_side_effect": True,
            }
        )
        await restarted.remove_workspace(identity)
        assert await runtime.workspace_exists(str(identity)) is False
        assert identity not in restarted._cache
        removed_owners = await restarted.desktop.lease_store.list_workspace(
            str(identity)
        )
        assert all(
            r["state"] in {"released", "expired"} and not r["activated"]
            for r in removed_owners
        ), removed_owners
        report["public_remove"] = {
            "guest_absent": True,
            "cache_absent": True,
            "retained_tombstones_all_ended": True,
        }
        replacement._operations.journal.close()
        report["checks"].append(
            {
                "runner_sigkill": runner_child.pid,
                "recovered_epoch": replacement._inventory_epoch,
                "old_browser_group_dead": True,
                "stale_renew_rejected": True,
            }
        )
        report["passed"] = True
    except BaseException as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:3000]
        raise
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
            await child.wait()
        for mcp in reversed(runtimes):
            await mcp.aclose()
        for viewer in viewers:
            await viewer.aclose()
        try:
            await runtime.remove_workspace(str(identity))
            report["cleanup"] = not await runtime.workspace_exists(str(identity))
        finally:
            await server.cleanup()
            for log_file in child_logs:
                log_file.close()
            save()


async def child_main(role: str, config_path: Path) -> None:
    """Own SDK scopes or runner managers in a killable OS process."""
    import django
    import socketio

    django.setup()
    from src.config import RunnerSettings
    from src.interfaces.websocket import WebSocketInterface
    from src.models import WorkspaceInfo
    from src.runtime.qemu_runtime import QemuRuntime
    from src.service import WorkspaceService

    from apps.harness.access import runner_accessor as routes
    from apps.harness.mcp_client.runtime import McpRuntime
    from apps.plugins.runtime_snapshot import (
        DesktopResourceSnapshot,
        EffectivePluginSnapshot,
        PluginMcpServerSnapshot,
        PluginResourcesSnapshot,
        PreparedPluginRuntime,
        WorkspacePluginSnapshot,
    )

    config = json.loads(config_path.read_text())
    identity = uuid.UUID(config["identity"])
    if role == "runner":
        settings = RunnerSettings(_env_file=None, **config["settings"])
        runtime = QemuRuntime(settings)
        service = WorkspaceService({"qemu": runtime}, settings)
        service._cache[identity] = WorkspaceInfo(
            identity, str(identity), "running", runtime_type="qemu"
        )
        interface = WebSocketInterface(service, settings)
        await interface._sio.connect(config["url"], transports=["websocket"])
        await interface._sio.wait()
        return
    client = socketio.AsyncClient()

    @client.on("*")
    async def incoming(event: str, data: dict) -> dict:
        if event == "workspace:stream_output":
            accepted = routes.route_stream_output(data)
        elif event == "workspace:stream_closed":
            accepted = routes.route_stream_closed(data)
        elif event == "harness:read_file_chunk":
            accepted = routes.route_harness_file_chunk(data)
        elif event.startswith("harness:") and event.endswith("_result"):
            accepted = routes.route_harness_result(data)
        else:
            accepted = True
        return {"ok": accepted}

    await client.connect(config["url"], transports=["websocket"])

    async def dispatch(event: str, data: dict) -> None:
        await client.call("relay", {"event": event, "data": data}, timeout=180)

    async def call(event: str, data: dict, timeout: float | None) -> dict:
        return await client.call(
            "relay", {"event": event, "data": data}, timeout=timeout or 180
        )

    accessor = routes.RunnerWorkspaceAccessor(
        str(identity), emit=dispatch, call=call, default_timeout=180
    )
    server = PluginMcpServerSnapshot(
        id=uuid.uuid4(),
        name="crash-browser",
        slug="crash-browser",
        transport="stdio",
        command="npx",
        args=(
            "-y",
            "@playwright/mcp@latest",
            "--browser",
            "chromium",
            "--no-sandbox",
            "--isolated",
        ),
        resources=PluginResourcesSnapshot(DesktopResourceSnapshot("first_tool")),
        startup_timeout_seconds=180,
        request_timeout_seconds=120,
    )
    plugin = EffectivePluginSnapshot(
        uuid.uuid4(),
        "crash",
        "crash",
        "OS crash fixture",
        None,
        True,
        mcp_servers=(server,),
    )
    mcp = McpRuntime()
    await mcp.setup(
        workspace=None,
        organization_id=None,
        accessor=accessor,
        snapshot=PreparedPluginRuntime(
            snapshot=WorkspacePluginSnapshot(identity, uuid.uuid4(), (plugin,))
        ),
    )
    assert len(mcp.connections) == 1, mcp.skipped
    conn = mcp.connections[0]
    await conn.call_tool("browser_navigate", {"url": "http://127.0.0.1:8765/live.html"})
    (config_path.parent / "backend-ready.json").write_text(
        json.dumps(conn.desktop_lease.owner)
    )
    await asyncio.Event().wait()


def main() -> None:
    """Require explicit opt-in before creating any resources."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--base-image", type=Path)
    parser.add_argument("--child", choices=["runner", "backend"])
    parser.add_argument("--child-config", type=Path)
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=REPO.parent / ".opencuria/playwright/managed-desktop",
    )
    parser.add_argument("--memory-mb", type=int, default=1024)
    args = parser.parse_args()
    if args.child:
        asyncio.run(child_main(args.child, args.child_config))
        return
    if not args.run_live:
        parser.error("Pass --run-live to authorize one disposable real guest")
    if args.base_image is None or not args.base_image.is_file():
        parser.error("Base image does not exist")
    asyncio.run(smoke(args))


if __name__ == "__main__":
    main()
