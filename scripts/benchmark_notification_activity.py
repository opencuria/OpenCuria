"""Measure notification metadata reads with real services and isolated SQLite.

Run with ``backend/.venv/bin/python scripts/benchmark_notification_activity.py``.
Use --backend-root with an archived backend to compare revisions. Only synthetic
rows in a disposable database are used; no provider or push service is contacted.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/activity/{name}")


def _measure(action: Callable[[], Any], samples: int) -> dict[str, Any]:
    """Separate warmed service latency, SQL counts and allocation measurements."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for _ in range(5):
        action()
    durations = []
    for _ in range(samples):
        started = time.perf_counter()
        result = action()
        durations.append((time.perf_counter() - started) * 1000)
    with CaptureQueriesContext(connection) as queries:
        action()
    gc.collect()
    tracemalloc.start()
    action()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(samples * 0.95) - 1], 3),
        "query_count": len(queries),
        "peak_python_allocated_bytes": peak,
        "output_sha256": hashlib.sha256(
            json.dumps(result, default=str, sort_keys=True).encode()
        ).hexdigest(),
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Exercise activity snapshots and completion sources without ORM mocks."""
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import connection

    from apps.harness.conversation_activity_service import ConversationActivityService
    from apps.harness.models import HarnessMessage, HarnessSession
    from apps.organizations.models import Organization
    from apps.runners.models import Runner, Workspace

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    org = Organization.objects.create(
        id=_identity("org"), name="Activity benchmark", slug="activity-bench"
    )
    owner = get_user_model().objects.create_user(email="activity@example.invalid")
    runner = Runner.objects.create(organization=org, api_token_hash="synthetic-unused")
    workspace = Workspace.objects.create(
        id=_identity("workspace"), name="Workspace", runner=runner, created_by=owner
    )
    sessions = [
        HarnessSession(
            id=_identity(f"root/{index}"),
            workspace=workspace,
            organization_id=org.id,
            status="busy",
            title=f"Conversation {index}",
            engine_state={"provider_id": "openai", "codex_tools": "a" * 64},
        )
        for index in range(args.roots)
    ]
    HarnessSession.objects.bulk_create(sessions)
    HarnessSession.objects.filter(workspace=workspace).update(updated_at=FIXTURE_TIME)
    service = ConversationActivityService()

    def active() -> list[Any]:
        return [
            (asdict(row), phase) for row, phase in service.active_rows(org.id, owner.id)
        ]

    results = []
    for body_kib in args.body_kib:
        HarnessMessage.objects.all().delete()
        messages = [
            HarnessMessage(
                id=_identity(f"answer/{index}"),
                session=session,
                role="assistant",
                content="x" * (body_kib * 1024),
            )
            for index, session in enumerate(sessions)
        ]
        HarnessMessage.objects.bulk_create(messages)

        def completion_burst(
            message_ids: tuple[uuid.UUID, ...] = tuple(row.id for row in messages),
        ) -> list[Any]:
            return [
                [asdict(row) for row in service.message_source(message_id)]
                for message_id in message_ids
            ]

        def source() -> list[Any]:
            return [asdict(row) for row in service.source(sessions[0].id)]

        results.append(
            {
                "answer_kib": body_kib,
                "active_rows": _measure(active, args.samples),
                "single_source": _measure(source, args.samples),
                "completion_sources": _measure(completion_burst, args.samples),
            }
        )
    return {
        "benchmark": "notification activity services, disposable file-backed SQLite",
        "python": platform.python_version(),
        "django": django.get_version(),
        "sqlite": connection.Database.sqlite_version,
        "roots": args.roots,
        "samples": args.samples,
        "results": results,
        "notes": [
            "Includes repository queries and immutable activity DTO construction.",
            "Five warmups; query counts and tracemalloc peaks measured separately.",
            "Bodies model empty, ordinary and long coding-agent responses.",
            "Local warm timings are not production or network latency claims.",
            "Python allocation peaks are not process RSS.",
        ],
    }


def main() -> None:
    """Choose a temporary database before importing Django settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", type=int, default=50)
    parser.add_argument("--body-kib", type=int, nargs="+", default=[0, 16, 512])
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend",
    )
    args = parser.parse_args()
    if args.roots < 1 or min(args.body_kib) < 0 or args.samples < 2:
        parser.error("roots >= 1, body-kib >= 0 and samples >= 2 are required")
    with tempfile.TemporaryDirectory(prefix="opencuria-activity-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-activity-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(args.backend_root.resolve()))
        sys.stdout.write(json.dumps(_run(args), indent=2) + "\n")


if __name__ == "__main__":
    main()
