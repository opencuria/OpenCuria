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
        reconcile_recovered_run=Mock(),
        is_running=Mock(return_value=False),
        _emit_frontend=AsyncMock(),
        _session_status_payload=Mock(return_value={"status": "idle"}),
        conversation_events=SimpleNamespace(emit=AsyncMock()),
    )
    monkeypatch.setattr(
        "apps.harness.harness_service.get_harness_service", lambda: service
    )
    workspace_id = uuid.uuid4()
    row = SimpleNamespace(
        session=SimpleNamespace(
            id=uuid.uuid4(), status=status, workspace_id=workspace_id
        ),
        assistant_message=SimpleNamespace(id=uuid.uuid4()),
    )
    worker = HarnessRecoveryWorker()
    worker.runs.repository = SimpleNamespace(get_by_id=lambda _identity: row)
    await worker._publish_recovered(uuid.uuid4())
    service.reconcile_recovered_run.assert_called_once_with(
        row.session.id, row.assistant_message.id
    )
    assert service._emit_frontend.await_count == (1 if status == "idle" else 0)
    service.conversation_events.emit.assert_awaited_once_with(workspace_id)


def test_recovery_clears_only_matching_stop_intent_and_external_projections():
    from apps.harness.harness_service import HarnessService

    service = HarnessService()
    registry = service.run_registry
    registry.attach_context("root", {"message_id": "old-root"})
    registry.request_abort("root")
    # Failed external cleanup releases heavy runtime state but retains identity
    # while the explicit stop bit still has an outstanding recovery obligation.
    registry.release_context("root")
    assert registry.get("root").assistant_message_id == "old-root"
    registry.attach_context(
        "projection",
        {
            "message_id": "old-child",
            "external_projection": True,
            "external_parent_session_id": "root",
            "external_parent_message_id": "old-root",
        },
    )
    registry.request_abort("projection")
    registry.link_parent("projection", "root")
    registry.attach_context("native-child", {"message_id": "native-message"})
    registry.link_parent("native-child", "root")
    registry.attach_context(
        "other-projection",
        {
            "message_id": "other-child",
            "external_projection": True,
            "external_parent_session_id": "root",
            "external_parent_message_id": "new-root",
        },
    )
    native = registry.get("native-child")
    other = registry.get("other-projection")
    service.reconcile_recovered_run("root", "old-root")
    assert not registry.is_aborted("root")
    assert registry.get("projection") is None
    assert registry.get("native-child") is native
    assert registry.get("other-projection") is other
    assert native.is_running
    assert other.is_running
    assert not service._user_abort_requested(
        SimpleNamespace(id="future", parent_id="root")
    )


async def test_recovery_preserves_new_admission_and_only_drops_old_projections():
    from apps.harness.harness_service import HarnessService

    service = HarnessService()
    registry = service.run_registry
    registry.attach_context("root", {"message_id": "fresh-root"})
    registry.request_abort("root")  # A newer stop belongs to the fresh answer.
    task = asyncio.create_task(asyncio.sleep(100))
    registry.start_execution("root", task)
    registry.attach_context(
        "old-projection",
        {
            "message_id": "old-child",
            "external_projection": True,
            "external_parent_session_id": "root",
            "external_parent_message_id": "old-root",
        },
    )
    registry.attach_context("resumed-child", {"message_id": "fresh-child"})
    registry.link_parent("resumed-child", "root")
    independent = registry.get("resumed-child")
    root = registry.get("root")
    try:
        service.reconcile_recovered_run("root", "old-root")
        assert registry.get("root") is root
        assert root.task is task and not task.cancelling()
        assert root.abort_requested
        assert root.context == {"message_id": "fresh-root"}
        assert registry.get("old-projection") is None
        assert registry.get("resumed-child") is independent
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_recovery_never_drops_a_matching_task_or_admission_reference():
    from apps.harness.harness_service import HarnessService

    service = HarnessService()
    registry = service.run_registry
    registry.attach_context("root", {"message_id": "old-root"})
    registry.request_abort("root")
    task = asyncio.create_task(asyncio.sleep(0))
    registry.start_execution("root", task)
    await task
    service.reconcile_recovered_run("root", "old-root")
    assert registry.get("root").task is task
    assert registry.get("root").abort_requested
    registry.release_task("root", task)
    registry.release_context("root")
    registry.begin_admission("root", asyncio.current_task())
    service.reconcile_recovered_run("root", "old-root")
    assert registry.get("root").admission is asyncio.current_task()
    assert registry.get("root").abort_requested


def test_released_context_keeps_recovery_identity_until_stop_clears():
    from apps.harness.run_state import RunRegistry

    registry = RunRegistry()
    registry.attach_context("session", {"message_id": "old-assistant"})
    registry.request_abort("session")
    registry.release_context("session")
    assert registry.get("session").assistant_message_id == "old-assistant"
    registry.clear_abort("session")
    assert registry.get("session") is None  # Identity alone never retains a run.


async def test_recovery_does_not_publish_stale_idle_over_local_fresh_admission(
    monkeypatch,
):
    from apps.harness.harness_service import HarnessService

    service = HarnessService()
    service._emit_frontend = AsyncMock()
    service.conversation_events.emit = AsyncMock()
    session_id, recovered_message_id, fresh_message_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    registry = service.run_registry
    registry.begin_admission(str(session_id), asyncio.current_task())
    registry.attach_context(str(session_id), {"message_id": str(fresh_message_id)})
    monkeypatch.setattr(
        "apps.harness.harness_service.get_harness_service",
        lambda: service,
    )
    # The repository snapshot was read before the fresh local admission began.
    row = SimpleNamespace(
        session=SimpleNamespace(
            id=session_id, status="idle", workspace_id=uuid.uuid4()
        ),
        assistant_message=SimpleNamespace(id=recovered_message_id),
    )
    worker = HarnessRecoveryWorker()
    worker.runs.repository = SimpleNamespace(get_by_id=lambda _: row)
    await worker._publish_recovered(uuid.uuid4())
    service._emit_frontend.assert_not_awaited()
    assert registry.get(str(session_id)).admission is asyncio.current_task()
    assert registry.get(str(session_id)).assistant_message_id == str(fresh_message_id)


@pytest.mark.parametrize("recovered", [False, True])
async def test_explicit_attempt_cleanup_reconciles_only_after_confirmation(recovered):
    from apps.harness.harness_service import HarnessService
    from common.exceptions import ConflictError

    attempt = SimpleNamespace(
        lease_id=None,
        status="closing",
        assistant_message_id=uuid.uuid4(),
    )
    run_service = SimpleNamespace(
        repository=SimpleNamespace(get_open_for_session=lambda _: attempt),
        prepare_session=AsyncMock(return_value=True),
    )
    if not recovered:
        run_service.prepare_session.side_effect = ConflictError("cleanup pending")
    service = HarnessService(run_service=run_service)
    service.reconcile_recovered_run = Mock()
    session = SimpleNamespace(id=uuid.uuid4(), harness_id="codex")
    if recovered:
        assert await service._ensure_external_attempt_reconciled(session, force=True)
        service.reconcile_recovered_run.assert_called_once_with(
            session.id,
            attempt.assistant_message_id,
        )
    else:
        with pytest.raises(ConflictError, match="cleanup pending"):
            await service._ensure_external_attempt_reconciled(session, force=True)
        service.reconcile_recovered_run.assert_not_called()


@pytest.mark.parametrize("recovered", [False, True])
async def test_startup_recovery_uses_same_local_reconciliation_port(recovered):
    from dataclasses import fields

    from apps.harness.run_preparation import RunPreparation, RunPreparationPorts
    from common.exceptions import ConflictError

    attempt = SimpleNamespace(lease_id=None, assistant_message_id=uuid.uuid4())
    run_service = SimpleNamespace(
        repository=SimpleNamespace(get_open_for_session=lambda _: attempt),
        prepare_session=AsyncMock(return_value=True),
    )
    if not recovered:
        run_service.prepare_session.side_effect = ConflictError("cleanup pending")
    dependencies = {field.name: Mock() for field in fields(RunPreparationPorts)}
    dependencies.update(
        accessor_factory=None,
        provider_turn_context=Mock(return_value={}),
        user_abort_requested=Mock(return_value=False),
        run_service_for_engine=lambda: run_service,
        sessions=SimpleNamespace(get_depth=lambda _: 0),
        validate_provider_for_run=Mock(return_value="openai/gpt"),
    )
    ports = RunPreparationPorts(**dependencies)
    preparation = RunPreparation(ports)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        harness_id="codex",
        created_by_id=42,
        parent_id=None,
        status="idle",
        mode="build",
        agent_name="build",
        model="openai/gpt",
        reasoning_effort="",
        skill_ids=[],
    )
    if recovered:
        prepared = await preparation.prepare(
            session,
            "prompt",
            user_id=42,
            account_ids={},
            max_depth=3,
        )
        assert prepared.session is session
        ports.reconcile_recovered_run.assert_called_once_with(
            session.id,
            attempt.assistant_message_id,
        )
    else:
        with pytest.raises(ConflictError, match="cleanup pending"):
            await preparation.prepare(
                session,
                "prompt",
                user_id=42,
                account_ids={},
                max_depth=3,
            )
        ports.reconcile_recovered_run.assert_not_called()
