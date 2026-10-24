"""Measure process-list RPCs and heartbeats against real local process probes.

Run with ``runner/.venv/bin/python scripts/benchmark_background_status.py``.
Fixtures and runner state live in a temporary directory. Shell commands run
locally instead of inside Docker/QEMU; optional per-call delay models transport
overhead and is reported separately from the undelayed local measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import math
import statistics
import sys
import tempfile
import time
import tracemalloc
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

from src.config import RunnerSettings  # noqa: E402
from src.interfaces.websocket import WebSocketInterface  # noqa: E402
from src.models import WorkspaceInfo  # noqa: E402
from src.service import WorkspaceService  # noqa: E402
from src.services.sessions.background import BackgroundProcess  # noqa: E402
from tests.background_status_runtime import LocalRuntime  # noqa: E402


async def _measure(
    request: Callable[[], Awaitable[list[dict[str, Any]]]],
    runtime: LocalRuntime,
    samples: int,
) -> dict[str, Any]:
    """Measure warmed latency separately from runtime calls and allocations."""
    expected = await request()
    elapsed = []
    for _ in range(samples):
        started = time.perf_counter()
        actual = await request()
        elapsed.append((time.perf_counter() - started) * 1000)
        if actual != expected:
            raise RuntimeError("Process status changed during measurement")
    runtime.calls = 0
    await request()
    calls = runtime.calls
    gc.collect()
    tracemalloc.start()
    await request()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # PIDs and temporary paths differ between runs; compare stable status fields.
    normalized = [
        {key: row[key] for key in ("process_id", "status", "exit_code")}
        for row in expected
    ]
    return {
        "p50_ms": round(statistics.median(elapsed), 3),
        "p95_ms": round(sorted(elapsed)[math.ceil(samples * 0.95) - 1], 3),
        "runtime_calls": calls,
        "peak_python_allocated_bytes": peak,
        "status_sha256": hashlib.sha256(
            json.dumps(normalized, sort_keys=True).encode()
        ).hexdigest(),
    }


async def _run(args: argparse.Namespace, directory: str) -> dict[str, Any]:
    """Exercise the real Socket.IO handler and service heartbeat projection."""
    runtime = LocalRuntime(directory)
    settings = RunnerSettings(state_dir=directory, _env_file=None)
    service = WorkspaceService(runtime, settings)
    interface = WebSocketInterface(service, settings)
    workspace_id = uuid.UUID(int=1)
    service.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id, "synthetic-instance", "running"
    )
    emitted: list[dict[str, Any]] = []

    async def emit(event: str, payload: dict, **kwargs: Any) -> None:
        emitted.append(json.loads(json.dumps(payload)))

    interface._sio.emit = emit

    async def rpc() -> list[dict[str, Any]]:
        emitted.clear()
        await interface._sio.handlers["/"]["harness:process_list"](
            {"request_id": "benchmark", "workspace_id": str(workspace_id)}
        )
        if len(emitted) != 1 or emitted[0].get("error"):
            raise RuntimeError("Process-list handler failed")
        return emitted[0]["processes"]

    async def heartbeat() -> list[dict[str, Any]]:
        payload = await service.get_workspace_heartbeat_statuses()
        return json.loads(json.dumps(payload[0]["processes"]))

    sleepers: list[asyncio.subprocess.Process] = []
    results = []
    try:
        for _ in range(max(args.processes)):
            sleepers.append(await asyncio.create_subprocess_exec("sleep", "600"))
        exited = await asyncio.create_subprocess_exec("true")
        await exited.wait()
        for count in args.processes:
            for mixed in (False, True):
                entries = {}
                for index in range(count):
                    stopped = mixed and index % 2 == 1
                    process_id = f"process-{index:03d}"
                    exit_path = Path(directory) / f"{process_id}.exit"
                    exit_path.write_text("3\n", encoding="utf-8")
                    entries[process_id] = BackgroundProcess(
                        process_id=process_id,
                        workspace_id=workspace_id,
                        pid=exited.pid if stopped else sleepers[index].pid,
                        command="synthetic job",
                        workdir="/workspace",
                        log_path=str(Path(directory) / f"{process_id}.log"),
                        exit_path=str(exit_path),
                    )
                service.background._background_processes[workspace_id] = entries
                for delay_ms in args.delay_ms:
                    runtime.delay_s = delay_ms / 1000
                    results.append(
                        {
                            "processes": count,
                            "workload": "mixed" if mixed else "running",
                            "simulated_delay_ms": delay_ms,
                            "rpc": await _measure(rpc, runtime, args.samples),
                            "heartbeat": await _measure(
                                heartbeat, runtime, args.samples
                            ),
                        }
                    )
    finally:
        for process in sleepers:
            if process.returncode is None:
                process.terminate()
        await asyncio.gather(*(process.wait() for process in sleepers))
    return {
        "benchmark": "real local process probes; real runner RPC and heartbeat",
        "samples_per_path": args.samples,
        "results": results,
        "notes": [
            "No Docker, QEMU, live server or network transport in this benchmark.",
            "Nonzero delay is simulated per runtime call, not measured SSH latency.",
            "Runtime calls and tracemalloc peaks measured separately from latency.",
            "Allocation peaks are Python allocations, not guest memory or RSS.",
        ],
    }


def main() -> None:
    """Run disposable fixtures and write a machine-readable result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processes", type=int, nargs="+", default=[1, 10, 50])
    parser.add_argument("--delay-ms", type=float, nargs="+", default=[0, 5])
    parser.add_argument("--samples", type=int, default=15)
    args = parser.parse_args()
    if min(args.processes) < 1 or min(args.delay_ms) < 0 or args.samples < 2:
        parser.error("processes >= 1, delay-ms >= 0, samples >= 2 required")
    with tempfile.TemporaryDirectory(prefix="opencuria-process-bench-") as directory:
        report = asyncio.run(_run(args, directory))
    sys.stdout.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
