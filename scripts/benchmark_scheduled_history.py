"""Measure scheduled-run history HTTP reads on disposable SQLite fixtures.

Run with ``backend/.venv/bin/python scripts/benchmark_scheduled_history.py``.
An optional ``--backend-root`` exercises the same workload on an archived backend.
No runners, providers or existing application databases are used.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, nargs="+", default=[1, 20, 100])
    parser.add_argument("--answer-kib", type=int, nargs="+", default=[0, 64])
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend",
    )
    args = parser.parse_args()
    if (
        min(args.runs) < 1
        or max(args.runs) > 100
        or min(args.answer_kib) < 0
        or args.samples < 2
        or args.warmups < 1
    ):
        parser.error("runs: 1..100, answer-kib >= 0, samples >= 2, warmups >= 1")
    return args


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/history/{name}")


def _measure(
    request: Callable[[], bytes],
    prepare: Callable[[], None],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Time HTTP separately from SQL capture and Python allocation tracing."""
    from django.db.backends.utils import CursorWrapper

    durations = []
    digests = set()
    for iteration in range(args.warmups + args.samples):
        prepare()
        start = time.perf_counter()
        payload = request()
        elapsed = (time.perf_counter() - start) * 1000
        digests.add(hashlib.sha256(payload).hexdigest())
        if iteration >= args.warmups:
            durations.append(elapsed)
    if len(digests) != 1:
        raise RuntimeError("History response changed between samples")
    prepare()
    # The authenticated HTTP stack also opens connections in executor threads.
    # Capture at the cursor boundary so those queries are counted as well.
    statements: list[str] = []
    execute = CursorWrapper.execute

    def capture(cursor: Any, sql: str, params: Any = None) -> Any:
        statements.append(sql)
        return execute(cursor, sql, params)

    with patch.object(CursorWrapper, "execute", capture):
        request()
    prepare()
    gc.collect()
    tracemalloc.start()
    request()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(args.samples * 0.95) - 1], 3),
        "query_count": len(statements),
        "answer_body_selects": sum(
            '"harness_message"."content"' in sql for sql in statements
        ),
        "peak_python_allocated_bytes": peak,
        "response_bytes": len(payload),
        "response_sha256": digests.pop(),
        "samples_ms": [round(value, 3) for value in durations],
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Create deterministic run histories and exercise their authenticated REST API."""
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.test import Client

    from apps.accounts.models import APIKey, APIKeyPermission
    from apps.harness.models import HarnessMessage, HarnessSession
    from apps.organizations.models import Membership, Organization
    from apps.runners.models import Runner, Workspace
    from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
    from common.utils import generate_api_token, hash_token

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    org = Organization.objects.create(name="Synthetic history", slug="history-bench")
    owner = get_user_model().objects.create_user(email="history@example.invalid")
    Membership.objects.create(user=owner, organization=org)
    runner = Runner.objects.create(organization=org, api_token_hash="synthetic")
    workspace = Workspace.objects.create(runner=runner, created_by=owner)
    token = generate_api_token()
    APIKey.objects.create(
        user=owner,
        name="Disposable history benchmark key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))
    results = []
    for count in sorted(set(args.runs)):
        task = ScheduledTask.objects.create(
            id=_identity(f"task/{count}"),
            organization=org,
            owner=owner,
            workspace=workspace,
            name="Daily review",
            prompt="Review the source files",
            recurrence="daily",
            local_time="09:00",
            next_run_at=FIXTURE_TIME,
        )
        for index in range(count):
            session = HarnessSession.objects.create(
                id=_identity(f"session/{count}/{index}"),
                organization_id=org.id,
                workspace=workspace,
                harness_id=("native", "claude", "codex")[index % 3],
                engine_state={"fixture": "private runtime metadata"},
            )
            completed_at = FIXTURE_TIME + timedelta(days=index, minutes=1)
            failed = index % 7 == 0
            message = HarnessMessage.objects.create(
                id=_identity(f"message/{count}/{index}"),
                session=session,
                role="assistant",
                finish="error" if failed else "stop",
                error="Synthetic provider failure" if failed else "",
                completed_at=completed_at,
            )
            ScheduledTaskRun.objects.create(
                id=_identity(f"run/{count}/{index}"),
                scheduled_task=task,
                session=session,
                assistant_message=message,
                scheduled_for=FIXTURE_TIME + timedelta(days=index),
                status="error" if failed else "succeeded",
                finished_at=completed_at,
                error=message.error,
                configuration_snapshot={"prompt": "Review the source files"},
            )

        def request(
            task_id: uuid.UUID = task.id, expected_count: int = count
        ) -> bytes:
            response = client.get(f"/api/v1/scheduled-tasks/{task_id}/runs/")
            if response.status_code != 200 or len(response.json()) != expected_count:
                raise RuntimeError(f"History failed: HTTP {response.status_code}")
            return response.content

        repair_id = _identity(f"run/{count}/0")

        def prepare_repair(run_id: uuid.UUID = repair_id) -> None:
            ScheduledTaskRun.objects.filter(pk=run_id).update(
                status="running", finished_at=None, error=""
            )

        for size in args.answer_kib:
            HarnessMessage.objects.filter(
                session__scheduled_task_runs__scheduled_task=task
            ).update(content="x" * (size * 1024))
            for repair in (False, True):
                results.append(
                    {
                        "runs": count,
                        "answer_kib": size,
                        "repair_completed_run": repair,
                        **_measure(
                            request,
                            prepare_repair if repair else lambda: None,
                            args,
                        ),
                    }
                )
    return {
        "benchmark": "authenticated scheduled-run history REST on temporary SQLite",
        "samples": args.samples,
        "warmups": args.warmups,
        "results": results,
        "notes": [
            "Latency includes authentication, service completion repair and JSON.",
            "Repair reset, SQL capture and tracemalloc run outside timed requests.",
            "One completed error run is repaired per request in repair cases.",
            "Python allocation peaks are not RSS; no production/network timings.",
        ],
    }


def main() -> None:
    """Set an isolated database before importing Django settings."""
    args = _arguments()
    with tempfile.TemporaryDirectory(prefix="opencuria-history-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-history-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(args.backend_root.resolve()))
        sys.stdout.write(json.dumps(_run(args), indent=2) + "\n")


if __name__ == "__main__":
    main()
