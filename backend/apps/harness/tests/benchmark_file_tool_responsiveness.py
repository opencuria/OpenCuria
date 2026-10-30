"""Measure full file tools and concurrent loop responsiveness on local files.

From backend/ (opt-in, no VM/provider/database writes):
    .venv/bin/pytest -q -s \
        apps/harness/tests/benchmark_file_tool_responsiveness.py

Use FILE_TOOL_BASELINE_REF to compare another revision. Both implementations
run in alternating order against the same disposable files and accessor. The
1 ms observer reports its worst scheduling gap per invocation. This isolates
backend processing; it does not estimate SSH or provider latency.
Set FILE_TOOL_CONCURRENCY=4 to measure four simultaneous tools on distinct paths.
"""

from __future__ import annotations

import asyncio
import gc
import math
import os
import statistics
import subprocess
import time
import tracemalloc
import types
from pathlib import Path
from typing import Any

import pytest
import structlog

from apps.harness.access.file_types import FileContent
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import files
from apps.harness.tools.base import ToolContext

log = structlog.get_logger(__name__)


class LocalFileAccessor(FakeAccessor):
    """Use real async file I/O, without introducing VM/SSH timing noise."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    async def read_file(self, path: str, max_size: int | None = None) -> FileContent:
        """Return the complete disposable file, as a runner read would."""
        content = await asyncio.to_thread(self.path.read_bytes)
        return FileContent(content=content, size=len(content))

    async def write_file(self, path: str, content: bytes) -> None:
        """Complete the mutation before the tool builds its patch metadata."""
        await asyncio.to_thread(self.path.write_bytes, content)


def _baseline(ref: str) -> types.ModuleType:
    repo = Path(__file__).resolve().parents[4]
    source = subprocess.run(
        ["git", "show", f"{ref}:backend/apps/harness/tools/files.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    module = types.ModuleType("apps.harness.tools._file_tool_baseline")
    module.__package__ = "apps.harness.tools"
    exec(compile(source, "baseline_files.py", "exec"), module.__dict__)
    return module


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50": round(statistics.median(samples), 3),
        "p95": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
    }


@pytest.mark.parametrize("lines", [100, 10_000, 100_000])
@pytest.mark.parametrize("tool_name", ["WriteTool", "EditTool"])
async def test_file_tool_benchmark(tmp_path: Path, lines: int, tool_name: str) -> None:
    """Compare latency, loop gaps, exact results and peak Python allocations."""
    original = "".join(
        f'entry_{index:06d} = "value_{index:06d}"\n' for index in range(lines)
    ).encode()
    old_string = f"value_{lines // 2:06d}"
    updated = original.replace(old_string.encode(), b"updated")
    concurrency = max(1, int(os.getenv("FILE_TOOL_CONCURRENCY", "1")))
    paths = [tmp_path / f"large-{index}.py" for index in range(concurrency)]
    contexts = [
        ToolContext("benchmark", "benchmark", LocalFileAccessor(path)) for path in paths
    ]
    arguments: list[dict[str, Any]] = []
    for path in paths:
        args: dict[str, Any] = {"path": f"/workspace/{path.name}"}
        if tool_name == "WriteTool":
            args["content"] = updated.decode()
        else:
            args.update(old_string=old_string, new_string="updated")
        arguments.append(args)
    modules = [_baseline(os.getenv("FILE_TOOL_BASELINE_REF", "28bed78")), files]
    tools = [getattr(module, tool_name)() for module in modules]

    async def execute(index: int) -> tuple[Any, float, float]:
        for path in paths:
            await asyncio.to_thread(path.write_bytes, original)
        gaps: list[float] = []
        stop = False

        async def observe() -> None:
            previous = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.001)
                now = time.perf_counter()
                gaps.append(1000 * (now - previous))
                previous = now

        observer = asyncio.create_task(observe())
        await asyncio.sleep(0)
        try:
            start = time.perf_counter()
            result = await asyncio.gather(
                *(
                    tools[index].execute(args, ctx)
                    for args, ctx in zip(arguments, contexts)
                )
            )
            elapsed = 1000 * (time.perf_counter() - start)
        finally:
            stop = True
            await observer
        assert all(path.read_bytes() == updated for path in paths)
        return result, elapsed, max(gaps)

    expected, _, _ = await execute(0)
    assert (await execute(1))[0] == expected
    samples = 20
    durations: list[list[float]] = [[], []]
    loop_gaps: list[list[float]] = [[], []]
    for iteration in range(samples):
        for index in [0, 1] if iteration % 2 == 0 else [1, 0]:
            result, elapsed, gap = await execute(index)
            assert result == expected
            durations[index].append(elapsed)
            loop_gaps[index].append(gap)

    metrics = []
    for index in range(2):
        gc.collect()
        tracemalloc.start()
        try:
            assert (await execute(index))[0] == expected
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        metrics.append(
            {
                "version": "baseline" if index == 0 else "current",
                "tool_ms": _percentiles(durations[index]),
                "max_observer_gap_ms": _percentiles(loop_gaps[index]),
                "peak_python_mib": round(peak / 1024**2, 3),
            }
        )
    log.info(
        "file_tool_benchmark",
        tool=tool_name,
        lines=lines,
        file_bytes=len(original),
        concurrency=concurrency,
        samples=samples,
        metrics=metrics,
    )
