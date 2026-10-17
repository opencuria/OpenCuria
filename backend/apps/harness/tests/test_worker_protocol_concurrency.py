"""PostgreSQL serialization tests for durable harness admission."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections

from apps.harness.models import (
    HarnessMessage,
    HarnessRun,
    HarnessRunCommand,
    HarnessSession,
    HarnessWorkerRegistration,
)
from apps.harness.worker_protocol import HarnessWorkerRepository
from common.exceptions import ConflictError

pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="Durable admission row-lock guarantees require PostgreSQL",
)


def _admit(barrier: threading.Barrier, session_id, worker_id: str):
    """Run one admission on a dedicated connection behind a common barrier."""
    db = connections["default"]
    try:
        db.ensure_connection()
        with db.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend_pid = cursor.fetchone()[0]
        barrier.wait(timeout=10)
        try:
            result = HarnessWorkerRepository.admit_run(
                session_id=session_id,
                prompt="concurrent prompt",
                model="provider/model",
                reasoning_effort="",
                provider="provider",
                skill_ids=[],
                engine_id="native",
                initiated_by_id=None,
                deployment_version="release-concurrency",
                gateway=True,
                admission_versions=("release-concurrency",),
            )
            return backend_pid, result
        except ConflictError as exc:
            return backend_pid, exc
    finally:
        connections.close_all()


@pytest.mark.django_db(transaction=True)
def test_concurrent_gateway_admissions_commit_one_turn(harness_workspace):
    """Two API requests racing one session create one user/assistant/outbox set."""
    worker = HarnessWorkerRepository.register(
        "concurrent-worker", "release-concurrency", pid=991
    )
    assert worker.state == HarnessWorkerRegistration.State.READY
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        harness_id="native",
    )
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_admit, barrier, session.id, "concurrent-worker")
            for _ in range(2)
        ]
        outcomes = [future.result(timeout=20) for future in futures]

    assert outcomes[0][0] != outcomes[1][0]
    successes = [result for _, result in outcomes if not isinstance(result, Exception)]
    failures = [result for _, result in outcomes if isinstance(result, ConflictError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert HarnessMessage.objects.filter(session=session).count() == 2
    assert HarnessRun.objects.filter(session=session).count() == 1
    assert HarnessRunCommand.objects.filter(
        run__session=session,
        command_type=HarnessRunCommand.Type.START,
    ).count() == 1
