"""Opt-in comparison of complete notification snapshot service calls.

From backend/ (uses only pytest's disposable database):
    ACTIVITY_BASELINE_REF=ecd6666 .venv/bin/pytest -q -s \
        apps/harness/tests/benchmark_activity_snapshot.py

Both revisions read identical fixtures in interleaved order. Timings exclude
fixture creation, SQL instrumentation, tracemalloc, HTTP and network latency.
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

import pytest
import structlog
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.harness.conversation_activity_service import ConversationActivityService
from apps.harness.models import HarnessMessage, HarnessSession, QuestionRequest
from apps.harness.permissions.models import PermissionRequest
from apps.notifications.models import MobileDevice
from apps.notifications.services import NotificationService
from apps.runners.models import Workspace

logger = structlog.get_logger(__name__)


def _baseline_service(
    ref: str, monkeypatch: pytest.MonkeyPatch
) -> ConversationActivityService:
    """Load both original modules with their normal model imports and DI seam."""
    repo = Path(__file__).resolve().parents[4]
    modules = []
    for name in ("conversation_activity_repository", "conversation_activity_service"):
        path = f"backend/apps/harness/{name}.py"
        source = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        module = types.ModuleType(f"apps.harness._benchmark_{name}")
        module.__package__ = "apps.harness"
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(compile(source, path, "exec"), module.__dict__)
        modules.append(module)
    return modules[1].ConversationActivityService(
        repository=modules[0].ConversationActivityRepository
    )


@pytest.mark.django_db
@pytest.mark.parametrize("roots,depth", [(1, 0), (20, 0), (20, 1), (100, 3)])
def test_snapshot_benchmark(
    harness_workspace: Workspace,
    roots: int,
    depth: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measure real ORM/auth/serialization work, with no push provider calls."""
    workspace = harness_workspace
    org_id = workspace.runner.organization_id
    user = workspace.created_by
    sessions = []
    for index in range(roots):
        if index:
            workspace = Workspace.objects.create(
                runner=harness_workspace.runner,
                created_by=user,
                name=f"Benchmark workspace {index}",
                status="running",
            )
        sessions.append(
            HarnessSession(workspace=workspace, organization_id=org_id, status="busy")
        )
    HarnessSession.objects.bulk_create(sessions)
    HarnessMessage.objects.bulk_create(
        [
            HarnessMessage(
                session=session,
                position=position,
                role="assistant" if position % 2 == 0 else "user",
                content="x" * (65536 if position == 10 else 4096),
            )
            for session in sessions
            for position in range(11)
        ]
    )
    children = sessions
    for _ in range(depth):
        children = [
            HarnessSession(
                workspace_id=parent.workspace_id,
                organization_id=org_id,
                parent=parent,
                status="busy",
            )
            for parent in children
        ]
        HarnessSession.objects.bulk_create(children)
    if depth:
        QuestionRequest.objects.bulk_create(
            [
                QuestionRequest(
                    organization_id=org_id,
                    workspace_id=child.workspace_id,
                    session_id=child.id,
                )
                for child in children
            ]
        )
        PermissionRequest.objects.bulk_create(
            [
                PermissionRequest(
                    organization_id=org_id,
                    workspace_id=child.workspace_id,
                    session_id=child.id,
                    tool="exec",
                )
                for child in children[::2]
            ]
        )
    device = MobileDevice.objects.create(
        user=user, organization_id=org_id, platform="ios", app_id="benchmark"
    )
    ref = os.getenv("ACTIVITY_BASELINE_REF", "ecd6666")
    services = [
        NotificationService(activity=_baseline_service(ref, monkeypatch)),
        NotificationService(),
    ]

    def execute(index: int) -> dict:
        result = services[index].snapshot(user, org_id, device.id)
        result.pop("updatedAt")
        assert result["activeCount"] == roots
        return result

    for _ in range(3):
        assert execute(0) == execute(1)
    samples = 40
    timings: list[list[float]] = [[], []]
    order_source = random.Random(20261028)
    for _ in range(samples):
        order = [0, 1]
        order_source.shuffle(order)
        results = {}
        for index in order:
            start = time.perf_counter()
            results[index] = execute(index)
            timings[index].append(1000 * (time.perf_counter() - start))
        assert results[0] == results[1]
    metrics = []
    for index in range(2):
        with CaptureQueriesContext(connection) as queries:
            execute(index)
        gc.collect()
        tracemalloc.start()
        execute(index)
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
    logger.info(
        "activity_snapshot_benchmark",
        database=connection.vendor,
        baseline_ref=ref,
        roots=roots,
        depth=depth,
        samples=samples,
        baseline=metrics[0],
        optimized=metrics[1],
    )
