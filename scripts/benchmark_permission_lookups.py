"""Measure pending permission reads and authenticated chat HTTP requests.

Run with ``backend/.venv/bin/python scripts/benchmark_permission_lookups.py``.
Uses a disposable SQLite database, or creates/drops a disposable database on
the local PostgreSQL server selected by --postgres-socket/--postgres-port.
No agents, runners, providers or external services are contacted.
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

BACKEND = Path(__file__).resolve().parents[1] / "backend"
FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/permissions/{name}")


def _measure(run: Callable[[], Any], samples: int) -> dict[str, Any]:
    """Separate warmed timings from query capture, hashing and allocation tracing."""
    from django.db.backends.utils import CursorWrapper

    for _ in range(3):
        run()
    elapsed = []
    for _ in range(samples):
        start = time.perf_counter()
        payload = run()
        elapsed.append(1000 * (time.perf_counter() - start))
    # Ninja may execute HTTP work on another thread/connection. Count at the
    # cursor boundary so those statements are included as well as direct reads.
    queries = []
    execute = CursorWrapper.execute

    def count_execute(cursor: Any, sql: str, params: Any = None) -> Any:
        queries.append(sql)
        return execute(cursor, sql, params)

    with patch.object(CursorWrapper, "execute", count_execute):
        run()
    gc.collect()
    tracemalloc.start()
    run()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "p50_ms": round(statistics.median(elapsed), 3),
        "p95_ms": round(sorted(elapsed)[math.ceil(samples * 0.95) - 1], 3),
        "queries": len(queries),
        "peak_python_bytes": peak,
        "response_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "samples_ms": [round(value, 3) for value in elapsed],
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Populate retained history and exercise the existing repository and REST."""
    import django

    django.setup()
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import connection
    from django.test import Client
    from django.test.utils import CaptureQueriesContext

    from apps.accounts.models import APIKey, APIKeyPermission
    from apps.harness.models import HarnessMessage, HarnessSession
    from apps.harness.permissions.models import PermissionRequest
    from apps.harness.permissions.repositories import PermissionRequestRepository
    from apps.organizations.models import Membership, Organization
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token

    call_command("migrate", verbosity=0)
    org = Organization.objects.create(id=_identity("org"), name="Bench", slug="bench")
    owner = get_user_model().objects.create_user(email="permissions@example.invalid")
    Membership.objects.create(user=owner, organization=org)
    runner = Runner.objects.create(organization=org, api_token_hash="synthetic-unused")
    sessions = []
    for index in range(50):
        workspace = Workspace.objects.create(
            id=_identity(f"workspace/{index}"),
            runner=runner,
            created_by=owner,
            name=f"Workspace {index:02}",
        )
        sessions.append(
            HarnessSession.objects.create(
                id=_identity(f"session/{index}"),
                workspace=workspace,
                organization_id=org.id,
                title=f"Conversation {index:02}",
                last_message_at=FIXTURE_TIME,
            )
        )
    root = sessions[0]
    for index in range(4):
        sessions.append(
            HarnessSession.objects.create(
                id=_identity(f"child/{index}"),
                workspace_id=root.workspace_id,
                organization_id=org.id,
                parent=root,
                agent_name="explore",
            )
        )
    HarnessSession.objects.update(created_at=FIXTURE_TIME, updated_at=FIXTURE_TIME)
    for index in range(20):
        HarnessMessage.objects.create(
            id=_identity(f"message/{index}"),
            session=root,
            position=index,
            role="user" if index % 2 == 0 else "assistant",
            content="Synthetic conversation text.",
            completed_at=FIXTURE_TIME,
        )
    HarnessMessage.objects.update(created_at=FIXTURE_TIME)
    # Half the history belongs to these sessions; half belongs to other scopes.
    # The current session also has substantial resolved history, not just new IDs.
    history_sessions = [session.id for session in sessions] + [
        _identity(f"historical-session/{index}") for index in range(len(sessions))
    ]
    for offset in range(0, args.history, 1000):
        PermissionRequest.objects.bulk_create(
            [
                PermissionRequest(
                    id=uuid.UUID(int=index + 1),
                    organization_id=org.id if index % 2 else _identity("other-org"),
                    session_id=history_sessions[index % len(history_sessions)],
                    tool="bash",
                    pattern="synthetic resolved command",
                    title="Resolved permission",
                    status="approved" if index % 3 else "rejected",
                )
                for index in range(offset, min(offset + 1000, args.history))
            ]
        )
    for index, session in enumerate(sessions):
        PermissionRequest.objects.create(
            id=_identity(f"pending/{index}"),
            organization_id=org.id,
            workspace_id=session.workspace_id,
            session_id=session.id,
            tool="bash" if index % 2 == 0 else "read",
            pattern=f"synthetic action {index:02}",
            title=f"Pending permission {index:02}",
        )
    for index in range(len(sessions)):
        PermissionRequest.objects.filter(id=_identity(f"pending/{index}")).update(
            created_at=FIXTURE_TIME + timedelta(seconds=index)
        )
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")
    token = "synthetic-benchmark-key-not-a-secret"
    APIKey.objects.create(
        user=owner,
        name="Disposable key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))

    def request(path: str) -> Any:
        response = client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f"{path}: HTTP {response.status_code}")
        return response.json()

    session_ids = [session.id for session in sessions]
    paths = {
        "pending_one": lambda: PermissionRequestRepository.list_pending_for_session(
            root.id
        ),
        "pending_tree_tool": lambda: (
            PermissionRequestRepository.list_pending_for_sessions(
                [root.id, *(session.id for session in sessions[50:])], tool="bash"
            )
        ),
        "pending_feed": lambda: PermissionRequestRepository.list_pending_for_sessions(
            session_ids
        ),
    }
    results = {}
    for name, read in paths.items():
        results[name] = _measure(
            lambda read=read: [str(row.id) for row in read()], args.samples
        )
        with CaptureQueriesContext(connection) as queries:
            read()
        with connection.cursor() as cursor:
            prefix = (
                "EXPLAIN QUERY PLAN "
                if connection.vendor == "sqlite"
                else ("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ")
            )
            cursor.execute(prefix + queries[0]["sql"])
            results[name]["plan"] = cursor.fetchall()
    results["rest_timeline"] = _measure(
        lambda: request(f"/api/v1/harness/sessions/{root.id}/timeline"), args.samples
    )
    results["rest_feed"] = _measure(
        lambda: request("/api/v1/harness/conversations/"), args.samples
    )

    def lifecycle() -> dict[str, str]:
        row = PermissionRequestRepository.create(
            organization_id=org.id,
            session_id=root.id,
            tool="bash",
            pattern="synthetic lifecycle sample",
        )
        PermissionRequestRepository.mark_resolved(row, approved=True, remember="once")
        return {"status": row.status, "remember": row.remember}

    results["create_and_resolve"] = _measure(lifecycle, args.samples)
    with connection.cursor() as cursor:
        indexes = connection.introspection.get_constraints(
            cursor, PermissionRequest._meta.db_table
        )
    index = next(
        (
            name
            for name, info in indexes.items()
            if info["columns"] == ["session_id", "status", "created_at"]
        ),
        None,
    )
    return {
        "database": connection.vendor,
        "history_rows": args.history,
        "pending_rows": len(sessions),
        "samples": args.samples,
        "lookup_index": index,
        "results": results,
        "notes": [
            "DEBUG=False; three warmups; real ORM and authenticated HTTP test client.",
            "Query plans/counts, response hashes and tracemalloc run outside timings.",
            "No network/provider/native UI time; allocation peak is not process RSS.",
        ],
    }


def main() -> None:
    """Always select disposable storage before Django setup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=int, default=100_000)
    parser.add_argument("--samples", type=int, default=51)
    parser.add_argument("--backend-root", type=Path, default=BACKEND)
    parser.add_argument("--postgres-socket", type=Path)
    parser.add_argument("--postgres-port", type=int, default=5432)
    args = parser.parse_args()
    if args.history < 0 or args.samples < 2:
        parser.error("history >= 0 and samples >= 2 are required")
    sys.path.insert(0, str(args.backend_root.resolve()))
    with tempfile.TemporaryDirectory(prefix="oc-permission-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "db.sqlite3"),
        )
        from django.conf import settings

        settings.DEBUG = False
        admin = None
        database = f"oc_permission_bench_{uuid.uuid4().hex}"
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
            sys.stdout.write(json.dumps(_run(args), indent=2, default=str) + "\n")
        finally:
            if admin:
                from django.db import connections

                connections.close_all()
                with admin.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("DROP DATABASE {}").format(sql.Identifier(database))
                    )
                admin.close()


if __name__ == "__main__":
    main()
