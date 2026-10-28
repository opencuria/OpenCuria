#!/usr/bin/env python3
"""Measure complete runner Git snapshots with disposable local repositories.

Run with ``runner/.venv/bin/python scripts/benchmark_git_snapshot.py``.
Git and state-marker probes run as real local subprocesses; the existing test
runtime maps workspace paths and emulates credential/containment probes.
Optional per-call delay models transport overhead, not measured QEMU/SSH latency.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import math
import os
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


async def measure(
    delay_ms: float,
    samples: int,
    warmups: int,
    untracked_files: int,
    baseline_type: type | None = None,
) -> dict[str, Any]:
    """Time the public snapshot operation, including validation and history."""
    from src.models import WorkspaceInfo
    from src.services.git_service import GitService
    from tests.test_git_operations import FakeGitRuntime, _make_repo

    class LocalProbeRuntime(FakeGitRuntime):
        """Execute marker tests locally and add the same delay to every call."""

        async def exec_command_wait(
            self,
            instance_id: str,
            command: list[str],
            workdir: str | None = None,
            env: dict[str, str] | None = None,
        ) -> tuple[int, str]:
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            probe: list[str] | None = None
            if command[:2] == ["test", "-e"]:
                probe = ["test", "-e", self._resolve_cwd(command[2])]
            elif command[:2] == ["sh", "-c"] and command[3:4] == [
                "opencuria-git-state"
            ]:
                probe = [*command[:4], self._resolve_cwd(command[4])]
            if probe is not None:
                self.calls.append((command, workdir, env))
                process = await asyncio.create_subprocess_exec(
                    *probe,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                output, _ = await process.communicate()
                return process.returncode, output.decode()
            return await super().exec_command_wait(instance_id, command, workdir, env)

    with tempfile.TemporaryDirectory(prefix="oc-git-snapshot-") as directory:
        root = Path(directory)
        repository = root / "repo"
        await asyncio.to_thread(_make_repo, str(repository))
        # Representative staged, unstaged and untracked content, plus history.
        (repository / "README.md").write_text("# unstaged change\n")
        for index in range(untracked_files):
            name = "untracked.txt" if index == 0 else f"untracked-{index:04}.txt"
            (repository / name).write_text("new file\n")
        (repository / "staged.txt").write_text("staged change\n")
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(repository), "add", "staged.txt"
        )
        if await process.wait():
            raise RuntimeError("Could not stage fixture file")
        runtime = LocalProbeRuntime(directory)
        workspace_id = uuid.UUID(int=1)
        info = WorkspaceInfo(
            workspace_id=workspace_id, instance_id="instance-1", status="running"
        )
        services = {"after": GitService(lambda _: info, lambda _: runtime)}
        if baseline_type is not None:
            services = {
                "before": baseline_type(lambda _: info, lambda _: runtime),
                **services,
            }

        async def snapshot(service: Any) -> dict[str, Any]:
            result = await service.execute_git_operation(
                workspace_id, "repo_snapshot", "/workspace/repo", {}
            )
            if not result.get("ok"):
                raise RuntimeError(f"Snapshot failed: {result}")
            return result

        durations: dict[str, list[float]] = {name: [] for name in services}
        counts: dict[str, set[int]] = {name: set() for name in services}
        digest: str | None = None
        for iteration in range(warmups + samples):
            order = list(services)
            if iteration % 2:
                order.reverse()
            for name in order:
                runtime.calls.clear()
                start = time.perf_counter()
                result = await snapshot(services[name])
                elapsed = (time.perf_counter() - start) * 1000
                current = hashlib.sha256(
                    json.dumps(result, sort_keys=True).encode()
                ).hexdigest()
                if digest is not None and current != digest:
                    raise RuntimeError(
                        "Snapshot response changed between versions/samples"
                    )
                digest = current
                if iteration >= warmups:
                    durations[name].append(elapsed)
                    counts[name].add(len(runtime.calls))
        metrics = {}
        for name, service in services.items():
            runtime.calls.clear()
            gc.collect()
            tracemalloc.start()
            try:
                await snapshot(service)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            values = durations[name]
            metrics[name] = {
                "p50_ms": round(statistics.median(values), 3),
                "p95_ms": round(sorted(values)[math.ceil(samples * 0.95) - 1], 3),
                "runtime_calls": sorted(counts[name]),
                "peak_python_bytes_separate_probe": peak,
                "response_sha256": digest,
                "samples_ms": [round(duration, 3) for duration in values],
            }
        return {
            "simulated_delay_ms_per_call": delay_ms,
            **(metrics if baseline_type is not None else metrics["after"]),
        }


def main() -> None:
    """Parse benchmark options and emit reproducible JSON measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--untracked-files", type=int, default=1)
    parser.add_argument("--delay-ms", type=float, nargs="+", default=[0, 5, 20])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--baseline-ref",
        help="Alternate GitService from this revision with current code",
    )
    args = parser.parse_args()
    if args.samples < 1 or args.warmups < 0:
        parser.error("samples must be positive and warmups nonnegative")
    if args.untracked_files < 0:
        parser.error("untracked-files must be nonnegative")
    if any(not math.isfinite(delay) or delay < 0 for delay in args.delay_ms):
        parser.error("delay-ms must be finite and nonnegative")
    sys.path.insert(0, str(args.repo.resolve(strict=True) / "runner"))
    # Child processes use only disposable repository configuration.
    for key in list(os.environ):
        if key.startswith("GIT_"):
            del os.environ[key]
    os.environ.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00",
        LC_ALL="C",
        TZ="UTC",
    )

    baseline_type = None
    if args.baseline_ref:
        source = subprocess.run(
            ["git", "show", f"{args.baseline_ref}:runner/src/services/git_service.py"],
            cwd=args.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        module = types.ModuleType("src.services._git_service_baseline")
        module.__package__ = "src.services"
        exec(compile(source, "<baseline GitService>", "exec"), module.__dict__)
        baseline_type = module.GitService

    async def run() -> list[dict[str, Any]]:
        return [
            await measure(
                delay, args.samples, args.warmups, args.untracked_files, baseline_type
            )
            for delay in args.delay_ms
        ]

    result = {
        "samples": args.samples,
        "warmups": args.warmups,
        "untracked_files": args.untracked_files,
        "baseline_ref": args.baseline_ref,
        "results": asyncio.run(run()),
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
