"""Characterize upload protocol limits and cleanup through registered RPCs."""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from src.chunking import CHUNK_B64_SIZE
from src.interfaces.websocket import WebSocketInterface
from tests.test_file_chunking import FakeChunkService, _emits_of, _interface, _payload

PREFIXES = ("files:upload", "harness:write_file")
Transfer = tuple[FakeChunkService, WebSocketInterface, str, dict[str, Any]]


@pytest.fixture(params=PREFIXES)
def transfer(request: pytest.FixtureRequest) -> Transfer:
    """Build the same upload conversation for either wire namespace."""
    service = FakeChunkService()
    interface = _interface(service)
    payload = _payload(
        uuid.uuid4(), "transfer", path="/workspace/a.txt", filename="a.txt"
    )
    return service, interface, request.param, payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunk", "error"),
    [
        ({"index": "bad"}, "invalid chunk index/total"),
        ({"index": -1}, "chunk index out of range"),
        ({"index": 1}, "chunk index out of range"),
        ({"total_chunks": 2}, "total_chunks mismatch for upload"),
        ({"content": " \n\t"}, "empty chunk 0"),
        ({"content": "A" * (CHUNK_B64_SIZE + 1)}, "chunk 0 too large"),
        ({"path": "/workspace/other"}, "path mismatch for upload"),
        ({"workspace_id": str(uuid.uuid4())}, "workspace mismatch for upload"),
    ],
)
async def test_invalid_chunk_drops_transfer(
    transfer: Transfer, chunk: dict[str, Any], error: str
) -> None:
    """Malformed chunks never reach files and cannot be finished afterward."""
    service, interface, prefix, payload = transfer
    handlers = interface._sio.handlers["/"]
    await handlers[f"{prefix}_start"]({**payload, "total_chunks": 1})
    await handlers[f"{prefix}_chunk"](
        {**payload, "index": 0, "total_chunks": 1, "content": "QUJD", **chunk}
    )
    assert _emits_of(interface, f"{prefix}_result")[-1]["error"] == error
    await handlers[f"{prefix}_finish"](payload)
    label = "write" if prefix.startswith("harness:") else "upload"
    assert _emits_of(interface, f"{prefix}_result")[-1]["error"] == (
        f"unknown {label} transfer"
    )
    assert service.write_calls == service.upload_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("announced", [None, "bad", 2])
async def test_invalid_finish_count_drops_transfer(
    transfer: Transfer, announced: object
) -> None:
    """Null, unparsable and mismatched finish counts retain their wire errors."""
    service, interface, prefix, payload = transfer
    handlers = interface._sio.handlers["/"]
    await handlers[f"{prefix}_start"]({**payload, "total_chunks": 0})
    await handlers[f"{prefix}_finish"]({**payload, "total_chunks": announced})
    label = "write" if prefix.startswith("harness:") else "upload"
    error = (
        f"total_chunks mismatch for {label}"
        if announced == 2
        else f"invalid total_chunks for {label}"
    )
    assert _emits_of(interface, f"{prefix}_result")[-1]["error"] == error
    await handlers[f"{prefix}_finish"](payload)
    assert _emits_of(interface, f"{prefix}_result")[-1]["error"] == (
        f"unknown {label} transfer"
    )
    assert service.write_calls == service.upload_calls == []


@pytest.mark.asyncio
async def test_buffer_budget_counts_normalized_chunks(
    transfer: Transfer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The encoded budget includes slack and is shared by both upload kinds."""
    monkeypatch.setattr("src.services.files.FILE_UPLOAD_MAX_SIZE", 3)
    service, interface, prefix, payload = transfer
    handlers = interface._sio.handlers["/"]
    await handlers[f"{prefix}_start"]({**payload, "total_chunks": 3})
    for index in range(3):
        await handlers[f"{prefix}_chunk"](
            {**payload, "index": index, "total_chunks": 3, "content": " Q U J D\n"}
        )
        results = _emits_of(interface, f"{prefix}_result")
        if index < 2:
            assert results == []
    assert results[-1]["error"] == "Upload exceeds maximum size of 3 bytes"
    await handlers[f"{prefix}_finish"](payload)
    assert "unknown" in _emits_of(interface, f"{prefix}_result")[-1]["error"]
    assert service.write_calls == service.upload_calls == []


@pytest.mark.asyncio
async def test_upload_capacity_is_shared_and_completion_releases_a_slot() -> None:
    """Browser uploads and harness writes share the existing 16-transfer cap."""
    service = FakeChunkService()
    interface = _interface(service)
    handlers = interface._sio.handlers["/"]
    payload = _payload(uuid.uuid4(), "", path="/workspace/a.txt", filename="a.txt")
    for index in range(16):
        prefix = PREFIXES[index % 2]
        await handlers[f"{prefix}_start"](
            {**payload, "request_id": str(index), "total_chunks": 0}
        )
    assert interface._sio.emit.await_count == 0
    await handlers["files:upload_start"](
        {**payload, "request_id": "overflow", "total_chunks": 0}
    )
    assert _emits_of(interface, "files:upload_result")[-1]["error"] == (
        "too many concurrent uploads"
    )
    await handlers["harness:write_file_finish"]({**payload, "request_id": "1"})
    await handlers["files:upload_start"](
        {**payload, "request_id": "overflow", "total_chunks": 0}
    )
    await handlers["files:upload_finish"]({**payload, "request_id": "overflow"})
    assert _emits_of(interface, "files:upload_result")[-1]["status"] == "success"
    assert len(service.write_calls) == len(service.upload_calls) == 1


@pytest.mark.asyncio
async def test_expiry_uses_creation_time_and_strict_120_second_limit(
    transfer: Transfer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunk at 120 seconds survives; a later chunk expires the same upload."""
    service, interface, prefix, payload = transfer
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    handlers = interface._sio.handlers["/"]
    await handlers[f"{prefix}_start"]({**payload, "total_chunks": 2})
    now[0] += 120
    await handlers[f"{prefix}_chunk"](
        {**payload, "index": 0, "total_chunks": 2, "content": "QUJD"}
    )
    assert _emits_of(interface, f"{prefix}_result") == []
    now[0] += 0.001
    await handlers[f"{prefix}_chunk"](
        {**payload, "index": 1, "total_chunks": 2, "content": "REVG"}
    )
    assert _emits_of(interface, f"{prefix}_result")[-1]["error"] == (
        "unknown upload transfer"
    )
    assert service.write_calls == service.upload_calls == []


@pytest.mark.asyncio
async def test_disconnect_drops_both_upload_kinds(transfer: Transfer) -> None:
    """Disconnect invalidates pending uploads without executing them."""
    service, interface, prefix, payload = transfer
    handlers = interface._sio.handlers["/"]
    await handlers[f"{prefix}_start"]({**payload, "total_chunks": 0})
    await handlers["disconnect"]()
    await handlers[f"{prefix}_finish"](payload)
    label = "write" if prefix.startswith("harness:") else "upload"
    assert _emits_of(interface, f"{prefix}_result")[-1]["error"] == (
        f"unknown {label} transfer"
    )
    assert service.write_calls == service.upload_calls == []


@pytest.mark.asyncio
async def test_duplicate_start_preserves_the_original_transfer(
    transfer: Transfer,
) -> None:
    """A rejected restart cannot replace the original chunks or file options."""
    service, interface, prefix, payload = transfer
    handlers = interface._sio.handlers["/"]
    await handlers[f"{prefix}_start"]({**payload, "total_chunks": 1, "mode": 0o640})
    await handlers[f"{prefix}_chunk"](
        {**payload, "index": 0, "total_chunks": 1, "content": "QUJD"}
    )
    await handlers[f"{prefix}_start"]({**payload, "total_chunks": 0, "mode": 0o777})
    label = "write" if prefix.startswith("harness:") else "upload"
    assert _emits_of(interface, f"{prefix}_result")[-1]["error"] == (
        f"{label} already in progress"
    )
    await handlers[f"{prefix}_finish"](payload)
    calls = service.write_calls or service.upload_calls
    assert len(calls) == 1
    assert calls[0]["content_b64"] == "QUJD"
    if prefix.startswith("harness:"):
        assert calls[0]["mode"] == 0o640


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["workspace_id", "path"])
async def test_finish_keeps_namespace_specific_error_precedence(
    transfer: Transfer, field: str
) -> None:
    """Harness identity errors precede counts; browser count errors come first."""
    service, interface, prefix, payload = transfer
    handlers = interface._sio.handlers["/"]
    await handlers[f"{prefix}_start"]({**payload, "total_chunks": 0})
    invalid = {"workspace_id": str(uuid.uuid4()), "path": "/workspace/other"}
    await handlers[f"{prefix}_finish"](
        {**payload, field: invalid[field], "total_chunks": None}
    )
    expected = (
        f"{'workspace' if field == 'workspace_id' else 'path'} mismatch for write"
        if prefix.startswith("harness:")
        else "invalid total_chunks for upload"
    )
    assert _emits_of(interface, f"{prefix}_result")[-1]["error"] == expected
    assert service.write_calls == service.upload_calls == []


@pytest.mark.asyncio
async def test_finish_does_not_itself_trigger_lazy_expiry(
    transfer: Transfer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry is triggered by starts/chunks; a complete idle upload may finish."""
    service, interface, prefix, payload = transfer
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    handlers = interface._sio.handlers["/"]
    await handlers[f"{prefix}_start"]({**payload, "total_chunks": 0})
    now[0] += 121
    await handlers[f"{prefix}_finish"](payload)
    assert "error" not in _emits_of(interface, f"{prefix}_result")[-1]
    assert len(service.write_calls or service.upload_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "chunk_event", "result_event"),
    [
        ("files:read", "files:content_chunk", "files:content_result"),
        ("harness:read_file", "harness:read_file_chunk", "harness:read_file_result"),
        ("files:download", "files:download_chunk", "files:download_result"),
    ],
)
@pytest.mark.parametrize("size", [0, CHUNK_B64_SIZE, CHUNK_B64_SIZE + 4])
async def test_reply_framing_at_chunk_threshold(
    event: str, chunk_event: str, result_event: str, size: int
) -> None:
    """Every reply kind normalizes content and shares the exact inline threshold."""
    content = "QUJD" * (size // 4)
    noisy = f" \n{content}\t "
    service = FakeChunkService(read_content=noisy, download_content=noisy)
    interface = _interface(service)
    payload = _payload(uuid.uuid4(), "reply", path="/workspace/a.txt")
    await interface._sio.handlers["/"][event](payload)
    chunks = _emits_of(interface, chunk_event)
    result = _emits_of(interface, result_event)
    assert len(result) == 1
    assert all(result[0][key] == value for key, value in payload.items())
    assert result[0]["size"] == len(noisy)
    if size <= CHUNK_B64_SIZE:
        assert chunks == []
        assert result[0]["content"] == content
        assert "chunked" not in result[0]
    else:
        assert [chunk["index"] for chunk in chunks] == [0, 1]
        assert all(chunk["total_chunks"] == 2 for chunk in chunks)
        assert "".join(chunk["content"] for chunk in chunks) == content
        assert result[0]["content"] == ""
        assert result[0]["chunked"] is True
        assert result[0]["total_chunks"] == 2
        assert interface._sio.emit.await_args.args[0] == result_event
