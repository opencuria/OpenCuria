"""Measure complete file reads and parsing of actual guest responses.

Run with runner/.venv/bin/python scripts/benchmark_file_reads.py
--baseline-ref <revision> --output report.json.
Uses disposable files, the public FileManager and real local guest commands.
Replay excludes guest I/O to expose response-processing latency and allocations.
No VM, SSH, backend or provider latency is included.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gc
import hashlib
import json
import math
import platform
import random
import statistics
import subprocess
import sys
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


class ResponseRuntime:
    """Replay one real response through the public service, without guest I/O."""

    def __init__(self, response: tuple[int, str]) -> None:
        self.response = response

    async def exec_command_wait(
        self, instance_id: str, command: list[str], **kwargs: Any
    ) -> tuple[int, str]:
        """Keep containment and response parsing on their ordinary service path."""
        if command[:2] == ["realpath", "-m"]:
            return 0, command[2] + "\n"
        return self.response


def _summary(values: list[float]) -> dict[str, Any]:
    """Report milliseconds with nearest-rank p95 and raw observations."""
    return {
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3),
        "samples_ms": [round(value, 3) for value in values],
    }


def _baseline_type(revision: str) -> type[FileManager]:
    """Read the original service from Git without changing the checkout."""
    source = subprocess.check_output(
        ["git", "show", f"{revision}:runner/src/services/files.py"],
        cwd=REPO,
        text=True,
    )
    module = types.ModuleType("src.services._benchmark_read_baseline")
    module.__package__ = "src.services"
    exec(compile(source, f"{revision}:files.py", "exec"), module.__dict__)
    return module.FileManager


async def _case(
    baseline: type[FileManager],
    directory: Path,
    size: int,
    concurrency: int,
    samples: int,
) -> dict[str, Any]:
    """Validate complete reads and separately observe their event-loop stalls."""
    source = directory / f"payload-{size}.bin"
    content = (bytes(range(256)) * (size // 256 + 1))[:size]
    source.write_bytes(content)
    expected = base64.encodebytes(content).decode().strip()
    workspace_id = uuid.UUID(int=1)
    info = WorkspaceInfo(workspace_id, "local", "running")
    local = LocalFileRuntime()
    responses = []

    class RecordingRuntime:
        async def exec_command_wait(self, *args: Any, **kwargs: Any) -> tuple[int, str]:
            result = await local.exec_command_wait(*args, **kwargs)
            if kwargs["command"][:2] == ["sh", "-c"]:
                responses.append(result)
            return result

    def manager(cls: type[FileManager], runtime: Any) -> FileManager:
        return cls(get_cached=lambda _: info, get_runtime=lambda _: runtime)

    initial = await manager(baseline, RecordingRuntime()).read_file(
        workspace_id, str(source), max_size=max(1, size)
    )
    assert initial["content"] == expected
    assert initial["size"] == size and initial["truncated"] is False
    replay = ResponseRuntime(responses.pop())
    report: dict[str, Any] = {
        "bytes_per_file": size,
        "response_chars": len(replay.response[1]),
        "concurrency": concurrency,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    for mode, runtime in (("complete_read", local), ("response_replay", replay)):
        services = {
            "before": manager(baseline, runtime),
            "after": manager(FileManager, runtime),
        }

        async def run(service: FileManager) -> list[dict[str, Any]]:
            return await asyncio.gather(
                *(
                    service.read_file(workspace_id, str(source), max_size=max(1, size))
                    for _ in range(concurrency)
                )
            )

        async def heartbeat(gaps: list[float]) -> None:
            previous = time.perf_counter()
            while True:
                await asyncio.sleep(0.001)
                now = time.perf_counter()
                gaps.append(1000 * (now - previous))
                previous = now

        elapsed = {name: [] for name in services}
        stalls = {name: [] for name in services}
        generator = random.Random(0)
        for index in range(samples + 3):
            order = list(services)
            generator.shuffle(order)
            for name in order:
                local.calls.clear()
                local.output_bytes.clear()
                local.processes.clear()
                gaps: list[float] = []
                ticker = asyncio.create_task(heartbeat(gaps))
                await asyncio.sleep(0)
                try:
                    start = time.perf_counter()
                    results = await run(services[name])
                    duration = 1000 * (time.perf_counter() - start)
                    await asyncio.sleep(0.002)
                finally:
                    ticker.cancel()
                    await asyncio.gather(ticker, return_exceptions=True)
                assert results == [initial] * concurrency
                if mode == "complete_read":
                    assert len(local.calls) == 2 * concurrency
                if index >= 3:
                    elapsed[name].append(duration)
                    stalls[name].append(max(gaps))
                del results
        report[mode] = {}
        for name, service in services.items():
            gc.collect()
            tracemalloc.start()
            try:
                results = await run(service)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            assert results == [initial] * concurrency
            report[mode][name] = {
                **_summary(elapsed[name]),
                "max_heartbeat_gap": _summary(stalls[name]),
                "peak_python_bytes": peak,
            }
            del results
    return report


async def _run(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    baseline = _baseline_type(args.baseline_ref)
    return {
        "baseline_ref": args.baseline_ref,
        "python": platform.python_version(),
        "samples": args.samples,
        "method": (
            "Public FileManager, containment, real shell/stat/file/GNU base64; "
            "three warmups; seeded shuffled pairs; 1 ms heartbeat; exact payload "
            "comparisons against the baseline response. "
            "Tracing/validation excluded from timing; replay fixtures excluded "
            "from allocations. No VM/SSH/network latency."
        ),
        "cases": [
            await _case(baseline, directory, size, concurrency, args.samples)
            for size, concurrency in (
                (4096, 1),
                (5 * 1024 * 1024, 1),
                (20 * 1024 * 1024, 1),
                (5 * 1024 * 1024, 4),
            )
        ],
    }


def main() -> None:
    """Write measurements while keeping all fixtures disposable and local."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("samples must be >= 2")
    with tempfile.TemporaryDirectory(prefix="file-read-bench-", dir=REPO) as directory:
        report = asyncio.run(_run(args, Path(directory)))
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
