"""Bounded file-transfer framing and upload reassembly, independent of Socket.IO.

The control adapter validates file destinations and invokes the file manager.
This component owns only encoded buffers, transfer identity, limits and expiry;
it never reads or writes workspace files or imports workspace orchestration.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ..chunking import (
    CHUNK_B64_SIZE,
    MAX_UPLOAD_CHUNKS_PER_TRANSFER,
    max_b64_chars_for_raw_bytes,
    normalize_base64,
    should_chunk,
    split_base64_chunks,
)

UploadKind = Literal["harness-write", "browser-upload"]
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class UploadTarget:
    """Validated destination and file options retained until upload completion."""

    workspace_id: str
    path: str
    filename: str = ""
    is_directory: bool = False
    mode: int = 0o644


@dataclass
class _PendingUpload:
    target: UploadTarget
    total_chunks: int
    max_size: int
    created_at: float
    chunks: dict[int, str] = field(default_factory=dict)
    buffered_chars: int = 0


class UploadTransfers:
    """Own pending uploads shared by browser uploads and harness writes."""

    def __init__(self) -> None:
        """Create an empty connection-scoped upload buffer."""
        self._uploads: dict[tuple[UploadKind, str], _PendingUpload] = {}

    @staticmethod
    def parse_total_chunks(raw: object) -> int:
        """Parse and bound a start announcement using the upload chunk cap."""
        try:
            total = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid total_chunks for transfer") from exc
        if total < 0 or total > MAX_UPLOAD_CHUNKS_PER_TRANSFER:
            raise ValueError("invalid total_chunks for transfer")
        return total

    def prune(self) -> None:
        """Drop buffers created more than 120 seconds ago, without refreshing TTL."""
        now = time.monotonic()
        expired = [
            key
            for key, upload in self._uploads.items()
            if now - upload.created_at > 120.0
        ]
        for key in expired:
            self._uploads.pop(key, None)

    def start(
        self,
        kind: UploadKind,
        request_id: str,
        *,
        target: UploadTarget,
        total_chunks: int,
        max_size: int,
    ) -> None:
        """Reserve a validated transfer in the shared 16-upload buffer budget."""
        key = (kind, request_id)
        if key in self._uploads:
            label = "write" if kind == "harness-write" else "upload"
            raise ValueError(f"{label} already in progress")
        if len(self._uploads) >= 16:
            raise ValueError("too many concurrent uploads")
        self._uploads[key] = _PendingUpload(
            target=target,
            total_chunks=total_chunks,
            max_size=max_size,
            created_at=time.monotonic(),
        )

    def append(
        self,
        kind: UploadKind,
        request_id: str,
        *,
        workspace_id: str,
        path: str,
        index: object,
        total_chunks: object,
        content: object,
    ) -> None:
        """Validate and buffer one chunk; callers discard on protocol errors."""
        upload = self._uploads.get((kind, request_id))
        if upload is None:
            raise ValueError("unknown upload transfer")
        if upload.target.workspace_id != workspace_id:
            raise ValueError("workspace mismatch for upload")
        if upload.target.path != path:
            raise ValueError("path mismatch for upload")
        try:
            index_n = int(index)  # type: ignore[arg-type]
            total_n = int(total_chunks)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid chunk index/total") from exc
        if total_n != upload.total_chunks:
            raise ValueError("total_chunks mismatch for upload")
        if index_n < 0 or index_n >= total_n:
            raise ValueError("chunk index out of range")
        if index_n in upload.chunks:
            raise ValueError(f"duplicate chunk {index_n}")
        clean = normalize_base64(str(content or ""))
        if not clean:
            raise ValueError(f"empty chunk {index_n}")
        if len(clean) > CHUNK_B64_SIZE:
            raise ValueError(f"chunk {index_n} too large")
        if upload.buffered_chars + len(clean) > max_b64_chars_for_raw_bytes(
            upload.max_size
        ):
            self.discard(kind, request_id)
            raise ValueError(f"Upload exceeds maximum size of {upload.max_size} bytes")
        upload.chunks[index_n] = clean
        upload.buffered_chars += len(clean)

    def finish(
        self,
        kind: UploadKind,
        request_id: str,
        *,
        workspace_id: str,
        path: str,
        announcement: Mapping[str, object],
    ) -> tuple[UploadTarget, str]:
        """Consume a complete upload, preserving namespace-specific wire errors."""
        key = (kind, request_id)
        upload = self._uploads.get(key)
        label = "write" if kind == "harness-write" else "upload"
        if upload is None:
            raise ValueError(f"unknown {label} transfer")
        # Harness writes check identity before the finish count. Browser uploads
        # historically check the count first; retain that error precedence.
        if kind == "harness-write":
            self._check_target(upload, workspace_id, path, label=label)
        announced = announcement.get("total_chunks", upload.total_chunks)
        try:
            announced_n = int(announced)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid total_chunks for {label}") from exc
        if announced_n != upload.total_chunks:
            raise ValueError(f"total_chunks mismatch for {label}")
        self._uploads.pop(key)
        self._check_target(upload, workspace_id, path, label="upload")
        total = upload.total_chunks
        missing = [i for i in range(total) if i not in upload.chunks]
        if missing:
            raise ValueError(
                f"incomplete upload: missing {len(missing)} of {total} chunks"
            )
        return upload.target, "".join(upload.chunks[i] for i in range(total))

    @staticmethod
    def _check_target(
        upload: _PendingUpload, workspace_id: str, path: str, *, label: str
    ) -> None:
        if upload.target.workspace_id != workspace_id:
            raise ValueError(f"workspace mismatch for {label}")
        if upload.target.path != path:
            raise ValueError(f"path mismatch for {label}")

    def discard(self, kind: UploadKind, request_id: str) -> None:
        """Drop one transfer after a malformed request or execution failure."""
        self._uploads.pop((kind, request_id), None)

    def cancel(self, kind: UploadKind, request_id: str, workspace_id: object) -> None:
        """Cancel only a transfer matching the namespace, request and workspace."""
        upload = self._uploads.get((kind, request_id))
        if upload and upload.target.workspace_id == workspace_id:
            self.discard(kind, request_id)

    def clear(self) -> None:
        """Drop connection-scoped partial uploads when the connection is lost."""
        self._uploads.clear()


async def emit_chunked_result(
    emit: Emit,
    *,
    chunk_event: str,
    result_event: str,
    base: dict[str, Any],
    content: str,
) -> None:
    """Emit an inline result or ordered chunks followed by metadata-only result."""
    clean = normalize_base64(content or "")
    if not should_chunk(clean):
        await emit(result_event, {**base, "content": clean})
        return
    chunks = split_base64_chunks(clean)
    total = len(chunks)
    for index, piece in enumerate(chunks):
        await emit(
            chunk_event,
            {**base, "index": index, "total_chunks": total, "content": piece},
        )
    await emit(
        result_event,
        {**base, "content": "", "chunked": True, "total_chunks": total},
    )
