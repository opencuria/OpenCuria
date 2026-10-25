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
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
import time
import tracemalloc
import uuid
from pathlib import Path
from typing import Any


async def measure(delay_ms: float, samples: int, warmups: int) -> dict[str, Any]:
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
        (repository / "untracked.txt").write_text("new file\n")
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
        service = GitService(lambda _: info, lambda _: runtime)

        async def snapshot() -> dict[str, Any]:
            result = await service.execute_git_operation(
                workspace_id, "repo_snapshot", "/workspace/repo", {}
            )
            if not result.get("ok"):
                raise RuntimeError(f"Snapshot failed: {result}")
            return result

        durations: list[float] = []
        counts: list[int] = []
        digest: str | None = None
        for iteration in range(warmups + samples):
            runtime.calls.clear()
            start = time.perf_counter()
            result = await snapshot()
            elapsed = (time.perf_counter() - start) * 1000
            current = hashlib.sha256(
                json.dumps(result, sort_keys=True).encode()
            ).hexdigest()
            if digest is not None and current != digest:
                raise RuntimeError("Snapshot response changed between samples")
            digest = current
            if iteration >= warmups:
                durations.append(elapsed)
                counts.append(len(runtime.calls))
        runtime.calls.clear()
        tracemalloc.start()
        try:
            await snapshot()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return {
            "simulated_delay_ms_per_call": delay_ms,
            "p50_ms": round(statistics.median(durations), 3),
            "p95_ms": round(sorted(durations)[math.ceil(samples * 0.95) - 1], 3),
            "runtime_calls": sorted(set(counts)),
            "peak_python_bytes_separate_probe": peak,
            "response_sha256": digest,
            "samples_ms": [round(duration, 3) for duration in durations],
        }


def main() -> None:
    """Parse benchmark options and emit reproducible JSON measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--delay-ms", type=float, nargs="+", default=[0, 5, 20])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 1 or args.warmups < 0:
        parser.error("samples must be positive and warmups nonnegative")
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

    async def run() -> list[dict[str, Any]]:
        return [
            await measure(delay, args.samples, args.warmups) for delay in args.delay_ms
        ]

    result = {
        "samples": args.samples,
        "warmups": args.warmups,
        "results": asyncio.run(run()),
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
