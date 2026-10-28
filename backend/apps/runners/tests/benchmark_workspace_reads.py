"""Opt-in comparison against the original complete workspace read path.

From backend/ (pytest's disposable SQLite database only):
    WORKSPACE_READ_BASELINE_REF=80bf104 .venv/bin/pytest -q -s \
        apps/runners/tests/benchmark_workspace_reads.py

Fixture creation, SQL instrumentation and allocation tracking are excluded from
latency samples. Both implementations read fresh rows from identical fixtures.
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
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import APIKeyPermission
from apps.organizations.models import Membership
from apps.runners import api as runner_api
from apps.runners.models import Runner
from apps.runners.services.workspace_reads import WorkspaceReadService
from apps.runners.tests.test_image_versions_api import headers
from apps.runners.tests.workspace_read_cases import workspace_read_cases

log = structlog.get_logger(__name__)


def _baseline(ref: str, monkeypatch: pytest.MonkeyPatch) -> WorkspaceReadService:
    """Load original read modules with their original persistence dependencies."""
    modules = {}
    root = Path(__file__).resolve().parents[4]
    for name in (
        "credentials.repositories",
        "plugins.repositories",
        "runners.image_lines",
        "runners.workspace_read_repository",
        "runners.services.image_versions",
        "runners.services.workspace_reads",
    ):
        path = f"backend/apps/{name.replace('.', '/')}.py"
        source = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        package, leaf = name.rsplit(".", 1)
        module = types.ModuleType(f"apps.{package}._benchmark_{leaf}")
        module.__package__ = f"apps.{package}"
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(compile(source, path, "exec"), module.__dict__)
        modules[name] = module
    # These original service ports simply forwarded to their repositories.
    credential_repo = modules["credentials.repositories"].CredentialRepository
    plugin_repo = modules["plugins.repositories"].WorkspacePluginActivationRepository
    credentials = types.SimpleNamespace(
        workspace_credential_ids=credential_repo.ids_for_workspace
    )
    plugins = types.SimpleNamespace(
        list_workspace_plugin_ids=plugin_repo.list_plugin_ids
    )
    versions = modules["runners.services.image_versions"].ImageVersionService()
    versions.lines = modules["runners.image_lines"].ImageLineRepository
    return modules["runners.services.workspace_reads"].WorkspaceReadService(
        repository=modules["runners.workspace_read_repository"].WorkspaceReadRepository,
        credentials=credentials,
        plugins=plugins,
        versions=versions,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("count", [1, 25, 100])
@pytest.mark.parametrize("with_metadata", [False, True])
def test_workspace_read_benchmark(
    runner: Runner,
    user: Any,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    with_metadata: bool,
) -> None:
    """Measure the authenticated REST list with real ORM and JSON serialization."""
    workspace_read_cases(runner, user, count, with_metadata=with_metadata)
    Membership.objects.create(user=user, organization=runner.organization)
    client = Client()
    auth = headers(user, runner.organization, [APIKeyPermission.WORKSPACES_READ])
    ref = os.getenv("WORKSPACE_READ_BASELINE_REF", "80bf104")
    readers = [_baseline(ref, monkeypatch), WorkspaceReadService()]
    # The old adapter called describe once per row. Adapt that call shape only
    # inside the benchmark so both paths traverse the same HTTP/auth stack.
    adapters = [
        types.SimpleNamespace(
            describe_many=lambda workspaces: [
                readers[0].describe(workspace) for workspace in workspaces
            ]
        ),
        readers[1],
    ]

    def execute(index: int) -> list[dict]:
        monkeypatch.setattr(runner_api, "WorkspaceReadService", lambda: adapters[index])
        response = client.get("/api/v1/workspaces/", **auth)
        assert response.status_code == 200
        return response.json()

    assert execute(0) == execute(1)
    queries = []
    for index in range(2):
        with CaptureQueriesContext(connection) as captured:
            execute(index)
        queries.append(len(captured))
    samples: list[list[float]] = [[], []]
    order = [0, 1] * 30
    random.Random(17).shuffle(order)
    for index in order:
        start = time.perf_counter()
        execute(index)
        samples[index].append((time.perf_counter() - start) * 1000)
    peaks = []
    for index in range(2):
        gc.collect()
        tracemalloc.start()
        execute(index)
        peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
    log.info(
        "workspace_read_benchmark",
        baseline_ref=ref,
        workspaces=count,
        with_metadata=with_metadata,
        queries_before_after=queries,
        p50_ms=[round(statistics.median(sample), 3) for sample in samples],
        p95_ms=[
            round(sorted(sample)[math.ceil(len(sample) * 0.95) - 1], 3)
            for sample in samples
        ],
        peak_mib=[round(peak / 2**20, 3) for peak in peaks],
    )
