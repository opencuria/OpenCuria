"""Measure authenticated chat forks against a disposable SQLite database.

Run with ``backend/.venv/bin/python scripts/benchmark_harness_fork.py``.
Use ``--backend-root`` to measure an archived baseline with the same script.
No runner, provider, network traffic or existing database is used.
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


def _run(args: argparse.Namespace) -> dict[str, Any]:
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import connection
    from django.test import Client
    from django.test.utils import CaptureQueriesContext

    import apps.harness.api as harness_api
    from apps.accounts.models import APIKey, APIKeyPermission
    from apps.harness.harness_service import HarnessService
    from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
    from apps.harness.timeline import build_part_display
    from apps.organizations.models import Membership, Organization
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token

    call_command("migrate", verbosity=0)
    settings.DEBUG = False
    org = Organization.objects.create(name="Fork benchmark", slug="fork-benchmark")
    owner = get_user_model().objects.create_user(email="fork@example.invalid")
    Membership.objects.create(user=owner, organization=org)
    runner = Runner.objects.create(
        name="Offline", organization=org, api_token_hash="synthetic-unused"
    )
    workspace = Workspace.objects.create(
        name="Synthetic", runner=runner, created_by=owner
    )
    token = "synthetic-fork-benchmark-api-key"
    APIKey.objects.create(
        user=owner,
        name="Disposable benchmark key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_RUN.value],
    )
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))

    async def emit(event: str, data: dict[str, Any]) -> None:
        """Replace only the outgoing Socket.IO transport, leaving services intact."""

    service = HarnessService(emit=emit)
    harness_api._resolve_harness_service = lambda: service
    results = []
    for turns in args.turns:
        source = HarnessSession.objects.create(
            workspace=workspace, organization_id=org.id, title="Synthetic history"
        )
        messages = []
        parts = []
        for index in range(turns):
            user = HarnessMessage(
                session=source,
                position=index * 2,
                role="user",
                content=f"Question {index}",
                skill_ids=["synthetic-skill"],
            )
            assistant = HarnessMessage(
                session=source,
                position=index * 2 + 1,
                role="assistant",
                content=f"Answer {index}",
                model="synthetic-model",
                cost=0.01,
                tokens={"prompt": 100, "completion": 200, "total": 300},
                finish="stop",
            )
            messages.extend((user, assistant))
            for position, kind in enumerate(
                ("text", "tool", "reasoning", "subtask", "compaction")
            ):
                meta = {"step": index, "tail_start_id": str(user.id)}
                if kind == "subtask":
                    meta["child_session_id"] = str(uuid.uuid4())
                part = HarnessPart(
                    message=assistant,
                    type=kind,
                    position=position,
                    state="completed",
                    title=f"{kind} {index}",
                    call_id=f"call-{index}-{position}",
                    input={"tool": "read", "arguments": {"path": "/workspace/a.py"}},
                    output=(f"{kind} {index}\n" * 1024),
                    meta=meta,
                )
                part.display = build_part_display(
                    part_type=kind,
                    title=part.title,
                    call_id=part.call_id,
                    input_data=part.input,
                    output=part.output,
                    meta_data=meta,
                )
                parts.append(part)
        HarnessMessage.objects.bulk_create(messages)
        HarnessPart.objects.bulk_create(parts)
        expected_digest: str | None = None

        def fork() -> uuid.UUID:
            response = client.post(
                f"/api/v1/harness/sessions/{source.id}/fork",
                data="{}",
                content_type="application/json",
            )
            if response.status_code != 201:
                raise RuntimeError(
                    f"Fork failed: {response.status_code} {response.content}"
                )
            return uuid.UUID(response.json()["id"])

        def validate_and_remove(destination_id: uuid.UUID) -> str:
            copied = list(HarnessMessage.objects.filter(session_id=destination_id))
            assert len(copied) == len(messages)
            ranks = {str(message.id): index for index, message in enumerate(copied)}
            normalized = []
            for message in copied:
                row = {
                    key: getattr(message, key)
                    for key in (
                        "position",
                        "role",
                        "content",
                        "model",
                        "reasoning_effort",
                        "provider",
                        "skill_ids",
                        "harness_id",
                        "engine_meta",
                        "cost",
                        "tokens",
                        "finish",
                        "error",
                        "completed_at",
                        "notice_dismissed_at",
                    )
                }
                row["parts"] = []
                for part in message.parts.all():
                    meta = dict(part.meta)
                    if "tail_start_id" in meta:
                        meta["tail_start_id"] = ranks[meta["tail_start_id"]]
                    if part.type == "subtask":
                        assert meta["child_session_id"] == ""
                        assert part.display["child_session_id"] == ""
                    row["parts"].append(
                        {
                            **{
                                key: getattr(part, key)
                                for key in (
                                    "position",
                                    "type",
                                    "state",
                                    "title",
                                    "call_id",
                                    "input",
                                    "output",
                                    "display",
                                )
                            },
                            "meta": meta,
                        }
                    )
                normalized.append(row)
            assert sum(len(row["parts"]) for row in normalized) == len(parts)
            digest = hashlib.sha256(
                json.dumps(normalized, sort_keys=True, default=str).encode()
            ).hexdigest()
            if expected_digest is not None:
                assert digest == expected_digest
            HarnessSession.objects.filter(id=destination_id).delete()
            return digest

        expected_digest = validate_and_remove(fork())
        elapsed = []
        cpu = []
        for _ in range(args.samples):
            start_cpu = time.process_time()
            start = time.perf_counter()
            destination_id = fork()
            elapsed.append((time.perf_counter() - start) * 1000)
            cpu.append((time.process_time() - start_cpu) * 1000)
            validate_and_remove(destination_id)
        with CaptureQueriesContext(connection) as queries:
            destination_id = fork()
        statements = Counter(query["sql"].split()[0] for query in queries)
        query_count = len(queries)
        validate_and_remove(destination_id)
        gc.collect()
        tracemalloc.start()
        destination_id = fork()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        validate_and_remove(destination_id)
        results.append(
            {
                "turns": turns,
                "messages": len(messages),
                "parts": len(parts),
                "p50_ms": round(statistics.median(elapsed), 3),
                "p95_ms": round(sorted(elapsed)[math.ceil(args.samples * 0.95) - 1], 3),
                "cpu_p50_ms": round(statistics.median(cpu), 3),
                "sql_statements": dict(statements),
                "sql_total": query_count,
                "peak_python_bytes": peak,
                "copied_content_sha256": expected_digest,
                "latency_samples_ms": elapsed,
            }
        )
    return {
        "database": "disposable file-backed SQLite",
        "samples": args.samples,
        "python": sys.version.split()[0],
        "django": django.get_version(),
        "sqlite": connection.Database.sqlite_version,
        "results": results,
    }


def main() -> None:
    """Configure an isolated database before loading any application code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend",
    )
    parser.add_argument("--turns", type=int, nargs="+", default=[10, 50])
    parser.add_argument("--samples", type=int, default=21)
    args = parser.parse_args()
    if min(args.turns) < 1 or args.samples < 2:
        parser.error("turns must be positive and samples >= 2")
    with tempfile.TemporaryDirectory(prefix="opencuria-fork-bench-") as directory:
        os.environ.update(
            DJANGO_SETTINGS_MODULE="config.settings.development",
            SQLITE_PATH=str(Path(directory) / "benchmark.sqlite3"),
            DJANGO_SECRET_KEY="synthetic-fork-benchmark-not-a-secret",
        )
        sys.path.insert(0, str(args.backend_root.resolve()))
        report = _run(args)
        sys.stdout.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
