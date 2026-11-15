"""Bounded base64 file transport over Socket.IO.

Read/download replies above ``CHUNK_B64_SIZE`` use ordered chunk events and
a metadata-only result; smaller replies carry content inline. File uploads
and harness writes always use start/chunk/finish, with zero chunks for empty files.
"""

from __future__ import annotations

#: Base64 characters per chunk event, a multiple of 4 so concatenated chunks
#: stay valid without re-padding. Must stay in sync with the backend copy in
#: ``backend/apps/harness/access/runner_accessor.py`` (``HARNESS_CHUNK_B64_SIZE``)
#: and the shared client constant in ``packages/chat-core/src/fileChunks.ts``
#: (``FILE_CHUNK_B64_SIZE``).
CHUNK_B64_SIZE = 256 * 1024

#: Upper bound for the number of chunks per upload/write transfer. 64 chunks
#: carry ~12 MiB of raw bytes, which covers the 10 MiB upload/write caps
#: with headroom. Applies to ``files:upload_*`` and
#: ``harness:write_file_*`` only.
MAX_UPLOAD_CHUNKS_PER_TRANSFER = 64

#: Upper bound for the number of chunks per read/download transfer. 100 MiB
#: of raw bytes encode to ~139.81 M base64 chars, i.e. ~534 chunks at
#: 256 KiB; 560 leaves headroom for padding and slack.
MAX_READ_CHUNKS_PER_TRANSFER = 560


def normalize_base64(payload: str) -> str:
    """Return *payload* with all whitespace removed."""
    if not payload:
        return ""
    # The native splitter shares regex \s's Unicode whitespace rules without
    # invoking the regex engine on every file-transfer chunk.
    return "".join(payload.split())


def split_base64_chunks(payload: str, size: int = CHUNK_B64_SIZE) -> list[str]:
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
