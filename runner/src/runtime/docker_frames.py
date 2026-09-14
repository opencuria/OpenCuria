"""Docker multiplexed stream framing (non-TTY exec, stdin/stdout/stderr).

Docker prefixes every non-TTY exec frame with an 8-byte header::

    >BxxxL  ->  stream (1=stdout, 2=stderr), payload length (big-endian u32)

Frames may arrive fragmented across socket recv() calls, and one recv()
may contain multiple frames.  :class:`DockerFrameParser` reassembles
them incrementally with strict bounds: unknown stream ids and frames
larger than ``max_frame_size`` fail closed.

https://docs.docker.com/engine/api/v1.24/#attach-to-a-container
"""

from __future__ import annotations

import struct

DOCKER_STDOUT = 1
DOCKER_STDERR = 2

DOCKER_FRAME_HEADER_LEN = 8
#: Max Docker multiplex frame payload (bounded, fail-closed above).
#: Docker may deliver frames well above 64KiB, so the parser accepts up
#: to 16MiB per frame; the pump layer re-chunks complete payloads into
#: <=64KiB queue items (see ``DOCKER_QUEUE_CHUNK_SIZE``) so no data is
#: ever truncated.
DOCKER_FRAME_MAX_SIZE = 16 * 1024 * 1024

#: Queue chunk size for demuxed output: complete frame payloads are
#: split losslessly into items of at most this size.
DOCKER_QUEUE_CHUNK_SIZE = 64 * 1024

_HEADER_STRUCT = struct.Struct(">BxxxL")


class DockerFrameError(ValueError):
    """Raised when Docker multiplex framing is invalid or oversized."""


class DockerFrameParser:
    """Incremental, bounded parser for Docker multiplexed exec output."""

    def __init__(self, max_frame_size: int = DOCKER_FRAME_MAX_SIZE) -> None:
        if max_frame_size <= 0:
            raise ValueError("max_frame_size must be positive")
        self._max_frame_size = max_frame_size
        self._buffer = bytearray()

    @property
    def buffered(self) -> int:
        """Return the number of currently buffered unparsed bytes."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        """Feed raw socket bytes; return complete ``(stream, payload)`` frames.

        ``stream`` is ``"stdout"`` or ``"stderr"``.  Raises
        :class:`DockerFrameError` on invalid headers, unknown stream ids,
        or oversized frames.
        """
        if data:
            self._buffer += data
        frames: list[tuple[str, bytes]] = []
        while True:
            if len(self._buffer) < DOCKER_FRAME_HEADER_LEN:
                return frames
            try:
                stream_id, length = _HEADER_STRUCT.unpack_from(self._buffer, 0)
            except struct.error as exc:
                raise DockerFrameError(
                    f"invalid docker frame header: {exc}"
                ) from exc
            if stream_id not in (DOCKER_STDOUT, DOCKER_STDERR):
                raise DockerFrameError(
                    f"invalid docker frame stream id: {stream_id}"
                )
            if length > self._max_frame_size:
                raise DockerFrameError(
                    f"docker frame too large: {length} bytes "
                    f"(max {self._max_frame_size})"
                )
            if len(self._buffer) - DOCKER_FRAME_HEADER_LEN < length:
                # Fragmented body: wait for more data.
                return frames
            del self._buffer[:DOCKER_FRAME_HEADER_LEN]
            payload = bytes(self._buffer[:length])
            del self._buffer[:length]
            stream = "stdout" if stream_id == DOCKER_STDOUT else "stderr"
            frames.append((stream, payload))

    def feed_eof(self) -> list[tuple[str, bytes]]:
        """Signal socket EOF; fail closed on a truncated trailing frame."""
        if self._buffer:
            raise DockerFrameError(
                f"truncated docker frame: {len(self._buffer)} trailing bytes"
            )
        return []
