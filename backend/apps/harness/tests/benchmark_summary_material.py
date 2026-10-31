"""Compare complete summary-material service calls on disposable pytest data.

From backend/:
    SUMMARY_BASELINE_REF=535185f .venv/bin/pytest -q -s \
        apps/harness/tests/benchmark_summary_material.py

The original repository/service modules are loaded from Git, without changing
the checkout. Timings exclude setup, instrumentation and provider/network I/O.
"""

from __future__ import annotations

import gc
import math
import os
import random
import statistics
import subprocess
import sys
import time
import tracemalloc
import types
from pathlib import Path
from typing import Any

import pytest
import structlog
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
from apps.harness.session_metadata_service import SessionMetadataService

log = structlog.get_logger(__name__)


async def _drop_event(*args: Any) -> None:
    """Summary-material reads must never emit events or invoke providers."""
    raise AssertionError("Unexpected event")


def _baseline_service(
    ref: str, monkeypatch: pytest.MonkeyPatch
) -> SessionMetadataService:
    """Load both original modules, keeping their repository dependency paired."""
    repo = Path(__file__).resolve().parents[4]
    modules = []
    for name in ("repositories", "session_metadata_service"):
        path = f"backend/apps/harness/{name}.py"
        source = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        module = types.ModuleType(f"apps.harness._summary_benchmark_{name}")
        module.__package__ = "apps.harness"
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(compile(source, path, "exec"), module.__dict__)
        modules.append(module)
    return modules[1].SessionMetadataService(
        emit=_drop_event,
        conversations_changed=_drop_event,
        messages=modules[0].HarnessMessageRepository,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "turns,body_bytes",
    [(1, 4096), (10, 4096), (50, 131072)],
)
def test_summary_material_benchmark(
    harness_workspace: Any,
    turns: int,
    body_bytes: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read full user requests and closing replies from a representative history."""
    workspace = harness_workspace
    session = HarnessSession.objects.create(
        workspace=workspace, organization_id=workspace.runner.organization_id
    )
    messages = HarnessMessage.objects.bulk_create(
        [
            HarnessMessage(
                session=session,
                position=index,
                role="user" if index % 2 == 0 else "assistant",
                content=(
                    "Fix the login."
                    if index % 2 == 0
                    else "progress " * (body_bytes // 8)
                ),
                engine_meta={"provider_metadata": "x" * 4096},
            )
            for index in range(turns * 2)
        ]
    )
    HarnessPart.objects.bulk_create(
        [
            HarnessPart(
                message=message,
                position=index,
                type=kind,
                state="completed",
                output=output,
            )
            for message in messages[1::2]
            for index, (kind, output) in enumerate(
                [
                    ("text", "progress " * (body_bytes // 8)),
                    ("tool", "private " * (body_bytes // 8)),
                    ("text", "Fixed login."),
                ]
            )
        ]
    )
    ref = os.getenv("SUMMARY_BASELINE_REF", "535185f")
    services = [
        _baseline_service(ref, monkeypatch),
        SessionMetadataService(emit=_drop_event, conversations_changed=_drop_event),
    ]
    expected = "\n\n".join(
        ["User:\nFix the login.", "Agent's final reply:\nFixed login."] * turns
    )

    def execute(index: int) -> str:
        return services[index].summary_material(session.id, messages[-1].id)

    for _ in range(3):
        assert execute(0) == execute(1) == expected
    samples = 40
    timings: list[list[float]] = [[], []]
    order_source = random.Random(20261030)
    for _ in range(samples):
        order = [0, 1]
        order_source.shuffle(order)
        for index in order:
            start = time.perf_counter()
            result = execute(index)
            timings[index].append(1000 * (time.perf_counter() - start))
            assert result == expected
    metrics = []
    for index in range(2):
        with CaptureQueriesContext(connection) as queries:
            assert execute(index) == expected
        gc.collect()
        tracemalloc.start()
        assert execute(index) == expected
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        ordered = sorted(timings[index])
        metrics.append(
            {
                "queries": len(queries),
                "p50_ms": round(statistics.median(ordered), 3),
                "p95_ms": round(ordered[math.ceil(samples * 0.95) - 1], 3),
                "peak_python_kib": round(peak / 1024, 1),
            }
        )
    log.info(
        "summary_material_benchmark",
        database=connection.vendor,
        baseline_ref=ref,
        turns=turns,
        body_bytes=body_bytes,
        samples=samples,
        baseline=metrics[0],
        optimized=metrics[1],
    )
