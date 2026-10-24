#!/usr/bin/env python3
"""Benchmark runner ``list_repos`` against disposable local Git fixtures.

Run from the repository root (or runner venv) with
``python scripts/benchmark_git_discovery.py``. The selected checkout supplies
both the real runner service and its existing ``FakeGitRuntime`` test helper.
All Git work is local; a temporary HOME and disabled system/global Git config
keep user configuration out of the measurement. Delay is simulated per
runtime call, not a Docker/SSH measurement.
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
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


def _load_test_helpers(repo: Path) -> Any:
    """Load FakeGitRuntime from the selected checkout's Git tests."""
    runner = repo / "runner"
    test_path = runner / "tests" / "test_git_operations.py"
    if not test_path.is_file():
        raise FileNotFoundError(f"Git test helpers not found: {test_path}")
    sys.path.insert(0, str(runner))
    spec = importlib.util.spec_from_file_location("oc_git_bench_tests", test_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Git test helpers: {test_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _percentile(values: list[float], fraction: float) -> float:
    """Return a nearest-rank percentile from nonempty measurements."""
    return sorted(values)[max(0, math.ceil(fraction * len(values)) - 1)]


def _command_categories(calls: list[tuple[Any, ...]], tests: Any) -> dict[str, int]:
    """Count coarse command categories without storing command text."""
    counts: dict[str, int] = {}
    for call in calls:
        argv = tests._inner_argv(call)
        if argv[:2] == ["sh", "-c"] or argv[:1] == ["test"]:
            category = "auth/probe-shell"
        elif argv[:2] == ["realpath", "-m"]:
            category = "realpath containment"
        elif argv[:2] == ["find", "/workspace"]:
            category = "find discovery"
        elif argv[:2] == ["git", "rev-parse"]:
            if "--show-toplevel" in argv:
                category = "git root verification"
            elif any(
                flag in argv
                for flag in ("--absolute-git-dir", "--git-common-dir", "--git-dir")
            ):
                category = "git metadata verification"
            else:
                category = "git list-entry HEAD/branch"
        else:
            category = "other"
        counts[category] = counts.get(category, 0) + 1
    return counts


def _make_fixtures(tests: Any, root: Path, count: int) -> list[str]:
    """Create real repositories using deterministic dates and local identities."""
    heads: list[str] = []
    dates = ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE")
    previous = {key: os.environ.get(key) for key in dates}
    os.environ.update({key: "2000-01-01T00:00:00+00:00" for key in dates})
    try:
        for index in range(count):
            path = root / f"repo-{index:02d}"
            tests._make_repo(str(path), branch="main")
            code, output = tests._run_git(
                ["git", "-C", str(path), "rev-parse", "HEAD"], str(root)
            )
            if code:
                raise RuntimeError(f"Could not read fixture {index} HEAD: {output}")
            heads.append(output.strip())
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return heads


class _DelayedFakeRuntime:
    """Add simulated per-call delay around the test FakeGitRuntime."""

    def __init__(self, tests: Any, root: str, delay_s: float) -> None:
        self._runtime = tests.FakeGitRuntime(root)
        self.delay_s = delay_s

    @property
    def calls(self) -> list[tuple[Any, ...]]:
        """Expose recorded runtime calls for separate command accounting."""
        return self._runtime.calls

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Sleep for the configured delay, then delegate to the test fake."""
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return await self._runtime.exec_command_wait(instance_id, command, workdir, env)


async def _measure_case(
    tests: Any,
    repo_count: int,
    delay_ms: float,
    samples: int,
    warmups: int,
    trace_allocations: bool,
) -> dict[str, Any]:
    """Measure one repo-count/delay case with the selected checkout's service."""
    from src.config import RunnerSettings
    from src.models import WorkspaceInfo
    from src.service import WorkspaceService

    with tempfile.TemporaryDirectory(prefix="oc-git-discovery-") as root:
        fixture_heads = _make_fixtures(tests, Path(root), repo_count)
        with tempfile.TemporaryDirectory(prefix="oc-git-discovery-state-") as state:
            runtime = _DelayedFakeRuntime(tests, root, delay_ms / 1000)
            service = WorkspaceService(
                runtimes={"docker": runtime}, settings=RunnerSettings(state_dir=state)
            )
            workspace_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"opencuria-git-discovery/{repo_count}"
            )
            service.registry._cache[workspace_id] = WorkspaceInfo(
                workspace_id=workspace_id,
                instance_id="instance-1",
                status="running",
                runtime_type="docker",
            )

            elapsed: list[float] = []
            call_counts: list[int] = []
            expected_categories: dict[str, int] | None = None
            expected_digest: str | None = None
            for iteration in range(warmups + samples):
                runtime.calls.clear()
                started = time.perf_counter()
                result = await service.git.execute_git_operation(
                    workspace_id, "list_repos", None, {}
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                if not result.get("ok") or len(result.get("repos", [])) != repo_count:
                    raise RuntimeError(
                        f"list_repos failed for {repo_count}: {result!r}"
                    )
                canonical = json.dumps(
                    result, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                )
                digest = hashlib.sha256(canonical.encode()).hexdigest()
                if expected_digest is not None and digest != expected_digest:
                    raise RuntimeError(
                        "list_repos response changed between repetitions"
                    )
                expected_digest = digest
                if iteration >= warmups:
                    categories = _command_categories(runtime.calls, tests)
                    if (
                        expected_categories is not None
                        and categories != expected_categories
                    ):
                        raise RuntimeError(
                            "runtime command mix changed between samples"
                        )
                    expected_categories = categories
                    elapsed.append(elapsed_ms)
                    call_counts.append(len(runtime.calls))

            peak_bytes: int | None = None
            if trace_allocations:
                import tracemalloc

                gc.collect()
                runtime.calls.clear()
                tracemalloc.start()
                try:
                    result = await service.git.execute_git_operation(
                        workspace_id, "list_repos", None, {}
                    )
                    if not result.get("ok"):
                        raise RuntimeError("list_repos failed during allocation probe")
                    _, peak_bytes = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

            return {
                "repos": repo_count,
                "injected_delay_ms_per_runtime_call": delay_ms,
                "timing_ms": {
                    "p50": round(statistics.median(elapsed), 3),
                    "p95_nearest_rank": round(_percentile(elapsed, 0.95), 3),
                    "min": round(min(elapsed), 3),
                    "max": round(max(elapsed), 3),
                    "samples": [round(value, 3) for value in elapsed],
                },
                "runtime_calls_per_sample": call_counts,
                "runtime_call_categories": expected_categories,
                "response_sha256": expected_digest,
                "peak_python_allocated_bytes_separate_probe": peak_bytes,
                "fixture_head_oids": fixture_heads,
            }


async def _run(args: argparse.Namespace, tests: Any) -> dict[str, Any]:
    """Run all requested cases and return JSON-serializable measurements."""
    results = [
        await _measure_case(
            tests, count, delay, args.samples, args.warmups, args.tracemalloc
        )
        for count in sorted(set(args.repos))
        for delay in args.delay_ms
    ]
    return {
        "benchmark": "runner GitService list_repos via existing FakeGitRuntime",
        "checkout": str(args.repo),
        "samples": args.samples,
        "warmups": args.warmups,
        "results": results,
        "notes": [
            "Real local Git fixtures; test fake maps /workspace and runs local Git subprocesses.",
            "No network, provider, external service, or user Git config is used.",
            "Delay is simulated asyncio sleep, not Docker/SSH latency.",
            "No alternate subprocess runtime: find/realpath are emulated by FakeGitRuntime.",
            "Digest covers canonical response JSON; raw response and command text are omitted.",
            "Runtime counts are outside timing; optional allocation tracing is a separate probe.",
        ],
    }


def main() -> None:
    """Parse arguments, isolate Git configuration, and emit JSON to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--repos", type=int, nargs="+", default=[1, 5, 15])
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--delay-ms", type=float, nargs="+", default=[0.0, 5.0])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tracemalloc", action="store_true")
    args = parser.parse_args()
    if any(count < 1 for count in args.repos):
        parser.error("--repos values must be positive")
    if args.samples < 1 or args.warmups < 0:
        parser.error("--samples must be positive and --warmups nonnegative")
    if any(not math.isfinite(delay) or delay < 0 for delay in args.delay_ms):
        parser.error("--delay-ms values must be finite and nonnegative")
    args.repo = args.repo.resolve(strict=True)
    if not (args.repo / "runner" / "src").is_dir():
        parser.error(f"--repo must name an OpenCuria checkout: {args.repo}")

    tests = _load_test_helpers(args.repo)
    tests.setup_module()
    try:
        os.environ.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_SYSTEM="/dev/null",
            GIT_TERMINAL_PROMPT="0",
            LC_ALL="C",
            LANG="C",
            TZ="UTC",
        )
        result = asyncio.run(_run(args, tests))
    finally:
        tests.teardown_module()

    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
