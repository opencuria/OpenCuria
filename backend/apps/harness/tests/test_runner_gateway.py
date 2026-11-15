"""Tests for the harness implementation of the runners ``HarnessGateway``."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.harness.access import runner_accessor
from apps.harness.repositories import HarnessSessionRepository
from apps.harness.runner_gateway import RunnerHarnessGateway
from apps.runners.exceptions import HarnessActionError
from apps.runners.models import Workspace


class _StreamAccessor:
    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self.errors: list[str] = []

    def fail_all_streams(self, error: str) -> None:
        self.errors.append(error)


def test_fail_streams_only_fails_matching_workspaces(monkeypatch):
    target, other = _StreamAccessor("ws-a"), _StreamAccessor("ws-b")
    monkeypatch.setattr(
        runner_accessor, "_ACCESSORS_BY_STREAM", {"c1": target, "c2": other}
    )
    RunnerHarnessGateway().fail_streams(lambda ws: ws == "ws-a", "gone")
    assert target.errors == ["gone"]
    assert other.errors == []


def test_routing_delegates_to_accessor_registry(monkeypatch):
    calls = []
    for name in (
        "route_harness_result",
        "route_harness_file_chunk",
        "route_stream_output",
        "route_stream_closed",
    ):
        monkeypatch.setattr(
            runner_accessor,
            name,
            lambda data, name=name: calls.append((name, data)) or True,
        )
    gateway = RunnerHarnessGateway()
    payload = {"request_id": "r"}
    assert gateway.route_harness_result(payload) is True
    assert gateway.route_harness_file_chunk(payload) is True
    assert gateway.route_stream_output(payload) is True
    assert gateway.route_stream_closed(payload) is True
    assert [name for name, _ in calls] == [
        "route_harness_result",
        "route_harness_file_chunk",
        "route_stream_output",
        "route_stream_closed",
    ]


def test_desktop_action_translates_accessor_errors(monkeypatch):
    accessor = SimpleNamespace(desktop_action=None)
    created = []

    async def _create(service, workspace_id):
        created.append((service, workspace_id))
        return accessor

    monkeypatch.setattr(runner_accessor, "create_harness_accessor", _create)
    service = object()

    async def _ok(action, args):
        return {"ok": True, "action": action, "args": args}

    accessor.desktop_action = _ok
    result = asyncio.run(
        RunnerHarnessGateway().desktop_action(service, "ws", "status", {"a": 1})
    )
    assert result == {"ok": True, "action": "status", "args": {"a": 1}}
    assert created == [(service, "ws")]

    async def _fail(action, args):
        raise runner_accessor.RunnerAccessorError("desktop unavailable")

    accessor.desktop_action = _fail
    with pytest.raises(HarnessActionError, match="desktop unavailable"):
        asyncio.run(
            RunnerHarnessGateway().desktop_action(service, "ws", "status", {})
        )


@pytest.mark.parametrize("sessions, expected", [([], False), ([object()], True)])
def test_has_busy_computeruse_session(monkeypatch, sessions, expected):
    lookup = Mock(return_value=sessions)
    monkeypatch.setattr(
        HarnessSessionRepository, "list_busy_computeruse_for_workspace", lookup
    )
    workspace_id = uuid.uuid4()
    assert RunnerHarnessGateway().has_busy_computeruse_session(workspace_id) is expected
    lookup.assert_called_once_with(workspace_id)


@pytest.mark.django_db
def test_has_busy_session_sees_any_busy_session_in_the_workspace(
    harness_workspace,
):
    gateway = RunnerHarnessGateway()
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    assert gateway.has_busy_session(harness_workspace.id) is False
    HarnessSessionRepository.mark_status(session, "busy")
    assert gateway.has_busy_session(harness_workspace.id) is True
    assert gateway.has_busy_session(uuid.uuid4()) is False


@pytest.mark.django_db
def test_busy_workspace_batch_is_scoped_and_includes_children(
    harness_workspace, django_assert_num_queries
):
    """Busy children count once, idle/foreign/missing workspaces stay absent."""
    gateway = RunnerHarnessGateway()
    idle = Workspace.objects.create(
        runner=harness_workspace.runner, created_by=harness_workspace.created_by
    )
    root = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    for _ in range(2):
        child = HarnessSessionRepository.create(
            workspace_id=harness_workspace.id,
            organization_id=root.organization_id,
            parent_id=root.id,
        )
        HarnessSessionRepository.mark_status(child, "busy")
    HarnessSessionRepository.create(
        workspace_id=idle.id, organization_id=root.organization_id
    )

    with django_assert_num_queries(1):
        assert gateway.busy_workspace_ids(
            [harness_workspace.id, idle.id, uuid.uuid4()]
        ) == {harness_workspace.id}
    with django_assert_num_queries(1):
        assert gateway.busy_workspace_ids([idle.id]) == set()
    with django_assert_num_queries(0):
        assert gateway.busy_workspace_ids([]) == set()


def test_abort_busy_computeruse_returns_aborted_session_ids(monkeypatch):
    aborted = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    calls = []

    async def _abort(workspace_id):
        calls.append(workspace_id)
        return aborted

    monkeypatch.setattr(
        "apps.harness.runner_gateway.get_harness_service",
        lambda: SimpleNamespace(abort_busy_computeruse_for_workspace=_abort),
    )
    workspace_id = uuid.uuid4()
    result = asyncio.run(RunnerHarnessGateway().abort_busy_computeruse(workspace_id))
    assert result == [session.id for session in aborted]
    assert calls == [workspace_id]
