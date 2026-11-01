"""Measure the real Codex reader and dispatcher with bounded local byte streams.

Run with ``backend/.venv/bin/python scripts/benchmark_codex_framing.py``.
Use ``--baseline-ref REV`` to load only protocol.py from a prior revision.
No database, provider, runner or network is used. Fixtures are built before timing.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
import tracemalloc
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "backend/apps/harness/engines/codex/protocol.py"


class ByteStream:
    """Deliver prebuilt chunks with a scheduling point, then keep the pipe open."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = iter(chunks)
        self.drained = asyncio.Event()

    async def receive(self) -> bytes:
        """Signal when the reader has processed every chunk."""
        await asyncio.sleep(0)
        chunk = next(self.chunks, None)
        if chunk is not None:
            return chunk
        self.drained.set()
        await asyncio.Event().wait()
        return b""  # pragma: no cover


async def _consume(protocol: Any, chunks: list[bytes]) -> list[dict[str, Any]]:
    stream = ByteStream(chunks)
    rpc = protocol.CodexProtocol(stream, None)
    try:
        await stream.drained.wait()
        assert rpc.failure is None
        messages = []
        while not rpc.events.empty():
            message, _ = rpc.events.get_nowait()
            messages.append(message)
        return messages
    finally:
        rpc.reader.cancel()
        await asyncio.gather(rpc.reader, return_exceptions=True)


async def _measure(
    protocol: Any, messages: list[dict[str, Any]], chunk_bytes: int, samples: int
) -> dict[str, Any]:
    wire = b"".join(
        json.dumps(message, ensure_ascii=False).encode() + b"\n" for message in messages
    )
    chunks = [wire[i : i + chunk_bytes] for i in range(0, len(wire), chunk_bytes)]
    assert await _consume(protocol, chunks) == messages
    elapsed, cpu = [], []
    for _ in range(samples):
        start, cpu_start = time.perf_counter(), time.process_time()
        actual = await _consume(protocol, chunks)
        cpu.append((time.process_time() - cpu_start) * 1000)
        elapsed.append((time.perf_counter() - start) * 1000)
        assert actual == messages

    gc.collect()
    tracemalloc.start()
    actual = await _consume(protocol, chunks)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert actual == messages

    gaps = []

    async def heartbeat() -> None:
        previous = time.perf_counter()
        while True:
            await asyncio.sleep(0)
            now = time.perf_counter()
            gaps.append((now - previous) * 1000)
            previous = now

    task = asyncio.create_task(heartbeat())
    try:
        await asyncio.sleep(0)
        assert await _consume(protocol, chunks) == messages
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    return {
        "wire_bytes": len(wire),
        "chunk_bytes": chunk_bytes,
        "messages": len(messages),
        "p50_ms": round(statistics.median(elapsed), 3),
        "p95_ms": round(sorted(elapsed)[math.ceil(samples * 0.95) - 1], 3),
        "cpu_p50_ms": round(statistics.median(cpu), 3),
        "peak_python_allocated_bytes": peak,
        "max_heartbeat_gap_ms": round(max(gaps), 3),
        "result_sha256": hashlib.sha256(
            json.dumps(actual, sort_keys=True).encode()
        ).hexdigest(),
    }


async def _run(protocol: Any, samples: int) -> list[dict[str, Any]]:
    results = []
    for size in (64 * 1024, 1024 * 1024, 4 * 1024 * 1024, 12 * 1024 * 1024):
        for chunk_bytes in (4096, 65536):
            messages = [{"method": "item/completed", "params": {"text": "x" * size}}]
            results.append(
                {
                    "case": f"ascii-{size}",
                    **await _measure(protocol, messages, chunk_bytes, samples),
                }
            )
    messages = [{"method": "item/completed", "params": {"text": "é🙂" * 170000}}]
    results.append(
        {"case": "utf8", **await _measure(protocol, messages, 4093, samples)}
    )
    messages = [
        {
            "method": "item/agentMessage/delta",
            "params": {"index": i, "delta": "x" * 128},
        }
        for i in range(256)
    ]
    results.append(
        {
            "case": "coalesced-deltas",
            **await _measure(protocol, messages, 65536, samples),
        }
    )
    return results


def main() -> None:
    """Load the requested implementation and emit reproducible JSON metrics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--baseline-ref")
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("samples must be >= 2")
    source = (
        subprocess.check_output(
            ["git", "show", f"{args.baseline_ref}:{SOURCE}"], cwd=ROOT
        )
        if args.baseline_ref
        else (ROOT / SOURCE).read_bytes()
    )
    protocol = types.ModuleType("benchmarked_codex_protocol")
    # Execute only the explicitly requested local Git source, never wire input.
    exec(compile(source, SOURCE, "exec"), protocol.__dict__)  # noqa: S102
    result = {
        "source_ref": args.baseline_ref or "working-tree",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "python": sys.version,
        "samples": args.samples,
        "results": asyncio.run(_run(protocol, args.samples)),
        "notes": [
            "Actual reader, JSON parser, validation, queue accounting and dispatch.",
            "Prebuilt synthetic chunks; one scheduling point per receive.",
            "Allocation peak and heartbeat gap are separate untimed observations.",
            "No network/provider time; allocations are not process RSS.",
        ],
    }
    sys.stdout.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
