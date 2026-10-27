"""Measure full working_diff RPCs with real Git in disposable repositories.

Run with ``runner/.venv/bin/python scripts/benchmark_git_untracked.py
--baseline-ref <revision>``. Baseline/current samples run in shuffled pairs.
The test runtime maps guest paths and executes Git locally. Optional per-call
latency models SSH overhead; it is not a measurement of QEMU or a network.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.util
import json
import math
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import uuid
from pathlib import Path
from typing import Any


async def measure(
    files: int, delay_ms: float, args: argparse.Namespace, baseline: type
) -> dict[str, Any]:
    """Time the public operation, including validation, status and all diffs."""
    from src import git as git_ops
    from src.models import WorkspaceInfo
    from src.services.git_service import GitService
    from tests.test_git_operations import FakeGitRuntime, _make_repo, _run_git

    class MeasuredRuntime(FakeGitRuntime):
        """Count active untracked diffs and model latency for every guest call."""

        active = 0
        peak = 0

        async def exec_command_wait(
            self,
            instance_id: str,
            command: list[str],
            workdir: str | None = None,
            env: dict[str, str] | None = None,
        ) -> tuple[int, str]:
            inner = git_ops.unwrap_git_exec_argv(command) or command
            untracked = inner[:3] == ["git", "diff", "--no-index"]
            if untracked:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000)
                # Use exactly one worker thread for each Git subprocess, just
                # as FakeGitRuntime does. Probe translation stays in the fake.
                return await super().exec_command_wait(
                    instance_id, command, workdir, env
                )
            finally:
                if untracked:
                    self.active -= 1

    with tempfile.TemporaryDirectory(prefix="oc-git-untracked-") as directory:
        repo = Path(directory) / "repo"
        await asyncio.to_thread(_make_repo, str(repo))
        (repo / "README.md").write_text("# changed tracked file\n")
        (repo / "staged.txt").write_text("staged content\n")
        await asyncio.to_thread(_run_git, ["git", "add", "staged.txt"], str(repo))
        for index in range(files):
            path = repo / f"new file {index:04d}.txt"
            if index % 10 == 9:
                path.write_bytes(bytes(range(256)) * 16)
            else:
                path.write_text(f"File {index}\n" + "new source line\n" * 256)
        runtimes = [MeasuredRuntime(directory), MeasuredRuntime(directory)]
        workspace_id = uuid.UUID(int=1)
        info = WorkspaceInfo(
            workspace_id=workspace_id, instance_id="bench", status="running"
        )
        services = [
            cls(lambda _: info, lambda _, runtime=runtime: runtime)
            for cls, runtime in zip((baseline, GitService), runtimes, strict=True)
        ]

        async def request(index: int) -> bytes:
            result = await services[index].execute_git_operation(
                workspace_id, "working_diff", "/workspace/repo", {}
            )
            if not result.get("ok"):
                raise RuntimeError(f"working_diff failed: {result}")
            if len(result["diff"]["unstaged"]) != files + 1:
                raise RuntimeError("Incomplete working diff")
            return json.dumps(result, sort_keys=True).encode()

        elapsed: list[list[float]] = [[], []]
        counts: list[set[int]] = [set(), set()]
        peaks: list[set[int]] = [set(), set()]
        digest = None
        order_generator = random.Random(20261027)
        for iteration in range(args.warmups + args.samples):
            order = [0, 1]
            order_generator.shuffle(order)
            for index in order:
                runtime = runtimes[index]
                runtime.calls.clear()
                runtime.peak = 0
                start = time.perf_counter()
                payload = await request(index)
                duration = (time.perf_counter() - start) * 1000
                current = hashlib.sha256(payload).hexdigest()
                if digest is not None and current != digest:
                    raise RuntimeError("Baseline/current responses differ")
                digest = current
                if iteration >= args.warmups:
                    elapsed[index].append(duration)
                    counts[index].add(len(runtime.calls))
                    peaks[index].add(runtime.peak)
        metrics = {}
        for index, label in enumerate(("baseline", "optimized")):
            runtimes[index].calls.clear()
            gc.collect()
            tracemalloc.start()
            try:
                await request(index)
                _, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            metrics[label] = {
                "p50_ms": round(statistics.median(elapsed[index]), 3),
                "p95_ms": round(
                    sorted(elapsed[index])[math.ceil(args.samples * 0.95) - 1], 3
                ),
                "runtime_calls": sorted(counts[index]),
                "peak_concurrent_untracked_diffs": sorted(peaks[index]),
                "peak_python_bytes_separate_probe": peak_bytes,
            }
        return {
            "untracked_files": files,
            "simulated_delay_ms_per_call": delay_ms,
            "response_bytes": len(payload),
            "response_sha256": digest,
            **metrics,
        }


def main() -> None:
    """Configure an isolated benchmark and write its JSON measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--files", type=int, nargs="+", default=[1, 20, 100])
    parser.add_argument("--delay-ms", type=float, nargs="+", default=[0, 5, 20])
    parser.add_argument("--samples", type=int, default=21)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.files) < 1 or args.samples < 2 or args.warmups < 0:
        parser.error("files/samples must be positive, samples >= 2, warmups >= 0")
    if any(not math.isfinite(delay) or delay < 0 for delay in args.delay_ms):
        parser.error("delay-ms must be finite and nonnegative")
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "runner"))
    source = subprocess.run(
        ["git", "show", f"{args.baseline_ref}:runner/src/services/git_service.py"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="oc-git-baseline-") as directory:
        source_file = Path(directory) / "baseline.py"
        source_file.write_text(source)
        spec = importlib.util.spec_from_file_location(
            "src.services._benchmark_baseline", source_file
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    baseline = module.GitService
    for key in list(os.environ):
        if key.startswith("GIT_"):
            del os.environ[key]
    os.environ.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
    )

    async def run() -> list[dict[str, Any]]:
        return [
            await measure(files, delay, args, baseline)
            for files in args.files
            for delay in args.delay_ms
        ]

    result = {
        "benchmark": "full runner working_diff with real local Git",
        "baseline_ref": args.baseline_ref,
        "samples": args.samples,
        "warmups": args.warmups,
        "notes": [
            "One staged and one tracked unstaged file; untracked 4 KiB text/binary.",
            "JSON serialization included; allocation probe measured separately.",
            "Baseline/current alternate in seeded shuffled pairs on one repository.",
            "Guest path/security probes are emulated; no QEMU/network claims.",
        ],
        "results": asyncio.run(run()),
    }
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
