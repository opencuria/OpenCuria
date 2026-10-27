"""Measure workspace plugin readiness with real services, SQL and OAuth grants.

Run with ``backend/.venv/bin/python scripts/benchmark_plugin_readiness.py``.
Each run migrates a disposable file-backed SQLite database. No network, live
credentials or existing database is used. An archived backend can be selected
with ``--backend-root`` to repeat the same workload against a baseline.
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
from pathlib import Path
from typing import Any


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/readiness/{name}")


def _measure(action: Callable[[], Any], args: argparse.Namespace) -> dict[str, Any]:
    """Keep output hashing, query capture and allocation tracing out of timings."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    durations = []
    digests = set()
    for index in range(args.warmups + args.samples):
        started = time.perf_counter()
        result = action()
        elapsed = (time.perf_counter() - started) * 1000
        digests.add(
            hashlib.sha256(
                json.dumps(result, default=str, sort_keys=True).encode()
            ).hexdigest()
        )
        if index >= args.warmups:
            durations.append(elapsed)
    if len(digests) != 1:
        raise RuntimeError("Readiness output changed between samples")
    with CaptureQueriesContext(connection) as queries:
        action()
    gc.collect()
    tracemalloc.start()
    action()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "query_count": len(queries),
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(args.samples * 0.95) - 1], 3),
        "peak_python_allocated_bytes": peak,
        "output_sha256": digests.pop(),
        "samples_ms": [round(value, 3) for value in durations],
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import connection

    from apps.credentials.models import (
        Credential,
        CredentialService,
        McpOAuthClientRegistration,
    )
    from apps.credentials.services import CredentialSvc
    from apps.organizations.models import Organization
    from apps.plugins.models import (
        OrgPluginActivation,
        Plugin,
        PluginCredentialRequirement,
        PluginMcpServer,
        WorkspacePluginActivation,
    )
    from apps.plugins.services import PluginService
    from apps.runners.models import Runner, Workspace
    from common.utils import encrypt_value

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    Plugin.objects.all().delete()
    org = Organization.objects.create(
        id=_identity("org"), name="Readiness", slug="readiness"
    )
    owner = get_user_model().objects.create_user(email="readiness@example.invalid")
    runner = Runner.objects.create(organization=org, api_token_hash="synthetic-unused")
    workspace = Workspace.objects.create(
        id=_identity("workspace"), runner=runner, created_by=owner
    )
    token = CredentialService.objects.create(
        id=_identity("token"),
        name="Token",
        slug="readiness-token",
        credential_type="env",
    )
    endpoint = "https://mcp.example.invalid/mcp"
    oauth = CredentialService.objects.create(
        id=_identity("oauth"),
        name="OAuth",
        slug="readiness-oauth",
        credential_type="mcp_oauth",
        oauth_server_url=endpoint,
    )
    registration = McpOAuthClientRegistration.objects.create(
        id=_identity("registration"),
        server_url=endpoint,
        callback_url="https://example.invalid/callback",
        issuer="https://example.invalid",
        client_id="synthetic-client",
        authorization_endpoint="https://example.invalid/authorize",
        token_endpoint="https://example.invalid/token",
    )
    workspace.credentials.add(
        Credential.objects.create(
            service=token, organization=org, encrypted_value=encrypt_value("synthetic")
        ),
        Credential.objects.create(
            service=oauth,
            user=owner,
            oauth_status="connected",
            oauth_server_url=endpoint,
            oauth_resource=endpoint,
            oauth_registration=registration,
            encrypted_value=encrypt_value(
                json.dumps(
                    {
                        "access_token": "synthetic-oauth-token",
                        "refresh_token": "synthetic-refresh-token",
                        "server_url": endpoint,
                        "resource": endpoint,
                        "service_id": str(oauth.id),
                        "registration_id": str(registration.id),
                    }
                )
            ),
        ),
    )
    results = []
    for kind in ("ordinary", "oauth"):
        Plugin.objects.all().delete()
        created = 0
        plugin_ids = []
        for count in sorted(set(args.plugins)):
            for index in range(created, count):
                plugin = Plugin.objects.create(
                    id=_identity(f"plugin/{kind}/{index}"),
                    organization=org if index % 2 else None,
                    name=f"Plugin {index:04}",
                    slug=f"readiness-{kind}-{index}",
                )
                plugin_ids.append(plugin.id)
                OrgPluginActivation.objects.create(organization=org, plugin=plugin)
                if index % 2:
                    WorkspacePluginActivation.objects.create(
                        workspace=workspace, plugin=plugin
                    )
                PluginCredentialRequirement.objects.create(
                    plugin=plugin,
                    key="token",
                    credential_service=oauth if kind == "oauth" else token,
                )
                if kind == "oauth":
                    # Two servers sharing one binding exercise the all-servers rule.
                    for server in range(2):
                        PluginMcpServer.objects.create(
                            plugin=plugin,
                            name=f"Tools {server}",
                            slug=f"tools-{server}",
                            auth_type="oauth",
                            oauth_requirement_key="token",
                            transport="streamable_http",
                            url=endpoint,
                        )
            created = count

            def listing(expected: int = count) -> list[dict]:
                rows = PluginService().list_workspace_plugins(
                    workspace=workspace, org_id=org.id
                )
                if len(rows) != expected or any(not row["ready"] for row in rows):
                    raise RuntimeError("Unexpected workspace readiness")
                return rows

            def validation(selected: tuple = tuple(reversed(plugin_ids))) -> list[dict]:
                gaps = PluginService().validate_workspace_plugin_selection(
                    plugin_ids=list(selected),
                    credentials=CredentialSvc().list_workspace_credentials(
                        workspace.id
                    ),
                    owner=owner,
                    org_id=org.id,
                )
                if gaps:
                    raise RuntimeError("Unexpected selection gaps")
                return gaps

            results.append(
                {
                    "kind": kind,
                    "plugins": count,
                    "list_workspace_plugins": _measure(listing, args),
                    "validate_workspace_plugin_selection": _measure(validation, args),
                }
            )
    return {
        "benchmark": "workspace plugin readiness, disposable file-backed SQLite",
        "python": platform.python_version(),
        "django": django.get_version(),
        "sqlite": connection.Database.sqlite_version,
        "debug": settings.DEBUG,
        "samples": args.samples,
        "warmups": args.warmups,
        "results": results,
        "notes": [
            "Includes real repository reads and OAuth grant validation/decryption.",
            "Both scopes include loading attached credentials; no ORM mocks.",
            "Query capture, output hashing and allocation tracing are not timed.",
            "Local warm latency is not a production database or agent latency SLO.",
            "Allocation peaks are temporary Python allocations, not RSS.",
        ],
    }


def main() -> None:
    """Select isolated settings and a fresh database before importing Django."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins", type=int, nargs="+", default=[0, 1, 5, 15, 50])
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend",
    )
    args = parser.parse_args()
    if min(args.plugins) < 0 or args.samples < 2 or args.warmups < 1:
        parser.error("plugins >= 0, samples >= 2 and warmups >= 1 are required")
    with tempfile.TemporaryDirectory(prefix="opencuria-readiness-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-readiness-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(args.backend_root.resolve()))
        result = json.dumps(_run(args), indent=2) + "\n"
        if args.output:
            args.output.write_text(result)
        else:
            sys.stdout.write(result)


if __name__ == "__main__":
    main()
