"""Tests for RunnerWorkspaceAccessor against a fake Socket.IO transport."""

from __future__ import annotations

import asyncio
import base64
import threading
import uuid

import pytest

from apps.harness.access.base import ExecChunk
from apps.harness.access.runner_accessor import (
    RunnerAccessorError,
    RunnerWorkspaceAccessor,
    create_harness_accessor,
    route_harness_chunk,
    route_harness_done,
    route_harness_result,
)


class FakeTransport:
    """Records emits and lets tests inject runner replies."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []
        self.auto_reply = None
        self.event = asyncio.Event()

    async def __call__(self, event: str, payload: dict) -> None:
        self.emitted.append((event, payload))
        self.event.set()
        if self.auto_reply is not None:
            await self.auto_reply(event, payload)


def _accessor(transport: FakeTransport, **kwargs) -> RunnerWorkspaceAccessor:
    return RunnerWorkspaceAccessor("ws-1", emit=transport, **kwargs)


def _request_id(transport: FakeTransport, event: str) -> dict:
    for emitted_event, payload in transport.emitted:
        if emitted_event == event:
            return payload
    raise AssertionError(f"event {event} was not emitted")


async def test_exec_wait_result_and_request_id_correlation() -> None:
    """exec_wait emits harness:exec_wait and resolves the correlated reply."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "exit_code": 0,
                "stdout": "out",
                "stderr": "err",
            }
        )

    transport.auto_reply = auto_reply
    accessor = _accessor(transport)
    result = await accessor.exec_wait(["echo", "hi"])
    assert result.exit_code == 0
    assert result.stdout == "out"
    assert result.stderr == "err"
    payload = _request_id(transport, "harness:exec_wait")
    assert payload["workspace_id"] == "ws-1"
    assert payload["workdir"] == "/workspace"
    assert payload["request_id"]


async def test_exec_stream_chunks_stdout_stderr_and_exit() -> None:
    """exec_stream yields separated stdout/stderr chunks then exit code."""
    transport = FakeTransport()
    accessor = _accessor(transport)

    async def consume():
        chunks: list[ExecChunk] = []
        async for chunk in accessor.exec_stream(["ls"]):
            chunks.append(chunk)
        return chunks

    task = asyncio.create_task(consume())
    await transport.event.wait()
    payload = _request_id(transport, "harness:exec_stream")
    request_id = payload["request_id"]
    route_harness_chunk(
        {
            "request_id": request_id,
            "workspace_id": "ws-1",
            "stream": "stdout",
            "data": "hello",
        }
    )
    route_harness_chunk(
        {
            "request_id": request_id,
            "workspace_id": "ws-1",
            "stream": "stderr",
            "data": "oops",
        }
    )
    route_harness_done(
        {"request_id": request_id, "workspace_id": "ws-1", "exit_code": 3}
    )
    chunks = await task
    assert [c.stream for c in chunks] == ["stdout", "stderr", ""]
    assert chunks[0].data == "hello"
    assert chunks[1].data == "oops"
    assert chunks[2].done is True
    assert chunks[2].exit_code == 3


async def test_runner_error_raises() -> None:
    """Runner-reported errors surface as RunnerAccessorError."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "error": "boom",
            }
        )

    transport.auto_reply = auto_reply
    accessor = _accessor(transport)
    with pytest.raises(RunnerAccessorError, match="boom"):
        await accessor.exec_wait(["false"])


async def test_exec_wait_timeout_sends_cancel() -> None:
    """A timed-out request emits harness:cancel for cleanup."""
    transport = FakeTransport()
    accessor = _accessor(transport, default_timeout=30.0)
    with pytest.raises(TimeoutError, match="timed out"):
        await accessor.exec_wait(["sleep", "5"], timeout=0.02)
    events = [event for event, _ in transport.emitted]
    assert events[0] == "harness:exec_wait"
    assert events[-1] == "harness:cancel"


async def test_exec_stream_cancel_sends_cancel() -> None:
    """Cancelling exec_stream emits harness:cancel for cleanup."""
    transport = FakeTransport()
    accessor = _accessor(transport)
    task = asyncio.create_task(
        _collect(accessor.exec_stream(["sleep", "5"], timeout=30.0))
    )
    await transport.event.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = [event for event, _ in transport.emitted]
    assert "harness:cancel" in events


async def _collect(agen):
    return [chunk async for chunk in agen]


async def test_read_write_list_stat_roundtrip() -> None:
    """read/write/list/stat use request_id correlation and sandbox paths."""
    transport = FakeTransport()
    accessor = _accessor(transport)

    async def auto_reply(event: str, payload: dict) -> None:
        rid = payload["request_id"]
        if event == "harness:read_file":
            route_harness_result(
                {
                    "request_id": rid,
                    "workspace_id": "ws-1",
                    "content": base64.b64encode(b"hi").decode(),
                    "size": 2,
                    "truncated": False,
                    "mime": "text/plain",
                }
            )
        elif event == "harness:write_file":
            route_harness_result(
                {"request_id": rid, "workspace_id": "ws-1", "ok": True}
            )
        elif event == "harness:list":
            route_harness_result(
                {
                    "request_id": rid,
                    "workspace_id": "ws-1",
                    "entries": [
                        {
                            "name": "a.txt",
                            "path": "/workspace/a.txt",
                            "is_dir": False,
                            "size": 2,
                        }
                    ],
                }
            )
        elif event == "harness:stat":
            route_harness_result(
                {
                    "request_id": rid,
                    "workspace_id": "ws-1",
                    "path": "/workspace/a.txt",
                    "is_dir": False,
                    "size": 2,
                    "mime": "text/plain",
                }
            )

    transport.auto_reply = auto_reply
    content = await accessor.read_file("/workspace/a.txt")
    assert content.content == b"hi"
    assert content.mime == "text/plain"
    await accessor.write_file("/workspace/a.txt", b"hi")
    entries = await accessor.list_dir("/workspace")
    assert entries[0].name == "a.txt"
    assert entries[0].is_dir is False
    info = await accessor.stat("/workspace/a.txt")
    assert info.size == 2
    assert info.is_dir is False

    with pytest.raises(ValueError, match="under /workspace"):
        await accessor.read_file("/etc/passwd")


async def test_desktop_action_returns_screenshot_payload() -> None:
    """desktop_action emits harness:desktop_action and returns image fields."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "ok": True,
                "image_b64": "aGVsbG8=",
                "mime": "image/png",
                "width": 10,
                "height": 20,
            }
        )

    transport.auto_reply = auto_reply
    accessor = _accessor(transport)
    result = await accessor.desktop_action("screenshot", {"full": True})
    assert result["image_b64"] == "aGVsbG8="
    assert result["width"] == 10
    payload = _request_id(transport, "harness:desktop_action")
    assert payload["action"] == "screenshot"
    assert payload["args"] == {"full": True}


async def test_desktop_action_ensure_includes_geometry() -> None:
    """ensure/hold attach the workspace framebuffer size for Xvnc start."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "ok": True,
                "display": ":1",
                "port": 6901,
            }
        )

    async def geometry() -> tuple[int, int]:
        return 1280, 720

    transport.auto_reply = auto_reply
    accessor = _accessor(transport, desktop_geometry=geometry)
    result = await accessor.desktop_action("ensure")
    assert result["ok"] is True
    payload = _request_id(transport, "harness:desktop_action")
    assert payload["action"] == "ensure"
    assert payload["args"] == {"desktop_width": 1280, "desktop_height": 720}


async def test_desktop_action_runner_error_raises() -> None:
    """Runner-reported desktop errors surface as RunnerAccessorError."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "error": "desktop unavailable",
            }
        )

    transport.auto_reply = auto_reply
    accessor = _accessor(transport)
    with pytest.raises(RunnerAccessorError, match="desktop unavailable"):
        await accessor.desktop_action("screenshot")


async def test_desktop_action_timeout_sends_cancel() -> None:
    """A timed-out desktop action emits harness:cancel for cleanup."""
    transport = FakeTransport()
    accessor = _accessor(transport, default_timeout=30.0)
    with pytest.raises(TimeoutError, match="timed out"):
        await accessor.desktop_action("screenshot", timeout=0.02)
    events = [event for event, _ in transport.emitted]
    assert events[0] == "harness:desktop_action"
    assert events[-1] == "harness:cancel"


async def test_unknown_request_id_returns_false() -> None:
    """Routing helpers return False for unknown request ids."""
    assert route_harness_result({"request_id": "nope"}) is False
    assert route_harness_chunk({"request_id": "nope"}) is False
    assert route_harness_done({"request_id": "nope"}) is False


async def test_result_delivered_from_worker_thread() -> None:
    """Replies arriving on a worker thread still resolve the waiter."""
    transport = FakeTransport()
    finished = threading.Event()

    async def auto_reply(event: str, payload: dict) -> None:
        def from_thread() -> None:
            route_harness_result(
                {
                    "request_id": payload["request_id"],
                    "workspace_id": "ws-1",
                    "exit_code": 0,
                    "stdout": "from-thread",
                    "stderr": "",
                }
            )
            finished.set()

        threading.Thread(target=from_thread, daemon=True).start()

    transport.auto_reply = auto_reply
    result = await asyncio.wait_for(_accessor(transport).exec_wait(["echo"]), 2)
    assert result.stdout == "from-thread"
    assert finished.wait(timeout=2)


class _RecordingRunnerService:
    """Minimal runner service for create_harness_accessor tests."""

    def __init__(self) -> None:
        from apps.runners.repositories import WorkspaceRepository

        self.workspaces = WorkspaceRepository
        self.emitted: list[tuple[str, str, dict]] = []

    async def emit_harness_event(self, runner, event, payload) -> None:  # type: ignore[no-untyped-def]
        self.emitted.append((str(runner.sid), event, payload))
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": payload["workspace_id"],
                "entries": [
                    {
                        "name": "a.txt",
                        "path": "/workspace/a.txt",
                        "is_dir": False,
                        "size": 1,
                    }
                ],
            }
        )


@pytest.mark.django_db(transaction=True)
async def test_create_harness_accessor_lists_via_runner_rpc(harness_workspace) -> None:
    """Factory-built accessors emit harness:list and return correlated entries."""
    service = _RecordingRunnerService()
    accessor = await create_harness_accessor(service, str(harness_workspace.id))
    entries = await accessor.list_dir("/workspace")
    assert entries[0].name == "a.txt"
    assert service.emitted[0][0] == harness_workspace.runner.sid
    assert service.emitted[0][1] == "harness:list"


@pytest.mark.django_db(transaction=True)
async def test_create_harness_accessor_uses_live_runner_sid(harness_workspace) -> None:
    """Emit uses the runner SID from the DB, not a stale captured object."""
    service = _RecordingRunnerService()
    accessor = await create_harness_accessor(service, str(harness_workspace.id))
    runner = harness_workspace.runner
    runner.sid = "rotated-sid"
    runner.save(update_fields=["sid"])
    await accessor.list_dir("/workspace")
    assert service.emitted[-1][0] == "rotated-sid"


@pytest.mark.django_db(transaction=True)
async def test_create_harness_accessor_rejects_offline_runner(
    harness_workspace,
) -> None:
    """Factory raises when the owning runner is offline."""
    from apps.runners.enums import RunnerStatus
    from apps.runners.exceptions import RunnerOfflineError

    runner = harness_workspace.runner
    runner.status = RunnerStatus.OFFLINE
    runner.sid = ""
    runner.save(update_fields=["status", "sid"])
    with pytest.raises(RunnerOfflineError):
        await create_harness_accessor(
            _RecordingRunnerService(), str(harness_workspace.id)
        )


@pytest.mark.django_db(transaction=True)
async def test_create_harness_accessor_rejects_unknown_workspace() -> None:
    """Factory raises when the workspace id does not exist."""
    from apps.runners.exceptions import WorkspaceNotFoundError

    with pytest.raises(WorkspaceNotFoundError):
        await create_harness_accessor(_RecordingRunnerService(), str(uuid.uuid4()))


@pytest.mark.django_db(transaction=True)
async def test_emit_fails_when_runner_goes_offline_mid_run(harness_workspace) -> None:
    """A later emit surfaces a tool-level accessor error if the runner drops."""
    from apps.runners.enums import RunnerStatus

    service = _RecordingRunnerService()
    accessor = await create_harness_accessor(service, str(harness_workspace.id))
    runner = harness_workspace.runner
    runner.status = RunnerStatus.OFFLINE
    runner.sid = ""
    runner.save(update_fields=["status", "sid"])
    with pytest.raises(RunnerAccessorError, match="offline"):
        await accessor.list_dir("/workspace")


async def test_cancelled_wait_still_emits_cancel() -> None:
    """Cancelling exec_wait emits harness:cancel (abort-safety)."""
    import asyncio as _asyncio

    transport = FakeTransport()
    accessor = _accessor(transport)

    async def _never_reply(event: str, payload: dict) -> None:
        return None

    transport.auto_reply = _never_reply
    task = _asyncio.create_task(accessor.exec_wait(["sleep", "9"], timeout=30.0))
    await transport.event.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = [event for event, _ in transport.emitted]
    assert "harness:cancel" in events


async def test_cancel_never_classified_retryable() -> None:
    """CancelledError is never a retryable provider error."""
    import asyncio as _asyncio

    from apps.harness.provider_retry import is_retryable_provider_error

    assert is_retryable_provider_error(_asyncio.CancelledError()) is False
    assert is_retryable_provider_error(GeneratorExit()) is False  # type: ignore[arg-type]


async def test_cancel_during_retry_sleep_propagates() -> None:
    """Cancel during the provider retry sleep is not swallowed as a retry."""
    import asyncio as _asyncio

    from apps.harness.runner import HarnessRunner

    calls = {"n": 0}

    class _SlowProvider:
        async def chat_stream(self, model, messages, schemas, opts=None):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            from apps.harness.providers.base import ProviderTimeoutError

            raise ProviderTimeoutError("slow", provider="fake")
            yield  # pragma: no cover

    from apps.harness.tools import default_tool_registry

    runner = HarnessRunner(
        provider=_SlowProvider(),  # type: ignore[arg-type]
        tools=default_tool_registry(),
    )
    task = _asyncio.create_task(
        runner._provider_step_with_transient_retry(
            model="m", messages=[], schemas=[], step=1
        )
    )
    await _asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(_asyncio.CancelledError):
        await task
