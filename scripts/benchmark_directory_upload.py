"""Measure actual upload RPCs, local tar extraction and event-loop stalls.

Run with ``runner/.venv/bin/python scripts/benchmark_directory_upload.py``.
``--baseline-ref REV`` compares with that revision in alternating sample order.
``--file-bytes 1048576 10485760`` measures ordinary binary files instead.
No QEMU/SSH/network latency or frontend rendering is measured.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gc
import gzip
import io
import json
import math
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import tracemalloc
import types
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runner"))

from src import chunking  # noqa: E402
from src.chunking import CHUNK_B64_SIZE  # noqa: E402
from src.config import RunnerSettings  # noqa: E402
from src.interfaces.websocket import WebSocketInterface  # noqa: E402
from src.models import WorkspaceInfo  # noqa: E402
from src.service import WorkspaceService  # noqa: E402
from src.services.files import FileManager  # noqa: E402
from tests.directory_upload_runtime import LocalUploadRuntime  # noqa: E402


def _manager_type(revision: str | None) -> type[FileManager]:
    """Load baseline source without changing the worktree or dependencies."""
    if revision is None:
        return FileManager
    source = subprocess.check_output(
        ["git", "show", f"{revision}:runner/src/services/files.py"],
        cwd=REPO,
        text=True,
    )
    module = types.ModuleType("src.services._benchmark_baseline_files")
    module.__package__ = "src.services"
    exec(compile(source, f"{revision}:files.py", "exec"), module.__dict__)
    return module.FileManager


def _normalizer(revision: str | None) -> Callable[[str], str]:
    """Compare the transfer helper as well as the FileManager implementation."""
    if revision is None:
        return chunking.normalize_base64
    source = subprocess.check_output(
        ["git", "show", f"{revision}:runner/src/chunking.py"], cwd=REPO, text=True
    )
    module = types.ModuleType("src._benchmark_baseline_chunking")
    exec(compile(source, f"{revision}:chunking.py", "exec"), module.__dict__)
    return module.normalize_base64


def _payload(count: int) -> str:
    """Create deterministic compressed source files below the upload cap."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for index in range(count):
            member = tarfile.TarInfo(f"project/file-{index:05}.txt")
            content = f"source file {index}\n".encode() * 64
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))
    return base64.b64encode(gzip.compress(buffer.getvalue(), mtime=0)).decode()


def _percentiles(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3),
    }


async def _measure(
    args: argparse.Namespace, directory: Path, count: int
) -> dict[str, Any]:
    """Time full chunk-upload RPCs with a concurrent five-ms event-loop probe."""
    runtime = LocalUploadRuntime()
    settings = RunnerSettings(
        state_dir=str(directory / f"state-{count}"), _env_file=None
    )
    service = WorkspaceService(runtime, settings)
    workspace_id = uuid.UUID(int=1)
    service.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id, "local", "running"
    )
    revisions = {"current": None}
    if args.baseline_ref:
        revisions["baseline"] = args.baseline_ref
    managers = {}
    normalizers = {
        variant: _normalizer(revision) for variant, revision in revisions.items()
    }
    for variant, revision in revisions.items():
        manager = _manager_type(revision)(
            get_cached=service.registry.get_cached,
            get_runtime=service.registry.get_runtime,
        )
        manager.capture_fence = service.capture_fence
        managers[variant] = manager
    interface = WebSocketInterface(service, settings)
    emitted: list[dict] = []

    async def emit(event: str, payload: dict, **kwargs: Any) -> None:
        assert event == "files:upload_result"
        emitted.append(payload)

    interface._sio.emit = emit
    handlers = interface._sio.handlers["/"]
    destination = directory / f"upload-{count}"
    if args.file_bytes:
        content = base64.b64encode(
            (bytes(range(256)) * math.ceil(count / 256))[:count]
        ).decode()
    else:
        content = _payload(count)
    chunks = [
        content[offset : offset + CHUNK_B64_SIZE]
        for offset in range(0, len(content), CHUNK_B64_SIZE)
    ]
    context = {
        "workspace_id": str(workspace_id),
        "request_id": "benchmark",
        "path": str(destination),
        "total_chunks": len(chunks),
    }

    async def upload() -> None:
        emitted.clear()
        await handlers["files:upload_start"](
            {
                **context,
                "filename": "file.bin" if args.file_bytes else "project.tar.gz",
                "is_directory": not args.file_bytes,
            }
        )
        for index, chunk in enumerate(chunks):
            await handlers["files:upload_chunk"](
                {**context, "index": index, "content": chunk}
            )
            # Incoming Socket.IO chunks arrive as separate events. Give the
            # loop a turn instead of synchronously replaying the whole wire.
            await asyncio.sleep(0)
        await handlers["files:upload_finish"](context)
        assert emitted == [
            {
                **{k: context[k] for k in ("workspace_id", "request_id", "path")},
                "status": "success",
            }
        ]
        assert not interface._upload_transfers

    async def timed(variant: str) -> tuple[float, float, float]:
        service.files = managers[variant]
        chunking.normalize_base64 = normalizers[variant]
        ready, stop = asyncio.Event(), asyncio.Event()
        stalls = []

        async def probe() -> None:
            ready.set()
            while not stop.is_set():
                start = time.perf_counter()
                await asyncio.sleep(0.005)
                stalls.append(max(0.0, time.perf_counter() - start - 0.005) * 1000)

        task = asyncio.create_task(probe())
        await ready.wait()
        cpu = time.process_time()
        start = time.perf_counter()
        try:
            await upload()
            elapsed = (time.perf_counter() - start) * 1000
            cpu_ms = (time.process_time() - cpu) * 1000
        finally:
            stop.set()
            await task
        return elapsed, max(stalls), cpu_ms

    observations = {
        variant: {"elapsed": [], "stalls": [], "cpu": []} for variant in managers
    }
    digests: set[str] = set()
    for iteration in range(args.warmups + args.samples):
        order = list(managers)
        if iteration % 2:
            order.reverse()
        for variant in order:
            duration, stall, cpu_ms = await timed(variant)
            digests.add(runtime.archive_sha256)
            if iteration >= args.warmups:
                observations[variant]["elapsed"].append(duration)
                observations[variant]["stalls"].append(stall)
                observations[variant]["cpu"].append(cpu_ms)
    assert len(digests) == 1
    if args.file_bytes:
        path = destination / "file.bin"
        assert path.read_bytes() == base64.b64decode(content)
        assert path.stat().st_mode & 0o777 == 0o644
    else:
        assert len(list((destination / "project").iterdir())) == count
        for index in range(count):
            assert (destination / "project" / f"file-{index:05}.txt").read_bytes() == (
                f"source file {index}\n".encode() * 64
            )
    variants = {}
    for variant, observation in observations.items():
        service.files = managers[variant]
        chunking.normalize_base64 = normalizers[variant]
        gc.collect()
        tracemalloc.start()
        await upload()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        variants[variant] = {
            "upload_latency": _percentiles(observation["elapsed"]),
            "worst_event_loop_stall_per_upload": _percentiles(observation["stalls"]),
            "runner_cpu": _percentiles(observation["cpu"]),
            "peak_python_allocated_bytes": peak,
        }
    chunking.normalize_base64 = normalizers["current"]
    input_bytes = len(base64.b64decode(content))
    return {
        **(
            {"file_bytes": count}
            if args.file_bytes
            else {"files": count, "compressed_bytes": input_bytes}
        ),
        "input_bytes": input_bytes,
        "transfer_chunks": len(chunks),
        "tar_bytes": runtime.archive_bytes,
        "variants": variants,
        "archive_sha256": digests.pop(),
    }


async def _run(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    original = chunking.normalize_base64
    try:
        results = [
            await _measure(args, directory, count)
            for count in args.file_bytes or args.files
        ]
    finally:
        chunking.normalize_base64 = original
    return {
        "baseline_ref": args.baseline_ref,
        "samples": args.samples,
        "warmups": args.warmups,
        "results": results,
        "notes": [
            "Real chunk-upload Socket.IO handlers, FileManager and capture fence.",
            "Real local containment/mkdir and tar extraction; no QEMU/SSH/network.",
            "Five-ms periodic probe; report the worst excess delay per upload.",
            "CPU is runner process CPU including threads, excluding local tar child.",
            "Separate tracemalloc sample; Python allocations, not total RSS.",
            "Every extracted file is verified outside the timing interval.",
            "Baseline/current order alternates; fixtures and runtime are shared.",
            "Chunk handlers run in separate event-loop turns, without network delay.",
        ],
    }


def main() -> None:
    """Run disposable fixtures and emit JSON for baseline/current comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--files", type=int, nargs="+", default=[200, 5000])
    parser.add_argument("--file-bytes", type=int, nargs="+")
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=3)
    args = parser.parse_args()
    if min(args.file_bytes or args.files) < 1 or args.samples < 2 or args.warmups < 0:
        parser.error("files/bytes >= 1, samples >= 2 and warmups >= 0 required")
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
    with tempfile.TemporaryDirectory(prefix="upload-bench-", dir=REPO) as directory:
        result = asyncio.run(_run(args, Path(directory)))
    sys.stdout.write(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
