"""Capture fences apply to live harness actions, not saved history."""

import pytest
from asgiref.sync import sync_to_async

from apps.harness.access.runner_accessor import create_harness_accessor
from apps.harness.conversation_service import ConversationService
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessSession
from apps.harness.repositories import HarnessMessageRepository, HarnessSessionRepository
from apps.harness.session_settings_service import SessionSettingsService
from apps.runners.models import Workspace
from common.exceptions import ConflictError


def session_for(workspace, *, parent=None):
    """Create a persisted chat without starting a provider."""
    return HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
        parent_id=parent.id if parent else None,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("scheduled", [False, True])
def test_capture_blocks_all_run_reservations(harness_workspace, child, scheduled):
    root = session_for(harness_workspace)
    session = session_for(harness_workspace, parent=root) if child else root
    Workspace.objects.filter(id=harness_workspace.id).update(
        active_operation="capturing_image"
    )
    if scheduled:
        assert not HarnessService().reserve_run(session, scheduled=True)
    else:
        with pytest.raises(ConflictError, match="capturing image"):
            HarnessService().reserve_run(session)
    session.refresh_from_db()
    assert session.status == "idle"


@pytest.mark.django_db
def test_manual_roots_and_children_remain_concurrent(harness_workspace):
    root = session_for(harness_workspace)
    child = session_for(harness_workspace, parent=root)
    second = session_for(harness_workspace)
    for session in (root, child, second):
        assert HarnessService().reserve_run(session)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "action", ["set_mode", "set_model", "set_reasoning_effort", "update_title"]
)
def test_session_mutations_reload_workspace_fence(harness_workspace, action):
    service = SessionSettingsService()
    session = session_for(harness_workspace)
    Workspace.objects.filter(id=harness_workspace.id).update(
        active_operation="capturing_image"
    )
    with pytest.raises(ConflictError):
        getattr(service, action)(session.id, "plan")
    with pytest.raises(ConflictError):
        HarnessService().create_session(
            workspace_id=harness_workspace.id,
            organization_id=session.organization_id,
            prompt="new chat",
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["fork_session", "delete_session", "abort_run"])
async def test_async_session_mutations_blocked(harness_workspace, action):
    session = session_for(harness_workspace)
    await sync_to_async(Workspace.objects.filter(id=harness_workspace.id).update)(
        active_operation="capturing_image"
    )
    with pytest.raises(ConflictError):
        service = (
            ConversationService() if action == "fork_session" else HarnessService()
        )
        await getattr(service, action)(session.id)


@pytest.mark.django_db
def test_saved_history_and_navigation_allowed_during_capture(harness_workspace):
    session = session_for(harness_workspace)
    HarnessMessageRepository.create(session_id=session.id, role="user", content="saved")
    Workspace.objects.filter(id=harness_workspace.id).update(
        active_operation="capturing_image"
    )
    service = ConversationService()
    assert service.get_session(session.id).id == session.id
    assert service.list_timeline_messages(session.id)[0].timeline_content == "saved"
    assert service.list_sessions(harness_workspace.id)
    ConversationService().mark_session_read(session.id)


@pytest.mark.django_db(transaction=True)
async def test_accessor_factory_and_live_emit_recheck_fence(harness_workspace):
    from apps.harness.tests.test_accessor_runner import _RecordingRunnerService

    transport = _RecordingRunnerService()
    accessor = await create_harness_accessor(transport, str(harness_workspace.id))
    await accessor.list_dir("/workspace")
    await sync_to_async(Workspace.objects.filter(id=harness_workspace.id).update)(
        active_operation="capturing_image"
    )
    emitted = len(transport.emitted)
    with pytest.raises(ConflictError):
        await accessor.list_dir("/workspace")
    assert len(transport.emitted) == emitted
    with pytest.raises(ConflictError):
        await create_harness_accessor(transport, str(harness_workspace.id))


@pytest.mark.django_db
def test_reservation_uses_runner_then_workspace_lock(harness_workspace, monkeypatch):
    """A fence claimed at the shared runner boundary cannot be bypassed."""
    from apps.runners import workspace_admission_repository

    original_lock = workspace_admission_repository.lock_runner
    session = session_for(harness_workspace)

    def capture_at_lock(runner_id):
        runner = original_lock(runner_id)
        Workspace.objects.filter(id=harness_workspace.id).update(
            active_operation="capturing_image"
        )
        return runner

    monkeypatch.setattr(workspace_admission_repository, "lock_runner", capture_at_lock)
    with pytest.raises(ConflictError):
        HarnessService().reserve_run(session)
    assert HarnessSession.objects.get(id=session.id).status == "idle"


@pytest.mark.django_db
@pytest.mark.parametrize("scheduled", [False, True])
def test_unresolved_fence_blocks_reservation(harness_workspace, scheduled):
    from apps.runners.models import Task

    session = session_for(harness_workspace)
    task = Task.objects.create(
        runner=harness_workspace.runner,
        workspace=harness_workspace,
        type="stop_workspace",
        status="failed",
    )
    Workspace.objects.filter(id=harness_workspace.id).update(current_task=task)
    with pytest.raises(ConflictError, match="unresolved"):
        HarnessService().reserve_run(session, scheduled=scheduled)
    assert HarnessSession.objects.get(id=session.id).status == "idle"


@pytest.mark.django_db(transaction=True)
async def test_pending_question_cannot_resume_during_capture(harness_workspace):
    from apps.harness.repositories import QuestionRequestRepository

    session = session_for(harness_workspace)
    question = QuestionRequestRepository.create(
        session_id=session.id,
        organization_id=session.organization_id,
        questions=[{"question": "Continue?"}],
    )
    Workspace.objects.filter(id=harness_workspace.id).update(
        active_operation="capturing_image"
    )
    with pytest.raises(ConflictError):
        await HarnessService().interactions.resolve_question(
            session=session, question_id=question.id, answers=["yes"]
        )
    question.refresh_from_db()
    assert question.status == "pending"


@pytest.mark.django_db(transaction=True)
async def test_pending_permission_cannot_resume_during_capture(harness_workspace):
    from apps.harness.permissions.repositories import PermissionRequestRepository

    session = session_for(harness_workspace)
    request = PermissionRequestRepository.create(
        organization_id=session.organization_id,
        session_id=session.id,
        workspace_id=session.workspace_id,
        tool="bash",
        pattern="echo yes",
    )
    Workspace.objects.filter(id=harness_workspace.id).update(
        active_operation="capturing_image"
    )
    with pytest.raises(ConflictError):
        await HarnessService().interactions.resolve_permission(
            session=session, request_id=request.id, response="always"
        )
    request.refresh_from_db()
    assert request.status == "pending"


@pytest.mark.django_db
@pytest.mark.parametrize("child", [False, True])
def test_mode_change_preserves_inbox_notifications_and_capture_guard(
    harness_workspace, monkeypatch, child
):
    """Blocked changes never publish; permitted root changes refresh the inbox."""
    root = session_for(harness_workspace)
    session = session_for(harness_workspace, parent=root) if child else root
    service = SessionSettingsService()
    notifications = []
    monkeypatch.setattr(
        service.events, "emit_sync", notifications.append
    )
    Workspace.objects.filter(id=harness_workspace.id).update(
        active_operation="capturing_image"
    )
    with pytest.raises(ConflictError):
        service.set_mode(session.id, "plan")
    session.refresh_from_db()
    assert session.mode == "build"
    assert notifications == []

    Workspace.objects.filter(id=harness_workspace.id).update(active_operation=None)
    updated = service.set_mode(session.id, "plan")
    assert updated.mode == "plan"
    assert notifications == ([] if child else [harness_workspace.id])


@pytest.mark.django_db(transaction=True)
async def test_nested_abort_settles_before_capture_and_blocks_child_resume(
    harness_workspace,
):
    """Dev tree cancellation releases feature capture only after every run settles."""
    from apps.harness.tests.test_subagent_depth import _service
    from apps.harness.tests.test_subagent_lifecycle import checkpoint, start
    from apps.harness.tests.test_subagent_nested_lifecycle import (
        NestedProvider,
        assert_user_abort,
        tree,
    )
    from apps.runners.services.capture import CaptureService

    Workspace.objects.filter(id=harness_workspace.id).update()
    provider = NestedProvider()
    service, _, _ = _service(provider)
    root = session_for(harness_workspace)
    _, task = await start(service, root)
    try:
        await checkpoint(provider.grandchild_entered)
        sessions = tree(root)
        with pytest.raises(ConflictError, match="agent is active"):
            await sync_to_async(CaptureService.allocate)(
                harness_workspace.id,
                "blocked capture",
                has_busy_agent=HarnessSessionRepository.has_busy_for_workspace,
            )
        await service.abort_run(root.id)
        assert_user_abort(service, sessions)
        await sync_to_async(CaptureService.allocate)(
            harness_workspace.id,
            "after tree abort",
            has_busy_agent=HarnessSessionRepository.has_busy_for_workspace,
        )
        leaf = sessions[-1]
        before = HarnessMessageRepository.list_for_session(leaf.id)
        with pytest.raises(ConflictError, match="capturing image"):
            await service.start_run(leaf, "resume", max_depth=2)
        assert HarnessMessageRepository.list_for_session(leaf.id) == before
        assert not service._tasks and not service._admissions
    finally:
        if not task.done():
            await service.abort_run(root.id)


@pytest.mark.django_db(transaction=True)
async def test_capture_between_abort_join_and_gate_cleanup_keeps_tree_cleanup(
    harness_workspace, monkeypatch
):
    """A capture winning after idle must not strand a stopped child's user gates."""
    from apps.harness.permissions.repositories import PermissionRequestRepository
    from apps.harness.repositories import QuestionRequestRepository
    from apps.harness.tests.test_subagent_depth import _service
    from apps.harness.tests.test_subagent_lifecycle import checkpoint, start
    from apps.harness.tests.test_subagent_nested_lifecycle import (
        NestedProvider,
        assert_user_abort,
        tree,
    )
    from apps.runners.services.capture import CaptureService

    Workspace.objects.filter(id=harness_workspace.id).update()
    provider = NestedProvider()
    service, _, _ = _service(provider)
    root = session_for(harness_workspace)
    _, task = await start(service, root)
    captured = []
    original_reject = service.interactions.reject_pending
    try:
        await checkpoint(provider.grandchild_entered)
        sessions = tree(root)
        leaf = sessions[-1]
        permission = PermissionRequestRepository.create(
            organization_id=leaf.organization_id,
            session_id=leaf.id,
            workspace_id=leaf.workspace_id,
            tool="bash",
            pattern="echo approved",
        )
        question = QuestionRequestRepository.create(
            session_id=leaf.id,
            organization_id=leaf.organization_id,
            questions=[{"question": "Continue?"}],
        )

        async def reject_after_capture(session):
            if session.id == root.id:
                assert not HarnessSession.objects.filter(
                    workspace=harness_workspace, status="busy"
                ).exists()
                captured.append(
                    await sync_to_async(CaptureService.allocate)(
                        harness_workspace.id,
                        "during stop cleanup",
                        has_busy_agent=HarnessSessionRepository.has_busy_for_workspace,
                    )
                )
            await original_reject(session)

        monkeypatch.setattr(
            service.interactions, "reject_pending", reject_after_capture
        )
        await service.abort_run(root.id)
        assert len(captured) == 1
        assert_user_abort(service, sessions)
        permission.refresh_from_db()
        question.refresh_from_db()
        assert permission.status == "rejected"
        assert question.status == "rejected"
        # Only already-authorized internal cleanup is allowed through the fence.
        with pytest.raises(ConflictError, match="capturing image"):
            await service.abort_run(leaf.id)
    finally:
        if not task.done():
            await service.abort_run(root.id)
