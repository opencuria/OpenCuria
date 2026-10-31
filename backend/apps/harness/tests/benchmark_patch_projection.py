"""Compare patch projection and persistence using only disposable pytest data.

From backend/:
    .venv/bin/pytest -q -s apps/harness/tests/benchmark_patch_projection.py

The baseline helper is loaded from Git without changing the checkout. Timings
exclude fixture creation, cleanup, SQL instrumentation and allocation tracing.
"""

from __future__ import annotations

import gc
import itertools
import json
import math
import os
import random
import statistics
import subprocess
import time
import tracemalloc
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import structlog
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.harness import repositories, timeline
from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession

ROOT = Path(__file__).resolve().parents[4]
BASELINE_REF = os.environ.get(
    "PATCH_BASELINE_REF", "55739586fdc3c19672067af571fcdfe69eadbcff"
)
log = structlog.get_logger(__name__)


def _baseline() -> Callable[..., dict[str, Any]]:
    """Read the original pure projection implementation from its revision."""
    path = "backend/apps/harness/timeline.py"
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE_REF}:{path}"], cwd=ROOT, text=True
    )
    module = types.ModuleType("baseline_patch_timeline")
    exec(compile(source, f"{BASELINE_REF}:{path}", "exec"), module.__dict__)
    return module.build_part_display


def test_generated_projection_parity() -> None:
    """Compare all short line-kind sequences and deterministic mixed-hunk input."""
    original = _baseline()
    cases = 0
    for size in range(9):
        for prefixes in itertools.product(" +-", repeat=size):
            output = "@@ -10,20 +30,20 @@\n" + "".join(
                f"{prefix}line-{index}\n" for index, prefix in enumerate(prefixes)
            )
            assert timeline.build_part_display(
                part_type="patch", output=output
            ) == original(part_type="patch", output=output)
            cases += 1
    random_source = random.Random(20261031)
    tokens = [
        "--- a/f",
        "+++ b/f",
        "@@ -0,0 +1,2 @@",
        "@@ -100,2 +300,2 @@ fn",
        "@@ malformed",
        " context",
        "+added",
        "-deleted",
        "",
        " ",
        "\\ No newline at end of file",
        "unknown",
        "+++value",
        "---value",
        "+" + "ä" * 1000,
        " a\rb\x00c",
        "diff --git a/f b/f",
    ]
    for _ in range(1000):
        newline = random_source.choice(["\n", "\r\n"])
        output = newline.join(random_source.choices(tokens, k=100))
        assert timeline.build_part_display(
            part_type="patch", output=output
        ) == original(part_type="patch", output=output)
        cases += 1
    log.info("patch_projection_parity", cases=cases, baseline_ref=BASELINE_REF)


def _patch_text(changed_lines: int, leading_context: int) -> str:
    """Create a generated-file replacement, optionally after a context prefix."""
    old = [f"old generated value {index}" for index in range(changed_lines)]
    new = [f"new generated value {index}" for index in range(changed_lines)]
    hunk_lines = leading_context + changed_lines
    return (
        "--- a/generated.txt\n+++ b/generated.txt\n"
        f"@@ -1,{hunk_lines} +1,{hunk_lines} @@\n"
        + " context\n" * leading_context
        + "".join(f"-{line}\n" for line in old)
        + "".join(f"+{line}\n" for line in new)
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "changed_lines,leading_context", [(1, 3), (1000, 3), (50000, 3), (1, 100000)]
)
def test_patch_persistence_benchmark(
    harness_workspace: Any, changed_lines: int, leading_context: int
) -> None:
    """Time real repository writes and compact response serialization as well as CPU."""
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
    )
    message = HarnessMessage.objects.create(
        session=session, role="assistant", position=0
    )
    output = _patch_text(changed_lines, leading_context)
    helpers = {"baseline": _baseline(), "optimized": timeline.build_part_display}
    meta = {"path": "generated.txt", "step": 1, "tool": "edit"}
    expected = helpers["baseline"](part_type="patch", output=output, meta_data=meta)
    timings = {
        name: {stage: [] for stage in ("projection", "persistence")} for name in helpers
    }
    samples = 31
    order_source = random.Random(20261031)

    def project(helper: Callable) -> dict[str, Any]:
        return helper(part_type="patch", output=output, meta_data=meta)

    def persist(helper: Callable) -> tuple[HarnessPart, str]:
        with patch.object(repositories, "build_part_display", helper):
            part = repositories.HarnessPartRepository.create(
                message_id=message.id,
                type="patch",
                state="completed",
                output=output,
                meta=meta,
            )
        payload = timeline.timeline_part_payload(part)
        # Part IDs are intentionally different; every other response field agrees.
        payload.pop("id")
        return part, json.dumps(payload, default=str, sort_keys=True)

    expected_response = None
    for iteration in range(samples + 3):
        order = list(helpers)
        order_source.shuffle(order)
        for name in order:
            helper = helpers[name]
            start = time.perf_counter()
            result = project(helper)
            projection_ms = (time.perf_counter() - start) * 1000
            assert result == expected
            start = time.perf_counter()
            part, response = persist(helper)
            persistence_ms = (time.perf_counter() - start) * 1000
            assert part.display == expected
            part.refresh_from_db()
            assert part.output == output and part.display == expected
            if expected_response is None:
                expected_response = response
            assert response == expected_response
            HarnessPart.objects.filter(id=part.id).delete()
            if iteration >= 3:
                timings[name]["projection"].append(projection_ms)
                timings[name]["persistence"].append(persistence_ms)

    metrics = {}
    for name, helper in helpers.items():
        with CaptureQueriesContext(connection) as queries:
            part, _ = persist(helper)
        query_count = len(queries)
        HarnessPart.objects.filter(id=part.id).delete()
        stages = {}
        for stage in ("projection", "persistence"):
            gc.collect()
            tracemalloc.start()
            if stage == "projection":
                assert project(helper) == expected
                part = None
            else:
                part, _ = persist(helper)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            if part is not None:
                HarnessPart.objects.filter(id=part.id).delete()
            values = timings[name][stage]
            stages[stage] = {
                "p50_ms": round(statistics.median(values), 3),
                "p95_ms": round(sorted(values)[math.ceil(samples * 0.95) - 1], 3),
                "peak_python_allocated_bytes": peak,
                "samples_ms": [round(value, 3) for value in values],
            }
        metrics[name] = {"queries": query_count, **stages}
    record = {
        "baseline_ref": BASELINE_REF,
        "database": connection.vendor,
        "changed_lines_per_side": changed_lines,
        "leading_context": leading_context,
        "input_bytes": len(output.encode()),
        "samples": samples,
        "warmups": 3,
        "variants": metrics,
    }
    log.info("patch_persistence_benchmark", result=record)
    directory = ROOT / os.environ.get(
        "PATCH_BENCHMARK_DIR", "docs/benchmarks/patch-projection"
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"paired-{changed_lines}-{leading_context}.json").write_text(
        json.dumps(record, indent=2) + "\n"
    )
