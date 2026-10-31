"""Backend restart repair and autonomous external-run recovery."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from django.utils import timezone

from apps.harness.engines.run_repository import HarnessRunRepository
from apps.harness.engines.runs import HarnessRunService
from apps.harness.models import HarnessPart, QuestionRequest
from apps.harness.permissions.models import PermissionRequest
from apps.harness.provider_turn_models import ProviderTurn
from apps.harness.recovery_worker import HarnessRecoveryWorker
from apps.harness.repositories import HarnessMessageRepository, HarnessSessionRepository
from apps.harness.run_recovery_service import RunRecoveryService


@pytest.mark.django_db(transaction=True)
def test_restart_closes_unowned_work_and_preserves_external_attempts(
    harness_workspace,
):
    sessions = []
    messages = []
    requests = []
    parts = []
    turns = []
    for engine in ("native", "claude", "codex"):
        session = HarnessSessionRepository.create(
            workspace_id=harness_workspace.id,
            organization_id=harness_workspace.runner.organization_id,
        )
        session.harness_id = engine
        session.status = "busy"
        session.save(update_fields=["harness_id", "status"])
        assistant = HarnessMessageRepository.create(
            session_id=session.id, role="assistant"
        )
        assistant.harness_id = engine
        assistant.save(update_fields=["harness_id"])
        parts.append(
            HarnessPart.objects.create(message=assistant, type="tool", state="running")
        )
        fields = {
            "organization_id": session.organization_id,
            "workspace_id": session.workspace_id,
            "session_id": session.id,
            "message_id": assistant.id,
        }
        requests.append(
            (
                PermissionRequest.objects.create(**fields, tool="exec", pattern="*"),
                QuestionRequest.objects.create(**fields, questions=[]),
            )
        )
        turns.append(
            ProviderTurn.objects.create(
                assistant_message=assistant,
                organization_id=session.organization_id,
                holder_tokens=["owner"],
                monitor_required=True,
            )
        )
        sessions.append(session)
        messages.append(assistant)
        if engine == "claude":
            HarnessRunService().create_attempt(
                session_id=session.id,
                assistant_message_id=assistant.id,
                harness_id=engine,
            )

    completed = HarnessMessageRepository.create(
        session_id=sessions[0].id, role="assistant", content="already done"
    )
    completed.completed_at = timezone.now()
    completed.finish = "stop"
    completed.save(update_fields=["completed_at", "finish"])
    assert RunRecoveryService().recover_backend_restart() == 2
    assert RunRecoveryService().recover_backend_restart() == 0
    for row in [*sessions, *messages, *parts, *turns, completed]:
        row.refresh_from_db()
    for group in requests:
        for row in group:
            row.refresh_from_db()
    assert sessions[0].status == "idle"
    assert messages[0].finish == "aborted" and messages[0].completed_at is not None
    assert parts[0].state == "error"
    assert all(row.status == "rejected" for row in requests[0])
    assert not turns[0].active and turns[0].holder_tokens == []
    assert turns[0].holds == 0 and not turns[0].monitor_required
    assert sessions[1].status == "busy"
    assert messages[1].completed_at is None and parts[1].state == "running"
    assert all(row.status == "pending" for row in requests[1])
    assert turns[1].active
    assert sessions[2].status == "idle"
    assert messages[2].finish == "aborted" and parts[2].state == "error"
    assert not turns[2].active
    assert completed.finish == "stop" and completed.error == ""


async def test_worker_is_singleton_and_survives_failed_sweeps(monkeypatch):
    restart = Mock(return_value=0)
    monkeypatch.setattr(RunRecoveryService, "recover_backend_restart", restart)
    recovered = asyncio.Event()

    async def sweep(_accessor, **_kwargs):
        if worker.runs.sweep_stale.await_count == 1:
            raise RuntimeError("temporary database outage")
        recovered.set()

    worker = HarnessRecoveryWorker(interval=0.001)
    worker.runs.sweep_stale = AsyncMock(side_effect=sweep)
    await worker.start()
    first = worker._task
    await worker.start()
    try:
        await asyncio.wait_for(recovered.wait(), timeout=2)
        assert worker._task is first
        restart.assert_called_once()
    finally:
        await worker.stop()
    assert worker._task is None and first.done()


@pytest.mark.django_db(transaction=True)
async def test_external_projections_wait_for_ancestor_lease_but_keep_independent_owners(
    harness_workspace,
):
    def child(engine, parent=None, owned=False):
        session = HarnessSessionRepository.create(
            workspace_id=harness_workspace.id,
            organization_id=harness_workspace.runner.organization_id,
            harness_id=engine,
            parent_id=parent.id if parent else None,
        )
        session.status = "busy"
        session.save(update_fields=["status"])
        message = HarnessMessageRepository.create(
            session_id=session.id, role="assistant", harness_id=engine
        )
        owner = None
        if owned:
            owner = HarnessRunService().create_attempt(
                session_id=session.id,
                assistant_message_id=message.id,
                harness_id=engine,
            )
        return session, message, owner

    root, _root_message, owner = child("claude", owned=True)
    projection, projection_message, _ = child("claude", parent=root)
    independent, independent_message, _ = child("codex", parent=root, owned=True)
    nested, nested_message, _ = child("codex", parent=independent)
    native, native_message, _ = child("native", parent=root)
    assert RunRecoveryService().recover_backend_restart() == 1
    native_message.refresh_from_db()
    projection_message.refresh_from_db()
    assert native_message.finish == "aborted"
    assert projection_message.completed_at is None
    HarnessRunRepository.model.objects.filter(id=owner.run_id).update(
        heartbeat_at=timezone.now() - timedelta(minutes=10)
    )
    assert await HarnessRunService().prepare_session(root.id)
    projection_message.refresh_from_db()
    projection.refresh_from_db()
    independent_message.refresh_from_db()
    independent.refresh_from_db()
    nested_message.refresh_from_db()
    assert projection_message.finish == "aborted" and projection.status == "idle"
    assert independent_message.completed_at is None and independent.status == "busy"
    assert nested_message.completed_at is None


@pytest.mark.parametrize("status", ["idle", "busy"])
async def test_recovery_publication_preserves_a_newly_admitted_turn(
    monkeypatch, status
):
    service = SimpleNamespace(
        _emit_frontend=AsyncMock(),
        _session_status_payload=Mock(return_value={"status": "idle"}),
        conversation_events=SimpleNamespace(emit=AsyncMock()),
    )
    monkeypatch.setattr(
        "apps.harness.harness_service.get_harness_service", lambda: service
    )
    workspace_id = uuid.uuid4()
    row = SimpleNamespace(
        session=SimpleNamespace(status=status, workspace_id=workspace_id),
        assistant_message=object(),
    )
    worker = HarnessRecoveryWorker()
    worker.runs.repository = SimpleNamespace(get_by_id=lambda _identity: row)
    await worker._publish_recovered(uuid.uuid4())
    assert service._emit_frontend.await_count == (1 if status == "idle" else 0)
    service.conversation_events.emit.assert_awaited_once_with(workspace_id)
