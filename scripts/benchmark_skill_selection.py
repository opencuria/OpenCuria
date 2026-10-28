"""Measure skill selection on disposable, file-backed SQLite.

Run with ``backend/.venv/bin/python scripts/benchmark_skill_selection.py``.
Use ``--baseline-ref`` to alternate baseline/current services against identical
fixtures in one process. Provider, network and frontend time are excluded.
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
import subprocess
import sys
import tempfile
import time
import tracemalloc
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _identity(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/skills/{name}")


def _baseline_service(revision: str) -> Any:
    """Load only the old service/repository, sharing unchanged Django models."""
    namespaces = []
    for module in ("repositories", "services"):
        path = f"backend/apps/skills/{module}.py"
        source = subprocess.check_output(
            ["git", "show", f"{revision}:{path}"], cwd=REPO, text=True
        )
        namespace = {
            "__name__": f"apps.skills._benchmark_{module}",
            "__package__": "apps.skills",
        }
        exec(compile(source, f"{revision}:{path}", "exec"), namespace)  # noqa: S102
        namespaces.append(namespace)
    service = namespaces[1]["SkillService"]()
    service.skills = namespaces[0]["SkillRepository"]
    return service


def _measure(
    operations: dict[str, Callable[[], list[str]]], args: argparse.Namespace
) -> dict[str, Any]:
    """Alternate timings; capture queries and allocation peaks separately."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    timings: dict[str, list[float]] = {name: [] for name in operations}
    digests: set[str] = set()
    for iteration in range(args.warmups + args.samples):
        names = list(operations)
        if iteration % 2:
            names.reverse()
        for name in names:
            start = time.perf_counter()
            payload = operations[name]()
            elapsed = (time.perf_counter() - start) * 1000
            digests.add(hashlib.sha256(json.dumps(payload).encode()).hexdigest())
            if iteration >= args.warmups:
                timings[name].append(elapsed)
    if len(digests) != 1:
        raise RuntimeError("Skill selection output differs between samples/versions")
    results = {}
    for name, operation in operations.items():
        connection.queries_log.clear()
        with CaptureQueriesContext(connection) as queries:
            payload = operation()
        with connection.cursor() as cursor:
            query_plans = []
            for query in queries:
                cursor.execute("EXPLAIN QUERY PLAN " + query["sql"])
                query_plans.append([row[3] for row in cursor.fetchall()])
        gc.collect()
        tracemalloc.start()
        operation()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        durations = timings[name]
        results[name] = {
            "p50_ms": round(statistics.median(durations), 3),
            "p95_ms": round(sorted(durations)[math.ceil(args.samples * 0.95) - 1], 3),
            "query_count": len(queries),
            "query_plans": query_plans,
            "peak_python_allocated_bytes": peak,
            "output_items": len(payload),
            "output_sha256": next(iter(digests)),
            "samples_ms": [round(value, 3) for value in durations],
        }
    return results


def _run(args: argparse.Namespace) -> dict[str, Any]:
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import connection

    from apps.organizations.models import Organization
    from apps.skills.models import Skill
    from apps.skills.services import SkillService

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    org = Organization.objects.create(name="Skills benchmark", slug="skills-bench")
    owner = get_user_model().objects.create_user(email="skills@example.invalid")
    services = {"current": SkillService()}
    if args.baseline_ref:
        services = {"baseline": _baseline_service(args.baseline_ref), **services}
    scope = {"user_id": owner.id, "organization_id": org.id}
    results = []
    created = 0
    for count in sorted(set(args.catalogue_sizes)):
        Skill.objects.bulk_create(
            [
                Skill(
                    id=_identity(f"skill/{index}"),
                    name=f"Skill {index:05}",
                    body=f"Follow convention {index}.\n" + "Synthetic guidance. " * 100,
                    user=owner if index % 2 else None,
                    organization=org if index % 2 == 0 else None,
                    created_by=owner,
                )
                for index in range(created, count)
            ]
        )
        created = count
        selected = [
            str(_identity(f"skill/{index}"))
            for index in reversed(range(min(count, args.selected)))
        ]

        def resolve(service: Any, selection: list[str] = selected) -> list[str]:
            return service.resolve_bodies(selection, **scope)

        def inherited(service: Any, selection: list[str] = selected) -> list[str]:
            available = service.filter_available_ids(selection, **scope)
            return service.resolve_bodies(available, **scope)

        def available(service: Any, selection: list[str] = selected) -> list[str]:
            return service.filter_available_ids(selection, **scope)

        for path, operation in (
            ("resolve_bodies", resolve),
            ("filter_available_ids", available),
            ("inherited_selection", inherited),
        ):
            results.append(
                {
                    "catalogue_size": count,
                    "selected": len(selected),
                    "path": path,
                    "measurements": _measure(
                        {
                            name: lambda service=service, operation=operation: (
                                operation(service)
                            )
                            for name, service in services.items()
                        },
                        args,
                    ),
                }
            )
    return {
        "benchmark": "skill selection, disposable file-backed SQLite",
        "python": platform.python_version(),
        "django": django.get_version(),
        "sqlite": connection.Database.sqlite_version,
        "baseline_ref": args.baseline_ref,
        "samples": args.samples,
        "warmups": args.warmups,
        "results": results,
        "notes": [
            "Real service and ORM queries; no mocks or simulated I/O delays.",
            "Inherited selection filters persisted IDs then resolves prompt bodies.",
            "Personal and organization skills share the same visible catalogue.",
            "Timing excludes query capture, output hashing and allocation tracing.",
            "Allocation peaks are temporary Python allocations, not process RSS.",
            "Warm SQLite timings are not production database or chat latency SLOs.",
        ],
    }


def main() -> None:
    """Configure a disposable database before importing application code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalogue-sizes", type=int, nargs="+", default=[0, 100, 1000, 10000]
    )
    parser.add_argument("--selected", type=int, default=3)
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        min(args.catalogue_sizes) < 0
        or args.selected < 0
        or args.samples < 2
        or args.warmups < 1
    ):
        parser.error("sizes/selected >= 0, samples >= 2 and warmups >= 1 required")
    with tempfile.TemporaryDirectory(prefix="opencuria-skills-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-skills-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(REPO / "backend"))
        result = json.dumps(_run(args), indent=2) + "\n"
        if args.output:
            args.output.write_text(result)
        else:
            sys.stdout.write(result)


if __name__ == "__main__":
    main()
