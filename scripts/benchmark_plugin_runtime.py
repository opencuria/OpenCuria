"""Measure plugin snapshots and complete harness MCP preparation with real SQL.

Run with ``backend/.venv/bin/python scripts/benchmark_plugin_runtime.py``.
Uses a disposable file-backed SQLite database, synthetic credentials and no
provider/runner/network calls. ``--backend-root`` accepts an archived baseline.
"""

from __future__ import annotations

import argparse
import gc
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
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/runtime/{name}")


def _measure(run: Callable[[], Any], args: argparse.Namespace) -> dict[str, Any]:
    """Separate warmed latency samples from SQL capture and allocation tracing."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    durations = []
    digests = set()
    for iteration in range(args.warmups + args.samples):
        start = time.perf_counter()
        snapshot = run()
        elapsed = (time.perf_counter() - start) * 1000
        digests.add(
            hashlib.sha256(
                json.dumps(asdict(snapshot), default=str, sort_keys=True).encode()
            ).hexdigest()
        )
        if iteration >= args.warmups:
            durations.append(elapsed)
    if len(digests) != 1:
        raise RuntimeError("Snapshot changed between samples")
    connection.queries_log.clear()
    with CaptureQueriesContext(connection) as queries:
        run()
    gc.collect()
    tracemalloc.start()
    run()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "query_count": len(queries),
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(args.samples * 0.95) - 1], 3),
        "peak_python_allocated_bytes": peak,
        "snapshot_sha256": digests.pop(),
        "samples_ms": [round(value, 3) for value in durations],
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    from apps.credentials.models import Credential, CredentialService
    from apps.harness.harness_service import HarnessService
    from apps.organizations.models import Organization
    from apps.plugins.models import (
        OrgPluginActivation,
        Plugin,
        PluginCredentialRequirement,
        PluginMcpServer,
        PluginSkill,
        WorkspacePluginActivation,
    )
    from apps.plugins.runtime import build_workspace_plugin_snapshot
    from apps.runners.models import Runner, Workspace
    from common.utils import encrypt_value

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    Plugin.objects.all().delete()
    org = Organization.objects.create(
        id=_identity("org"), name="Runtime benchmark", slug="runtime-bench"
    )
    owner = get_user_model().objects.create_user(email="runtime@example.invalid")
    runner = Runner.objects.create(organization=org, api_token_hash="synthetic-unused")
    workspace = Workspace.objects.create(
        id=_identity("workspace"), runner=runner, created_by=owner
    )
    service = CredentialService.objects.create(
        id=_identity("service"),
        name="Synthetic token",
        slug="runtime-token",
        credential_type="env",
        env_var_name="TOKEN",
    )
    workspace.credentials.add(
        Credential.objects.create(
            organization=org,
            service=service,
            name="Disposable benchmark token",
            encrypted_value=encrypt_value("synthetic-runtime-token"),
        )
    )
    session = SimpleNamespace(workspace_id=workspace.id)

    def snapshot() -> Any:
        return build_workspace_plugin_snapshot(workspace=workspace, org_id=org.id)

    def prepare() -> Any:
        prepared = HarnessService._prepare_mcp_snapshot_for_run(session, org.id)
        if len(prepared.plaintexts) != len(prepared.snapshot.plugins):
            raise RuntimeError("Prepared credentials do not match effective plugins")
        return prepared.snapshot

    results = []
    created = 0
    for count in sorted(set(args.plugins)):
        for index in range(created, count):
            plugin = Plugin.objects.create(
                id=_identity(f"plugin/{index}"),
                organization=org if index % 2 else None,
                name=f"Plugin {index:04}",
                slug=f"runtime-{index}",
            )
            OrgPluginActivation.objects.create(organization=org, plugin=plugin)
            WorkspacePluginActivation.objects.create(workspace=workspace, plugin=plugin)
            for component in range(3):
                PluginSkill.objects.create(
                    id=_identity(f"skill/{index}/{component}"),
                    plugin=plugin,
                    name=f"Guide {component}",
                    slug=f"guide-{component}",
                    body="Synthetic instructions.\n" * 250,
                    position=2 - component,
                )
            PluginMcpServer.objects.create(
                id=_identity(f"server/{index}"),
                plugin=plugin,
                name="Tools",
                slug="tools",
                command="synthetic-mcp",
                env={"TOKEN": "{{credential.api_key}}"},
            )
            PluginCredentialRequirement.objects.create(
                id=_identity(f"requirement/{index}"),
                plugin=plugin,
                key="api_key",
                credential_service=service,
            )
        created = count
        if len(snapshot().plugins) != count:
            raise RuntimeError("Unexpected effective plugin count")
        results.append(
            {
                "plugins": count,
                "snapshot": _measure(snapshot, args),
                "prepare_mcp_for_run": _measure(prepare, args),
            }
        )
    return {
        "benchmark": "plugin runtime, file-backed SQLite, DEBUG=False",
        "samples": args.samples,
        "warmups": args.warmups,
        "components_per_plugin": {"skills": 3, "mcp_servers": 1, "requirements": 1},
        "results": results,
        "notes": [
            "Preparation includes workspace reads and real credential decryption.",
            "Snapshot hashing, SQL capture and allocation tracing are not timed.",
            "Allocation peaks measure Python allocations, not process RSS.",
            "Warm local timings do not predict production network/database latency.",
        ],
    }


def main() -> None:
    """Select an isolated database before importing Django settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins", type=int, nargs="+", default=[0, 1, 5, 15, 50])
    parser.add_argument("--samples", type=int, default=51)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend",
    )
    args = parser.parse_args()
    if min(args.plugins) < 0 or args.samples < 2 or args.warmups < 1:
        parser.error("plugins >= 0, samples >= 2 and warmups >= 1 are required")
    with tempfile.TemporaryDirectory(prefix="opencuria-runtime-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-runtime-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(args.backend_root.resolve()))
        sys.stdout.write(json.dumps(_run(args), indent=2) + "\n")


if __name__ == "__main__":
    main()
