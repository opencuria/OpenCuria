"""Measure real stream persistence and frontend forwarding on disposable databases.

Run with ``backend/.venv/bin/python scripts/benchmark_stream_persistence.py``.
Use ``--repo`` for an archived baseline, or ``--postgres-socket`` and
``--postgres-port`` for a local PostgreSQL cluster. PostgreSQL measurements create
and drop a unique benchmark database; SQLite uses a temporary file.
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
from collections import Counter
from pathlib import Path
from typing import Any


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/stream/{name}")


def _run(args: argparse.Namespace) -> dict[str, Any]:
    import django

    django.setup()
    from asgiref.sync import async_to_sync
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.harness.harness_service import HarnessService
    from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
    from apps.harness.timeline import append_part_display, build_part_display
    from apps.organizations.models import Organization
    from apps.runners.models import Runner, Workspace

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    org = Organization.objects.create(name="Stream benchmark", slug="stream-bench")
    owner = get_user_model().objects.create_user(email="stream@example.invalid")
    runner = Runner.objects.create(
        name="Offline", organization=org, api_token_hash="synthetic-unused"
    )
    workspace = Workspace.objects.create(
        name="Synthetic", runner=runner, created_by=owner
    )
    session = HarnessSession.objects.create(
        id=_identity("session"), workspace=workspace, organization_id=org.id
    )
    assistant = HarnessMessage.objects.create(
        id=_identity("message"), session=session, role="assistant", position=0
    )
    emitted: list[dict[str, Any]] = []

    async def emit(event: str, data: dict[str, Any]) -> None:
        # Match the injected frontend seam, without a network/provider or queries.
        emitted.append({"event": event, **data})

    service = HarnessService(emit=emit)
    results = []
    for kind in args.kinds:
        prefix = "Synthetic previous output.\n"
        size = args.prefix_kib * 1024
        initial = (prefix * math.ceil(size / len(prefix)))[:size]
        part = HarnessPart.objects.create(
            id=_identity(kind),
            message=assistant,
            type=kind,
            position=0,
            state="running",
            meta={"step": 1},
        )
        display = build_part_display(
            part_type=kind, output=initial, meta_data=part.meta
        )
        deltas = [
            f"Synthetic {kind} chunk {index:04d}.\n" for index in range(args.deltas)
        ]
        expected_display = display
        for delta in deltas:
            expected_display = append_part_display(
                expected_display, part_type=kind, delta=delta
            )
        expected_output = initial + "".join(deltas)

        def reset() -> None:
            HarnessPart.objects.filter(id=part.id).update(
                output=initial, display=display
            )
            HarnessMessage.objects.filter(id=assistant.id).update(
                content=initial if kind == "text" else ""
            )
            assistant.refresh_from_db()
            service._runs[str(session.id)] = {
                f"{kind}_part_id": str(part.id),
                "part_positions": {str(part.id): 0},
            }
            emitted.clear()

        async def stream() -> None:
            for delta in deltas:
                await service._on_runner_event(
                    session,
                    assistant,
                    {"type": "part_updated", "delta": {kind: delta}, "step": 1},
                )

        stream_sync = async_to_sync(stream)

        def verify() -> str:
            stored = HarnessPart.objects.get(id=part.id)
            message = HarnessMessage.objects.get(id=assistant.id)
            assert stored.output == expected_output
            assert stored.display == expected_display
            assert message.content == (expected_output if kind == "text" else "")
            assert assistant.content == message.content
            assert [event["delta"][kind] for event in emitted] == deltas
            assert all(event["part_id"] == str(part.id) for event in emitted)
            assert all(event["part_position"] == 0 for event in emitted)
            normalized = {
                "content": message.content,
                "output": stored.output,
                "display": stored.display,
                "events": [
                    {
                        key: value
                        for key, value in event.items()
                        if key != "workspace_id"
                    }
                    for event in emitted
                ],
            }
            return hashlib.sha256(
                json.dumps(normalized, sort_keys=True).encode()
            ).hexdigest()

        reset()
        stream_sync()
        expected_digest = verify()
        elapsed = []
        cpu_elapsed = []
        for _ in range(args.samples):
            reset()
            cpu_start = time.process_time()
            start = time.perf_counter()
            stream_sync()
            elapsed.append(1000 * (time.perf_counter() - start))
            cpu_elapsed.append(1000 * (time.process_time() - cpu_start))
            assert verify() == expected_digest

        reset()
        connection.queries_log.clear()
        with CaptureQueriesContext(connection) as queries:
            stream_sync()
        statements = Counter(query["sql"].split()[0].upper() for query in queries)
        assert verify() == expected_digest
        reset()
        gc.collect()
        tracemalloc.start()
        try:
            stream_sync()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert verify() == expected_digest
        results.append(
            {
                "kind": kind,
                "initial_output_bytes": len(initial.encode()),
                "p50_ms": round(statistics.median(elapsed), 3),
                "p95_ms": round(sorted(elapsed)[math.ceil(args.samples * 0.95) - 1], 3),
                "python_cpu_p50_ms": round(statistics.median(cpu_elapsed), 3),
                "sql_statements": dict(statements),
                "peak_python_allocated_bytes": peak,
                "persisted_and_emitted_sha256": expected_digest,
                "latencies_ms": [round(value, 3) for value in elapsed],
            }
        )
        part.delete()

    return {
        "database": connection.vendor,
        "path": "HarnessService._on_runner_event, real repositories and emit seam",
        "samples": args.samples,
        "deltas_per_sample": args.deltas,
        "prefix_kib": args.prefix_kib,
        "results": results,
        "notes": [
            "Warmed latency excludes reset, verification, SQL counting and tracing.",
            "No provider/network pacing; persistence overhead, not answer latency.",
            "Every delta is persisted and forwarded immediately; no outer transaction.",
        ],
    }


def main() -> None:
    """Prepare isolated settings before loading the target checkout's backend."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--deltas", type=int, default=100)
    parser.add_argument("--prefix-kib", type=int, default=32)
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=["text", "reasoning"],
        default=["text", "reasoning"],
    )
    parser.add_argument("--postgres-socket", type=Path)
    parser.add_argument("--postgres-port", type=int, default=5432)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 2 or args.deltas < 1 or args.prefix_kib < 1:
        parser.error("samples must be >= 2; deltas and prefix-kib must be positive")
    with tempfile.TemporaryDirectory(prefix="opencuria-stream-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            DJANGO_SECRET_KEY="synthetic-stream-benchmark-only",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
        )
        sys.path.insert(0, str(args.repo.resolve() / "backend"))
        admin = None
        database = f"stream_bench_{uuid.uuid4().hex}"
        try:
            if args.postgres_socket:
                import psycopg2
                from django.conf import settings
                from psycopg2 import sql

                admin = psycopg2.connect(
                    dbname="postgres",
                    user="postgres",
                    host=str(args.postgres_socket),
                    port=args.postgres_port,
                )
                admin.autocommit = True
                with admin.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
                    )
                settings.DATABASES = {
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": database,
                        "USER": "postgres",
                        "HOST": str(args.postgres_socket),
                        "PORT": args.postgres_port,
                    }
                }
            result = json.dumps(_run(args), indent=2) + "\n"
            if args.output:
                args.output.write_text(result)
            sys.stdout.write(result)
        finally:
            from django.db import connections

            connections.close_all()
            if admin is not None:
                with admin.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(
                            sql.Identifier(database)
                        )
                    )
                admin.close()


if __name__ == "__main__":
    main()
