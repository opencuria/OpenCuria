"""Compare full Git RPC execution against a revision using real local Git.

Run from runner/: PYTHONPATH=. .venv/bin/python tests/benchmark_git_diff.py \
    --baseline-ref b60d90f --samples 40

The existing runtime test double maps guest paths to disposable repositories.
No SSH/network latency is simulated. Timings include auth/containment probes,
status, subprocesses and response parsing. Shared parsers used by the baseline
service are unchanged by this optimization.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib.util
import json
import math
import random
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import uuid
from pathlib import Path

from src.models import WorkspaceInfo
from src.services.git_service import GitService
from tests.test_git_operations import FakeGitRuntime, _make_repo, _run_git
from tests.test_git_operations import setup_module as isolate_git
from tests.test_git_operations import teardown_module as restore_git


def _git(repo: Path, *args: str) -> str:
    """Run a successful Git command against disposable benchmark data."""
    code, output = _run_git(["git", *args], str(repo))
    if code:
        raise RuntimeError(output)
    return output.strip()


async def _measure(
    baseline: type[GitService],
    samples: int,
    root: Path,
    files: int,
    lines: int,
    operation: str,
) -> dict:
    """Interleave both implementations on exactly the same unchanged repository."""
    repo = root / "repo"
    _make_repo(str(repo))
    for index in range(files):
        (repo / f"file-{index:03}.txt").write_text("before\n" * lines)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tracked files")
    commit = _git(repo, "rev-parse", "HEAD")
    for index in range(files):
        (repo / f"file-{index:03}.txt").write_text("staged\n" * lines)
    _git(repo, "add", ".")
    for index in range(files):
        (repo / f"file-{index:03}.txt").write_text("unstaged\n" * lines)

    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(
        workspace_id=workspace_id, instance_id="instance-1", status="running"
    )
    runtimes = [FakeGitRuntime(str(root)), FakeGitRuntime(str(root))]
    services = [
        cls(lambda _: info, lambda _, runtime=runtime: runtime)
        for cls, runtime in zip([baseline, GitService], runtimes, strict=True)
    ]
    args = {"commit": commit} if operation == "commit_details" else {}

    async def execute(index: int) -> dict:
        runtimes[index].calls.clear()
        result = await services[index].execute_git_operation(
            workspace_id, operation, "/workspace/repo", args
        )
        if not result["ok"]:
            raise RuntimeError(result)
        return result

    for _ in range(5):
        assert await execute(0) == await execute(1)
    elapsed: list[list[float]] = [[], []]
    random_order = random.Random(20261026)
    for _ in range(samples):
        order = [0, 1]
        random_order.shuffle(order)
        results = {}
        for index in order:
            started = time.perf_counter()
            results[index] = await execute(index)
            elapsed[index].append(1000 * (time.perf_counter() - started))
        assert results[0] == results[1]

    metrics = []
    for index in range(2):
        gc.collect()
        tracemalloc.start()
        await execute(index)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        ordered = sorted(elapsed[index])
        metrics.append(
            {
                "p50_ms": round(statistics.median(ordered), 3),
                "p95_ms": round(ordered[math.ceil(samples * 0.95) - 1], 3),
                "guest_commands": len(runtimes[index].calls),
                "peak_python_kib": round(peak / 1024, 1),
            }
        )
    return {
        "operation": operation,
        "files": files,
        "lines_per_file": lines,
        "samples": samples,
        "baseline": metrics[0],
        "optimized": metrics[1],
    }


async def main() -> None:
    """Load the baseline service without modifying either checkout or user data."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--samples", type=int, default=40)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    source = (
        await asyncio.to_thread(
            subprocess.run,
            ["git", "show", f"{args.baseline_ref}:runner/src/services/git_service.py"],
            capture_output=True,
            text=True,
            check=True,
        )
    ).stdout
    with tempfile.TemporaryDirectory(prefix="oc-git-perf-") as directory:
        root = Path(directory)
        baseline_file = root / "baseline.py"
        baseline_file.write_text(source)
        spec = importlib.util.spec_from_file_location(
            "src.services._benchmark_baseline", baseline_file
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        isolate_git()
        try:
            for index, (files, lines, operation) in enumerate(
                [
                    (1, 50, "working_diff"),
                    (100, 50, "working_diff"),
                    (10, 5000, "working_diff"),
                    (100, 50, "commit_details"),
                ]
            ):
                case = root / str(index)
                case.mkdir()
                result = await _measure(
                    module.GitService, args.samples, case, files, lines, operation
                )
                sys.stdout.write(json.dumps(result) + "\n")
                sys.stdout.flush()
        finally:
            restore_git()


if __name__ == "__main__":
    asyncio.run(main())
