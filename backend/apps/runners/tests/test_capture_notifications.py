"""Capture notifications use the real bus and committed lifecycle projection."""

import asyncio
from unittest.mock import Mock

import pytest
from asgiref.sync import async_to_sync
from django.db import transaction

from apps.runners.capture_repository import CaptureRepository
from apps.runners.models import CaptureRequest, LifecycleCommand
from apps.runners.operations import OperationRepository
from apps.runners.services.recovery import RecoveryService
from apps.runners.tests.test_capture_pipeline import result
from apps.runners.tests.test_services import service as service
from apps.runners.tests.test_services import sio_mock as sio_mock

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def frontend(service):
    """Inject a sync bus, retaining both scheduling and actual event delivery."""
    bus = Mock()
    service._frontend_bus = bus
    return bus.emit


def operation_payload(workspace, diagnostic="", intervention=False):
    return {
        "workspace_id": str(workspace.id),
        "active_operation": None,
        "intervention_required": intervention,
        "lifecycle_diagnostic": diagnostic,
    }


def test_unknown_result_notifies_only_after_commit(
    service, frontend, runner, workspace
):
    workspace.runtime_type = "qemu"
    workspace.save()
    _, child = CaptureRepository.allocate(workspace.id, "capture")
    diagnostic = "Unknown outcome; intervention required"
    with transaction.atomic():
        assert not result(
            service,
            runner,
            child,
            "workspace:error",
            error="scrub unknown",
            execution_finished=True,
            outcome_known=False,
        )
        frontend.assert_not_called()
        workspace.refresh_from_db()
        assert workspace.intervention_required
        assert workspace.current_task_id == child.id
    assert frontend.call_count == 2
    frontend.assert_any_call(
        "workspace:operation_changed",
        operation_payload(workspace, diagnostic, True),
        str(workspace.id),
    )
    frontend.assert_any_call(
        "workspace:error",
        {
            "workspace_id": str(workspace.id),
            "task_id": str(child.id),
            "error": diagnostic,
        },
        str(workspace.id),
    )


@pytest.mark.parametrize("phase", ["stop", "capture", "resume"])
def test_child_result_rollback_discards_changes_and_events(
    service, frontend, runner, workspace, phase
):
    workspace.runtime_type = "qemu"
    workspace.credentials_present = True
    workspace.save()
    _, child = CaptureRepository.allocate(workspace.id, "capture")
    request = CaptureRequest.objects.get(workspace=workspace)
    if phase in {"capture", "resume"}:
        assert result(
            service, runner, child, "workspace:stopped", credentials_present=False
        )
        assert CaptureRepository.tick() == []
        request.refresh_from_db()
    if phase == "resume":
        assert result(
            service,
            runner,
            request.child,
            "image_artifact:created",
            image_artifact_id="/capture.qcow2",
            name="capture",
            size_bytes=123,
        )
        assert CaptureRepository.tick() == []
        request.refresh_from_db()
    child = request.child
    workspace.refresh_from_db()
    request.image.refresh_from_db()
    before_command_phase = LifecycleCommand.objects.get(task=child).phase
    before_workspace = {
        f.attname: getattr(workspace, f.attname)
        for f in workspace._meta.concrete_fields
    }
    before_image = {
        f.attname: getattr(request.image, f.attname)
        for f in request.image._meta.concrete_fields
    }
    frontend.reset_mock()
    event, extra = {
        "stop": ("workspace:stopped", {"credentials_present": False}),
        "capture": (
            "image_artifact:created",
            {
                "image_artifact_id": "/capture.qcow2",
                "name": "capture",
                "size_bytes": 123,
            },
        ),
        "resume": ("workspace:resumed", {"credentials_present": False}),
    }[phase]
    with pytest.raises(RuntimeError, match="intentional rollback"):
        with transaction.atomic():
            assert result(service, runner, child, event, **extra)
            child.refresh_from_db()
            workspace.refresh_from_db()
            assert child.status == "completed"
            assert workspace.current_task_id is None
            frontend.assert_not_called()
            raise RuntimeError("intentional rollback")
    frontend.assert_not_called()
    workspace.refresh_from_db()
    request.image.refresh_from_db()
    child.refresh_from_db()
    assert child.status == "pending"
    assert LifecycleCommand.objects.get(task=child).phase == before_command_phase
    assert {
        f.attname: getattr(workspace, f.attname)
        for f in workspace._meta.concrete_fields
    } == before_workspace
    assert {
        f.attname: getattr(request.image, f.attname)
        for f in request.image._meta.concrete_fields
    } == before_image


def test_deadline_intervention_tick_and_recovery_flags(
    service, frontend, sio_mock, workspace, monkeypatch
):
    workspace.runtime_type = "qemu"
    workspace.save()
    _, child = CaptureRepository.allocate(workspace.id, "capture")
    diagnostic = "Unknown capture deadline"
    OperationRepository.intervene(child, diagnostic)
    notification = {
        "workspace_id": str(workspace.id),
        "phase": "intervention",
        "diagnostic": "Unknown child outcome; exact journal reconciliation required",
    }
    # Probe the notification without consuming it before recovery can publish it.
    with pytest.raises(RuntimeError, match="notification probe"):
        with transaction.atomic():
            assert CaptureRepository.tick() == [notification]
            assert CaptureRepository.tick() == []
            raise RuntimeError("notification probe")
    monkeypatch.setattr(
        "apps.runners.services.RunnerService", lambda transport: service
    )
    async_to_sync(RecoveryService().tick)(sio_mock)
    assert CaptureRepository.tick() == []
    frontend.assert_any_call(
        "workspace:operation_changed",
        operation_payload(workspace, diagnostic, True),
        str(workspace.id),
    )
    frontend.assert_any_call(
        "workspace:error",
        {"workspace_id": str(workspace.id), "error": notification["diagnostic"]},
        str(workspace.id),
    )


def test_completed_duplicate_ticks_notify_exactly_once(
    service, frontend, sio_mock, runner, stopped_workspace, monkeypatch
):
    workspace = stopped_workspace
    workspace.runtime_type = "qemu"
    workspace.save()
    _, child = CaptureRepository.allocate(workspace.id, "capture")
    assert result(
        service,
        runner,
        child,
        "image_artifact:created",
        image_artifact_id="/capture.qcow2",
        name="capture",
        size_bytes=123,
    )
    frontend.reset_mock()
    monkeypatch.setattr(
        "apps.runners.services.RunnerService", lambda transport: service
    )
    async_to_sync(RecoveryService().tick)(sio_mock)
    assert CaptureRequest.objects.get(workspace=workspace).phase == "completed"
    frontend.assert_called_once_with(
        "workspace:operation_changed", operation_payload(workspace), str(workspace.id)
    )
    assert CaptureRepository.tick() == []
    assert CaptureRepository.tick() == []
    async_to_sync(RecoveryService().tick)(sio_mock)
    assert frontend.call_count == 1


@pytest.mark.asyncio
async def test_async_operation_projection_without_unsafe_orm(
    service, frontend, workspace
):
    from asgiref.sync import sync_to_async

    def allocate():
        workspace.runtime_type = "qemu"
        workspace.save()
        CaptureRepository.allocate(workspace.id, "capture")

    await sync_to_async(allocate)()
    service._forward_workspace_operation(str(workspace.id), None)
    # Await the scheduled projection rather than permitting ORM access on the loop.
    pending = asyncio.all_tasks() - {asyncio.current_task()}
    await asyncio.gather(*pending)
    frontend.assert_called_once_with(
        "workspace:operation_changed",
        {
            "workspace_id": str(workspace.id),
            "active_operation": "capturing_image",
            "intervention_required": False,
            "lifecycle_diagnostic": "",
        },
        str(workspace.id),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [False, True])
async def test_async_operation_delivery_stays_on_original_loop(
    service, workspace, monkeypatch, injected
):
    """Delivery can re-enter the sync executor without a cross-loop bounce."""
    from asgiref.sync import sync_to_async

    loop = asyncio.get_running_loop()
    delivered = loop.create_future()

    def unexpected_bridge(*args, **kwargs):
        raise AssertionError("Async delivery must not bounce through async_to_sync")

    monkeypatch.setattr(
        "apps.runners.services.infra.frontend_bus.async_to_sync", unexpected_bridge
    )

    async def emit(event, payload, workspace_id):
        try:
            assert asyncio.get_running_loop() is loop
            # A frontend bus may itself need synchronous repository access.
            projected_workspace = await sync_to_async(service.workspaces.get_by_id)(
                workspace_id
            )
            assert str(projected_workspace.id) == workspace_id
            assert event == "workspace:operation_changed"
            assert payload == operation_payload(workspace)
            delivered.set_result(None)
        except Exception as exc:
            delivered.set_exception(exc)

    if injected:
        service._frontend_bus = emit
    else:
        monkeypatch.setattr("apps.runners.sio_server.emit_to_frontend", emit)

    # Capture only this projection task, not unrelated tasks on pytest's loop.
    tasks = []
    create_task = loop.create_task

    def capture_task(coro):
        task = create_task(coro)
        tasks.append(task)
        return task

    with monkeypatch.context() as patch:
        patch.setattr(loop, "create_task", capture_task)
        service._forward_workspace_operation(str(workspace.id), None)

    assert len(tasks) == 1
    await asyncio.wait_for(asyncio.gather(*tasks, delivered), timeout=5)
