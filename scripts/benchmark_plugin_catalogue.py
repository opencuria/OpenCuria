"""Measure plugin catalogue REST queries, latency and Python allocations.

Run with ``backend/.venv/bin/python scripts/benchmark_plugin_catalogue.py``.
Uses deterministic synthetic fixtures in a temporary SQLite database. An
optional ``--backend-root`` runs the same workload against an archived backend.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins", type=int, nargs="+", default=[0, 1, 10, 50])
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend",
    )
    args = parser.parse_args()
    if min(args.plugins) < 0 or args.samples < 2 or args.warmups < 1:
        parser.error("plugins >= 0, samples >= 2 and warmups >= 1 are required")
    return args


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/plugins/{name}")


def _measure(request: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Measure warmed requests separately from SQL and allocation tracing."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    digests: set[str] = set()
    durations: list[float] = []
    for iteration in range(args.warmups + args.samples):
        start = time.perf_counter()
        payload = request()
        elapsed = (time.perf_counter() - start) * 1000
        digests.add(
            hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        )
        if iteration >= args.warmups:
            durations.append(elapsed)
    if len(digests) != 1:
        raise RuntimeError("Catalogue response changed between samples")
    connection.queries_log.clear()
    with CaptureQueriesContext(connection) as queries:
        request()
    query_count = len(queries)
    query_ms = sum(float(query["time"]) * 1000 for query in queries)
    gc.collect()
    tracemalloc.start()
    request()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "query_count": query_count,
        "captured_sql_ms": round(query_ms, 3),
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(args.samples * 0.95) - 1], 3),
        "peak_python_allocated_bytes": peak,
        "response_sha256": digests.pop(),
        "samples_ms": [round(value, 3) for value in durations],
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.test import Client

    from apps.accounts.models import APIKey, APIKeyPermission
    from apps.credentials.models import CredentialService
    from apps.organizations.models import Membership, Organization
    from apps.plugins.models import (
        OrgPluginActivation,
        Plugin,
        PluginCredentialRequirement,
        PluginMcpServer,
        PluginSkill,
    )
    from common.utils import generate_api_token, hash_token

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    # Keep fixture size independent of seeded/global catalogue definitions.
    Plugin.objects.all().delete()
    org = Organization.objects.create(
        id=_identity("org"), name="Synthetic catalogue", slug="catalogue-bench"
    )
    foreign_org = Organization.objects.create(
        id=_identity("foreign-org"), name="Hidden catalogue", slug="hidden-bench"
    )
    user = get_user_model().objects.create_user(email="catalogue@example.invalid")
    Membership.objects.create(user=user, organization=org)
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="Disposable catalogue benchmark key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.PLUGINS_READ.value],
    )
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))
    services = [
        CredentialService.objects.create(
            id=_identity(f"service/{index}"),
            name=f"Synthetic credential service {index}",
            slug=f"catalogue-bench-{index}",
            credential_type="env",
            env_var_name=f"BENCHMARK_{index}",
        )
        for index in range(2)
    ]
    Plugin.objects.create(
        id=_identity("foreign-plugin"),
        organization=foreign_org,
        name="Hidden plugin",
        slug="hidden-plugin",
    )

    def rest() -> list[dict[str, Any]]:
        response = client.get("/api/v1/plugins/")
        if response.status_code != 200:
            raise RuntimeError(f"REST catalogue failed: HTTP {response.status_code}")
        return response.json()

    created = 0
    results = []
    for count in sorted(set(args.plugins)):
        for index in range(created, count):
            plugin = Plugin.objects.create(
                id=_identity(f"plugin/{index}"),
                organization=org if index % 2 else None,
                name=f"Synthetic plugin {index:04}",
                slug=f"catalogue-bench-{index}",
                enabled=index % 7 != 0,
                published=index % 11 != 0,
            )
            if index % 3 == 0:
                OrgPluginActivation.objects.create(organization=org, plugin=plugin)
            for component in range(2):
                PluginSkill.objects.create(
                    id=_identity(f"skill/{index}/{component}"),
                    plugin=plugin,
                    name=f"Guide {component}",
                    slug=f"guide-{component}",
                    position=1 - component,
                    body="Synthetic instructions.\n" * 100,
                )
                PluginMcpServer.objects.create(
                    id=_identity(f"server/{index}/{component}"),
                    plugin=plugin,
                    name=f"Tools {component}",
                    slug=f"tools-{component}",
                    command="synthetic-mcp",
                    args=["--example"],
                    env={"TOKEN": "{{credential.example}}"},
                )
                PluginCredentialRequirement.objects.create(
                    id=_identity(f"requirement/{index}/{component}"),
                    plugin=plugin,
                    key=f"example-{component}",
                    credential_service=services[component],
                    required=component == 0,
                )
        created = count
        Plugin.objects.update(created_at=FIXTURE_TIME, updated_at=FIXTURE_TIME)
        if len(rest()) != count:
            raise RuntimeError("Catalogue visibility or fixture count is incorrect")
        result = {"plugins": count, "rest": _measure(rest, args)}
        if count:

            def detail() -> dict[str, Any]:
                response = client.get(f"/api/v1/plugins/{_identity('plugin/0')}/")
                if response.status_code != 200:
                    raise RuntimeError(
                        f"REST detail failed: HTTP {response.status_code}"
                    )
                return response.json()

            result["detail"] = _measure(detail, args)
        results.append(result)
    return {
        "benchmark": "isolated file-backed SQLite plugin catalogue REST",
        "samples": args.samples,
        "warmups": args.warmups,
        "components_per_plugin": {"skills": 2, "mcp_servers": 2, "requirements": 2},
        "results": results,
        "notes": [
            "Authenticated Django HTTP test client, including JSON serialization.",
            "Mixed global/org, disabled/unpublished, activated and foreign fixtures.",
            "No live credentials, provider calls, runner or external services.",
            "SQL capture and Python allocation tracing are separate from timings.",
            "SQL timing has millisecond resolution; allocation peaks are not RSS.",
            "Warm local timings are not production database/network SLOs.",
        ],
    }


def main() -> None:
    """Select a disposable database before Django imports settings."""
    args = _arguments()
    with tempfile.TemporaryDirectory(prefix="opencuria-plugin-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-plugin-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(args.backend_root.resolve()))
        sys.stdout.write(json.dumps(_run(args), indent=2) + "\n")


if __name__ == "__main__":
    main()
