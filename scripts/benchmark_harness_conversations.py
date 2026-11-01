"""Measure conversation-feed queries, latency and allocations on isolated SQLite.

Run with ``backend/.venv/bin/python scripts/benchmark_harness_conversations.py``.
Creates only synthetic data in a temporary database, removed on exit. REST uses
an authenticated Django HTTP test client, including JSON serialization.
Use ``--pending-gates`` to populate concurrent subagent decisions and
``--baseline-ref <commit>`` to alternate that revision's service with the
working tree against identical fixtures. Repository detail reads stay available
to the baseline; the optimized service uses only session-ID projections.
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
import types
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1] / "backend"
FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspaces", type=int, nargs="+", default=[1, 10, 50])
    parser.add_argument("--children", type=int, default=4)
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--pending-gates", action="store_true")
    parser.add_argument("--baseline-ref", help="Compare the service at this Git ref")
    args = parser.parse_args()
    if min(args.workspaces) < 1 or args.children < 0 or args.samples < 2:
        parser.error("workspaces must be positive, children >= 0, samples >= 2")
    return args


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/feed-bench/{name}")


def _measure(
    requests: dict[str, Callable[[], list[dict[str, Any]]]], samples: int
) -> dict[str, Any]:
    """Alternate warmed versions; count queries and trace allocations separately."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    elapsed: dict[str, list[float]] = {name: [] for name in requests}
    payloads = {}
    for request in requests.values():
        request()
    for sample in range(samples):
        order = list(requests)
        if sample % 2:
            order.reverse()
        for name in order:
            start = time.perf_counter()
            payloads[name] = requests[name]()
            elapsed[name].append((time.perf_counter() - start) * 1000)

    results = {}
    for name, request in requests.items():
        connection.queries_log.clear()
        with CaptureQueriesContext(connection) as queries:
            request()
        query_count = len(queries)
        gc.collect()
        tracemalloc.start()
        try:
            request()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # Normalize equivalent datetime spellings for stable payload digests.
        normalized = [
            {
                **row,
                "updated_at": row["updated_at"].replace("+00:00", "Z"),
                "last_message_at": row["last_message_at"].replace("+00:00", "Z"),
            }
            for row in payloads[name]
        ]
        results[name] = {
            "query_count": query_count,
            "p50_ms": round(statistics.median(elapsed[name]), 3),
            "p95_ms": round(sorted(elapsed[name])[math.ceil(samples * 0.95) - 1], 3),
            "peak_python_allocated_bytes": peak,
            "response_rows": len(normalized),
            "response_sha256": hashlib.sha256(
                json.dumps(normalized, sort_keys=True).encode()
            ).hexdigest(),
        }
    return results


def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Populate a disposable database and exercise REST without runners."""
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.test import Client

    from apps.accounts.models import APIKey, APIKeyPermission
    from apps.harness.conversation_service import ConversationService
    from apps.harness.models import HarnessSession, QuestionRequest
    from apps.harness.permissions.models import PermissionRequest
    from apps.organizations.models import Membership, Organization
    from apps.runners.models import Runner, Workspace
    from common.utils import generate_api_token, hash_token

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    org = Organization.objects.create(name="Synthetic feed", slug="feed-bench")
    owner = get_user_model().objects.create_user(email="feed-bench@example.invalid")
    Membership.objects.create(user=owner, organization=org)
    runner = Runner.objects.create(
        name="Offline synthetic runner",
        organization=org,
        api_token_hash=hash_token(uuid.uuid4().hex),
    )
    token = generate_api_token()
    APIKey.objects.create(
        user=owner,
        name="Disposable benchmark key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))

    services = {"after": ConversationService}
    if args.baseline_ref:
        module = types.ModuleType("apps.harness._benchmark_conversation_baseline")
        module.__package__ = "apps.harness"
        source = subprocess.check_output(
            [
                "git",
                "show",
                f"{args.baseline_ref}:backend/apps/harness/conversation_service.py",
            ],
            cwd=BACKEND.parent,
            text=True,
        )
        exec(
            compile(source, "<baseline-conversation-service>", "exec"), module.__dict__
        )
        services = {"before": module.ConversationService, **services}

    def rest(service_type: type) -> list[dict[str, Any]]:
        with patch("apps.harness.api.ConversationService", service_type):
            response = client.get("/api/v1/harness/conversations/")
        if response.status_code != 200:
            raise RuntimeError(f"REST feed failed: HTTP {response.status_code}")
        return response.json()

    results = []
    created = 0
    for count in sorted(set(args.workspaces)):
        for index in range(created, count):
            workspace = Workspace.objects.create(
                id=_identity(f"workspace/{index}"),
                name=f"Synthetic workspace {index}",
                runner=runner,
                created_by=owner,
            )
            root = HarnessSession.objects.create(
                id=_identity(f"root/{index}"),
                workspace=workspace,
                organization_id=org.id,
                title=f"Synthetic conversation {index}",
                harness_id=("native", "claude", "codex")[index % 3],
                last_message_at=FIXTURE_TIME + timedelta(seconds=index),
                engine_state={"fixture": "private-engine-state-not-in-response"},
            )
            children = [
                HarnessSession(
                    id=_identity(f"child/{index}/{child}"),
                    workspace=workspace,
                    organization_id=org.id,
                    parent=root,
                    title="Synthetic child not in feed",
                )
                for child in range(args.children)
            ]
            HarnessSession.objects.bulk_create(children)
            if args.pending_gates:
                # Concurrent subagents waiting on decisions; about 3.5 KiB of
                # question JSON per gate, with one permission per session too.
                for session in children or [root]:
                    QuestionRequest.objects.create(
                        organization_id=org.id,
                        workspace_id=workspace.id,
                        session_id=session.id,
                        questions=[
                            {
                                "question": "Review this implementation: "
                                + "context " * 500,
                                "options": [{"label": "Proceed"}, {"label": "Revise"}],
                            }
                        ],
                    )
                    PermissionRequest.objects.create(
                        organization_id=org.id,
                        workspace_id=workspace.id,
                        session_id=session.id,
                        tool="exec",
                        pattern="npm test",
                    )
        created = count
        HarnessSession.objects.update(updated_at=FIXTURE_TIME)
        measurements = _measure(
            {
                name: partial(rest, service_type)
                for name, service_type in services.items()
            },
            args.samples,
        )
        if args.baseline_ref:
            assert (
                measurements["before"]["response_sha256"]
                == measurements["after"]["response_sha256"]
            ), "Conversation feed changed"
        results.append({"workspaces": count, "rest": measurements})
    return {
        "benchmark": "isolated file-backed SQLite conversation feed",
        "samples": args.samples,
        "children_per_workspace": args.children,
        "roots_per_workspace": 1,
        "pending_gates_per_workspace": (args.children or 1)
        if args.pending_gates
        else 0,
        "results": results,
        "notes": [
            "Warm local fixtures, DEBUG=False; no external services or provider calls.",
            "REST includes authentication, ownership checks and JSON serialization.",
            "Versions alternate order on the same fixtures for timing samples.",
            "Query counts and tracemalloc peaks measured separately from timings.",
            "Allocation peak is not process RSS; timings are not production SLOs.",
        ],
    }


def main() -> None:
    """Always select a temporary database before importing Django settings."""
    args = _arguments()
    with tempfile.TemporaryDirectory(prefix="opencuria-feed-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-feed-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(BACKEND))
        report = _run(args)
        sys.stdout.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
