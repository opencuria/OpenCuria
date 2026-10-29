"""Measure harness exec decoding and complete commands using real local shells.

Run with runner/.venv/bin/python scripts/benchmark_harness_exec.py
--baseline-ref <revision> --output report.json. Reads the baseline from Git
without changing the checkout. No VM, SSH, backend or provider latency is included.
"""

from __future__ import annotations

import argparse
import asyncio
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
from src.services.harness_exec import HarnessExecService  # noqa: E402
from tests.directory_download_runtime import LocalFileRuntime  # noqa: E402


def _baseline_type(revision: str) -> type[HarnessExecService]:
    """Load the actual baseline service, retaining its relative imports."""
    source = subprocess.check_output(
        ["git", "show", f"{revision}:runner/src/services/harness_exec.py"],
        cwd=REPO,
        text=True,
    )
    module = types.ModuleType("src.services._benchmark_exec_baseline")
    module.__package__ = "src.services"
    exec(compile(source, f"{revision}:harness_exec.py", "exec"), module.__dict__)
    return module.HarnessExecService


def _summary(samples: list[float]) -> dict[str, Any]:
    """Report milliseconds with nearest-rank p95 and raw samples."""
    return {
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3),
        "samples_ms": [round(sample, 3) for sample in samples],
    }


async def _case(
    baseline: type[HarnessExecService],
    directory: Path,
    size: int,
    concurrency: int,
    samples: int,
) -> dict[str, Any]:
    """Use real wrapper output and compare identical service/decoder workloads."""
    out_line = b"Compiled src/module.ts successfully.\n"
    err_line = b"Warning: source map unavailable.\n"
    stdout = (out_line * (size // len(out_line) + 1))[:size]
    stderr = (err_line * (size // len(err_line) + 1))[:size]
    (directory / "out").write_bytes(stdout)
    (directory / "err").write_bytes(stderr)
    expected = (7, stdout.decode(), stderr.decode())
    workspace_id = uuid.UUID(int=1)
    info = WorkspaceInfo(workspace_id, "local", "running")
    runtimes = {name: LocalFileRuntime() for name in ("before", "after")}
    services = {
        name: cls(
            get_cached=lambda _: info,
            get_runtime=lambda _, runtime=runtimes[name]: runtime,
            credential_env_file=str(directory / "absent-credentials"),
        )
        for name, cls in (("before", baseline), ("after", HarnessExecService))
    }
    command = ["sh", "-c", "cat out; cat err >&2; exit 7"]
    # Capture one actual shell-produced frame; fixture creation stays untimed.
    capture = runtimes["before"]
    execute = capture.exec_command_wait
    frames = []

    async def record(*args: Any, **kwargs: Any) -> tuple[int, str]:
        result = await execute(*args, **kwargs)
        frames.append(result[1])
        return result

    capture.exec_command_wait = record
    assert (
        await services["before"].exec_harness_command(
            workspace_id, command, workdir=str(directory)
        )
        == expected
    )
    capture.exec_command_wait = execute
    frame = frames.pop()
    report: dict[str, Any] = {
        "bytes_per_stream": size,
        "frame_bytes": len(frame),
        "concurrency": concurrency,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    }
    rng = random.Random(20261029)
    for mode in ("decoder", "complete_command"):
        elapsed: dict[str, list[float]] = {name: [] for name in services}
        gaps: dict[str, list[float]] = {name: [] for name in services}

        async def run(name: str, measurement: str = mode) -> list[tuple[int, str, str]]:
            if measurement == "decoder":
                return [
                    (7, *services[name]._parse_harness_exec_output(frame))
                    for _ in range(concurrency)
                ]
            return await asyncio.gather(
                *(
                    services[name].exec_harness_command(
                        workspace_id, command, workdir=str(directory)
                    )
                    for _ in range(concurrency)
                )
            )

        async def heartbeat(observations: list[float]) -> None:
            previous = time.perf_counter()
            while True:
                await asyncio.sleep(0.001)
                now = time.perf_counter()
                observations.append(1000 * (now - previous))
                previous = now

        for iteration in range(samples + 3):
            order = list(services)
            rng.shuffle(order)
            for name in order:
                runtime = runtimes[name]
                runtime.calls.clear()
                runtime.processes.clear()
                runtime.output_bytes.clear()
                observations: list[float] = []
                ticker = asyncio.create_task(heartbeat(observations))
                await asyncio.sleep(0)  # Start the heartbeat before CPU work.
                try:
                    start = time.perf_counter()
                    results = await run(name)
                    duration = 1000 * (time.perf_counter() - start)
                    await asyncio.sleep(0.002)  # Observe the final decode gap.
                finally:
                    ticker.cancel()
                    await asyncio.gather(ticker, return_exceptions=True)
                assert results == [expected] * concurrency
                assert len(runtime.calls) == (
                    concurrency if mode == "complete_command" else 0
                )
                if iteration >= 3:
                    elapsed[name].append(duration)
                    gaps[name].append(max(observations))
                del results
        metrics = {}
        for name in services:
            gc.collect()
            tracemalloc.start()
            try:
                results = await run(name)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            assert results == [expected] * concurrency
            metrics[name] = {
                **_summary(elapsed[name]),
                "max_heartbeat_gap": _summary(gaps[name]),
                "peak_python_bytes": peak,
            }
            del results
        report[mode] = metrics
    return report


async def _run(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    baseline = _baseline_type(args.baseline_ref)
    cases = []
    for size, concurrency in (
        (4096, 1),
        (256 * 1024, 1),
        (5 * 1024 * 1024, 1),
        (256 * 1024, 4),
    ):
        cases.append(await _case(baseline, directory, size, concurrency, args.samples))
    return {
        "baseline_ref": args.baseline_ref,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "method": (
            "Real shell/mktemp/cat/GNU base64 and public HarnessExecService; "
            "decoder uses actual wrapper frame. Three warmups, shuffled paired "
            "samples; timings exclude validation/tracing; 1 ms heartbeat; "
            "separate tracemalloc probes exclude fixtures. No QEMU/SSH latency."
        ),
        "cases": cases,
    }


def main() -> None:
    """Run disposable integration fixtures and emit the reproducible report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("samples must be >= 2")
    with tempfile.TemporaryDirectory(prefix="exec-bench-", dir=REPO) as directory:
        report = asyncio.run(_run(args, Path(directory)))
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
