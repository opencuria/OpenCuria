"""Tests for M6 harness socket payloads (fake frontend emit)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from apps.harness.harness_service import (
    FRONTEND_EVENT_PART,
    FRONTEND_EVENT_PERMISSION,
    FRONTEND_EVENT_STATUS,
    FRONTEND_EVENT_SUBTASK_FINISHED,
    FRONTEND_EVENT_SUBTASK_STARTED,
    FRONTEND_EVENT_TODO,
    HarnessService,
)
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import (
    Delta,
    ProviderAdapter,
    Usage,
)
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)


class FakeProvider(ProviderAdapter):
    """Immediate answer provider (no network)."""

    name = "fake"

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Yield one text delta."""
        yield Delta(text="socket-answer", usage=Usage(1, 1, 2))


@pytest.mark.django_db(transaction=True)
async def test_socket_event_payloads(harness_workspace) -> None:
    """Runner events persist to DB and forward shaped socket payloads."""
    emitted: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        emitted.append({"event": event, **data})

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_emit,
        provider_factory=lambda _org: FakeProvider(),
    )
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="sockets",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    await service.start_run(
        session,
        "socket run",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    by_event: dict[str, list[dict[str, Any]]] = {}
    for item in emitted:
        by_event.setdefault(item["event"], []).append(item)
    assert FRONTEND_EVENT_STATUS in by_event
    assert by_event[FRONTEND_EVENT_STATUS][0]["status"] == "busy"
    assert by_event[FRONTEND_EVENT_STATUS][-1]["status"] == "idle"
    part_events = by_event[FRONTEND_EVENT_PART]
    assert part_events
    assert all(
        e["workspace_id"] == str(harness_workspace.id)
        and e["session_id"] == str(session.id)
        for e in part_events
    )
    deltas = [e.get("delta", {}) for e in part_events]
    assert any("text" in d for d in deltas)
    assert any("step_finish" in d for d in deltas)


@pytest.mark.django_db(transaction=True)
async def test_todo_subtask_permission_socket_shapes(harness_workspace) -> None:
    """todo/subtask/permission events carry workspace+session ids."""
    emitted: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        emitted.append({"event": event, **data})

    service = HarnessService(emit=_emit)
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="shapes",
    )
    assistant = HarnessMessageRepository.create(session_id=session.id, role="assistant")
    await service._on_runner_event(
        session,
        assistant,
        {"type": "todo_updated", "step": 1, "todos": [{"content": "x"}]},
    )
    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "subtask_started",
            "subtask_id": "sub-1",
            "agent": "explore",
            "description": "research",
        },
    )
    part = HarnessPartRepository.list_for_session(session.id)
    assert any(p.type == "subtask" for p in part)
    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "subtask_finished",
            "subtask_id": "sub-1",
            "agent": "explore",
            "status": "completed",
            "summary": "found it",
        },
    )

    by_event: dict[str, list[dict[str, Any]]] = {}
    for item in emitted:
        by_event.setdefault(item["event"], []).append(item)
    todo = by_event[FRONTEND_EVENT_TODO][0]
    assert todo["workspace_id"] == str(harness_workspace.id)
    assert todo["session_id"] == str(session.id)
    assert todo["todos"] == [{"content": "x"}]
    started = by_event[FRONTEND_EVENT_SUBTASK_STARTED][0]
    assert started["subtask_id"] == "sub-1"
    assert started["part_id"]
    assert started.get("child_session_id", "") == ""
    finished = by_event[FRONTEND_EVENT_SUBTASK_FINISHED][0]
    assert finished["status"] == "completed"


@pytest.mark.django_db(transaction=True)
async def test_subtask_part_persists_child_session_id(harness_workspace) -> None:
    """subtask_started/finished store child_session_id on the part meta."""
    emitted: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        emitted.append({"event": event, **data})

    service = HarnessService(emit=_emit)
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
    )
    assistant = HarnessMessageRepository.create(session_id=session.id, role="assistant")
    service._runs[str(session.id)] = {
        "session_id": str(session.id),
        "message_id": str(assistant.id),
        "tool_parts": {},
        "step_parts": {},
        "subtask_parts": {},
    }
    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "subtask_started",
            "subtask_id": "sub-1",
            "agent": "explore",
            "description": "research",
            "child_session_id": "child-session-1",
        },
    )
    started_parts = [
        part
        for part in HarnessPartRepository.list_for_session(session.id)
        if part.type == "subtask"
    ]
    assert len(started_parts) == 1
    assert started_parts[0].meta.get("child_session_id") == "child-session-1"
    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "subtask_finished",
            "subtask_id": "sub-1",
            "agent": "explore",
            "status": "completed",
            "summary": "found it",
            "child_session_id": "child-session-1",
        },
    )
    finished_parts = [
        part
        for part in HarnessPartRepository.list_for_session(session.id)
        if part.type == "subtask"
    ]
    assert finished_parts[0].meta.get("child_session_id") == "child-session-1"
    assert finished_parts[0].meta.get("status") == "completed"
    assert finished_parts[0].state == "completed"


@pytest.mark.django_db(transaction=True)
async def test_permission_event_emitted_once_with_request_id(
    harness_workspace,
) -> None:
    """_on_permission emits a single harness.permission_required with request_id."""
    emitted: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        emitted.append({"event": event, **data})

    service = HarnessService(emit=_emit)
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="perm",
    )
    assistant = HarnessMessageRepository.create(session_id=session.id, role="assistant")
    permission_task = asyncio.create_task(
        service._on_permission(
            session,
            assistant,
            tool="bash",
            action="rm -rf /tmp/x",
            title="$ rm -rf /tmp/x",
            call_id="call-9",
        )
    )
    await asyncio.sleep(0.05)
    permission_events = [
        item for item in emitted if item["event"] == FRONTEND_EVENT_PERMISSION
    ]
    assert len(permission_events) == 1
    assert permission_events[0]["request_id"]
    assert permission_events[0]["tool"] == "bash"
    assert permission_events[0]["call_id"] == "call-9"
    await service.resolve_permission(
        session=session,
        request_id=uuid.UUID(permission_events[0]["request_id"]),
        response="once",
    )
    decision = await permission_task
    assert decision == "once"


@pytest.mark.django_db(transaction=True)
async def test_subtask_link_documents_child_session_shape(
    harness_workspace,
) -> None:
    """Child sessions link via parent FK (subtask_started/finished shape)."""
    parent = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
    )
    assert parent.parent_id is None
    child = HarnessSessionRepository.create(
        workspace_id=parent.workspace_id,
        organization_id=parent.organization_id,
        title="child",
        parent_id=parent.id,
    )
    assert child.parent_id == parent.id
