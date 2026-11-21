"""Tests for chunked base64 harness reads/writes (runner side, fakes only)."""

from __future__ import annotations

import json
import tempfile
import uuid
from unittest.mock import AsyncMock

import pytest
from src.chunking import (
    CHUNK_B64_SIZE,
    MAX_READ_CHUNKS_PER_TRANSFER,
    MAX_UPLOAD_CHUNKS_PER_TRANSFER,
    normalize_base64,
    split_base64_chunks,
)
from src.config import RunnerSettings
from tests.command_support import make_interface as WebSocketInterface

_SMALL_JSON = 64 * 1024
_HALF_MIB_JSON = 512 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeChunkService:
    """Minimal fake WorkspaceService for chunked-transfer handler tests."""

    def __init__(self, *, read_content: str = "aGk=") -> None:
        self.read_content = read_content
        self.read_calls: list[tuple] = []
        self.write_calls: list[dict] = []

    # Step 4: websocket files/harness handlers call ``service.files``
    # directly. Mirror that surface so handler tests exercise the same
    # path; the underlying impls stay directly awaitable.
        self.files = self._FakeFiles(self)

    class _FakeFiles:
        def __init__(self, outer) -> None:
            self._outer = outer

        async def read_file(self, workspace_id, path, max_size=None, *, allow_external=False):
            return await self._outer._read_file_impl(
                workspace_id, path, max_size=max_size
            )

        async def write_file_content(
            self, workspace_id, path, content_b64, mode=0o644, *, allow_external=False
        ):
            return await self._outer._write_file_content_impl(
                workspace_id, path, content_b64, mode=mode
            )

    async def _read_file_impl(self, workspace_id, path, max_size=None):
        self.read_calls.append((workspace_id, path, max_size))
        return {"content": self.read_content, "size": len(self.read_content),
                "truncated": False, "mime_type": "text/plain"}

    async def _write_file_content_impl(
        self, workspace_id, path, content_b64, mode=0o644
    ):
        self.write_calls.append({"workspace_id": workspace_id, "path": path,
                                 "content_b64": content_b64, "mode": mode})


def _interface(service) -> WebSocketInterface:
    interface = WebSocketInterface(service, RunnerSettings(state_dir=tempfile.mkdtemp()))
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
    assert count > MAX_UPLOAD_CHUNKS_PER_TRANSFER  # 64-cap must not apply to reads
    assert count > 64


# ---------------------------------------------------------------------------
# handler contract: reads
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# handler contract: chunked harness write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "aGk=", "QUJDREVG"])
async def test_harness_write_out_of_order_happy(content: str) -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    request_id, path = "w-happy", "/workspace/a.txt"
    handlers = interface._sio.handlers["/"]
    chunks = [content[i : i + 4] for i in range(0, len(content), 4)]
    total = len(chunks)
    await handlers["harness:write_file_start"](
        _payload(workspace_id, request_id, path=path, total_chunks=total)
    )
    for index in reversed(range(total)):
        await handlers["harness:write_file_chunk"](
            _payload(
                workspace_id, request_id, path=path,
                index=index, total_chunks=total, content=chunks[index],
            )
        )
    await handlers["harness:write_file_finish"](
        _payload(workspace_id, request_id, path=path)
    )
    assert len(service.write_calls) == 1
    assert service.write_calls[0]["content_b64"] == content
    results = _emits_of(interface, "harness:write_file_result")
    assert results and results[-1].get("ok") is True
    await handlers["harness:write_file_finish"](
        _payload(workspace_id, request_id, path=path)
    )
    assert _emits_of(interface, "harness:write_file_result")[-1]["error"] == (
        "unknown write transfer"
    )


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


@pytest.mark.asyncio
async def test_harness_write_cancel_is_scoped_to_workspace() -> None:
    service = FakeChunkService()
    interface = _interface(service)
    workspace_id = uuid.uuid4()
    handlers = interface._sio.handlers["/"]
    payload = _payload(workspace_id, "cancel", path="/workspace/a.txt")
    await handlers["harness:write_file_start"]({**payload, "total_chunks": 0})
    await handlers["harness:cancel"]({**payload, "workspace_id": str(uuid.uuid4())})
    await handlers["harness:write_file_finish"](payload)
    assert _emits_of(interface, "harness:write_file_result")[-1]["ok"] is True
    await handlers["harness:write_file_start"]({**payload, "total_chunks": 0})
    await handlers["harness:cancel"](payload)
    await handlers["harness:write_file_finish"](payload)
    assert _emits_of(interface, "harness:write_file_result")[-1]["error"] == (
        "unknown write transfer"
    )
    assert len(service.write_calls) == 1


# ---------------------------------------------------------------------------
# malformed workspace/payload + small result payloads
# ---------------------------------------------------------------------------


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
@pytest.mark.parametrize("stage", ["start", "chunk", "finish"])
async def test_harness_write_malformed_workspace_answers_small_error(stage: str) -> None:
    service = FakeChunkService()
    interface = _interface(service)
    await interface._sio.handlers["/"][f"harness:write_file_{stage}"](
        {"workspace_id": "bad", "request_id": "w-bad",
         "path": "/workspace/a.txt", "index": 0, "total_chunks": 1,
         "content": "QUJD"})
    results = _emits_of(interface, "harness:write_file_result")
    assert len(results) == 1
    assert results[0]["workspace_id"] == "bad" and results[0].get("error")
    assert len(json.dumps(results[0])) < _SMALL_JSON
    assert service.write_calls == []


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
    import src.services.files as files_module
    from src.models import WorkspaceInfo
    from src.service import WorkspaceService

    monkeypatch.setattr(files_module, "FILE_UPLOAD_MAX_SIZE", cap_bytes)
    runtime = _CapProbeRuntime()
    service = WorkspaceService(
        runtime=runtime, settings=RunnerSettings(state_dir=tempfile.mkdtemp())
    )
    workspace_id = uuid.uuid4()
    service.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
    )
    return service, runtime, workspace_id


def _raw_bytes(n: int) -> bytes:
    return bytes((i % 251 for i in range(n)))


@pytest.mark.asyncio
async def test_write_file_content_invalid_base64_rejected_before_write(
    monkeypatch,
) -> None:
    service, runtime, workspace_id = _cap_service(monkeypatch, 16)
    with pytest.raises(ValueError, match="Invalid base64"):
        await service.files.write_file_content(
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
        await service.files.write_file_content(
            workspace_id, "/workspace/a.txt", oversize
        )
    assert runtime.archives == []


@pytest.mark.asyncio
async def test_write_file_content_exact_cap_allowed(monkeypatch) -> None:
    import base64

    service, runtime, workspace_id = _cap_service(monkeypatch, 16)
    exact = base64.b64encode(_raw_bytes(16)).decode()
    await service.files.write_file_content(workspace_id, "/workspace/a.txt", exact)
    assert runtime.archives != []
