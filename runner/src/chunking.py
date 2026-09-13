"""Chunked base64 file transport over Socket.IO.

Daphne's default inbound message/frame cap is 1 MiB (oversize frames were
dropped as disconnects), so no single ``files:*`` / ``harness:*`` event may
carry a large base64 payload. Payloads above :data:`CHUNK_B64_SIZE` are
split into ordered chunk events of at most 256 KiB base64 each, followed
by a small final result event that only carries metadata
(``chunked=True``, ``total_chunks=N``, empty content). Engine.IO keeps its
separate 200 MiB HTTP buffer for pre-existing monolithic non-chunk events.

Small payloads keep riding inline in the final result for backward
compatibility with older peers.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

#: Base64 characters per chunk event. Well under Daphne's default 1 MiB
#: inbound cap (oversize frames were dropped as disconnects) including
#: JSON framing, and a multiple of 4 so concatenated chunks stay valid
#: base64 without re-padding. Must stay in sync with the backend copy in
#: ``backend/apps/harness/access/runner_accessor.py`` (``HARNESS_CHUNK_B64_SIZE``)
#: and the webapp copy in ``webapp/src/lib/fileChunks.ts``
#: (``FILE_CHUNK_B64_SIZE``).
CHUNK_B64_SIZE = 256 * 1024

#: Upper bound for the number of chunks per upload/write transfer. 64 chunks
#: carry ~12 MiB of raw bytes, which covers the 10 MiB upload/write caps
#: with headroom. Applies to ``files:upload_*`` and
#: ``harness:write_file_*`` only.
MAX_UPLOAD_CHUNKS_PER_TRANSFER = 64

#: Backward-compatible alias for upload/write chunk caps.
MAX_CHUNKS_PER_TRANSFER = MAX_UPLOAD_CHUNKS_PER_TRANSFER

#: Upper bound for the number of chunks per read/download transfer. 100 MiB
#: of raw bytes encode to ~139.81 M base64 chars, i.e. ~534 chunks at
#: 256 KiB; 560 leaves headroom for padding and slack.
MAX_READ_CHUNKS_PER_TRANSFER = 560

_BASE64_WS_RE = re.compile(r"\s+")


def normalize_base64(payload: str) -> str:
    """Return *payload* with all whitespace removed."""
    if not payload:
        return ""
    return _BASE64_WS_RE.sub("", payload)


def split_base64_chunks(
    payload: str, size: int = CHUNK_B64_SIZE
) -> list[str]:
    """Split whitespace-free base64 into chunks of at most *size* chars.

    The chunk size is rounded down to a multiple of 4 so every non-final
    chunk stays valid base64 on its own and chunks can be concatenated
    without re-padding.
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    size -= size % 4
    clean = normalize_base64(payload)
    if not clean:
        return []
    return [clean[i : i + size] for i in range(0, len(clean), size)]


def should_chunk(payload: str, limit: int = CHUNK_B64_SIZE) -> bool:
    """Return True when *payload* is too large for a single inline result."""
    return len(normalize_base64(payload)) > limit


def max_b64_chars_for_raw_bytes(raw_bytes: int) -> int:
    """Return the base64 char budget for *raw_bytes* of raw data (+ slack)."""
    return (raw_bytes + 2) // 3 * 4 + 4


class ChunkedTransferError(ValueError):
    """Raised for invalid, oversized, expired or incomplete chunk transfers."""


@dataclass
class _PendingTransfer:
    """Reassembly state for one in-flight chunked transfer."""

    total_chunks: int
    metadata: dict
    created_at: float
    chunks: dict[int, str] = field(default_factory=dict)
    buffered_chars: int = 0


class BoundedReassembler:
    """Bounded, timeout-scoped reassembly of ordered base64 chunk streams.

    One instance serves many concurrent ``request_id`` transfers but never
    holds more than ``max_entries`` transfers or ``max_total_chars`` base64
    characters per transfer. Stale entries expire after ``timeout_s`` and are
    purged lazily on every mutation, so partial state can never grow without
    bounds or linger forever.
    """

    def __init__(
        self,
        *,
        max_entries: int = 16,
        max_total_chars: int,
        timeout_s: float = 120.0,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if max_total_chars <= 0:
            raise ValueError("max_total_chars must be positive")
        self._max_entries = max_entries
        self._max_total_chars = max_total_chars
        self._timeout_s = timeout_s
        self._entries: dict[str, _PendingTransfer] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def purge_expired(self, now: float | None = None) -> int:
        """Drop transfers idle for longer than ``timeout_s``; return count."""
        current = time.monotonic() if now is None else now
        expired = [
            key
            for key, entry in self._entries.items()
            if current - entry.created_at > self._timeout_s
        ]
        for key in expired:
            del self._entries[key]
        return len(expired)

    def clear(self) -> None:
        """Drop all pending transfers (e.g. on disconnect)."""
        self._entries.clear()

    def discard(self, key: str) -> None:
        """Drop one pending transfer, if present."""
        self._entries.pop(key, None)

    def start(
        self,
        key: str,
        *,
        total_chunks: int,
        metadata: dict | None = None,
        max_chunks: int = MAX_CHUNKS_PER_TRANSFER,
    ) -> None:
        """Begin tracking a new transfer of *total_chunks* chunks."""
        if not key:
            raise ChunkedTransferError("request_id must not be empty")
        try:
            total = int(total_chunks)
        except (TypeError, ValueError) as exc:
            raise ChunkedTransferError(
                f"invalid total_chunks: {total_chunks!r}"
            ) from exc
        try:
            cap = int(max_chunks)
        except (TypeError, ValueError) as exc:
            raise ChunkedTransferError(
                f"invalid max_chunks: {max_chunks!r}"
            ) from exc
        if total <= 0 or total > cap:
            raise ChunkedTransferError(
                f"invalid total_chunks: {total_chunks!r}"
            )
        self.purge_expired()
        if key in self._entries:
            raise ChunkedTransferError(
                f"transfer already in progress: {key!r}"
            )
        if len(self._entries) >= self._max_entries:
            raise ChunkedTransferError("too many concurrent chunked transfers")
        self._entries[key] = _PendingTransfer(
            total_chunks=total,
            metadata=dict(metadata or {}),
            created_at=time.monotonic(),
        )

    def add_chunk(
        self,
        key: str,
        *,
        index: int,
        total_chunks: int,
        content: str,
    ) -> None:
        """Append one chunk to the transfer *key*."""
        entry = self._entries.get(key)
        if entry is None:
            raise ChunkedTransferError(f"unknown transfer: {key!r}")
        if time.monotonic() - entry.created_at > self._timeout_s:
            del self._entries[key]
            raise ChunkedTransferError(f"transfer expired: {key!r}")
        try:
            chunk_index = int(index)
            chunk_total = int(total_chunks)
        except (TypeError, ValueError) as exc:
            raise ChunkedTransferError(
                f"invalid chunk index/total: {index!r}/{total_chunks!r}"
            ) from exc
        if chunk_total != entry.total_chunks:
            raise ChunkedTransferError(
                f"total_chunks mismatch for {key!r}: "
                f"{chunk_total!r} != {entry.total_chunks!r}"
            )
        if chunk_index < 0 or chunk_index >= entry.total_chunks:
            raise ChunkedTransferError(
                f"chunk index out of range for {key!r}: {index!r}"
            )
        if chunk_index in entry.chunks:
            raise ChunkedTransferError(
                f"duplicate chunk {chunk_index} for {key!r}"
            )
        clean = normalize_base64(content or "")
        if not clean:
            raise ChunkedTransferError(
                f"empty chunk {chunk_index} for {key!r}"
            )
        if len(clean) > CHUNK_B64_SIZE:
            raise ChunkedTransferError(
                f"chunk {chunk_index} for {key!r} exceeds "
                f"{CHUNK_B64_SIZE} chars"
            )
        if entry.buffered_chars + len(clean) > self._max_total_chars:
            del self._entries[key]
            raise ChunkedTransferError(
                f"transfer {key!r} exceeds size limit"
            )
        entry.chunks[chunk_index] = clean
        entry.buffered_chars += len(clean)

    def finish(self, key: str) -> tuple[str, dict]:
        """Join and return ``(content, metadata)`` for a complete transfer."""
        entry = self._entries.pop(key, None)
        if entry is None:
            raise ChunkedTransferError(f"unknown transfer: {key!r}")
        if time.monotonic() - entry.created_at > self._timeout_s:
            raise ChunkedTransferError(f"transfer expired: {key!r}")
        missing = [
            i for i in range(entry.total_chunks) if i not in entry.chunks
        ]
        if missing:
            raise ChunkedTransferError(
                f"incomplete transfer {key!r}: "
                f"missing chunks {missing[:8]}"
                f" ({len(missing)} of {entry.total_chunks})"
            )
        joined = "".join(entry.chunks[i] for i in range(entry.total_chunks))
        return joined, entry.metadata
