"""Durable terminal session registry boundary tests."""

from __future__ import annotations

import uuid

import pytest

from apps.runners.models import TerminalSession
from apps.runners.terminal_registry import TerminalSessionRepository


def test_terminal_event_handlers_are_registered_without_local_sid_state():
    from apps.runners.sio_server import _register_frontend_handlers

    class FakeSocket:
        def __init__(self):
            self.handlers = {"/frontend": {}}

        def on(self, event, handler=None, namespace=None):
            name_space = namespace or "/"
            self.handlers.setdefault(name_space, {})[event] = handler
            if handler is None:
                return lambda callback: self.on(
                    event, callback, namespace=name_space
                )
            return handler

    sio = FakeSocket()
    _register_frontend_handlers(sio)
    assert "frontend:subscribe_workspace" in sio.handlers["/frontend"]
    assert "frontend:terminal_attach" in sio.handlers["/frontend"]
    assert "frontend:terminal_input" in sio.handlers["/frontend"]


@pytest.mark.django_db(transaction=True)
def test_terminal_registry_reuses_session_and_rejects_wrong_runner(workspace):
    """The DB registry is process-independent and fences runner ownership."""
    from apps.runners.enums import TaskStatus, TaskType
    from apps.runners.models import Task

    task = Task.objects.create(
        runner=workspace.runner,
        workspace=workspace,
        type=TaskType.START_TERMINAL,
        status=TaskStatus.IN_PROGRESS,
    )
    terminal_id = uuid.uuid4()
    session = TerminalSession.objects.create(
        terminal_id=terminal_id,
        workspace=workspace,
        runner=workspace.runner,
        task=task,
        status=TerminalSession.Status.ACTIVE,
    )

    lookup = TerminalSessionRepository.get_active(workspace.id, terminal_id)
    assert lookup.pk == session.pk
    assert TerminalSessionRepository.record_output(
        workspace_id=str(workspace.id),
        terminal_id=str(terminal_id),
        runner_id=str(workspace.runner_id),
        sequence=1,
    ) == "accepted"
    assert TerminalSessionRepository.record_output(
        workspace_id=str(workspace.id),
        terminal_id=str(terminal_id),
        runner_id=str(uuid.uuid4()),
        sequence=2,
    ) == "unknown"
    assert TerminalSessionRepository.record_output(
        workspace_id=str(workspace.id),
        terminal_id=str(terminal_id),
        runner_id=str(workspace.runner_id),
        sequence=3,
    ) == "gap"


@pytest.mark.django_db(transaction=True)
def test_terminal_registry_enforces_one_live_session_per_workspace(workspace):
    """The conditional uniqueness constraint is the cross-process race fence."""
    from django.db import IntegrityError, transaction

    first = TerminalSession.objects.create(
        workspace=workspace,
        runner=workspace.runner,
        status=TerminalSession.Status.ACTIVE,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        TerminalSession.objects.create(
            workspace=workspace,
            runner=workspace.runner,
            status=TerminalSession.Status.STARTING,
        )
    first.status = TerminalSession.Status.CLOSED
    first.save(update_fields=["status"])
    TerminalSession.objects.create(
        workspace=workspace,
        runner=workspace.runner,
        status=TerminalSession.Status.STARTING,
    )
