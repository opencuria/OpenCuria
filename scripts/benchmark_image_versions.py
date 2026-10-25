"""Measure captured-image REST reads using a disposable SQLite database.

Run with ``backend/.venv/bin/python scripts/benchmark_image_versions.py``.
Use ``--backend-root`` to measure an archived baseline with identical fixtures.
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
from unittest.mock import patch

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/image-bench/{name}")


def _measure(request: Callable[[], bytes], args: argparse.Namespace) -> dict[str, Any]:
    """Time complete responses; trace SQL and allocations separately."""
    from django.db.backends.utils import CursorWrapper

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
    queries: list[float] = []
    original_execute = CursorWrapper.execute

    def execute(cursor: Any, sql: str, params: Any = None) -> Any:
        start = time.perf_counter()
        try:
            return original_execute(cursor, sql, params)
        finally:
            queries.append((time.perf_counter() - start) * 1000)

    # Cover middleware/adapter worker threads as well as the calling thread.
    with patch.object(CursorWrapper, "execute", execute):
        request()
    gc.collect()
    tracemalloc.start()
    request()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "queries": len(queries),
        "sql_execute_ms": round(sum(queries), 3),
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(args.samples * 0.95) - 1], 3),
        "peak_python_allocated_bytes": peak,
        "response_bytes": len(payload),
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

    from apps.accounts.models import APIKey
    from apps.organizations.models import Membership, Organization
    from apps.runners.models import CapturedImage, ImageInstance, Runner, Workspace
    from common.utils import hash_token

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    user = get_user_model().objects.create_user(email="images@example.invalid")
    org = Organization.objects.create(
        id=_identity("org"), name="Image benchmark", slug="image-benchmark"
    )
    Membership.objects.create(user=user, organization=org)
    runner = Runner.objects.create(
        id=_identity("runner"), name="Runner", organization=org, api_token_hash="unused"
    )
    token = "synthetic-local-image-benchmark-key"
    APIKey.objects.create(
        user=user,
        name="Benchmark",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=["images:read"],
    )
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))

    def rest(path: str) -> bytes:
        response = client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.content!r}")
        return response.content

    results = []
    created = 0
    for count in sorted(set(args.images)):
        for index in range(created, count):
            line = CapturedImage.objects.create(
                id=_identity(f"image/{index}"),
                name=f"Image {index:04}",
                runner=runner,
                organization=org,
                created_by=user,
            )
            versions = []
            for number, status in enumerate(
                ["ready", "ready", "ready", "failed", "capturing"], 1
            ):
                versions.append(
                    ImageInstance.objects.create(
                        id=_identity(f"version/{index}/{number}"),
                        name=line.name,
                        runner=runner,
                        captured_image=line,
                        origin_type="workspace_capture",
                        generation=number,
                        status=status,
                        created_by=user,
                        size_bytes=number * 100,
                        runner_ref=f"image-{index}-{number}"
                        if status == "ready"
                        else "",
                    )
                )
            Workspace.objects.create(
                id=_identity(f"workspace/{index}"),
                name=f"Workspace {index:04}",
                runner=runner,
                created_by=user,
                status="stopped",
                base_image_instance=versions[0],
            )
        created = count
        CapturedImage.objects.update(created_at=FIXTURE_TIME)
        ImageInstance.objects.update(created_at=FIXTURE_TIME)
        if len(json.loads(rest("/api/v1/captured-images/"))) != count:
            raise RuntimeError("Incorrect fixture visibility/count")
        row = {
            "images": count,
            "versions": count * 5,
            "list": _measure(lambda: rest("/api/v1/captured-images/"), args),
        }
        if count:
            row["detail"] = _measure(
                lambda: rest(f"/api/v1/captured-images/{_identity('image/0')}/"), args
            )
        results.append(row)
    return {
        "benchmark": "authenticated captured-image REST, disposable file-backed SQLite",
        "samples": args.samples,
        "warmups": args.warmups,
        "versions_per_image": 5,
        "workspace_pins_per_image": 1,
        "results": results,
        "notes": [
            "Complete Django requests, including middleware and JSON serialization.",
            "SQL and allocation tracing run separately from latency samples.",
            "Allocation peaks are not RSS; SQL timing excludes fetching and ORM work.",
            "No runner, external services, production data or network latency.",
        ],
    }


def main() -> None:
    """Set the disposable database path before settings are imported."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=int, nargs="+", default=[0, 1, 10, 30])
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend",
    )
    args = parser.parse_args()
    if min(args.images) < 0 or args.samples < 2 or args.warmups < 1:
        parser.error("images >= 0, samples >= 2 and warmups >= 1 are required")
    with tempfile.TemporaryDirectory(prefix="opencuria-image-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-local-image-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(args.backend_root.resolve()))
        sys.stdout.write(json.dumps(_run(args), indent=2) + "\n")


if __name__ == "__main__":
    main()
