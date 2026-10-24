"""Measure complete directory downloads using real local tar/base64 commands.

Run with ``runner/.venv/bin/python scripts/benchmark_directory_download.py``.
Use ``--revision REV`` to load the unmodified FileManager from a Git revision.
Fixtures live under /workspace to exercise the real containment checks. No
QEMU, Socket.IO or network transport is included in these measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gc
import hashlib
import io
import json
import math
import random
import resource
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import tracemalloc
import types
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runner"))

from src.models import WorkspaceInfo  # noqa: E402
from src.services.files import FileManager  # noqa: E402
from tests.directory_download_runtime import LocalFileRuntime  # noqa: E402


def _manager_type(revision: str | None) -> type[FileManager]:
    """Load baseline code without modifying the checkout or its dependencies."""
    if not revision:
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


def _digest(payload: dict[str, Any]) -> str:
    """Validate actual archive bytes and hash ordered member names/content."""
    data = base64.b64decode(payload["content"])
    assert len(data) == payload["size"]
    assert payload["is_archive"]
    digest = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            digest.update(member.name.encode())
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                digest.update(stream.read())
    return digest.hexdigest()


async def _measure(
    manager: FileManager,
    runtime: LocalFileRuntime,
    workspace_id: uuid.UUID,
    source: Path,
    samples: int,
) -> dict[str, Any]:
    """Measure latency, child CPU and allocations separately from validation."""
    expected = await manager.download_file(workspace_id, str(source))
    expected_digest = _digest(expected)
    archive_size = expected["size"]
    del expected
    elapsed, cpu = [], []
    for _ in range(samples):
        before_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.perf_counter()
        payload = await manager.download_file(workspace_id, str(source))
        elapsed.append((time.perf_counter() - start) * 1000)
        after_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu.append(
            (
                after_cpu.ru_utime
                + after_cpu.ru_stime
                - before_cpu.ru_utime
                - before_cpu.ru_stime
            )
            * 1000
        )
        assert _digest(payload) == expected_digest
        del payload
    runtime.calls.clear()
    await manager.download_file(workspace_id, str(source))
    calls = len(runtime.calls)
    gc.collect()
    tracemalloc.start()
    await manager.download_file(workspace_id, str(source))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "p50_ms": round(statistics.median(elapsed), 3),
        "p95_ms": round(sorted(elapsed)[math.ceil(samples * 0.95) - 1], 3),
        "child_cpu_p50_ms": round(statistics.median(cpu), 3),
        "runtime_calls": calls,
        "archive_bytes": archive_size,
        "peak_python_allocated_bytes": peak,
        "archive_contents_sha256": expected_digest,
    }


async def _run(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    """Create deterministic source/asset fixtures and exercise FileManager."""
    source = directory / "project"
    source.mkdir()
    generator = random.Random(0)
    for index in range(args.pairs):
        (source / f"asset-{index:03}.bin").write_bytes(generator.randbytes(128 * 1024))
        (source / f"source-{index:03}.txt").write_text(
            'export const value = "sample";\n' * 1024, encoding="utf-8"
        )
    runtime = LocalFileRuntime()
    workspace_id = uuid.UUID(int=1)
    info = WorkspaceInfo(workspace_id, "local", "running")
    manager = _manager_type(args.revision)(
        get_cached=lambda _: info, get_runtime=lambda _: runtime
    )
    return {
        "revision": args.revision or "working-tree",
        "samples": args.samples,
        "files": args.pairs * 2,
        "input_bytes": sum(path.stat().st_size for path in source.iterdir()),
        "download": await _measure(
            manager, runtime, workspace_id, source, args.samples
        ),
        "notes": [
            "Real FileManager, containment, tar/gzip, base64 and result parsing.",
            "Local subprocess runtime; no QEMU or network latency.",
            "Child CPU includes waited-for tar/gzip/base64 and guest helper.",
            "Tracemalloc is a separate run: runner allocations, not guest RSS.",
        ],
    }


def main() -> None:
    """Run disposable fixtures and emit machine-readable results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision")
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--pairs", type=int, default=128)
    args = parser.parse_args()
    if args.samples < 2 or args.pairs < 1:
        parser.error("samples must be >= 2 and pairs must be >= 1")
    with tempfile.TemporaryDirectory(prefix="directory-bench-", dir=REPO) as path:
        report = asyncio.run(_run(args, Path(path)))
    sys.stdout.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
