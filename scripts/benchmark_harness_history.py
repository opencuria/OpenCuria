"""Measure provider-history reconstruction on disposable file-backed SQLite.

Run with ``backend/.venv/bin/python scripts/benchmark_harness_history.py``.
Only synthetic data is created; the database is always removed on exit. Use
``--repo`` to run the same benchmark against an archived baseline checkout.
Use ``--baseline-repo`` to alternate archived/current service and repositories
against identical fixtures in one process (requires an unchanged model schema).
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
import tracemalloc
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any


def _identity(value: str) -> uuid.UUID:
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"https://example.invalid/history-bench/{value}"
    )


def _populate(turns: int, compacted: bool, workspace: Any, output_bytes: int) -> Any:
    from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession

    key = f"{turns}/{compacted}"
    session = HarnessSession.objects.create(
        id=_identity(key),
        workspace=workspace,
        organization_id=workspace.runner.organization_id,
        title="Synthetic history",
    )
    messages, parts = [], []
    for index in range(turns):
        user_id = _identity(f"{key}/{index}/user")
        assistant_id = _identity(f"{key}/{index}/assistant")
        before, after = f"Inspecting turn {index}.", f"Completed turn {index}."
        messages.extend(
            [
                HarnessMessage(
                    id=user_id,
                    session=session,
                    role="user",
                    content=f"Check step {index}",
                    position=index * 2,
                ),
                HarnessMessage(
                    id=assistant_id,
                    session=session,
                    role="assistant",
                    content=before + after,
                    position=index * 2 + 1,
                ),
            ]
        )
        for position, (kind, output) in enumerate(
            [
                ("step-start", ""),
                ("text", before),
                ("tool", f"fixture-{index}:" + "x" * output_bytes),
                ("text", after),
                ("step-finish", ""),
            ]
        ):
            parts.append(
                HarnessPart(
                    id=_identity(f"{key}/{index}/part/{position}"),
                    message_id=assistant_id,
                    type=kind,
                    position=position,
                    output=output,
                    state="completed",
                    call_id=f"call-{index}" if kind == "tool" else "",
                    input=(
                        {
                            "tool": "read",
                            "arguments": {"path": f"/workspace/{index}.txt"},
                        }
                        if kind == "tool"
                        else {}
                    ),
                )
            )
        if compacted and index == max(0, turns - 5):
            parts.append(
                HarnessPart(
                    id=_identity(f"{key}/compaction"),
                    message_id=assistant_id,
                    type="compaction",
                    position=5,
                    state="completed",
                    output="Synthetic checkpoint",
                    meta={"tail_start_id": str(user_id)},
                )
            )
    HarnessMessage.objects.bulk_create(messages)
    HarnessPart.objects.bulk_create(parts)
    return session


async def _measure(
    services: dict[str, Any], session: Any, samples: int
) -> dict[str, Any]:
    from asgiref.sync import sync_to_async
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def digest(history: Any) -> str:
        payload = json.dumps([asdict(row) for row in history], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    expected: str | None = None
    elapsed: dict[str, list[float]] = {name: [] for name in services}
    for iteration in range(samples + 5):
        order = list(services)
        if iteration % 2:
            order.reverse()
        for name in order:
            start = time.perf_counter()
            history = await services[name]._build_history(session)
            duration = (time.perf_counter() - start) * 1000
            if iteration >= 5:
                elapsed[name].append(duration)
            actual = digest(history)
            if expected is None:
                expected = actual
            elif actual != expected:
                raise RuntimeError("Provider history changed across versions/samples")

    # Django connections are thread-local: capture queries on the same
    # thread-sensitive executor used by the service's repository calls.
    def start_queries() -> Any:
        connection.queries_log.clear()
        context = CaptureQueriesContext(connection)
        context.__enter__()
        return context

    def finish_queries(context: Any) -> int:
        context.__exit__(None, None, None)
        return len(context)

    results = {}
    for name, service in services.items():
        queries = await sync_to_async(start_queries)()
        try:
            await service._build_history(session)
        finally:
            query_count = await sync_to_async(finish_queries)(queries)
        gc.collect()
        tracemalloc.start()
        try:
            await service._build_history(session)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        results[name] = {
            "query_count": query_count,
            "p50_ms": round(statistics.median(elapsed[name]), 3),
            "p95_ms": round(sorted(elapsed[name])[math.ceil(samples * 0.95) - 1], 3),
            "peak_python_allocated_bytes": peak,
            "history_rows": len(history),
            "history_sha256": expected,
            "samples_ms": [round(value, 3) for value in elapsed[name]],
        }
    return results if len(services) > 1 else results["after"]


def _baseline_service(repo: Path) -> Any:
    """Load archived service/repositories against the same unchanged model schema."""
    modules = {}
    for name in ("repositories", "harness_service"):
        path = repo / "backend" / "apps" / "harness" / f"{name}.py"
        module_name = f"apps.harness._benchmark_baseline_{name}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load baseline module: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        modules[name] = module
    return modules["harness_service"].HarnessService(
        messages=modules["repositories"].HarnessMessageRepository,
        parts=modules["repositories"].HarnessPartRepository,
    )


def _run(args: argparse.Namespace) -> dict[str, Any]:
    import django

    django.setup()
    from apps.harness.harness_service import HarnessService
    from apps.organizations.models import Organization
    from apps.runners.models import Runner, Workspace
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    org = Organization.objects.create(
        id=_identity("org"), name="Synthetic history", slug="history-bench"
    )
    runner = Runner.objects.create(
        id=_identity("runner"),
        name="Offline",
        organization=org,
        api_token_hash="synthetic-unused",
    )
    user = get_user_model().objects.create_user(email="history-bench@example.invalid")
    workspace = Workspace.objects.create(
        id=_identity("workspace"), name="Synthetic", runner=runner, created_by=user
    )
    scenarios = [
        (
            turns,
            compacted,
            _populate(turns, compacted, workspace, args.output_kib * 1024),
        )
        for turns in sorted(set(args.turns))
        for compacted in (False, True)
    ]

    async def measurements() -> list[dict[str, Any]]:
        services = {}
        if args.baseline_repo:
            services["before"] = _baseline_service(args.baseline_repo.resolve())
        services["after"] = HarnessService()
        return [
            {
                "assistant_turns": turns,
                "compacted": compacted,
                **await _measure(services, session, args.samples),
            }
            for turns, compacted, session in scenarios
        ]

    return {
        "benchmark": "provider-history reconstruction; real service and repositories, "
        "temporary file-backed SQLite",
        "samples": args.samples,
        "warmups": 5,
        "paired": bool(args.baseline_repo),
        "tool_output_bytes_per_turn": args.output_kib * 1024,
        "parts_per_turn": 5,
        "compaction_tail_turns": 5,
        "results": asyncio.run(measurements()),
        "notes": [
            "No external services or provider calls.",
            "Warmed latency; SQL counting and allocation tracing measured separately.",
            "Paired mode alternates version order on identical fixtures in one process.",
            (
                "Timings are local integration measurements, not production SLOs; "
                "memory is Python allocation peak, not RSS."
            ),
        ],
    }


def main() -> None:
    """Select a disposable database before importing Django settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--turns", type=int, nargs="+", default=[1, 10, 50])
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--output-kib", type=int, default=32)
    parser.add_argument(
        "--baseline-repo",
        type=Path,
        help="Pair archived service/repositories with current code on the same fixtures",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.turns) < 1 or args.samples < 2 or args.output_kib < 1:
        parser.error("turns/output-kib must be positive and samples must be >= 2")
    with tempfile.TemporaryDirectory(prefix="opencuria-history-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-history-not-a-secret",
        )
        sys.path.insert(0, str(args.repo.resolve() / "backend"))
        result = json.dumps(_run(args), indent=2) + "\n"
        if args.output:
            args.output.write_text(result)
        sys.stdout.write(result)


if __name__ == "__main__":
    main()
