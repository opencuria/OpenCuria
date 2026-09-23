"""Tests for chunked base64 file transport (runner side, fakes only)."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from src.chunking import (
    CHUNK_B64_SIZE,
    MAX_CHUNKS_PER_TRANSFER,
    MAX_READ_CHUNKS_PER_TRANSFER,
    BoundedReassembler,
    ChunkedTransferError,
    normalize_base64,
    split_base64_chunks,
)
from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface

_BIG_CAP = 10_000_000
_SMALL_JSON = 64 * 1024
_HALF_MIB_JSON = 512 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeChunkService:
    """Minimal fake WorkspaceService for chunked-transfer handler tests."""

    def __init__(self, *, read_content: str = "aGk=", download_content: str = "aGk=") -> None:
        self.supported_runtimes = ["docker"]
        self.sync_from_runtime = AsyncMock()
        self.recover_desktop_sessions_from_runtime = AsyncMock()
        self.run_health_check_loop = AsyncMock()
        self.get_workspace_heartbeat_statuses = AsyncMock(return_value=[])
        self.read_content = read_content
        self.download_content = download_content
        self.read_calls: list[tuple] = []
        self.upload_calls: list[dict] = []
        self.write_calls: list[dict] = []

    # Step 4: websocket files/harness handlers call ``service.files``
    # directly. Mirror that surface so handler tests exercise the same
    # path; the underlying impls stay directly awaitable.
        self.files = self._FakeFiles(self)

    class _FakeFiles:
        def __init__(self, outer) -> None:
            self._outer = outer

        async def read_file(self, workspace_id, path, max_size=None):
            return await self._outer._read_file_impl(
                workspace_id, path, max_size=max_size
            )

        async def download_file(self, workspace_id, path):
            return await self._outer._download_file_impl(workspace_id, path)

        async def upload_file(
            self, workspace_id, path, filename, content_b64, is_directory=False
        ):
            return await self._outer._upload_file_impl(
                workspace_id, path, filename, content_b64,
                is_directory=is_directory,
            )

        async def write_file_content(
            self, workspace_id, path, content_b64, mode=0o644
        ):
            return await self._outer._write_file_content_impl(
                workspace_id, path, content_b64, mode=mode
            )

    async def _read_file_impl(self, workspace_id, path, max_size=None):
        self.read_calls.append((workspace_id, path, max_size))
        return {"content": self.read_content, "size": len(self.read_content),
                "truncated": False, "mime_type": "text/plain"}

    async def _download_file_impl(self, workspace_id, path):
        return {"content": self.download_content, "filename": "a.txt",
                "is_archive": False, "size": len(self.download_content)}

    async def _upload_file_impl(
        self, workspace_id, path, filename, content_b64, is_directory=False
    ):
        self.upload_calls.append({"workspace_id": workspace_id, "path": path,
                                  "filename": filename, "content_b64": content_b64,
                                  "is_directory": is_directory})

    async def _write_file_content_impl(
        self, workspace_id, path, content_b64, mode=0o644
    ):
        self.write_calls.append({"workspace_id": workspace_id, "path": path,
                                 "content_b64": content_b64, "mode": mode})

    # Back-compat shims: existing tests call the facade methods directly.
    async def read_file(self, workspace_id, path, max_size=None):
        return await self._read_file_impl(workspace_id, path, max_size=max_size)

    async def download_file(self, workspace_id, path):
        return await self._download_file_impl(workspace_id, path)

    async def upload_file(
        self, workspace_id, path, filename, content_b64, is_directory=False
    ):
        return await self._upload_file_impl(
            workspace_id, path, filename, content_b64,
            is_directory=is_directory,
        )

    async def write_file_content(
        self, workspace_id, path, content_b64, mode=0o644
    ):
        return await self._write_file_content_impl(
            workspace_id, path, content_b64, mode=mode
        )


def _interface(service) -> WebSocketInterface:
    interface = WebSocketInterface(service, RunnerSettings())
    interface._sio.emit = AsyncMock()
    return interface


def _payload(workspace_id: uuid.UUID, request_id: str, **extra) -> dict:
    return {"workspace_id": str(workspace_id), "request_id": request_id, **extra}


def _emits(interface: WebSocketInterface) -> list[tuple[str, dict]]:
    return [(c.args[0], c.args[1]) for c in interface._sio.emit.await_args_list]


def _emits_of(interface: WebSocketInterface, event: str) -> list[dict]:
    return [p for name, p in _emits(interface) if name == event]


# ---------------------------------------------------------------------------
# chunking unit tests
# ---------------------------------------------------------------------------


def test_normalize_base64_strips_whitespace() -> None:
    assert normalize_base64("QU JD\nRE\tVG\r\n R0hJ") == "QUJDREVGR0hJ"
    assert normalize_base64("  \n\t ") == ""
    assert normalize_base64("") == ""


def test_split_base64_chunks_shape_and_roundtrip() -> None:
    raw = "QUJD" * 70_000  # 280000 chars, spans more than one chunk
    noisy = "\n".join(raw[i : i + 60] for i in range(0, len(raw), 60))
    chunks = split_base64_chunks(noisy)
    assert len(chunks) >= 2
    assert all(len(c) <= CHUNK_B64_SIZE for c in chunks)
    assert all(len(c) % 4 == 0 for c in chunks[:-1])
    assert "".join(chunks) == normalize_base64(noisy) == raw


def test_split_base64_chunks_edge_cases() -> None:
    assert split_base64_chunks("") == []
    assert split_base64_chunks("  \n\t") == []
    with pytest.raises(ValueError):
        split_base64_chunks("QUJD", size=0)
    with pytest.raises(ValueError):
        split_base64_chunks("QUJD", size=-8)


def test_100mib_read_fits_read_cap_but_not_upload_cap() -> None:
    raw_bytes = 100 * 1024 * 1024
    total_chars = (raw_bytes + 2) // 3 * 4
    count = -(-total_chars // CHUNK_B64_SIZE)  # ceil without floats
    assert count == 534
    assert count <= MAX_READ_CHUNKS_PER_TRANSFER  # 560
    assert count > MAX_CHUNKS_PER_TRANSFER  # 64-cap must not apply to reads
    assert count > 64


def test_reassembler_rejects_upload_over_64_chunks() -> None:
    asm = BoundedReassembler(max_total_chars=_BIG_CAP)
    with pytest.raises(ChunkedTransferError):
        asm.start("too-many", total_chunks=65)


def test_reassembler_start_invalid_and_duplicate() -> None:
    asm = BoundedReassembler(max_total_chars=_BIG_CAP)
    with pytest.raises(ChunkedTransferError):
        asm.start("zero", total_chunks=0)
    asm.start("dup", total_chunks=2)
    with pytest.raises(ChunkedTransferError, match="already in progress"):
        asm.start("dup", total_chunks=2)


def test_reassembler_out_of_order_finish_joins_in_index_order() -> None:
    asm = BoundedReassembler(max_total_chars=_BIG_CAP)
    asm.start("ooo", total_chunks=3, metadata={"path": "/workspace/a.txt"})
    asm.add_chunk("ooo", index=2, total_chunks=3, content="R0hJ")
    asm.add_chunk("ooo", index=0, total_chunks=3, content="QUJD")
    asm.add_chunk("ooo", index=1, total_chunks=3, content="REVG")
    content, metadata = asm.finish("ooo")
    assert content == "QUJD" + "REVG" + "R0hJ"
    assert metadata == {"path": "/workspace/a.txt"}


def test_reassembler_finish_missing_chunk_raises() -> None:
    asm = BoundedReassembler(max_total_chars=_BIG_CAP)
    asm.start("gap", total_chunks=2)
    asm.add_chunk("gap", index=0, total_chunks=2, content="QUJD")
    with pytest.raises(ChunkedTransferError, match="missing"):
        asm.finish("gap")


def test_reassembler_duplicate_chunk_raises() -> None:
    asm = BoundedReassembler(max_total_chars=_BIG_CAP)
    asm.start("dch", total_chunks=2)
    asm.add_chunk("dch", index=0, total_chunks=2, content="QUJD")
    with pytest.raises(ChunkedTransferError, match="duplicate"):
        asm.add_chunk("dch", index=0, total_chunks=2, content="QUJD")


def test_reassembler_oversize_single_chunk_raises() -> None:
    asm = BoundedReassembler(max_total_chars=_BIG_CAP * 10)
    asm.start("big1", total_chunks=1)
    with pytest.raises(ChunkedTransferError, match="exceeds"):
        asm.add_chunk("big1", index=0, total_chunks=1, content="A" * (CHUNK_B64_SIZE + 4))


def test_reassembler_buffer_cap_drops_transfer() -> None:
    asm = BoundedReassembler(max_total_chars=10)
    asm.start("cap", total_chunks=2)
    asm.add_chunk("cap", index=0, total_chunks=2, content="QUJD")
    with pytest.raises(ChunkedTransferError, match="size limit"):
        asm.add_chunk("cap", index=1, total_chunks=2, content="QUJDREVG")
    with pytest.raises(ChunkedTransferError, match="unknown transfer"):
        asm.finish("cap")


@pytest.mark.asyncio
async def test_reassembler_expiry_and_purge() -> None:
    asm = BoundedReassembler(max_total_chars=_BIG_CAP, timeout_s=0.01)
    asm.start("exp", total_chunks=1)
    await asyncio.sleep(0.05)
    with pytest.raises(ChunkedTransferError, match="expired"):
        asm.add_chunk("exp", index=0, total_chunks=1, content="QUJD")
    asm2 = BoundedReassembler(max_total_chars=_BIG_CAP, timeout_s=0.01)
    asm2.start("old", total_chunks=1)
    await asyncio.sleep(0.05)
    assert asm2.purge_expired() == 1
    assert len(asm2) == 0


# ---------------------------------------------------------------------------
# handler contract: reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_read_small_stays_inline() -> None:
    service = FakeChunkService(read_content="aGk=")
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    await interface._sio.handlers["/"]["files:read"](
        _payload(workspace_id, "r-small", path="/workspace/a.txt"))
    emitted = _emits(interface)
    assert len([e for e, _ in emitted if e == "files:content_chunk"]) == 0
    results = _emits_of(interface, "files:content_result")
    assert len(results) == 1
    assert results[0]["content"] == "aGk="
    assert not results[0].get("chunked")
    for _, payload in emitted:
        assert len(json.dumps(payload)) < _HALF_MIB_JSON


@pytest.mark.asyncio
async def test_files_read_large_emits_ordered_chunks() -> None:
    big = "QUJD" * 75_000  # 300000 base64 chars
    service = FakeChunkService(read_content=big)
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    await interface._sio.handlers["/"]["files:read"](
        _payload(workspace_id, "r-big", path="/workspace/a.txt"))
    chunks = _emits_of(interface, "files:content_chunk")
    assert len(chunks) >= 2
    total = chunks[0]["total_chunks"]
    assert [p["index"] for p in chunks] == list(range(total))
    assert all(p["total_chunks"] == total for p in chunks)
    assert all(len(p["content"]) <= CHUNK_B64_SIZE for p in chunks)
    results = _emits_of(interface, "files:content_result")
    assert len(results) == 1
    final = results[0]
    assert final["content"] == "" and final["chunked"] is True
    assert final["total_chunks"] == total
    assert "".join(p["content"] for p in chunks) == normalize_base64(big)
    for _, payload in _emits(interface):
        assert len(json.dumps(payload)) < _HALF_MIB_JSON


@pytest.mark.asyncio
async def test_harness_read_large_emits_ordered_chunks() -> None:
    big = "QUJD" * 75_000  # 300000 base64 chars
    service = FakeChunkService(read_content=big)
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    await interface._sio.handlers["/"]["harness:read_file"](
        _payload(workspace_id, "hr-big", path="/workspace/a.txt"))
    chunks = _emits_of(interface, "harness:read_file_chunk")
    assert len(chunks) >= 2
    total = chunks[0]["total_chunks"]
    assert [p["index"] for p in chunks] == list(range(total))
    assert all(p["total_chunks"] == total for p in chunks)
    assert all(len(p["content"]) <= CHUNK_B64_SIZE for p in chunks)
    results = _emits_of(interface, "harness:read_file_result")
    assert len(results) == 1
    final = results[0]
    assert final["content"] == "" and final["chunked"] is True
    assert final["total_chunks"] == total
    assert "".join(p["content"] for p in chunks) == normalize_base64(big)
    for _, payload in _emits(interface):
        assert len(json.dumps(payload)) < _HALF_MIB_JSON


# ---------------------------------------------------------------------------
# handler contract: chunked files upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_upload_chunked_out_of_order_happy() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    request_id, path = "up-happy", "/workspace"
    handlers = interface._sio.handlers["/"]
    await handlers["files:upload"](_payload(workspace_id, request_id, path=path,
        filename="a.txt", chunked=True, total_chunks=3))
    pieces = ["QUJD", "REVG", "R0hJ"]
    for index in (2, 0, 1):
        await handlers["files:upload_chunk"](_payload(workspace_id, request_id, path=path,
            index=index, total_chunks=3, content=pieces[index]))
    await handlers["files:upload_finish"](_payload(workspace_id, request_id, path=path, total_chunks=3))
    assert len(service.upload_calls) == 1
    assert service.upload_calls[0]["content_b64"] == "".join(pieces)
    assert service.upload_calls[0]["filename"] == "a.txt"
    results = _emits_of(interface, "files:upload_result")
    assert results and results[-1]["status"] == "success"


@pytest.mark.asyncio
async def test_files_upload_duplicate_chunk_fails_and_cleans_up() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    request_id, path = "up-dup", "/workspace"
    handlers = interface._sio.handlers["/"]
    await handlers["files:upload"](_payload(workspace_id, request_id, path=path,
        filename="a.txt", chunked=True, total_chunks=2))
    mk = lambda idx: _payload(workspace_id, request_id, path=path, index=idx,
                              total_chunks=2, content="QUJD")
    await handlers["files:upload_chunk"](mk(0))
    await handlers["files:upload_chunk"](mk(0))  # duplicate -> error + cleanup
    results = _emits_of(interface, "files:upload_result")
    assert results and results[-1]["status"] == "error" and results[-1].get("error")
    await handlers["files:upload_finish"](_payload(workspace_id, request_id, path=path, total_chunks=2))
    assert service.upload_calls == []
    assert _emits_of(interface, "files:upload_result")[-1]["status"] == "error"


@pytest.mark.asyncio
async def test_files_upload_invalid_start_and_unknown_chunk() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    handlers = interface._sio.handlers["/"]
    await handlers["files:upload"](_payload(workspace_id, "up-bad", path="/workspace",
        filename="a.txt", chunked=True, total_chunks=0))  # bad total_chunks=0
    await handlers["files:upload_chunk"](_payload(workspace_id, "up-ghost", path="/workspace",
        index=0, total_chunks=1, content="QUJD"))  # unknown transfer
    results = _emits_of(interface, "files:upload_result")
    assert len(results) == 2
    for payload in results:
        assert payload["status"] == "error" and payload.get("error")
        assert len(json.dumps(payload)) < _SMALL_JSON
    assert service.upload_calls == []


@pytest.mark.asyncio
async def test_files_upload_missing_chunk_finish_errors() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    request_id, path = "up-gap", "/workspace"
    handlers = interface._sio.handlers["/"]
    await handlers["files:upload"](_payload(workspace_id, request_id, path=path,
        filename="a.txt", chunked=True, total_chunks=2))
    await handlers["files:upload_chunk"](_payload(workspace_id, request_id, path=path,
        index=0, total_chunks=2, content="QUJD"))
    await handlers["files:upload_finish"](_payload(workspace_id, request_id, path=path, total_chunks=2))
    assert service.upload_calls == []
    results = _emits_of(interface, "files:upload_result")
    assert results and results[-1]["status"] == "error"
    err = results[-1]["error"].lower()
    assert "missing" in err or "incomplete" in err


@pytest.mark.asyncio
async def test_files_upload_start_over_64_rejected() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    await interface._sio.handlers["/"]["files:upload"](_payload(workspace_id, "up-65",
        path="/workspace", filename="a.txt", chunked=True, total_chunks=65))
    results = _emits_of(interface, "files:upload_result")
    assert len(results) == 1
    assert results[0]["status"] == "error" and results[0].get("error")
    assert service.upload_calls == []


# ---------------------------------------------------------------------------
# handler contract: chunked harness write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_write_chunked_out_of_order_happy() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    request_id, path = "w-happy", "/workspace/a.txt"
    handlers = interface._sio.handlers["/"]
    await handlers["harness:write_file_start"](_payload(workspace_id, request_id, path=path, total_chunks=2))
    for index, piece in ((1, "REVG"), (0, "QUJD")):
        await handlers["harness:write_file_chunk"](_payload(workspace_id, request_id, path=path,
            index=index, total_chunks=2, content=piece))
    await handlers["harness:write_file_finish"](_payload(workspace_id, request_id, path=path, total_chunks=2))
    assert len(service.write_calls) == 1
    assert service.write_calls[0]["content_b64"] == "QUJDREVG"
    results = _emits_of(interface, "harness:write_file_result")
    assert results and results[-1].get("ok") is True


@pytest.mark.asyncio
async def test_harness_write_unknown_chunk_errors_small() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    await interface._sio.handlers["/"]["harness:write_file_chunk"](_payload(workspace_id,
        "w-ghost", path="/workspace/a.txt", index=0, total_chunks=1, content="QUJD"))
    results = _emits_of(interface, "harness:write_file_result")
    assert len(results) == 1 and results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON
    assert service.write_calls == []


# ---------------------------------------------------------------------------
# download size cap (real WorkspaceService, faked runtime probes)
# ---------------------------------------------------------------------------


class _OversizeProbeRuntime:
    """Fake runtime whose size probe reports 200MB; records every command."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def exec_command_wait(self, instance_id, command, workdir=None, env=None):
        self.calls.append(list(command))
        if command[:2] == ["realpath", "-m"]:
            return 0, command[2] + "\n"
        if command[:2] == ["test", "-d"]:
            return 1, ""
        if command[:2] == ["sh", "-c"]:
            return 0, "200000000\n"
        raise AssertionError(f"unexpected command: {command!r}")


def _oversize_download_service():
    from src.models import WorkspaceInfo
    from src.service import WorkspaceService

    runtime = _OversizeProbeRuntime()
    service = WorkspaceService(runtimes={"docker": runtime}, settings=RunnerSettings())
    workspace_id = uuid.uuid4()
    service._cache[workspace_id] = WorkspaceInfo(workspace_id=workspace_id,
        instance_id="instance-1", status="running", runtime_type="docker")
    return service, runtime, workspace_id


@pytest.mark.asyncio
async def test_download_oversize_rejected_via_size_precheck() -> None:
    # Single-file downloads fail via a stat size precheck (no base64 read);
    # directory downloads need a size-reporting tar pass first, then the
    # payload pass — so "before base64" holds for files, not for dirs.
    service, runtime, workspace_id = _oversize_download_service()
    with pytest.raises(ValueError, match="maximum size"):
        await service.download_file(workspace_id, "/workspace/big.bin")
    assert not any(cmd[:1] == ["base64"] for cmd in runtime.calls)


@pytest.mark.asyncio
async def test_files_download_handler_reports_oversize_error() -> None:
    service, _runtime, workspace_id = _oversize_download_service()
    interface = _interface(service)
    await interface._sio.handlers["/"]["files:download"](
        _payload(workspace_id, "dl-big", path="/workspace/big.bin"))
    results = _emits_of(interface, "files:download_result")
    assert len(results) == 1
    assert results[0]["content"] == "" and "maximum size" in results[0].get("error", "")
    assert len(json.dumps(results[0])) < _SMALL_JSON


# ---------------------------------------------------------------------------
# malformed workspace/payload + small result payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_read_malformed_workspace_answers_small_error() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    await interface._sio.handlers["/"]["files:read"](
        {"workspace_id": "not-a-uuid", "request_id": "r-bad",
         "path": "/workspace/a.txt"})
    results = _emits_of(interface, "files:content_result")
    assert len(results) == 1
    assert results[0]["workspace_id"] == "not-a-uuid"
    assert results[0]["request_id"] == "r-bad"
    assert results[0]["content"] == "" and results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON
    assert service.read_calls == []


@pytest.mark.asyncio
async def test_files_read_non_string_path_answers_small_error() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    await interface._sio.handlers["/"]["files:read"](
        {"workspace_id": str(workspace_id), "request_id": "r-path",
         "path": ["not", "a", "string"]})
    results = _emits_of(interface, "files:content_result")
    assert len(results) == 1 and results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON
    assert service.read_calls == []


@pytest.mark.asyncio
async def test_files_download_malformed_workspace_answers_small_error() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    await interface._sio.handlers["/"]["files:download"](
        {"workspace_id": "!!!", "request_id": "dl-bad",
         "path": "/workspace/a.txt"})
    results = _emits_of(interface, "files:download_result")
    assert len(results) == 1
    assert results[0]["workspace_id"] == "!!!"
    assert results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON


@pytest.mark.asyncio
async def test_harness_read_malformed_workspace_answers_small_error() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    await interface._sio.handlers["/"]["harness:read_file"](
        {"workspace_id": "bad", "request_id": "hr-bad",
         "path": "/workspace/a.txt"})
    results = _emits_of(interface, "harness:read_file_result")
    assert len(results) == 1
    assert results[0]["workspace_id"] == "bad" and results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON
    assert service.read_calls == []


@pytest.mark.asyncio
async def test_harness_write_malformed_workspace_answers_small_error() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    await interface._sio.handlers["/"]["harness:write_file"](
        {"workspace_id": "bad", "request_id": "w-bad",
         "path": "/workspace/a.txt", "content": "QUJD"})
    results = _emits_of(interface, "harness:write_file_result")
    assert len(results) == 1
    assert results[0]["workspace_id"] == "bad" and results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON
    assert service.write_calls == []


@pytest.mark.asyncio
async def test_harness_write_chunk_malformed_workspace_answers_small_error() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    await interface._sio.handlers["/"]["harness:write_file_chunk"](
        {"workspace_id": "bad", "request_id": "w-bad",
         "path": "/workspace/a.txt", "index": 0, "total_chunks": 1,
         "content": "QUJD"})
    results = _emits_of(interface, "harness:write_file_result")
    assert len(results) == 1
    assert results[0]["workspace_id"] == "bad" and results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON


@pytest.mark.asyncio
async def test_files_upload_chunk_malformed_workspace_answers_small_error() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    await interface._sio.handlers["/"]["files:upload_chunk"](
        {"workspace_id": "bad", "request_id": "up-bad",
         "path": "/workspace", "index": 0, "total_chunks": 1,
         "content": "QUJD"})
    results = _emits_of(interface, "files:upload_result")
    assert len(results) == 1
    assert results[0]["workspace_id"] == "bad"
    assert results[0]["status"] == "error" and results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON
    assert service.upload_calls == []


@pytest.mark.asyncio
async def test_files_upload_chunk_missing_request_id_stays_silent() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    await interface._sio.handlers["/"]["files:upload_chunk"](
        {"workspace_id": str(workspace_id), "path": "/workspace",
         "index": 0, "total_chunks": 1, "content": "QUJD"})
    assert _emits_of(interface, "files:upload_result") == []
    assert service.upload_calls == []


@pytest.mark.asyncio
async def test_harness_write_finish_malformed_workspace_answers_small_error() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    await interface._sio.handlers["/"]["harness:write_file_finish"](
        {"workspace_id": "bad", "request_id": "w-fin",
         "path": "/workspace/a.txt", "total_chunks": 1})
    results = _emits_of(interface, "harness:write_file_result")
    assert len(results) == 1 and results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON


# ---------------------------------------------------------------------------
# service exact-cap: upload_file / write_file_content reject before side
# effects (real WorkspaceService, fake runtime, monkeypatched small cap)
# ---------------------------------------------------------------------------


class _CapProbeRuntime:
    """Fake runtime recording mkdir/put_archive; realpath always succeeds."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.archives: list[tuple[str, str, bytes]] = []

    async def exec_command_wait(self, instance_id, command, workdir=None, env=None):
        self.commands.append(list(command))
        if command[:2] == ["realpath", "-m"]:
            return 0, command[2] + "\n"
        return 0, ""

    async def put_archive(self, instance_id, target_dir, archive_data):
        self.archives.append((instance_id, target_dir, archive_data))


def _cap_service(monkeypatch, cap_bytes: int):
    """Real WorkspaceService with FILE_UPLOAD_MAX_SIZE patched small."""
    import src.service as service_module
    from src.models import WorkspaceInfo

    monkeypatch.setattr(service_module, "FILE_UPLOAD_MAX_SIZE", cap_bytes)
    runtime = _CapProbeRuntime()
    service = service_module.WorkspaceService(
        runtimes={"docker": runtime}, settings=RunnerSettings()
    )
    workspace_id = uuid.uuid4()
    service._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    return service, runtime, workspace_id


def _raw_bytes(n: int) -> bytes:
    return bytes((i % 251 for i in range(n)))


@pytest.mark.asyncio
async def test_upload_file_invalid_base64_rejected_before_mkdir(monkeypatch) -> None:
    service, runtime, workspace_id = _cap_service(monkeypatch, 16)
    with pytest.raises(ValueError, match="Invalid base64"):
        await service.upload_file(
            workspace_id, "/workspace", "a.bin", "!!!not-base64!!!"
        )
    # Only the read-only realpath probe may run; no mkdir side effect.
    assert all(cmd[:2] == ["realpath", "-m"] for cmd in runtime.commands)
    assert not any(cmd[:2] == ["mkdir", "-p"] for cmd in runtime.commands)
    assert runtime.archives == []


@pytest.mark.asyncio
async def test_upload_file_decoded_oversize_rejected_before_mkdir(monkeypatch) -> None:
    import base64

    service, runtime, workspace_id = _cap_service(monkeypatch, 16)
    oversize = base64.b64encode(_raw_bytes(17)).decode()
    with pytest.raises(ValueError, match="maximum size"):
        await service.upload_file(workspace_id, "/workspace", "a.bin", oversize)
    assert all(cmd[:2] == ["realpath", "-m"] for cmd in runtime.commands)
    assert not any(cmd[:2] == ["mkdir", "-p"] for cmd in runtime.commands)
    assert runtime.archives == []


@pytest.mark.asyncio
async def test_upload_file_exact_cap_allowed(monkeypatch) -> None:
    import base64

    service, runtime, workspace_id = _cap_service(monkeypatch, 16)
    exact = base64.b64encode(_raw_bytes(16)).decode()
    await service.upload_file(workspace_id, "/workspace", "a.bin", exact)
    assert runtime.archives != []
    assert any(cmd[:2] == ["mkdir", "-p"] for cmd in runtime.commands)


@pytest.mark.asyncio
async def test_upload_file_whitespace_normalized_and_accepted(monkeypatch) -> None:
    import base64

    service, runtime, workspace_id = _cap_service(monkeypatch, 16)
    exact = base64.b64encode(_raw_bytes(16)).decode()
    noisy = "\n".join(exact[i : i + 60] for i in range(0, len(exact), 60))
    await service.upload_file(workspace_id, "/workspace", "a.bin", noisy)
    assert runtime.archives != []


@pytest.mark.asyncio
async def test_write_file_content_invalid_base64_rejected_before_write(
    monkeypatch,
) -> None:
    service, runtime, workspace_id = _cap_service(monkeypatch, 16)
    with pytest.raises(ValueError, match="Invalid base64"):
        await service.write_file_content(
            workspace_id, "/workspace/a.txt", "!!!not-base64!!!"
        )
    assert runtime.archives == []


@pytest.mark.asyncio
async def test_write_file_content_decoded_oversize_rejected_before_write(
    monkeypatch,
) -> None:
    import base64

    service, runtime, workspace_id = _cap_service(monkeypatch, 16)
    oversize = base64.b64encode(_raw_bytes(17)).decode()
    with pytest.raises(ValueError, match="maximum size"):
        await service.write_file_content(
            workspace_id, "/workspace/a.txt", oversize
        )
    assert runtime.archives == []


@pytest.mark.asyncio
async def test_write_file_content_exact_cap_allowed(monkeypatch) -> None:
    import base64

    service, runtime, workspace_id = _cap_service(monkeypatch, 16)
    exact = base64.b64encode(_raw_bytes(16)).decode()
    await service.write_file_content(workspace_id, "/workspace/a.txt", exact)
    assert runtime.archives != []
