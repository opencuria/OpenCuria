"""Compare complete file downloads with real local guest commands.

Run with runner/.venv/bin/python scripts/benchmark_file_download.py.
Fixtures stay within /workspace. Local subprocesses are the default; optionally
use a persistent loopback SSH connection through the production exec method.
No QEMU or remote network latency is included.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gc
import getpass
import hashlib
import json
import math
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

import asyncssh

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "runner"))

from src.models import WorkspaceInfo  # noqa: E402
from src.services.files import FileManager  # noqa: E402
from tests.directory_download_runtime import LocalFileRuntime  # noqa: E402


class SSHRuntime:
    """Use the production exec implementation over one persistent SSH link."""

    def __init__(self, connection: asyncssh.SSHClientConnection) -> None:
        from src.runtime.qemu_runtime import QemuRuntime

        self._runtime = object.__new__(QemuRuntime)
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
        """Count channels without replacing command construction or execution."""
        self.calls.append(list(command))
        return await self._runtime.exec_command_wait(instance_id, command, workdir, env)


def _baseline_type(revision: str) -> type[FileManager]:
    """Load the original implementation without changing the checkout."""
    source = subprocess.check_output(
        ["git", "show", f"{revision}:runner/src/services/files.py"],
        cwd=REPO,
        text=True,
    )
    module = types.ModuleType("src.services._benchmark_download_baseline")
    module.__package__ = "src.services"
    exec(compile(source, f"{revision}:files.py", "exec"), module.__dict__)  # noqa: S102
    return module.FileManager


def _summary(samples: list[float]) -> dict[str, float]:
    """Return median and nearest-rank p95 in milliseconds."""
    return {
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3),
    }


async def _case(
    baseline: type[FileManager],
    source: Path,
    samples: int,
    concurrency: int,
    ssh: asyncssh.SSHClientConnection | None,
) -> dict[str, Any]:
    """Interleave full-service calls on identical bytes and validate every pair."""
    workspace_id = uuid.UUID(int=1)
    info = WorkspaceInfo(workspace_id, "local", "running")
    runtimes = {
        name: SSHRuntime(ssh) if ssh else LocalFileRuntime()
        for name in ("before", "after")
    }
    managers = {
        name: cls(get_cached=lambda _: info, get_runtime=lambda _, r=runtimes[name]: r)
        for name, cls in (("before", baseline), ("after", FileManager))
    }
    expected_content = source.read_bytes()
    expected_digest = hashlib.sha256(expected_content).hexdigest()
    elapsed: dict[str, list[float]] = {name: [] for name in managers}
    counters: dict[str, int] = {}

    async def run(name: str) -> list[dict[str, Any]]:
        return await asyncio.gather(
            *(
                managers[name].download_file(workspace_id, str(source))
                for _ in range(concurrency)
            )
        )

    def validate(payloads: list[dict[str, Any]]) -> None:
        for payload in payloads:
            assert payload["filename"] == source.name
            assert payload["is_archive"] is False
            assert payload["size"] == len(expected_content)
            assert base64.b64decode(payload["content"]) == expected_content

    generator = random.Random(0)
    for index in range(samples + 3):
        order = list(managers)
        generator.shuffle(order)
        pair = {}
        for name in order:
            runtimes[name].calls.clear()
            start = time.perf_counter()
            payloads = await run(name)
            duration = (time.perf_counter() - start) * 1000
            if index >= 3:
                elapsed[name].append(duration)
            validate(payloads)
            pair[name] = payloads
            counters[name] = len(runtimes[name].calls)
        assert pair["before"] == pair["after"]

    report: dict[str, Any] = {
        "bytes_per_file": len(expected_content),
        "concurrency": concurrency,
        "samples": samples,
        "sha256": expected_digest,
    }
    for name in managers:
        gc.collect()
        tracemalloc.start()
        payloads = await run(name)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        validate(payloads)
        report[name] = {
            **_summary(elapsed[name]),
            "guest_exec_calls": counters[name],
            "peak_python_bytes": peak,
        }
    return report


async def _workloads(
    args: argparse.Namespace, directory: Path, ssh: asyncssh.SSHClientConnection | None
) -> dict[str, Any]:
    baseline = _baseline_type(args.baseline)
    cases = []
    for size, concurrency in (
        (0, 1),
        (64 * 1024, 1),
        (5 * 1024 * 1024, 1),
        (64 * 1024, 4 if ssh else 8),
    ):
        path = directory / f"payload-{size}.bin"
        path.write_bytes(bytes(range(256)) * (size // 256))
        cases.append(await _case(baseline, path, args.samples, concurrency, ssh))
    return {
        "baseline": args.baseline,
        "transport": "persistent-loopback-ssh" if ssh else "local-subprocess",
        "method": (
            "Real FileManager/containment/shell/stat/base64/parsing; 3 warmups, "
            "seeded interleaved pairs; exact payload equality; "
            "no QEMU or remote network."
        ),
        "cases": cases,
    }


async def _run(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    """Reuse one verified SSH connection when that transport is requested."""
    if args.ssh_port:
        async with asyncssh.connect(
            "127.0.0.1",
            port=args.ssh_port,
            username=getpass.getuser(),
            client_keys=[str(args.ssh_key)],
            known_hosts=str(args.known_hosts),
        ) as ssh:
            return await _workloads(args, directory, ssh)
    return await _workloads(args, directory, None)


def main() -> None:
    """Run disposable integration workloads and emit a JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", default="ea8ebfdda9ea87cc237ff18ea7eb1bc4b6c6c4e8"
    )
    parser.add_argument("--samples", type=int, default=41)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ssh-port", type=int)
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument("--known-hosts", type=Path)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("samples must be >= 2")
    if args.ssh_port and (not args.ssh_key or not args.known_hosts):
        parser.error("--ssh-port requires --ssh-key and --known-hosts")
    with tempfile.TemporaryDirectory(prefix="file-download-bench-", dir=REPO) as path:
        report = asyncio.run(_run(args, Path(path)))
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
