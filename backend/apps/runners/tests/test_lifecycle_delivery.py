"""Approval checks preserve outbox deadlines, leases and transport boundaries."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from django.db import connection
from django.utils import timezone

from apps.runners.deletion_repository import DeletionRepository
from apps.runners.models import LifecycleCommand, Runner, Task, Workspace
from apps.runners.operations import OperationRepository
from apps.runners.repositories import TaskRepository
from apps.runners.services.recovery import RecoveryService

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def command(runner: Runner, workspace: Workspace) -> LifecycleCommand:
    """Prepare an online runner's exact, durable stop command."""
    runner.last_heartbeat_at = timezone.now()
    runner.save(update_fields=["last_heartbeat_at"])
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    return OperationRepository.prepare(
        str(task.id),
        "task:stop_workspace",
        {"task_id": str(task.id), "workspace_id": str(workspace.id)},
    )


def test_withheld_first_delivery_renews_deadline_without_claiming(
    command: LifecycleCommand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waiting for approval must never turn an unissued command into a timeout."""
    now = timezone.now()
    LifecycleCommand.objects.filter(pk=command.pk).update(
        deadline_at=now - timedelta(seconds=1)
    )
    monkeypatch.setattr("apps.runners.operations.timezone.now", lambda: now)

    def withheld(task_id: uuid.UUID) -> bool:
        assert task_id == command.task_id
        assert connection.in_atomic_block
        return False

    approval = Mock(side_effect=withheld)
    assert OperationRepository.candidates(delivery_allowed=approval) == []

    command.refresh_from_db()
    assert command.deadline_at == now + timedelta(hours=2)
    assert command.deliveries == 0
    assert command.lease_until <= now
    assert command.next_delivery_at <= now
    assert command.task.status == "pending"
    approval.assert_called_once_with(command.task_id)


def test_withheld_retry_still_expires(command: LifecycleCommand) -> None:
    """A previously issued command retains its deadline and intervention fence."""
    now = timezone.now()
    LifecycleCommand.objects.filter(pk=command.pk).update(
        deliveries=1, deadline_at=now - timedelta(seconds=1)
    )
    approval = Mock(return_value=False)

    assert OperationRepository.candidates(delivery_allowed=approval) == []

    command.refresh_from_db()
    assert command.phase == "intervention"
    assert command.task.status == "failed"
    assert command.deliveries == 1
    assert command.task.workspace.current_task_id == command.task_id
    approval.assert_not_called()


def test_approval_failure_rolls_back_claim(command: LifecycleCommand) -> None:
    """A failing approval lookup must leave the command available for retry."""
    lease_until = command.lease_until
    next_delivery_at = command.next_delivery_at
    approval = Mock(side_effect=[True, RuntimeError("approval unavailable")])

    with pytest.raises(RuntimeError, match="approval unavailable"):
        OperationRepository.candidates(delivery_allowed=approval)

    command.refresh_from_db()
    assert command.deliveries == 0
    assert command.lease_until == lease_until
    assert command.next_delivery_at == next_delivery_at
    assert command.task.status == "pending"
    assert approval.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decisions", "deliveries", "emitted"),
    [
        ([False], 0, False),
        ([True, False], 0, False),
        ([True, True, False], 1, False),
        ([True, True, True], 1, True),
    ],
)
async def test_recovery_checks_approval_at_each_release_boundary(
    command: LifecycleCommand,
    monkeypatch: pytest.MonkeyPatch,
    decisions: list[bool],
    deliveries: int,
    emitted: bool,
) -> None:
    """Approval is checked before expiry, before claiming, and before sending."""
    approval = Mock(side_effect=decisions)
    monkeypatch.setattr(DeletionRepository, "delivery_allowed", approval)
    transport = AsyncMock()

    await RecoveryService().tick(transport)

    command.refresh_from_db()
    assert command.deliveries == deliveries
    assert command.task.status == "pending"
    assert approval.call_count == len(decisions)
    assert all(call.args == (command.task_id,) for call in approval.call_args_list)
    if deliveries:
        assert command.lease_until > timezone.now()
        assert command.next_delivery_at > timezone.now()
    if emitted:
        transport.emit.assert_awaited_once_with(
            command.event,
            OperationRepository.envelope(command, command.payload),
            room=command.task.runner.sid,
        )
    else:
        transport.emit.assert_not_awaited()


@pytest.mark.parametrize("waiting", ["offline", "heartbeat", "approval", "unprepared"])
def test_waiting_batch_cannot_starve_other_commands(
    command: LifecycleCommand, offline_runner: Runner, waiting: str
) -> None:
    """More than one batch of waiting work still lets a ready command progress."""
    now = timezone.now()
    waiting_runner = offline_runner if waiting == "offline" else command.task.runner
    tasks = [
        Task(
            id=uuid.uuid4(), runner=waiting_runner, type="build_image", status="pending"
        )
        for _ in range(100)
    ]
    Task.objects.bulk_create(tasks)
    LifecycleCommand.objects.bulk_create(
        [
            LifecycleCommand(
                task=task,
                event="" if waiting == "unprepared" else "task:build_image",
                deadline_at=now + timedelta(hours=2),
                next_delivery_at=now - timedelta(minutes=1),
                heartbeat_at=now if waiting == "heartbeat" else None,
            )
            for task in tasks
        ]
    )
    ready_id = command.task_id

    def allowed(task_id: uuid.UUID) -> bool:
        return waiting != "approval" or task_id == ready_id

    assert OperationRepository.candidates(delivery_allowed=allowed) == []
    ready = OperationRepository.candidates(delivery_allowed=allowed)
    assert [row.task_id for row in ready] == [ready_id]
    assert (
        LifecycleCommand.objects.filter(
            task_id__in=[task.id for task in tasks], deliveries=0
        ).count()
        == 100
    )
