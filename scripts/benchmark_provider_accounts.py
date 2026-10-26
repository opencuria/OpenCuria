"""Measure provider-account REST and selection reads on disposable SQLite.

Run with ``backend/.venv/bin/python scripts/benchmark_provider_accounts.py``.
``--backend-root`` measures an archived baseline with the same fixtures.
No providers, runners, or external services are contacted.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/accounts/{name}")


def _measure(request: Callable[[], bytes], args: argparse.Namespace) -> dict[str, Any]:
    """Time complete reads; trace SQL and allocations in separate samples."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    durations = []
    digests = set()
    for iteration in range(args.warmups + args.samples):
        start = time.perf_counter()
        payload = request()
        elapsed = (time.perf_counter() - start) * 1000
        digests.add(hashlib.sha256(payload).hexdigest())
        if iteration >= args.warmups:
            durations.append(elapsed)
    if len(digests) != 1:
        raise RuntimeError("Response changed between samples")
    with CaptureQueriesContext(connection) as queries:
        request()
    query_count = len(queries)
    gc.collect()
    tracemalloc.start()
    request()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "queries": query_count,
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(args.samples * 0.95) - 1], 3),
        "peak_python_allocated_bytes": peak,
        "response_bytes": len(payload),
        "response_sha256": digests.pop(),
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.test import Client

    from apps.accounts.models import APIKey
    from apps.credentials.services import CredentialSvc
    from apps.harness.models import ProviderConnection
    from apps.harness.provider_accounts import ProviderAccountService
    from apps.organizations.models import Membership, Organization
    from common.utils import hash_token

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    user = get_user_model().objects.create_user(email="accounts@example.invalid")
    org = Organization.objects.create(
        id=_identity("org"), name="Account benchmark", slug="account-benchmark"
    )
    Membership.objects.create(user=user, organization=org, role="member")
    token = "synthetic-local-account-benchmark-key"
    APIKey.objects.create(
        user=user,
        name="Benchmark",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=["harness:providers"],
    )
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))
    service = ProviderAccountService()
    credentials = CredentialSvc()
    providers = [
        "openrouter",
        "chatgpt",
        "amazon-bedrock",
        "openai-compatible",
        "claude-agent",
    ]

    def rest() -> bytes:
        response = client.get("/api/v1/provider-config/accounts/")
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.content!r}")
        return response.content

    def selected() -> bytes:
        return json.dumps(
            {
                key: str(row.id)
                for key, row in service.selected_accounts(org.id, user.id).items()
            },
            sort_keys=True,
        ).encode()

    results = []
    created = 0
    for count in sorted(set(args.accounts)):
        for index in range(created, count):
            provider = providers[index % len(providers)]
            owner_id = user.id if index % 2 else None
            credential = credentials.create_provider_secret(
                organization_id=org.id,
                user_id=owner_id,
                provider=provider,
                name=f"Account {index:04}",
                value={"api_key": "synthetic-secret", "account_id": "synthetic-id"},
                created_by=user,
            )
            ProviderConnection.objects.create(
                id=_identity(f"account/{index}"),
                organization=org,
                user_id=owner_id,
                provider=provider,
                label=f"Account {index:04}",
                credential=credential,
                config={"models": ["synthetic-model"]},
            )
        created = count
        ProviderConnection.objects.update(
            created_at=FIXTURE_TIME, updated_at=FIXTURE_TIME
        )
        if len(json.loads(rest())) != count:
            raise RuntimeError("Incorrect account visibility/count")
        results.append(
            {
                "accounts": count,
                "rest": _measure(rest, args),
                "selected": _measure(selected, args),
            }
        )
    return {
        "benchmark": "authenticated REST and selected-account service, file SQLite",
        "samples": args.samples,
        "warmups": args.warmups,
        "results": results,
        "notes": [
            "Five providers; alternating shared and personal accounts; member caller.",
            "Steady state after first-selection initialization, no network latency.",
            "REST includes middleware, authentication, decryption and serialization.",
            "Query tracing and Python allocation peaks are separate from latency.",
            "Allocation peaks are not RSS. No production data or services are used.",
        ],
    }


def main() -> None:
    """Set up isolated settings before importing Django."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=int, nargs="+", default=[0, 1, 10, 30])
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend",
    )
    args = parser.parse_args()
    if min(args.accounts) < 0 or args.samples < 2 or args.warmups < 1:
        parser.error("accounts >= 0, samples >= 2, warmups >= 1 required")
    with tempfile.TemporaryDirectory(prefix="opencuria-account-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-local-account-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(args.backend_root.resolve()))
        sys.stdout.write(json.dumps(_run(args), indent=2) + "\n")


if __name__ == "__main__":
    main()
