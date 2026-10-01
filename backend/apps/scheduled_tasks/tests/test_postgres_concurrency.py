"""PostgreSQL concurrency guarantees for scheduled dispatch and admission."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from django.db import connection, connections

from apps.accounts.models import User
from apps.harness.models import HarnessSession
from apps.harness.repositories import HarnessSessionRepository
from apps.organizations.models import Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.scheduled_tasks.models import ScheduledTask
from apps.scheduled_tasks.repositories import ScheduledTaskRepository

pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="These locking guarantees require PostgreSQL",
)


@pytest.fixture
def schedule_graph(db: Any) -> tuple[Organization, User, Workspace]:
    """Create a running workspace with all relations needed by dispatch."""
    organization = Organization.objects.create(
        name="Concurrency Org", slug=f"concurrency-{uuid.uuid4().hex}"
    )
    owner = User.objects.create_user(
        email=f"{uuid.uuid4().hex}@example.com", password="test-only"
    )
    runner = Runner.objects.create(
        organization=organization,
        api_token_hash=uuid.uuid4().hex,
        status=RunnerStatus.ONLINE,
    )
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=owner,
        name="Concurrency workspace",
        status=WorkspaceStatus.RUNNING,
    )
    return organization, owner, workspace


def _create_schedule(
    organization: Organization, owner: User, workspace: Workspace
) -> ScheduledTask:
    scheduled_for = datetime.now(timezone.utc) + timedelta(minutes=1)
    return ScheduledTask.objects.create(
        organization=organization,
        owner=owner,
        workspace=workspace,
        name="Concurrency schedule",
        prompt="Inspect the repository",
        recurrence="daily",
        local_time="09:00",
        timezone_name="UTC",
        next_run_at=scheduled_for,
    )


def _create_root_session(
    workspace: Workspace, organization: Organization
) -> HarnessSession:
    return HarnessSession.objects.create(
        workspace=workspace,
        organization_id=organization.id,
    )


def _postgres_call(
    barrier: threading.Barrier, callback: Callable[[], Any]
) -> tuple[int, Any]:
    """Open a thread-local Django connection and synchronize before work."""
    db_connection = connections["default"]
    try:
        db_connection.ensure_connection()
        with db_connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend_pid = cursor.fetchone()[0]
        barrier.wait(timeout=10)
        return backend_pid, callback()
    finally:
        connections.close_all()


def _run_concurrently(
    callbacks: tuple[Callable[[], Any], Callable[[], Any]],
) -> tuple[tuple[int, Any], tuple[int, Any]]:
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_postgres_call, barrier, callback) for callback in callbacks
        ]
        return tuple(future.result(timeout=20) for future in futures)  # type: ignore[return-value]


def _run_in_thread(callback: Callable[[], Any]) -> tuple[int, Any]:
    """Run a DB operation on its own connection, synchronized with the test."""
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_postgres_call, barrier, callback)
        barrier.wait(timeout=10)
        return future.result(timeout=20)


@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_claim_one_occurrence_and_advance_once(
    schedule_graph: tuple[Organization, User, Workspace],
) -> None:
    organization, owner, workspace = schedule_graph
    schedule = _create_schedule(organization, owner, workspace)
    scheduled_for = schedule.next_run_at
    next_run_at = scheduled_for + timedelta(days=1)

    (pid_a, claim_a), (pid_b, claim_b) = _run_concurrently(
        (
            lambda: ScheduledTaskRepository.claim(
                schedule, scheduled_for=scheduled_for, next_run_at=next_run_at
            ),
            lambda: ScheduledTaskRepository.claim(
                schedule, scheduled_for=scheduled_for, next_run_at=next_run_at
            ),
        )
    )

    assert pid_a != pid_b
    assert sorted([claim_a is not None, claim_b is not None]) == [False, True]
    schedule.refresh_from_db()
    assert schedule.next_run_at == next_run_at
    assert schedule.runs.count() == 1
    assert schedule.runs.get().status == "claimed"


@pytest.mark.django_db(transaction=True)
def test_concurrent_scheduled_roots_reserve_workspace_once(
    schedule_graph: tuple[Organization, User, Workspace],
) -> None:
    organization, _, workspace = schedule_graph
    session_a = _create_root_session(workspace, organization)
    session_b = _create_root_session(workspace, organization)

    (pid_a, reserved_a), (pid_b, reserved_b) = _run_concurrently(
        (
            lambda: HarnessSessionRepository.reserve_workspace_run(
                session_a.id, scheduled=True
            ),
            lambda: HarnessSessionRepository.reserve_workspace_run(
                session_b.id, scheduled=True
            ),
        )
    )

    assert pid_a != pid_b
    assert sorted([reserved_a, reserved_b]) == [False, True]
    assert (
        HarnessSession.objects.filter(
            workspace=workspace, parent__isnull=True, status="busy"
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_manual_reservation_is_allowed_after_scheduled_and_blocks_later_schedule(
    schedule_graph: tuple[Organization, User, Workspace],
) -> None:
    organization, owner, workspace = schedule_graph
    scheduled_session = _create_root_session(workspace, organization)
    manual_session = _create_root_session(workspace, organization)

    scheduled_pid, scheduled_reserved = _run_in_thread(
        lambda: HarnessSessionRepository.reserve_workspace_run(
            scheduled_session.id, scheduled=True
        )
    )
    manual_pid, manual_reserved = _run_in_thread(
        lambda: HarnessSessionRepository.reserve_workspace_run(
            manual_session.id, scheduled=False
        )
    )

    assert scheduled_pid != manual_pid
    assert scheduled_reserved is True
    assert manual_reserved is True

    # A fresh workspace isolates the reverse ordering: user activity wins admission.
    manual_first_workspace = Workspace.objects.create(
        runner=workspace.runner,
        created_by=owner,
        name="Manual-first workspace",
        status=WorkspaceStatus.RUNNING,
    )
    manual_first = _create_root_session(manual_first_workspace, organization)
    scheduled_second = _create_root_session(manual_first_workspace, organization)
    manual_first_pid, manual_first_reserved = _run_in_thread(
        lambda: HarnessSessionRepository.reserve_workspace_run(
            manual_first.id, scheduled=False
        )
    )
    scheduled_second_pid, scheduled_second_reserved = _run_in_thread(
        lambda: HarnessSessionRepository.reserve_workspace_run(
            scheduled_second.id, scheduled=True
        )
    )

    assert manual_first_pid != scheduled_second_pid
    assert manual_first_reserved is True
    assert scheduled_second_reserved is False
    assert HarnessSession.objects.get(id=manual_first.id).status == "busy"
    assert HarnessSession.objects.get(id=scheduled_second.id).status == "idle"
