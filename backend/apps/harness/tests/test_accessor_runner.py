"""Tests for RunnerWorkspaceAccessor against a fake Socket.IO transport."""

from __future__ import annotations

import asyncio
import base64
import threading
import uuid

import pytest

from apps.harness.access.base import ExecChunk
from apps.harness.access.runner_accessor import (
    HARNESS_CHUNK_B64_SIZE,
    RunnerAccessorError,
    RunnerWorkspaceAccessor,
    create_harness_accessor,
    route_harness_chunk,
    route_harness_done,
    route_harness_file_chunk,
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


async def test_exec_wait_allows_external_workdir() -> None:
    """exec_wait forwards workdir outside /workspace to the runner."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "exit_code": 0,
                "stdout": "/tmp",
                "stderr": "",
            }
        )

    transport.auto_reply = auto_reply
    accessor = _accessor(transport)
    result = await accessor.exec_wait(["pwd"], workdir="/tmp")
    assert result.stdout == "/tmp"
    payload = _request_id(transport, "harness:exec_wait")
    assert payload["workdir"] == "/tmp"


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
        name = "fake"

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


# --- Chunked file transfer -------------------------------------------------


def _chunk_payloads(raw: bytes, total: int, request_id: str) -> list[dict]:
    """Split base64(raw) into *total* slices for read_chunk routing."""
    encoded = base64.b64encode(raw).decode("ascii")
    size = (len(encoded) + total - 1) // total
    slices = [encoded[i : i + size] for i in range(0, len(encoded), size)]
    while len(slices) < total:
        slices.append("QQ==")
    return [
        {
            "request_id": request_id,
            "workspace_id": "ws-1",
            "path": "/workspace/big.bin",
            "index": index,
            "total_chunks": total,
            "content": piece,
        }
        for index, piece in enumerate(slices[:total])
    ]


async def test_chunked_read_reassembles_out_of_order() -> None:
    """Three out-of-order read chunks plus a chunked final join in order."""
    transport = FakeTransport()
    raw = b"0123456789abcdef" * 64
    seen: dict[str, dict] = {}

    async def auto_reply(event: str, payload: dict) -> None:
        seen["payload"] = payload
        rid = payload["request_id"]
        chunks = _chunk_payloads(raw, 3, rid)
        for chunk in (chunks[2], chunks[0], chunks[1]):
            assert route_harness_file_chunk(chunk) is True
            await asyncio.sleep(0)
        route_harness_result(
            {
                "request_id": rid,
                "workspace_id": "ws-1",
                "path": "/workspace/big.bin",
                "chunked": True,
                "total_chunks": 3,
                "size": len(raw),
                "mime": "application/octet-stream",
            }
        )

    transport.auto_reply = auto_reply
    accessor = _accessor(transport)
    content = await accessor.read_file("/workspace/big.bin")
    assert content.content == raw
    assert content.size == len(raw)
    assert seen["payload"]["path"] == "/workspace/big.bin"


async def test_chunked_read_final_total_mismatch_raises() -> None:
    """Chunks pin total 3 but the final announces total 2 → mismatch error."""
    transport = FakeTransport()
    raw = b"0123456789abcdef" * 64

    async def auto_reply(event: str, payload: dict) -> None:
        rid = payload["request_id"]
        for chunk in _chunk_payloads(raw, 3, rid):
            route_harness_file_chunk(chunk)
            await asyncio.sleep(0)
        route_harness_result(
            {
                "request_id": rid,
                "workspace_id": "ws-1",
                "path": "/workspace/big.bin",
                "chunked": True,
                "total_chunks": 2,
            }
        )

    transport.auto_reply = auto_reply
    with pytest.raises(RunnerAccessorError, match="mismatch"):
        await _accessor(transport).read_file("/workspace/big.bin")


async def test_chunked_read_incomplete_transfer_raises() -> None:
    """Only 2 of 3 announced chunks arrive → incomplete/missing error."""
    transport = FakeTransport()
    raw = b"0123456789abcdef" * 64

    async def auto_reply(event: str, payload: dict) -> None:
        rid = payload["request_id"]
        for chunk in _chunk_payloads(raw, 3, rid)[:2]:
            route_harness_file_chunk(chunk)
            await asyncio.sleep(0)
        route_harness_result(
            {
                "request_id": rid,
                "workspace_id": "ws-1",
                "path": "/workspace/big.bin",
                "chunked": True,
                "total_chunks": 3,
            }
        )

    transport.auto_reply = auto_reply
    with pytest.raises(RunnerAccessorError, match="incomplete|missing"):
        await _accessor(transport).read_file("/workspace/big.bin")


async def test_chunked_read_bad_index_raises() -> None:
    """A chunk with an out-of-range index fails the pending read."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        rid = payload["request_id"]
        assert route_harness_file_chunk(
            {
                "request_id": rid,
                "workspace_id": "ws-1",
                "path": "/workspace/big.bin",
                "index": 9,
                "total_chunks": 3,
                "content": base64.b64encode(b"nope").decode(),
            }
        ) is True
        await asyncio.sleep(0)
        route_harness_result(
            {
                "request_id": rid,
                "workspace_id": "ws-1",
                "path": "/workspace/big.bin",
                "chunked": True,
                "total_chunks": 3,
            }
        )

    transport.auto_reply = auto_reply
    with pytest.raises(RunnerAccessorError):
        await _accessor(transport).read_file("/workspace/big.bin")


async def test_chunked_read_chunk_workspace_mismatch_raises() -> None:
    """A chunk for another workspace fails the pending read."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        rid = payload["request_id"]
        assert route_harness_file_chunk(
            {
                "request_id": rid,
                "workspace_id": "ws-other",
                "path": "/workspace/big.bin",
                "index": 0,
                "total_chunks": 3,
                "content": base64.b64encode(b"nope").decode(),
            }
        ) is True
        await asyncio.sleep(0)
        route_harness_result(
            {
                "request_id": rid,
                "workspace_id": "ws-1",
                "path": "/workspace/big.bin",
                "chunked": True,
                "total_chunks": 3,
            }
        )

    transport.auto_reply = auto_reply
    with pytest.raises(RunnerAccessorError, match="workspace mismatch"):
        await _accessor(transport).read_file("/workspace/big.bin")


async def test_read_file_small_inline_payload_needs_no_chunks() -> None:
    """Small inline finals (no chunked flag) still decode without chunks."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "path": "/workspace/a.txt",
                "content": base64.b64encode(b"hi").decode(),
                "size": 2,
                "truncated": False,
                "mime": "text/plain",
            }
        )

    transport.auto_reply = auto_reply
    content = await _accessor(transport).read_file("/workspace/a.txt")
    assert content.content == b"hi"
    assert content.mime == "text/plain"


async def test_chunked_write_event_sequence() -> None:
    """A ~300 KiB write rides start + ordered chunks + finish, then ok."""
    transport = FakeTransport()
    content = bytes((i % 251 for i in range(300 * 1024)))

    async def auto_reply(event: str, payload: dict) -> None:
        if event == "harness:write_file_finish":
            route_harness_result(
                {
                    "request_id": payload["request_id"],
                    "workspace_id": "ws-1",
                    "ok": True,
                }
            )

    transport.auto_reply = auto_reply
    await _accessor(transport).write_file("/workspace/big.bin", content)

    events = [event for event, _ in transport.emitted]
    assert events[0] == "harness:write_file_start"
    assert events[-1] == "harness:write_file_finish"
    chunk_events = [event for event in events if event == "harness:write_file_chunk"]
    assert len(chunk_events) >= 2

    start_payload = _request_id(transport, "harness:write_file_start")
    assert start_payload["path"] == "/workspace/big.bin"
    total = int(start_payload["total_chunks"])
    assert total >= 2
    assert len(chunk_events) == total

    chunk_payloads = [
        payload
        for event, payload in transport.emitted
        if event == "harness:write_file_chunk"
    ]
    assert [p["index"] for p in chunk_payloads] == list(range(total))
    assert all(int(p["total_chunks"]) == total for p in chunk_payloads)
    assert all(
        len("".join(str(p["content"]).split())) <= HARNESS_CHUNK_B64_SIZE
        for p in chunk_payloads
    )
    joined = "".join(str(p["content"]) for p in chunk_payloads)
    assert base64.b64decode(joined) == content


async def test_write_file_rejects_above_absolute_cap_without_emitting() -> None:
    """Content past the 10 MiB cap raises before any socket emit."""
    transport = FakeTransport()
    accessor = _accessor(transport)
    with pytest.raises(ValueError, match="maximum size"):
        await accessor.write_file("/workspace/big.bin", b"x" * (10 * 1024 * 1024 + 4))
    assert transport.emitted == []


async def test_write_file_exact_cap_allowed_plus_one_rejected() -> None:
    """Exactly 10 MiB passes the cap check; 10 MiB + 1 raises, no emit."""
    transport = FakeTransport()
    accessor = _accessor(transport)

    async def auto_reply_ok(event: str, payload: dict) -> None:
        route_harness_result(
            {
                "request_id": payload["request_id"],
                "workspace_id": "ws-1",
                "ok": True,
            }
        )

    transport.auto_reply = auto_reply_ok
    await accessor.write_file("/workspace/exact.bin", b"x" * (10 * 1024 * 1024))
    assert transport.emitted, "exact-cap write must emit"

    transport2 = FakeTransport()
    with pytest.raises(ValueError, match="maximum size"):
        await _accessor(transport2).write_file(
            "/workspace/over.bin", b"x" * (10 * 1024 * 1024 + 1)
        )
    assert transport2.emitted == []


async def test_write_file_rejects_too_many_chunks(monkeypatch) -> None:
    """More than HARNESS_WRITE_MAX_CHUNKS slices raise before emitting."""
    from apps.harness.access import runner_accessor as accessor_module

    transport = FakeTransport()
    accessor = _accessor(transport)
    monkeypatch.setattr(
        accessor_module.RunnerWorkspaceAccessor,
        "_chunk_b64_slices",
        staticmethod(lambda payload_b64: ["QQ=="] * 65),
    )
    with pytest.raises(ValueError, match="maximum size"):
        await accessor.write_file("/workspace/big.bin", bytes(300 * 1024))
    assert transport.emitted == []


async def test_read_file_timeout_cancels_and_cleans_state() -> None:
    """A timed-out read emits harness:cancel and drops routing state."""
    transport = FakeTransport()
    accessor = _accessor(transport, default_timeout=0.02)
    with pytest.raises(TimeoutError, match="timed out"):
        await accessor.read_file("/workspace/big.bin")
    events = [event for event, _ in transport.emitted]
    assert events[0] == "harness:read_file"
    assert events[-1] == "harness:cancel"
    assert accessor._pending == {}
    assert accessor._file_chunks == {}


async def test_read_file_rejects_path_outside_workspace() -> None:
    """Absolute paths outside /workspace raise before any socket emit."""
    transport = FakeTransport()
    with pytest.raises(ValueError, match="under /workspace"):
        await _accessor(transport).read_file("/etc/passwd")
    assert transport.emitted == []


async def _read_with_final(
    final: dict, *, max_size: int | None = None
) -> object:
    """Run read_file against a canned inline final; return FileContent."""
    transport = FakeTransport()

    async def auto_reply(event: str, payload: dict) -> None:
        route_harness_result({"request_id": payload["request_id"], **final})

    transport.auto_reply = auto_reply
    return await _accessor(transport).read_file(
        "/workspace/a.txt", max_size=max_size
    )


async def test_read_file_rejects_invalid_base64() -> None:
    """Malformed base64 (validate=True) fails instead of decoding lossy."""
    with pytest.raises(RunnerAccessorError, match="invalid base64"):
        await _read_with_final(
            {
                "workspace_id": "ws-1",
                "content": "!!!not-base64!!!",
                "size": 4,
                "truncated": False,
            }
        )


async def test_read_file_rejects_size_mismatch() -> None:
    """Complete reads require decoded length == reported size."""
    with pytest.raises(RunnerAccessorError, match="size mismatch"):
        await _read_with_final(
            {
                "workspace_id": "ws-1",
                "content": base64.b64encode(b"hi").decode(),
                "size": 99,
                "truncated": False,
            }
        )


async def test_read_file_rejects_truncated_empty_payload() -> None:
    """Truncated reads must carry a non-empty prefix (empty file ⇒ False)."""
    with pytest.raises(RunnerAccessorError, match="non-empty"):
        await _read_with_final(
            {
                "workspace_id": "ws-1",
                "content": "",
                "size": 100,
                "truncated": True,
            }
        )


async def test_read_file_allows_truncated_prefix() -> None:
    """Truncated reads accept decoded length < reported size."""
    content = await _read_with_final(
        {
            "workspace_id": "ws-1",
            "content": base64.b64encode(b"hi").decode(),
            "size": 100,
            "truncated": True,
        }
    )
    assert content.content == b"hi"
    assert content.truncated is True


async def test_read_file_default_cap_is_5mib_not_100mib(
    monkeypatch,
) -> None:
    """Omitted max_size budgets the runner default (5 MiB), not 100 MiB."""
    from apps.harness.access import runner_accessor as accessor_module

    assert accessor_module.HARNESS_READ_DEFAULT_BYTES == 5 * 1024 * 1024
    assert accessor_module.HARNESS_READ_MAX_BYTES == 100 * 1024 * 1024

    # Pure arithmetic: the accessor must budget (5 MiB + 2) // 3 * 4 + 4
    # base64 chars for an omitted max_size, and the 100 MiB formula for
    # an explicit 100 MiB request.
    def chars_for(raw: int) -> int:
        return (raw + 2) // 3 * 4 + 4

    assert chars_for(5 * 1024 * 1024) < chars_for(100 * 1024 * 1024)

    seen: dict[str, object] = {}

    async def fake_wait(self, request_id, payload, timeout):
        try:
            read_limit = (
                int(payload.get("max_size"))
                if payload.get("max_size") is not None
                else accessor_module.HARNESS_READ_DEFAULT_BYTES
            )
        except (TypeError, ValueError):
            read_limit = accessor_module.HARNESS_READ_DEFAULT_BYTES
        if read_limit <= 0 or read_limit > accessor_module.HARNESS_READ_MAX_BYTES:
            read_limit = accessor_module.HARNESS_READ_DEFAULT_BYTES
        seen["max_chars"] = chars_for(read_limit)
        return {
            "request_id": request_id,
            "workspace_id": "ws-1",
            "content": base64.b64encode(b"hi").decode(),
            "size": 2,
            "truncated": False,
        }

    monkeypatch.setattr(
        accessor_module.RunnerWorkspaceAccessor,
        "_await_chunked_read_result",
        fake_wait,
    )
    content = await _accessor(FakeTransport()).read_file("/workspace/a.txt")
    assert content.content == b"hi"
    expected = (5 * 1024 * 1024 + 2) // 3 * 4 + 4
    assert seen["max_chars"] == expected
