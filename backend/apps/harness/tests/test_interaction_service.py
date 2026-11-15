"""Characterize user interactions independently of agent execution."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from apps.harness.interaction_service import InteractionService
from apps.harness.models import HarnessMessage, HarnessSession
from apps.harness.permissions.repositories import PermissionRequestRepository
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessSessionRepository,
    QuestionRequestRepository,
)
from common.exceptions import NotFoundError

pytestmark = pytest.mark.django_db(transaction=True)

InteractionSetup = tuple[
    InteractionService,
    HarnessSession,
    HarnessMessage,
    list[tuple[str, dict[str, Any]]],
    asyncio.Queue[dict[str, Any]],
]


@pytest.fixture
def interaction_setup(harness_workspace: Any) -> InteractionSetup:
    """Use real persistence and capture the existing frontend event contract."""
    events: list[tuple[str, dict[str, Any]]] = []
    emitted: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def emit(name: str, data: dict[str, Any], _workspace_id: str) -> None:
        events.append((name, data))
        await emitted.put(data)

    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="Interaction",
        agent_name="build",
    )
    assistant = HarnessMessageRepository.create(session_id=session.id, role="assistant")
    return (
        InteractionService(emit=emit, is_running=lambda _session_id: False),
        session,
        assistant,
        events,
        emitted,
    )


@pytest.mark.parametrize("kind", ["permission", "question"])
@pytest.mark.parametrize("state", ["missing", "foreign", "resolved"])
async def test_resolve_requires_pending_request_from_same_session(
    interaction_setup: InteractionSetup, kind: str, state: str
) -> None:
    """Unknown, settled and other-session IDs cannot emit or change a gate."""
    service, session, _, events, _ = interaction_setup
    values = {
        "organization_id": session.organization_id,
        "session_id": uuid.uuid4() if state == "foreign" else session.id,
        "workspace_id": session.workspace_id,
    }
    if kind == "question":
        row = QuestionRequestRepository.create(
            **values, questions=[{"question": "Go?"}]
        )
        if state == "resolved":
            QuestionRequestRepository.resolve(row, answers=["yes"], status="answered")
        resolve = service.resolve_question(
            session=session,
            question_id=uuid.uuid4() if state == "missing" else row.id,
            answers=["changed"],
        )
    else:
        row = PermissionRequestRepository.create(**values, tool="bash", pattern="ls")
        if state == "resolved":
            PermissionRequestRepository.mark_resolved(
                row, approved=True, remember="once"
            )
        resolve = service.resolve_permission(
            session=session,
            request_id=uuid.uuid4() if state == "missing" else row.id,
            response="reject",
        )
    previous_status = row.status
    with pytest.raises(NotFoundError):
        await resolve
    row.refresh_from_db()
    assert row.status == previous_status
    assert events == []


async def test_question_rejection_wakes_waiter(
    interaction_setup: InteractionSetup,
) -> None:
    """Declining rejects the persisted request and raises in the waiting tool."""
    service, session, assistant, events, emitted = interaction_setup
    task = asyncio.create_task(
        service.ask_question(
            session, assistant, questions=[{"question": "Continue?"}], call_id="call-1"
        )
    )
    pending = await asyncio.wait_for(emitted.get(), timeout=2)
    outcome = await service.resolve_question(
        session=session,
        question_id=uuid.UUID(pending["request_id"]),
        answers=[],
        reject=True,
    )
    with pytest.raises(ValueError, match="Question rejected by user"):
        await asyncio.wait_for(task, timeout=2)
    assert outcome == {
        "request_id": pending["request_id"],
        "status": "rejected",
        "resumed": True,
    }
    row = QuestionRequestRepository.get_by_id(uuid.UUID(pending["request_id"]))
    assert row.status == "rejected"
    assert service._pending_questions == {}
    assert events[-1] == (
        "harness.question_required",
        {
            "workspace_id": str(session.workspace_id),
            "session_id": str(session.id),
            "root_session_id": str(session.id),
            "request_id": pending["request_id"],
            "status": "rejected",
        },
    )


async def test_cancelled_question_rejects_persistence_and_clears_waiter(
    interaction_setup: InteractionSetup,
) -> None:
    """Tool timeout/cancellation cannot leave a pending question behind."""
    service, session, assistant, events, emitted = interaction_setup
    task = asyncio.create_task(
        service.ask_question(session, assistant, questions=[{"question": "Continue?"}])
    )
    pending = await asyncio.wait_for(emitted.get(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = QuestionRequestRepository.get_by_id(uuid.UUID(pending["request_id"]))
    assert row.status == "rejected"
    assert row.answers == []
    assert service._pending_questions == {}
    assert events[-1][1]["status"] == "rejected"


async def test_question_without_waiter_still_persists_answer(
    interaction_setup: InteractionSetup,
) -> None:
    """Restarted processes report resumed=False while settling durable state."""
    service, session, _, events, _ = interaction_setup
    row = QuestionRequestRepository.create(
        organization_id=session.organization_id,
        session_id=session.id,
        questions=[{"question": "Languages?", "multiple": True}],
    )
    outcome = await service.resolve_question(
        session=session, question_id=row.id, answers=["Python\x00Go"]
    )
    row.refresh_from_db()
    assert outcome["resumed"] is False
    assert row.status == "answered"
    assert row.answers == ["Python, Go"]
    assert events[-1][1]["status"] == "answered"


async def test_message_edit_cancels_only_removed_interaction_waiters(
    interaction_setup: InteractionSetup,
) -> None:
    """Suffix cleanup cancels both gate kinds while earlier questions still work."""
    service, session, assistant, _, emitted = interaction_setup
    tasks: list[asyncio.Task] = []
    try:
        tasks.append(
            asyncio.create_task(
                service.request_permission(
                    session, assistant, tool="bash", action="git status"
                )
            )
        )
        permission = await asyncio.wait_for(emitted.get(), timeout=2)
        for title in ("Removed?", "Keep?"):
            tasks.append(
                asyncio.create_task(
                    service.ask_question(
                        session, assistant, questions=[{"question": title}]
                    )
                )
            )
            if title == "Removed?":
                removed = await asyncio.wait_for(emitted.get(), timeout=2)
            else:
                kept = await asyncio.wait_for(emitted.get(), timeout=2)

        service.cancel_waiters(
            [uuid.UUID(permission["request_id"])],
            [uuid.UUID(removed["request_id"])],
        )
        for task in tasks[:2]:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        assert not tasks[2].done()
        outcome = await service.resolve_question(
            session=session,
            question_id=uuid.UUID(kept["request_id"]),
            answers=["still works"],
        )
        assert outcome["resumed"] is True
        assert await asyncio.wait_for(tasks[2], timeout=2) == ["still works"]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
