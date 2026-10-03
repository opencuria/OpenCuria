"""Capture admission is local defense in depth, not backend ownership."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.models import WorkspaceInfo
from src.service import WorkspaceService
from src.services.capture_fence import CaptureFence
from src.services.sessions.terminals import TerminalSession


@pytest.fixture
def service():
    runtime = SimpleNamespace(
        supports_image_artifacts=True, create_image_artifact=AsyncMock()
    )
    service = WorkspaceService({"qemu": runtime}, RunnerSettings())
    ws = uuid.uuid4()
    service._cache[ws] = WorkspaceInfo(
        workspace_id=ws, instance_id="guest", status="exited", runtime_type="qemu"
    )
    service.images.scrub_proof_hook = AsyncMock(return_value=True)
    return service, ws, runtime


async def test_capture_drains_admitted_work_before_proof_and_rejects_new_work(service):
    svc, ws, runtime = service
    admitted, release, proof = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def interaction():
        async with svc.capture_fence.interaction(ws):
            admitted.set()
            await release.wait()
            # A nested call from the already-admitted task must not deadlock.
            async with svc.capture_fence.interaction(ws):
                pass

    async def scrub(_):
        proof.set()
        return True

    svc.images.scrub_proof_hook.side_effect = scrub
    user = asyncio.create_task(interaction())
    await admitted.wait()
    capture = asyncio.create_task(
        svc.images.create_image_artifact(
            ws,
            "image",
            artifact_id="artifact",
            operation_id="op",
            credential_clean=False,
        )
    )
    await asyncio.sleep(0)
    assert not proof.is_set()
    with pytest.raises(RuntimeError, match="capturing image"):
        await svc.files.read_file(ws, "/workspace/test")
    async with svc.capture_fence.interaction(uuid.uuid4()):
        pass
    release.set()
    await asyncio.gather(user, capture)
    runtime.create_image_artifact.assert_awaited_once_with(
        "guest",
        "image",
        artifact_id="artifact",
        operation_id="op",
        credential_clean=True,
    )


@pytest.mark.parametrize("failure", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_capture_cleanup_on_runtime_failure_or_cancellation(service, failure):
    svc, ws, runtime = service
    runtime.create_image_artifact.side_effect = failure
    with pytest.raises(type(failure)):
        await svc.images.create_image_artifact(ws, "image")
    async with svc.capture_fence.interaction(ws):
        pass
    assert ws not in svc.registry._active


async def test_cancel_capture_while_draining_releases_fence():
    fence = CaptureFence()
    ws = uuid.uuid4()
    async with fence.interaction(ws):

        async def capture():
            async with fence.capture(ws):
                await fence.drain(ws)
                pytest.fail("Must drain admission first")

        task = asyncio.create_task(capture())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with fence.interaction(ws):
        pass


async def test_client_clean_flag_does_not_replace_scrub_proof(service):
    svc, ws, runtime = service
    svc.images.scrub_proof_hook.return_value = False
    await svc.images.create_image_artifact(ws, "image", credential_clean=True)
    assert runtime.create_image_artifact.await_args.kwargs["credential_clean"] is False


@pytest.mark.parametrize(
    "manager,method,args",
    [
        ("files", "read_file", ("/workspace/test",)),
        ("files", "list_files", ("/workspace",)),
        ("files", "find_files", ("query",)),
        ("files", "upload_file", ("/workspace", "test", "dGVzdA==")),
        ("files", "download_file", ("/workspace/test",)),
        ("files", "write_file_content", ("/workspace/test", "data")),
        ("files", "stat_path", ("/workspace/test",)),
        ("harness", "exec_harness_command", (["echo", "test"],)),
        ("git", "list_git_repositories", ()),
        ("terminal_manager", "start_terminal", ()),
        ("desktop", "start_desktop", ()),
        ("desktop", "stop_desktop", ()),
        ("desktop", "read_desktop_clipboard", ()),
        ("desktop", "write_desktop_clipboard", ("text",)),
        ("background", "list_background_processes", ()),
        ("background", "get_background_status", ("process",)),
        ("background", "stop_background_process", ("process",)),
        ("streams", "stream_start_process", ("connection", ["echo", "test"])),
        ("lifecycle", "stop_workspace", ()),
        ("lifecycle", "resume_workspace", ()),
        ("lifecycle", "remove_workspace", ()),
    ],
)
async def test_composed_manager_interactions_are_guarded(
    service, manager, method, args
):
    svc, ws, _ = service
    async with svc.capture_fence.capture(ws):
        with pytest.raises(RuntimeError, match="capturing image"):
            await getattr(getattr(svc, manager), method)(ws, *args)


async def test_session_id_only_writes_use_original_workspace(service):
    svc, ws, runtime = service
    svc._terminals["terminal"] = TerminalSession(
        handle=None, runtime=runtime, workspace_id=ws
    )
    svc._streams["stream"] = SimpleNamespace(workspace_id=ws)
    async with svc.capture_fence.capture(ws):
        for method, args in [
            (svc.terminal_manager.write_terminal, ("terminal", b"text")),
            (svc.terminal_manager.resize_terminal, ("terminal", 80, 24)),
            (svc.streams.stream_write, ("stream", b"text")),
            (svc.streams.stream_write_eof, ("stream",)),
        ]:
            with pytest.raises(RuntimeError, match="capturing image"):
                await method(*args)


async def test_exec_iterator_is_guarded_and_drained(service):
    svc, ws, _ = service
    async with svc.capture_fence.capture(ws):
        iterator = svc.harness.exec_harness_command_stream(ws, ["echo", "test"])
        with pytest.raises(RuntimeError, match="capturing image"):
            await anext(iterator)


async def test_websocket_exec_rejection_keeps_result_envelope(service, tmp_path):
    svc, ws, _ = service
    interface = WebSocketInterface(svc, RunnerSettings(state_dir=str(tmp_path)))
    interface._sio.emit = AsyncMock()
    async with svc.capture_fence.capture(ws):
        await interface._sio.handlers["/"]["harness:exec_wait"](
            {
                "workspace_id": str(ws),
                "request_id": "request",
                "command": ["echo", "test"],
            }
        )
        await asyncio.gather(*interface._running_tasks.values())
    event, payload = interface._sio.emit.await_args.args
    assert event == "harness:exec_wait_result"
    assert payload["request_id"] == "request"
    assert "capturing image" in payload["error"]


async def test_desktop_proxy_paths_are_guarded(service, tmp_path):
    svc, ws, _ = service
    interface = WebSocketInterface(svc, RunnerSettings(state_dir=str(tmp_path)))
    interface._desktop_proxy_tunnels["tunnel"] = SimpleNamespace(workspace_id=ws)
    async with svc.capture_fence.capture(ws):
        for method, args in [
            (interface._fetch_desktop_http, (ws, "/", "")),
            (interface._open_desktop_proxy_tunnel, (ws, "new")),
            (interface._send_desktop_proxy_tunnel_message, ("tunnel",)),
        ]:
            with pytest.raises(RuntimeError, match="capturing image"):
                await method(*args)


async def test_second_capture_is_rejected_and_does_not_release_first():
    fence = CaptureFence()
    ws = uuid.uuid4()
    async with fence.capture(ws):
        with pytest.raises(RuntimeError, match="capturing image"):
            async with fence.capture(ws):
                pass
        with pytest.raises(RuntimeError, match="capturing image"):
            async with fence.interaction(ws):
                pass


async def test_idle_iterator_does_not_hold_capture_admission():
    from src.services.capture_fence import live_interaction

    class Manager:
        capture_fence = CaptureFence()

        @live_interaction
        async def output(self, workspace_id):
            yield "first"
            yield "second"

    manager = Manager()
    ws = uuid.uuid4()
    iterator = manager.output(ws)
    assert await anext(iterator) == "first"
    async with manager.capture_fence.capture(ws):
        await asyncio.wait_for(manager.capture_fence.drain(ws), 1)
        with pytest.raises(RuntimeError, match="capturing image"):
            await anext(iterator)
    await iterator.aclose()
    assert not manager.capture_fence._users


async def test_live_call_queued_behind_stop_is_rejected_after_epoch_change(service):
    svc, ws, _ = service
    async with svc.registry.lifecycle(ws):
        task = asyncio.create_task(svc.files.read_file(ws, "/workspace/test"))
        await asyncio.sleep(0)
        assert not task.done()
        assert not svc.capture_fence._users
    with pytest.raises(RuntimeError, match="lifecycle changed"):
        await asyncio.wait_for(task, 1)


async def test_lifecycle_queued_before_capture_cannot_run_inside_capture(service):
    svc, ws, runtime = service
    runtime.get_workspace_status = AsyncMock()
    async with svc.registry.lifecycle(ws):
        stop = asyncio.create_task(svc.lifecycle.stop_workspace(ws))
        await asyncio.sleep(0)
        capture = asyncio.create_task(svc.images.create_image_artifact(ws, "image"))
        await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="capturing image"):
        await asyncio.wait_for(stop, 1)
    await asyncio.wait_for(capture, 1)
    runtime.get_workspace_status.assert_not_awaited()


async def test_stop_closes_active_stream_read_before_capture_drains(service):
    svc, ws, runtime = service
    entered, closed, stopping, release_stop = (asyncio.Event() for _ in range(4))
    svc._cache[ws].status = "running"
    svc._streams["stream"] = SimpleNamespace(
        workspace_id=ws,
        connection_id="stream",
        handle="handle",
        runtime=runtime,
        closed=False,
    )

    async def read(*args):
        entered.set()
        await closed.wait()
        return b""

    async def close(*args):
        closed.set()

    async def remove(*args):
        stopping.set()
        await release_stop.wait()

    runtime.process_read = AsyncMock(side_effect=read)
    runtime.process_close = AsyncMock(side_effect=close)
    runtime.get_workspace_status = AsyncMock(
        return_value=SimpleNamespace(status="running")
    )
    runtime.stop_workspace = AsyncMock()
    svc.lifecycle.remove_hook = remove
    svc.lifecycle.kill_all_hook = AsyncMock()
    svc.lifecycle.release_hook = AsyncMock()
    reader = asyncio.create_task(svc.streams.stream_read_once("stream"))
    await entered.wait()
    stop = asyncio.create_task(svc.lifecycle.stop_workspace(ws))
    await stopping.wait()
    capture = asyncio.create_task(svc.images.create_image_artifact(ws, "image"))
    await asyncio.sleep(0)
    release_stop.set()
    await asyncio.wait_for(asyncio.gather(reader, stop, capture), 1)
    assert closed.is_set()
    assert not svc.capture_fence._users
    assert svc._cache[ws].status == "exited"


async def test_admitted_desktop_waiter_is_rechecked_after_stop_epoch(service):
    svc, ws, _ = service
    lock = await svc.desktop._desktop_lock(ws)
    async with lock:
        task = asyncio.create_task(svc.desktop.ensure_desktop_process(ws))
        await asyncio.sleep(0)
        assert svc.capture_fence._users
        async with svc.registry.lifecycle(ws):
            svc._cache[ws].status = "exited"
    with pytest.raises(RuntimeError, match="lifecycle changed"):
        await asyncio.wait_for(task, 1)
