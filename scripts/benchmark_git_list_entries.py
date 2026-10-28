"""Compare Git list-entry reads on disposable repositories.

Run with runner/.venv/bin/python scripts/benchmark_git_list_entries.py.
The local run includes discovery via the existing Git test runtime. Optional
loopback SSH runs the complete list through QemuRuntime.exec_command_wait,
mapping /workspace to the disposable fixture tree. No simulated delays or
QEMU guest are used. --entries-only isolates the phase after discovery.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import gc
import getpass
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

import asyncssh

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runner"))

from src import git as git_ops  # noqa: E402
from src.models import WorkspaceInfo  # noqa: E402
from src.services.git_service import GitService  # noqa: E402
from tests.test_git_operations import FakeGitRuntime  # noqa: E402


def _baseline_type(revision: str) -> type[GitService]:
    """Load only the original list-entry method, sharing unchanged discovery."""
    source = subprocess.check_output(
        ["git", "show", f"{revision}:runner/src/services/git_service.py"],
        cwd=REPO,
        text=True,
    )
    service = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "GitService"
    )
    method = next(
        node
        for node in service.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_git_list_entry"
    )
    namespace = dict(vars(sys.modules[GitService.__module__]))
    tree = ast.Module(body=[method], type_ignores=[])
    exec(compile(tree, f"{revision}:_git_list_entry", "exec"), namespace)  # noqa: S102
    return type(
        "BaselineGitService",
        (GitService,),
        {
            "_git_list_entry": namespace["_git_list_entry"],
        },
    )


class SSHRuntime:
    """Count actual SSH channels through the production execution method."""

    def __init__(
        self, connection: asyncssh.SSHClientConnection, root: Path | None
    ) -> None:
        from src.runtime.qemu_runtime import QemuRuntime

        self._runtime = object.__new__(QemuRuntime)
        self._root = root
        self.calls: list[list[str]] = []

        async def get_ssh(_instance_id: str) -> asyncssh.SSHClientConnection:
            return connection

        self._runtime._get_ssh = get_ssh

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Do not source host credentials while running synthetic Git fixtures."""
        self.calls.append(list(command))
        safe_command = list(command)
        if safe_command[:2] == ["sh", "-c"]:
            safe_command[2] = safe_command[2].removeprefix(
                git_ops.GIT_SOURCE_ENV_SCRIPT
            )
        if self._root is not None:

            def mapped(path: str) -> str:
                if path == "/workspace" or path.startswith("/workspace/"):
                    return str(self._root) + path[len("/workspace") :]
                return path

            safe_command = [mapped(arg) for arg in safe_command]
            if safe_command == ["sh", "-c", "test -s /root/.opencuria-env.sh"]:
                safe_command = ["test", "-s", str(self._root / "absent-credentials")]
            workdir = mapped(workdir) if workdir else workdir
        code, output = await self._runtime.exec_command_wait(
            instance_id, safe_command, workdir, env
        )
        if self._root is not None:
            output = output.replace(str(self._root), "/workspace")
        return code, output


def _summary(samples: list[float]) -> dict[str, float]:
    """Return p50 and nearest-rank p95 latency in milliseconds."""
    return {
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3),
    }


def _fixtures(root: Path, count: int) -> list[str]:
    """Create committed repositories without using user Git config or hooks."""
    heads = []
    for index in range(count):
        path = root / f"repo-{index:02d}"
        path.mkdir()
        for args in (
            ["init", "-b", "main"],
            [
                "-c",
                "user.name=Benchmark",
                "-c",
                "user.email=bench@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--allow-empty",
                "-m",
                "Synthetic",
            ],
        ):
            subprocess.run(
                ["git", *args],
                cwd=path,
                check=True,
                capture_output=True,
            )
        heads.append(
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=path, text=True
            ).strip()
        )
    return heads


async def _case(
    args: argparse.Namespace,
    root: Path,
    count: int,
    ssh: asyncssh.SSHClientConnection | None,
) -> dict[str, Any]:
    heads = await asyncio.to_thread(_fixtures, root, count)
    workspace_id = uuid.UUID(int=1)
    info = WorkspaceInfo(workspace_id, "local", "running")
    runtimes = {
        name: SSHRuntime(ssh, None if args.entries_only else root)
        if ssh
        else FakeGitRuntime(str(root))
        for name in ("before", "after")
    }
    services = {
        name: cls(lambda _: info, lambda _, r=runtimes[name]: r)
        for name, cls in (
            ("before", _baseline_type(args.baseline)),
            ("after", GitService),
        )
    }
    env = {
        **git_ops.build_git_env(),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    async def run(name: str) -> list[dict[str, Any]]:
        if ssh and args.entries_only:
            # Same serial per-repo lock discipline as the complete list RPC.
            rows = []
            for index in range(count):
                path = str(root / f"repo-{index:02d}")
                lock = await services[name]._git_lock(workspace_id, path)
                async with lock:
                    rows.append(
                        await services[name]._git_list_entry(
                            runtimes[name], "local", path, env
                        )
                    )
            return rows
        result = await services[name].execute_git_operation(workspace_id, "list_repos")
        assert result["ok"], result
        return result["repos"]

    def validate(rows: list[dict[str, Any]]) -> None:
        assert len(rows) == count
        assert [row["head_hash"] for row in rows] == heads
        assert all(row["current_branch"] == "main" for row in rows)

    elapsed: dict[str, list[float]] = {name: [] for name in services}
    calls = {}
    generator = random.Random(0)
    for iteration in range(args.samples + 3):
        order = list(services)
        generator.shuffle(order)
        pair = {}
        for name in order:
            runtimes[name].calls.clear()
            start = time.perf_counter()
            rows = await run(name)
            duration = (time.perf_counter() - start) * 1000
            if iteration >= 3:
                elapsed[name].append(duration)
            validate(rows)
            pair[name] = rows
            calls[name] = len(runtimes[name].calls)
        assert pair["before"] == pair["after"]

    report: dict[str, Any] = {"repos": count, "samples": args.samples}
    report["median_paired_saving_ms"] = round(
        statistics.median(
            before - after for before, after in zip(elapsed["before"], elapsed["after"])
        ),
        3,
    )
    for name in services:
        gc.collect()
        tracemalloc.start()
        validate(await run(name))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report[name] = {
            **_summary(elapsed[name]),
            "guest_exec_calls": calls[name],
            "peak_python_bytes": peak,
            "latency_samples_ms": [round(value, 3) for value in elapsed[name]],
        }
    return report


async def _workloads(
    args: argparse.Namespace,
    ssh: asyncssh.SSHClientConnection | None,
) -> dict[str, Any]:
    cases = []
    for count in args.repos:
        with tempfile.TemporaryDirectory(prefix="git-list-bench-", dir=REPO) as root:
            cases.append(await _case(args, Path(root), count, ssh))
    return {
        "baseline": args.baseline,
        "transport": "persistent-loopback-ssh" if ssh else "local-git-test-runtime",
        "scope": "list-entry phase after discovery"
        if ssh and args.entries_only
        else "complete list_repos",
        "method": "3 warmups; seeded shuffled pairs on identical repos; exact result "
        "equality; separate tracemalloc probe; no artificial delay or QEMU",
        "cases": cases,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.ssh_port:
        async with asyncssh.connect(
            "127.0.0.1",
            port=args.ssh_port,
            username=getpass.getuser(),
            client_keys=[str(args.ssh_key)],
            known_hosts=str(args.known_hosts),
        ) as ssh:
            return await _workloads(args, ssh)
    return await _workloads(args, None)


def main() -> None:
    """Run paired measurements against the selected original method."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="922f3e2")
    parser.add_argument("--samples", type=int, default=41)
    parser.add_argument("--repos", type=int, nargs="+", default=[1, 5, 15])
    parser.add_argument("--ssh-port", type=int)
    parser.add_argument("--entries-only", action="store_true")
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 2 or min(args.repos) < 1:
        parser.error("samples must be >= 2 and repos >= 1")
    if args.ssh_port and not (args.ssh_key and args.known_hosts):
        parser.error("SSH requires --ssh-key and --known-hosts")
    os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    result = json.dumps(asyncio.run(_run(args)), indent=2) + "\n"
    if args.output:
        args.output.write_text(result)
    sys.stdout.write(result)


if __name__ == "__main__":
    main()
