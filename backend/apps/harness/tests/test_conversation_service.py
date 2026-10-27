"""Characterize saved-conversation mutations independently of agent execution."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from asgiref.sync import sync_to_async

from apps.harness.conversation_events import ConversationEvents
from apps.harness.conversation_service import ConversationService
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessSessionRepository,
)
from apps.runners.services.infra import realtime
from common.exceptions import NotFoundError


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["mark_session_read", "mark_session_unread"])
@pytest.mark.parametrize("is_child", [False, True], ids=["root", "child"])
def test_read_mutations_notify_after_persistence(
    harness_workspace, action: str, is_child: bool
) -> None:
    """Only root read changes publish, after the updated flag is visible."""
    root = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    session = (
        HarnessSessionRepository.create(
            workspace_id=root.workspace_id,
            organization_id=root.organization_id,
            parent_id=root.id,
        )
        if is_child
        else root
    )
    emitted = []

    async def emit(event: str, data: dict, workspace_id: str) -> None:
        stored = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
        if action == "mark_session_read":
            assert stored.last_read_at is not None
            assert stored.manual_unread_at is None
        else:
            assert stored.manual_unread_at is not None
        emitted.append((event, data))

    service = ConversationService(events=ConversationEvents(emit=emit))
    updated = getattr(service, action)(session.id)

    assert updated.id == session.id
    assert emitted == (
        []
        if is_child
        else [
            (
                "harness.conversations_changed",
                {"workspace_id": str(harness_workspace.id)},
            )
        ]
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "action", ["mark_session_read", "mark_session_unread", "dismiss_notice"]
)
def test_missing_conversation_preserves_not_found_contract(action: str) -> None:
    """Navigation mutations identify a missing session before its message."""
    session_id = uuid.uuid4()
    args = (session_id, uuid.uuid4()) if action == "dismiss_notice" else (session_id,)

    with pytest.raises(NotFoundError) as error:
        getattr(ConversationService(), action)(*args)

    assert error.value.message == f"HarnessSession '{session_id}' not found"


@pytest.mark.django_db(transaction=True)
async def test_read_mutation_schedules_notification_on_running_loop(
    harness_workspace,
) -> None:
    """A synchronous navigation call also works when its caller owns a loop."""
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    emitted = []

    async def emit(event: str, data: dict, workspace_id: str) -> None:
        emitted.append((event, data))

    ConversationService(events=ConversationEvents(emit=emit)).mark_session_read(
        session.id
    )
    await asyncio.sleep(0)

    assert emitted == [
        ("harness.conversations_changed", {"workspace_id": str(session.workspace_id)})
    ]


@pytest.mark.django_db
def test_notification_transport_failure_keeps_read_mutation(
    harness_workspace, monkeypatch
) -> None:
    """Unavailable fan-out must not undo a persisted navigation action."""
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    attempted = []

    async def fail(event: str, data: dict, workspace_id: str) -> None:
        attempted.append((event, data, workspace_id))
        raise RuntimeError("transport unavailable")

    monkeypatch.setattr(realtime, "emit_to_frontend", fail)
    ConversationService().mark_session_read(session.id)

    stored = HarnessSessionRepository.get_by_id(session.id)
    assert stored.last_read_at is not None
    assert attempted == [
        (
            "harness.conversations_changed",
            {"workspace_id": str(session.workspace_id)},
            str(session.workspace_id),
        )
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state,releases,deleted",
    [
        ("empty", True, True),
        ("empty", False, False),
        ("message", True, False),
        ("child", True, False),
        ("busy", True, False),
    ],
)
def test_discard_unstarted_deletes_only_empty_idle_roots_its_creator_releases(
    harness_workspace, state: str, releases: bool, deleted: bool
) -> None:
    """The creator's release runs under admission locks after harness checks."""
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    if state == "message":
        HarnessMessageRepository.create(
            session_id=session.id, role="user", content="Real chat"
        )
    elif state == "child":
        HarnessSessionRepository.create(
            workspace_id=session.workspace_id,
            organization_id=session.organization_id,
            parent_id=session.id,
        )
    elif state == "busy":
        HarnessSessionRepository.mark_status(session, "busy")
    released = []

    def release() -> bool:
        released.append(session.id)
        return releases

    assert (
        ConversationService().discard_unstarted(
            session.id,
            workspace_id=session.workspace_id,
            organization_id=session.organization_id,
            release=release,
        )
        is deleted
    )
    assert (HarnessSessionRepository.get_by_id(session.id) is None) is deleted
    assert released == ([session.id] if state == "empty" else [])
