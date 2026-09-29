"""Compare harness parts and timeline HTTP payloads on a disposable SQLite DB.

Run with ``backend/.venv/bin/python scripts/benchmark_harness_timeline.py``.
Only deterministic synthetic fixture content is created; SQLITE_PATH always
points inside a temporary directory and the database is removed on exit.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import tempfile
import time
import tracemalloc
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=100)
    parser.add_argument("--tools", type=int, default=1000)
    parser.add_argument("--min-output-kib", type=int, default=50)
    parser.add_argument("--max-output-kib", type=int, default=100)
    args = parser.parse_args()
    if args.messages < 2 or args.tools < 1:
        parser.error("--messages must be >= 2 and --tools must be >= 1")
    if args.min_output_kib < 1 or args.max_output_kib < args.min_output_kib:
        parser.error("output sizes must be positive and max >= min")
    return args


def _isolated_environment(database: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.development",
            "SQLITE_PATH": str(database),
            "DJANGO_SECRET_KEY": "synthetic-local-benchmark-key-not-a-secret",
            }
    )
    return env


def _migrate(database: Path, env: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput", "--verbosity", "0"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        # Don't echo subprocess output: settings and environment are not benchmark data.
        raise RuntimeError(
            f"Isolated Django migrations failed (exit {result.returncode})"
        )
    if not database.is_file():
        raise RuntimeError("Migrations did not create the isolated SQLite database")


def _create_fixture(args: argparse.Namespace) -> tuple[str, str, str, uuid.UUID, int]:
    """Populate only generated fixture data and return request credentials/ids."""
    import django

    django.setup()

    from apps.accounts.models import APIKey, APIKeyPermission
    from apps.harness.models import (
        HarnessMessage,
        HarnessPart,
        HarnessSession,
    )
    from apps.harness.timeline import build_part_display
    from apps.organizations.models import Membership, MembershipRole, Organization
    from apps.runners.enums import RunnerStatus, WorkspaceStatus
    from apps.runners.models import Runner, Workspace
    from common.utils import generate_api_token, hash_token
    from django.contrib.auth import get_user_model

    suffix = uuid.uuid4().hex[:12]
    org = Organization.objects.create(
        name=f"Synthetic timeline benchmark {suffix}",
        slug=f"timeline-bench-{suffix}",
    )
    user = get_user_model().objects.create_user(
        email=f"timeline-bench-{suffix}@example.invalid",
        password=uuid.uuid4().hex,
    )
    Membership.objects.create(
        user=user, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="Synthetic benchmark runner",
        api_token_hash=hash_token(uuid.uuid4().hex),
        status=RunnerStatus.OFFLINE,
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Synthetic benchmark workspace",
        status=WorkspaceStatus.STOPPED,
        created_by=user,
    )
    session = HarnessSession.objects.create(
        workspace=workspace,
        organization_id=org.id,
        title="Synthetic benchmark conversation",
    )

    messages = []
    for index in range(args.messages):
        messages.append(
            HarnessMessage(
                session=session,
                role="user" if index % 2 == 0 else "assistant",
                content=(f"Synthetic message {index}: " + "context ") * 8,
                position=index,
                model="synthetic-model" if index % 2 else "",
            )
        )
    messages = HarnessMessage.objects.bulk_create(messages, batch_size=200)
    HarnessSession.objects.filter(pk=session.pk).update(last_message_at=session.created_at)

    part_rows: list[HarnessPart] = []
    positions: dict[int, int] = {index: 0 for index in range(len(messages))}
    for index, message in enumerate(messages):
        if message.role == "assistant":
            text = "Synthetic assistant text " * 30
            part_rows.append(
                HarnessPart(
                    message=message,
                    type="text",
                    state="completed",
                    output=text,
                    position=positions[index],
                    display={"step": None},
                )
            )
            positions[index] += 1

    old_content = ("synthetic old line\n" * 1800)[:72 * 1024]
    new_content = ("synthetic new line\n" * 1800)[:72 * 1024]
    patch_diff = (
        "--- a/synthetic.txt\n+++ b/synthetic.txt\n"
        "@@ -1,2 +1,2 @@\n-synthetic old line\n+synthetic new line\n"
    )
    for tool_index in range(args.tools):
        # Spread all tool output sizes over 50-100 KiB by default. Content is
        # synthetic and recognizable; it contains no provider, user or secret data.
        size_kib = args.min_output_kib + (
            tool_index % (args.max_output_kib - args.min_output_kib + 1)
        )
        output_size = size_kib * 1024
        output = (f"synthetic-output-{tool_index:04d}-" * 64)
        output = (output * ((output_size // len(output)) + 1))[:output_size]
        message_index = (tool_index % max(1, (args.messages + 1) // 2)) * 2 + 1
        message_index = min(message_index, len(messages) - 1)
        message = messages[message_index]
        is_patch = tool_index in (0, args.tools // 2)
        part_type = "patch" if is_patch else "tool"
        title = "Update synthetic.txt" if is_patch else f"Synthetic tool {tool_index}"
        meta: dict[str, Any] = {
            "step": tool_index,
            "attachments": [
                {
                    "url": f"https://files.example.invalid/synthetic/{tool_index}",
                    "mime_type": "text/plain",
                }
            ],
        }
        if is_patch:
            output = patch_diff
            meta.update(
                path="synthetic.txt",
                old_content=old_content,
                new_content=new_content,
            )
        display = build_part_display(
            part_type=part_type,
            title=title,
            call_id=f"synthetic-call-{tool_index}",
            input_data={"tool": "apply_patch" if is_patch else "read_file"},
            output=output,
            meta_data=meta,
        )
        part_rows.append(
            HarnessPart(
                message=message,
                type=part_type,
                state="completed",
                call_id=f"synthetic-call-{tool_index}",
                input={
                    "tool": "apply_patch" if is_patch else "read_file",
                    "arguments": {"path": "synthetic.txt", "fixture_index": tool_index},
                },
                output=output,
                title=title,
                meta=meta,
                display=display,
                position=positions[message_index],
            )
        )
        positions[message_index] += 1

    HarnessPart.objects.bulk_create(part_rows, batch_size=200)
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="Synthetic one-run benchmark key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    detail_id = HarnessPart.objects.filter(
        message__session=session, type="tool"
    ).values_list("id", flat=True).first()
    if detail_id is None:
        raise RuntimeError("Synthetic fixture did not produce a tool detail row")
    return token, str(org.id), str(session.id), detail_id, len(part_rows)


def _measure(client: Any, path: str) -> dict[str, int | float]:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    gc.collect()
    current_before, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    start = time.perf_counter()
    with CaptureQueriesContext(connection) as queries:
        response = client.get(path)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if response.status_code != 200:
            raise RuntimeError(
                f"Synthetic HTTP benchmark request failed: {response.status_code}"
            )
        payload_bytes = len(response.content)
        part_selects = [
            query["sql"].upper().split(" FROM ", 1)[0]
            for query in queries
            if 'FROM "HARNESS_PART"' in query["sql"].upper()
        ]
        part_columns_in_projection = sorted(
            column
            for column in ("INPUT", "OUTPUT", "META", "DISPLAY")
            if any(f'"HARNESS_PART"."{column}"' in sql for sql in part_selects)
        )
        part_query_sql = next(
            (
                query["sql"]
                for query in queries
                if 'FROM "HARNESS_PART"' in query["sql"].upper()
                and 'FROM "HARNESS_MESSAGE"' not in query["sql"].upper()
            ),
            "",
        )
    _, peak = tracemalloc.get_traced_memory()
    del response
    gc.collect()
    return {
        "status": 200,
        "payload_bytes": payload_bytes,
        "query_count": len(queries),
        "part_heavy_columns_selected": part_columns_in_projection,
        "part_content_projection": (
            part_query_sql.split(" FROM ", 1)[0][:1000]
            if part_query_sql
            else "not_applicable"
        ),
        "elapsed_ms": round(elapsed_ms, 2),
        "peak_python_memory_delta_bytes": max(0, peak - current_before),
    }


def main() -> None:
    args = _arguments()
    with tempfile.TemporaryDirectory(prefix="opencuria-harness-bench-") as temp_dir:
        database = Path(temp_dir) / "synthetic-benchmark.sqlite3"
        env = _isolated_environment(database)
        _migrate(database, env)
        sys.path.insert(0, str(BACKEND))
        os.environ.update(env)
        token, org_id, session_id, detail_id, part_count = _create_fixture(args)

        from django.test import Client

        client = Client(
            HTTP_X_API_KEY=token,
            HTTP_X_ORGANIZATION_ID=org_id,
        )
        tracemalloc.start()
        prefix = f"/api/v1/harness/sessions/{session_id}"
        report = {
            "benchmark": "synthetic isolated SQLite + authenticated Django HTTP test client",
            "database": "temporary SQLite file (removed on exit)",
            "fixture": {
                "messages": args.messages,
                "large_tool_parts": args.tools,
                "parts_including_text_and_patches": part_count,
                "tool_output_size_kib": [args.min_output_kib, args.max_output_kib],
                "patches": 2 if args.tools > 1 else 1,
                "attachment_urls_per_tool": 1,
            },
            "requests": {
                "parts_full": _measure(client, f"{prefix}/parts"),
                "timeline": _measure(client, f"{prefix}/timeline"),
                "single_part_detail": _measure(
                    client, f"{prefix}/parts/{detail_id}"
                ),
            },
            "notes": [
                "Response bytes are the Django test client's actual serialized HTTP bodies.",
                "Query count includes authentication, ownership, and endpoint queries.",
                "Peak memory is Python tracemalloc peak delta, not process RSS or production server memory.",
                "Synthetic results do not represent production data or production DB performance.",
            ],
        }
        tracemalloc.stop()
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
