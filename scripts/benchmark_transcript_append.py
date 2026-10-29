"""Measure encrypted transcript flushes on disposable database fixtures.

Run with ``backend/.venv/bin/python scripts/benchmark_transcript_append.py``.
``--baseline-ref`` alternates old/current repositories against identical data.
No providers, runners, frontend or external services are contacted.
"""

from __future__ import annotations

import argparse
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
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _baseline_repository(revision: str) -> Any:
    """Load the old repository with the same unchanged models and encryption."""
    path = "backend/apps/harness/engines/repositories.py"
    source = subprocess.check_output(
        ["git", "show", f"{revision}:{path}"], cwd=REPO, text=True
    )
    namespace = {
        "__name__": "apps.harness.engines._benchmark_repositories",
        "__package__": "apps.harness.engines",
    }
    exec(compile(source, f"{revision}:{path}", "exec"), namespace)  # noqa: S102
    return namespace["HarnessEngineRepository"]


def _measure(
    repositories: dict[str, Any], session: Any, size: int, mode: str, samples: int
) -> dict[str, Any]:
    """Time transactions separately from reset/seeding, SQL and allocation tracing."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.harness.models import HarnessTranscriptBatch
    from common.utils import decrypt_value

    entries = [
        {
            "uuid": f"entry-{index}",
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "x" * 2048}]},
        }
        for index in range(size)
    ]
    kwargs = {
        "external_session_id": "synthetic-sdk-session",
        "subpath": "",
        "entries": entries,
    }
    timings: dict[str, list[float]] = {name: [] for name in repositories}
    seed_repository = next(iter(repositories.values()))
    seeded = {"fresh": 0, "replay": size, "mixed": size // 2}[mode]

    def prepare() -> None:
        HarnessTranscriptBatch.objects.filter(session=session).delete()
        if seeded:
            seed_repository.append_transcript_entries(
                session.id, **{**kwargs, "entries": entries[:seeded]}
            )

    def signature(rows: list[Any]) -> str:
        payload = [
            (row.position, json.loads(decrypt_value(row.payload_encrypted)))
            for row in rows
        ]
        assert payload == list(enumerate(entries))
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    for iteration in range(samples + 3):
        names = list(repositories)
        if iteration % 2:
            names.reverse()
        for name in names:
            prepare()
            start = time.perf_counter()
            rows = repositories[name].append_transcript_entries(session.id, **kwargs)
            elapsed = 1000 * (time.perf_counter() - start)
            assert [row.position for row in rows] == list(range(size))
            if iteration >= 3:
                timings[name].append(elapsed)

    results = {}
    digests = set()
    for name, repository in repositories.items():
        prepare()
        connection.queries_log.clear()
        with CaptureQueriesContext(connection) as queries:
            rows = repository.append_transcript_entries(session.id, **kwargs)
        digest = signature(rows)
        digests.add(digest)
        # Trace a separate call; do not include fixture seeding or decryptions.
        prepare()
        gc.collect()
        tracemalloc.start()
        try:
            rows = repository.append_transcript_entries(session.id, **kwargs)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert signature(rows) == digest
        durations = timings[name]
        results[name] = {
            "p50_ms": round(statistics.median(durations), 3),
            "p95_ms": round(sorted(durations)[math.ceil(samples * 0.95) - 1], 3),
            "queries": len(queries),
            "peak_python_bytes": peak,
            "output_sha256": digest,
            "samples_ms": [round(value, 3) for value in durations],
        }
    assert len(digests) == 1
    return {"entries": size, "mode": mode, "results": results}


def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Use real transactions, encryption, uniqueness constraints and ORM writes."""
    import django

    django.setup()
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import connection

    from apps.harness.engines.repositories import HarnessEngineRepository
    from apps.harness.models import HarnessSession
    from apps.organizations.models import Organization
    from apps.runners.models import Runner, Workspace

    call_command("migrate", verbosity=0)
    org = Organization.objects.create(name="Transcript benchmark", slug="transcripts")
    owner = get_user_model().objects.create_user(email="transcripts@example.invalid")
    runner = Runner.objects.create(organization=org, api_token_hash="synthetic-unused")
    workspace = Workspace.objects.create(runner=runner, created_by=owner)
    session = HarnessSession.objects.create(
        workspace=workspace,
        organization_id=org.id,
        harness_id="claude",
        external_session_id="synthetic-sdk-session",
    )
    repositories = {}
    if args.baseline_ref:
        repositories["baseline"] = _baseline_repository(args.baseline_ref)
    repositories["current"] = HarnessEngineRepository
    return {
        "database": connection.vendor,
        "baseline_ref": args.baseline_ref,
        "samples": args.samples,
        "payload_text_bytes_per_entry": 2048,
        "workloads": [
            _measure(repositories, session, size, mode, args.samples)
            for size in args.sizes
            for mode in args.modes
        ],
        "notes": [
            "DEBUG=False, three warmups, alternating implementation order.",
            "File-backed SQLite or disposable database on local PostgreSQL.",
            "Timings include canonical JSON, Fernet, SQL and transaction commit.",
            "Fixture reset/seeding, query capture and allocation tracing are untimed.",
            "No SDK bridge, provider, network or UI time; Python peaks are not RSS.",
        ],
    }


def main() -> None:
    """Configure disposable storage before loading Django; always remove it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1, 50, 200, 500])
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["fresh", "replay", "mixed"],
        default=["fresh", "replay", "mixed"],
    )
    parser.add_argument("--postgres-socket", type=Path)
    parser.add_argument("--postgres-port", type=int, default=5432)
    args = parser.parse_args()
    if args.samples < 2 or min(args.sizes) < 1:
        parser.error("samples >= 2 and sizes >= 1 are required")
    sys.path.insert(0, str(REPO / "backend"))
    with tempfile.TemporaryDirectory(prefix="oc-transcript-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "db.sqlite3"),
        )
        from django.conf import settings

        settings.DEBUG = False
        admin = None
        database = f"oc_transcript_bench_{uuid.uuid4().hex}"
        try:
            if args.postgres_socket:
                import psycopg2
                from psycopg2 import sql

                config = {
                    "USER": "postgres",
                    "HOST": str(args.postgres_socket.resolve()),
                    "PORT": args.postgres_port,
                }
                admin = psycopg2.connect(
                    dbname="postgres",
                    **{key.lower(): value for key, value in config.items()},
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
                        **config,
                    }
                }
            sys.stdout.write(json.dumps(_run(args), indent=2) + "\n")
        finally:
            from django.db import connections

            connections.close_all()
            if admin:
                with admin.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("DROP DATABASE {}").format(sql.Identifier(database))
                    )
                admin.close()


if __name__ == "__main__":
    main()
