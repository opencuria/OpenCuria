"""Tests for the Docker multiplex framing parser."""

from __future__ import annotations

import struct
import unittest
from typing import ClassVar

from src.runtime.docker_frames import (
    DOCKER_FRAME_HEADER_LEN,
    DockerFrameError,
    DockerFrameParser,
)


def _frame(stream_id: int, payload: bytes) -> bytes:
    return struct.pack(">BxxxL", stream_id, len(payload)) + payload


class DockerFrameParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_stdout_frame(self) -> None:
        parser = DockerFrameParser()
        frames = parser.feed(_frame(1, b"hello"))
        self.assertEqual(frames, [("stdout", b"hello")])

    async def test_stderr_stream_label(self) -> None:
        parser = DockerFrameParser()
        frames = parser.feed(_frame(2, b"oops"))
        self.assertEqual(frames, [("stderr", b"oops")])

    async def test_fragmented_header_and_body(self) -> None:
        parser = DockerFrameParser()
        wire = _frame(1, b"fragmented-payload")
        # Byte-at-a-time delivery must reassemble exactly one frame.
        collected: list[tuple[str, bytes]] = []
        for i in range(len(wire)):
            collected.extend(parser.feed(wire[i : i + 1]))
        self.assertEqual(collected, [("stdout", b"fragmented-payload")])

    async def test_split_header_then_partial_body(self) -> None:
        parser = DockerFrameParser()
        wire = _frame(2, b"0123456789")
        self.assertEqual(parser.feed(wire[:5]), [])
        self.assertEqual(parser.feed(wire[5:8]), [])
        frames = parser.feed(wire[8:])
        self.assertEqual(frames, [("stderr", b"0123456789")])

    async def test_multiple_frames_in_one_recv(self) -> None:
        parser = DockerFrameParser()
        wire = _frame(1, b"one") + _frame(2, b"two") + _frame(1, b"three")
        frames = parser.feed(wire)
        self.assertEqual(
            frames,
            [("stdout", b"one"), ("stderr", b"two"), ("stdout", b"three")],
        )

    async def test_zero_length_frame(self) -> None:
        parser = DockerFrameParser()
        frames = parser.feed(_frame(1, b""))
        self.assertEqual(frames, [("stdout", b"")])

    async def test_unknown_stream_id_fails_closed(self) -> None:
        parser = DockerFrameParser()
        with self.assertRaises(DockerFrameError):
            parser.feed(_frame(0, b"x"))
        parser = DockerFrameParser()
        with self.assertRaises(DockerFrameError):
            parser.feed(_frame(3, b"x"))

    async def test_oversize_frame_fails_closed(self) -> None:
        parser = DockerFrameParser(max_frame_size=16)
        header = struct.pack(">BxxxL", 1, 17)
        with self.assertRaises(DockerFrameError):
            parser.feed(header)

    async def test_large_frame_accepted_and_lossless(self) -> None:
        """Frames >64KiB (up to 16MiB) parse completely, no truncation."""
        from src.runtime.docker_frames import DOCKER_FRAME_MAX_SIZE

        self.assertEqual(DOCKER_FRAME_MAX_SIZE, 16 * 1024 * 1024)
        parser = DockerFrameParser()
        payload = bytes(i % 251 for i in range(200_000))
        wire = _frame(1, payload)
        # Fragmented delivery (odd sizes) must reassemble exactly.
        collected: list[tuple[str, bytes]] = []
        step = 7001
        for i in range(0, len(wire), step):
            collected.extend(parser.feed(wire[i : i + step]))
        self.assertEqual(collected, [("stdout", payload)])

    async def test_frame_above_16mib_fails_closed(self) -> None:
        parser = DockerFrameParser()
        header = struct.pack(">BxxxL", 1, 16 * 1024 * 1024 + 1)
        with self.assertRaises(DockerFrameError):
            parser.feed(header)

    async def test_truncated_trailing_frame_on_eof(self) -> None:
        parser = DockerFrameParser()
        wire = _frame(1, b"abc") + struct.pack(">BxxxL", 2, 10) + b"short"
        frames = parser.feed(wire)
        self.assertEqual(frames, [("stdout", b"abc")])
        with self.assertRaises(DockerFrameError):
            parser.feed_eof()

    async def test_clean_eof_ok(self) -> None:
        parser = DockerFrameParser()
        parser.feed(_frame(1, b"done"))
        self.assertEqual(parser.feed_eof(), [])

    async def test_header_only_waits_for_body(self) -> None:
        parser = DockerFrameParser()
        header = struct.pack(">BxxxL", 1, 4)
        self.assertEqual(parser.feed(header), [])
        self.assertEqual(parser.buffered, DOCKER_FRAME_HEADER_LEN)
        self.assertEqual(parser.feed(b"ab"), [])
        self.assertEqual(parser.feed(b"cd"), [("stdout", b"abcd")])


if __name__ == "__main__":
    unittest.main()


class DockerPumpRechunkTests(unittest.TestCase):
    """Pump-level: >64KiB frames arrive losslessly as <=64KiB chunks."""

    def test_pump_splits_large_frame_losslessly(self) -> None:
        import queue as _queue
        import threading

        from src.runtime.docker_frames import DOCKER_QUEUE_CHUNK_SIZE
        from src.runtime.docker_runtime import DockerRuntime

        payload = bytes(i % 251 for i in range(200_000))
        wire = _frame(1, payload)
        chunks = [wire[:7001], wire[7001:50000], wire[50000:]]

        received: list[bytes] = []

        class FakeSock:
            def __init__(self, parts: list[bytes]) -> None:
                self._parts = list(parts)

            def recv(self, _n: int) -> bytes:
                if self._parts:
                    return self._parts.pop(0)
                return b""

        runtime = object.__new__(DockerRuntime)
        out_q: _queue.Queue = _queue.Queue(maxsize=64)
        err_q: _queue.Queue = _queue.Queue(maxsize=64)
        stop = threading.Event()
        runtime._drain_docker_stream_socket(FakeSock(chunks), out_q, err_q, stop)
        while True:
            item = out_q.get_nowait() if not out_q.empty() else None
            if item is None:
                break
            assert isinstance(item, bytes)
            self.assertLessEqual(len(item), DOCKER_QUEUE_CHUNK_SIZE)
            received.append(item)
        self.assertEqual(b"".join(received), payload)
        # stderr got EOF only.
        self.assertIsNone(err_q.get_nowait())

    def test_pump_queue_full_never_blocks_close(self) -> None:
        """A full queue + stop set must release the pump promptly."""
        import queue as _queue
        import threading
        import time

        from src.runtime.docker_runtime import DockerRuntime

        payload = b"y" * (64 * 1024)
        wire = _frame(1, payload) * 200

        class SlowSock:
            def recv(self, _n: int) -> bytes:
                time.sleep(0.001)
                return wire[:8192]

        runtime = object.__new__(DockerRuntime)
        out_q: _queue.Queue = _queue.Queue(maxsize=1)
        out_q.put(b"blocker")  # fill it; nobody drains
        err_q: _queue.Queue = _queue.Queue(maxsize=1)
        stop = threading.Event()
        thread = threading.Thread(
            target=runtime._drain_docker_stream_socket,
            args=(SlowSock(), out_q, err_q, stop),
            daemon=True,
        )
        thread.start()
        time.sleep(0.3)
        stop.set()  # close path
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "pump blocked despite stop")

    def test_pump_frame_error_flags_handle_no_payload_leak(self) -> None:
        """Corrupt framing sets frame_error metadata (exit unknown, not 0)."""
        import queue as _queue
        import threading

        from src.runtime.base import ProcessHandle
        from src.runtime.docker_runtime import DockerRuntime

        class BadSock:
            # Unknown stream id 0x00: parser.feed raises DockerFrameError
            # immediately (mid-stream framing error path).
            chunks: ClassVar[list[bytes]] = [b"\x00\x00\x00\x00\x00\x00\x05hello", b""]

            def __init__(self) -> None:
                self._parts = list(self.chunks)

            def recv(self, _n: int) -> bytes:
                if self._parts:
                    return self._parts.pop(0)
                return b""

        runtime = object.__new__(DockerRuntime)
        out_q: _queue.Queue = _queue.Queue(maxsize=64)
        err_q: _queue.Queue = _queue.Queue(maxsize=64)
        stop = threading.Event()
        handle = ProcessHandle(instance_id="i", handle=object())
        runtime._drain_docker_stream_socket(BadSock(), out_q, err_q, stop, handle)
        # Mid-stream framing corruption is flagged (exit unknown, not 0).
        self.assertIn(
            handle.metadata.get("frame_error"), ("frame_error", "truncated_frame")
        )
        # EOF sentinels still delivered on both queues.
        self.assertIsNone(out_q.get_nowait())
        self.assertIsNone(err_q.get_nowait())
