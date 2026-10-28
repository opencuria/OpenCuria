"""Characterize the harness state consumed by native notification delivery."""

import asyncio
import uuid
from dataclasses import FrozenInstanceError, asdict
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from django.utils import timezone

from apps.harness.conversation_activity_service import ConversationActivityService
from apps.harness.models import HarnessMessage, HarnessSession, QuestionRequest
from apps.harness.permissions.models import PermissionRequest

from ..models import PushDelivery
from ..services import NotificationService
from ..worker import NotificationWorker

pytestmark = pytest.mark.django_db


def test_source_selects_latest_assistant_and_hides_non_root_completions(setup):
    org, _, workspace, session, first, _ = setup
    latest = HarnessMessage.objects.create(
        session=session, role="assistant", position=2
    )
    user_message = HarnessMessage.objects.create(
        session=session, role="user", position=3
    )
    child = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, parent=session
    )
    child_message = HarnessMessage.objects.create(session=child, role="assistant")

    assert ConversationActivityService().source(session.id)[1].id == latest.id
    assert ConversationActivityService().message_source(first.id)[1].id == first.id
    assert ConversationActivityService().source(child.id) is None
    assert ConversationActivityService().source(uuid.uuid4()) is None
    assert ConversationActivityService().message_source(user_message.id) is None
    assert ConversationActivityService().message_source(child_message.id) is None
    assert ConversationActivityService().message_source(uuid.uuid4()) is None


def test_root_resolution_has_no_depth_cap_and_rejects_cycles(setup):
    org, _, workspace, root, _, _ = setup
    child = root
    for _ in range(12):
        child = HarnessSession.objects.create(
            workspace=workspace, organization_id=org.id, parent=child
        )
    assert ConversationActivityService().root_id(child.id) == root.id
    assert ConversationActivityService().root_id(uuid.uuid4()) is None
    HarnessSession.objects.filter(id=root.id).update(parent=child)
    assert ConversationActivityService().root_id(child.id) is None


def test_active_rows_use_busy_descendant_gates_and_permission_priority(setup):
    org, user, workspace, session, message, _ = setup
    activity = ConversationActivityService()
    child = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, parent=session, status="idle"
    )
    QuestionRequest.objects.create(
        organization_id=org.id, workspace_id=workspace.id, session_id=child.id
    )
    assert activity.active_rows(org.id, user.id)[0][1] == "running"
    HarnessSession.objects.filter(id=child.id).update(status="busy")
    assert activity.active_rows(org.id, user.id)[0][1] == "waiting_for_input"
    PermissionRequest.objects.create(
        organization_id=org.id,
        workspace_id=workspace.id,
        session_id=child.id,
        tool="exec",
    )
    assert activity.active_rows(org.id, user.id)[0][1] == "waiting_for_approval"
    HarnessMessage.objects.filter(id=message.id).update(completed_at=timezone.now())
    assert activity.active_rows(org.id, user.id) == []
    HarnessMessage.objects.create(session=session, role="assistant", position=2)
    assert activity.active_rows(org.id, user.id)[0][0].id == session.id


@pytest.mark.parametrize(
    "model,phase",
    [
        (PermissionRequest, "waiting_for_approval"),
        (QuestionRequest, "waiting_for_input"),
    ],
)
def test_pending_gate_requires_exact_scope_and_pending_state(setup, model, phase):
    org, _, workspace, session, _, _ = setup
    gate = model.objects.create(
        organization_id=org.id,
        workspace_id=workspace.id,
        session_id=session.id,
        **({"tool": "exec"} if model is PermissionRequest else {}),
    )
    check = ConversationActivityService().pending_gate
    assert check(str(gate.id), phase, org.id, str(workspace.id))
    assert not check(str(gate.id), phase, uuid.uuid4(), str(workspace.id))
    assert not check(str(gate.id), phase, org.id, str(uuid.uuid4()))
    assert not check("invalid", phase, org.id, str(workspace.id))
    assert not check(str(gate.id), phase, org.id, "invalid")
    model.objects.filter(id=gate.id).update(status="resolved")
    assert not check(str(gate.id), phase, org.id, str(workspace.id))


def test_repair_limits_completion_and_attention_history(setup):
    org, _, workspace, session, message, _ = setup
    old = timezone.now() - timedelta(minutes=16)
    HarnessMessage.objects.filter(id=message.id).update(completed_at=old)
    recent = HarnessMessage.objects.create(
        session=session, role="assistant", position=2, completed_at=timezone.now()
    )
    child = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, parent=session, status="busy"
    )
    HarnessMessage.objects.create(
        session=child, role="assistant", completed_at=timezone.now()
    )
    permission = PermissionRequest.objects.create(
        organization_id=org.id,
        workspace_id=workspace.id,
        session_id=child.id,
        tool="exec",
    )
    question = QuestionRequest.objects.create(
        organization_id=org.id, workspace_id=workspace.id, session_id=session.id
    )
    assert ConversationActivityService().recent_completions() == [recent.id]
    assert ConversationActivityService().recent_attention_events() == [
        (
            "harness.permission_required",
            {"request_id": str(permission.id), "session_id": str(child.id)},
        ),
        (
            "harness.question_required",
            {"request_id": str(question.id), "session_id": str(session.id)},
        ),
    ]
    PermissionRequest.objects.filter(id=permission.id).update(created_at=old)
    HarnessSession.objects.filter(id=session.id).update(status="idle")
    assert ConversationActivityService().recent_attention_events() == []


def test_activity_results_are_immutable_metadata_without_lazy_queries(
    setup, django_assert_num_queries
):
    org, user, workspace, session, message, _ = setup
    message.content = "Private assistant response"
    message.error = "Private provider error"
    message.save()
    conversation, assistant = ConversationActivityService().message_source(message.id)
    with django_assert_num_queries(0):
        assert asdict(conversation) == {
            "id": session.id,
            "organization_id": org.id,
            "workspace_id": workspace.id,
            "workspace_owner_id": user.id,
            "workspace_name": workspace.name,
            "title": session.title,
            "status": session.status,
        }
        assert asdict(assistant) == {
            "id": message.id,
            "completed_at": None,
            "phase": "error",
        }
    with pytest.raises(FrozenInstanceError):
        conversation.title = "Changed"
    with pytest.raises(FrozenInstanceError):
        assistant.phase = "completed"


@pytest.mark.parametrize(
    "finish,error,phase",
    [
        ("abort", "error", "interrupted"),
        ("aborted", "error", "interrupted"),
        ("cancelled", "error", "interrupted"),
        ("interrupted", "error", "interrupted"),
        ("error", "", "error"),
        ("stop", "error", "error"),
        ("stop", "", "completed"),
    ],
)
def test_completion_outcomes_preserve_abort_precedence(setup, finish, error, phase):
    message = setup[4]
    completed_at = timezone.now()
    HarnessMessage.objects.filter(id=message.id).update(
        finish=finish, error=error, completed_at=completed_at
    )
    _, assistant = ConversationActivityService().message_source(message.id)
    assert assistant.phase == phase
    assert assistant.completed_at == completed_at


@pytest.mark.django_db(transaction=True)
def test_worker_repairs_missed_harness_events_through_activity_service(setup, device):
    org, _, workspace, session, message, _ = setup
    HarnessMessage.objects.filter(id=message.id).update(completed_at=timezone.now())
    question = QuestionRequest.objects.create(
        organization_id=org.id, workspace_id=workspace.id, session_id=session.id
    )
    service = NotificationService()
    service.process_one = AsyncMock(side_effect=asyncio.CancelledError)
    worker = NotificationWorker(service=service)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._run())
    assert set(
        PushDelivery.objects.filter(device=device, kind="alert").values_list(
            "event_key", flat=True
        )
    ) == {f"finished:{message.id}", f"gate:{question.id}:waiting_for_input"}
