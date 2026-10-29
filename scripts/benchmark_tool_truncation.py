"""Measure tool clipping through real MCP/registry dispatch, without external I/O.

Run with ``backend/.venv/bin/python scripts/benchmark_tool_truncation.py``.
``--baseline-ref`` alternates the archived/current helper in the same process.
The spill sink verifies full bytes but excludes network and disk latency.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.util
import json
import logging
import math
import os
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def _baseline(revision: str) -> Any:
    path = "backend/apps/harness/tools/truncate.py"
    source = subprocess.check_output(
        ["git", "show", f"{revision}:{path}"], cwd=REPO, text=True
    )
    name = "_benchmark_baseline_truncate"
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(source, f"{revision}:{path}", "exec"), module.__dict__)  # noqa: S102
    return module.truncate_tool_output


async def _measure(
    text: str, direction: str, helpers: dict[str, Any], samples: int
) -> dict[str, Any]:
    from mcp import types

    from apps.harness.mcp_client.connection import McpTool, normalize_call_result
    from apps.harness.tools import base

    response = types.CallToolResult(content=[types.TextContent(type="text", text=text)])
    expected_bytes = text.encode("utf-8")

    class Connection:
        """Supply a synthetic response through the normal MCP normalization."""

        async def call_tool(self, name: str, args: dict) -> base.ToolResult:
            return normalize_call_result(response)

    class SpillSink:
        """Check exact recovery bytes without retaining each new spill copy."""

        writes = 0

        async def write_file(self, path: str, content: bytes) -> None:
            assert path.startswith("/workspace/.opencuria/tool-output/")
            assert content == expected_bytes
            self.writes += 1

    sink = SpillSink()
    registry = base.ToolRegistry()
    tool = McpTool(
        namespaced_name="mcp_benchmark_documents_read",
        original_name="read",
        description="Synthetic document retrieval",
        input_schema={"type": "object"},
        connection=Connection(),
        title_text="Read document",
    )
    tool.truncate_direction = direction
    registry.register(tool)
    ctx = base.ToolContext(
        session_id="synthetic", workspace_id="synthetic", accessor=sink
    )
    timings: dict[str, list[float]] = {name: [] for name in helpers}
    digests: set[str] = set()

    async def invoke() -> base.ToolResult:
        return await registry.execute(tool.name, {}, ctx)

    for iteration in range(samples + 5):
        names = list(helpers)
        if iteration % 2:
            names.reverse()
        for name in names:
            with patch.object(base, "truncate_tool_output", helpers[name]):
                start = time.perf_counter()
                result = await invoke()
                elapsed = (time.perf_counter() - start) * 1000
            if iteration >= 5:
                timings[name].append(elapsed)
            # The UUID recovery path intentionally differs on every invocation.
            preview = result.output.split("\n\nFull output: ", 1)[0]
            digests.add(hashlib.sha256(preview.encode()).hexdigest())
    assert len(digests) == 1, "Preview or truncation marker changed"
    results = {}
    for name, helper in helpers.items():
        with patch.object(base, "truncate_tool_output", helper):
            gc.collect()
            tracemalloc.start()
            try:
                result = await invoke()
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        results[name] = {
            "p50_ms": round(statistics.median(timings[name]), 3),
            "p95_ms": round(sorted(timings[name])[math.ceil(samples * 0.95) - 1], 3),
            "peak_python_allocated_bytes": peak,
            "preview_sha256": next(iter(digests)),
            "samples_ms": [round(value, 3) for value in timings[name]],
        }
    expected_writes = (samples + 6) * len(helpers) if result.truncated else 0
    assert sink.writes == expected_writes
    return {
        "input_bytes": len(expected_bytes),
        "input_lines": text.count("\n") + 1,
        "direction": direction,
        **results,
    }


async def _run(
    args: argparse.Namespace, helpers: dict[str, Any]
) -> list[dict[str, Any]]:
    fixtures = {
        "short_ascii": "A short tool result.",
        "64KiB_log": ("document line: complete\n" * 3000).rstrip(),
        "1MiB_log": ("document line: complete\n" * 46000).rstrip(),
        "4MiB_log": ("document line: complete\n" * 184000).rstrip(),
        "4MiB_unicode_log": ("document: café 日本 🚀\n" * 144000).rstrip(),
        "4MiB_single_line": "x" * (4 * 1024 * 1024),
    }
    results = []
    for name, text in fixtures.items():
        for direction in ("head", "tail"):
            results.append(
                {
                    "fixture": name,
                    **await _measure(text, direction, helpers, args.samples),
                }
            )
    return results


def main() -> None:
    """Benchmark synthetic responses without opening an application database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    sys.path.insert(0, str(REPO / "backend"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
    import django
    import structlog

    django.setup()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING)
    )
    from apps.harness.tools.truncate import truncate_tool_output

    helpers = {"current": truncate_tool_output}
    if args.baseline_ref:
        helpers = {"baseline": _baseline(args.baseline_ref), **helpers}
    result = {
        "benchmark": (
            "MCP result normalization, validation, registry clipping and spill encoding"
        ),
        "python": platform.python_version(),
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        "baseline_ref": args.baseline_ref,
        "samples": args.samples,
        "warmups": 5,
        "results": asyncio.run(_run(args, helpers)),
        "limitations": [
            "Synthetic local CPU/allocation measurements, not production latency.",
            "Input fixtures/expected bytes are allocated before measurement.",
            "Allocation peaks are tracemalloc bytes, not RSS; measured separately.",
            "MCP transport, provider latency and spill network/disk I/O are excluded.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    structlog.get_logger(__name__).warning(
        "tool_truncation_benchmark_written", path=str(args.output)
    )


if __name__ == "__main__":
    main()
