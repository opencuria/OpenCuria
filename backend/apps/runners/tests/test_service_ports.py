"""Port/delegation contract tests for the runners service facade (Phase 5).

Only ports, delegation and facade contracts — no business-logic duplicates.
Covers constructor injection, the definition mutability guard,
repository-delegation methods (mocked repos), pending registry isolation,
and facade attribute reachability (whitelists, timeouts).
"""

from __future__ import annotations

import asyncio
import types
import uuid
from unittest.mock import MagicMock

import pytest

from apps.runners.services import RunnerService
from apps.runners.services.infra.harness_gateway import get_harness_gateway


class _FakeResolver:
    def __init__(self):
        self.calls = []

    def resolve_workspace_credentials(self, workspace):
        self.calls.append(workspace)
        return {"ok": True}


class _FakeHarnessGateway:
    def __init__(self, *, busy=False):
        self.calls = []
        self.busy = busy

    def route_harness_result(self, data):
        self.calls.append(("result", data))
        return True

    def route_harness_file_chunk(self, data):
        self.calls.append(("chunk", data))
        return True

    def route_stream_output(self, data):
        self.calls.append(("stream_output", data))
        return True

    def route_stream_closed(self, data):
        self.calls.append(("stream_closed", data))
        return True

    def fail_streams(self, matches_workspace, error):
        self.calls.append(("fail_streams", matches_workspace, error))

    async def desktop_action(self, runner_service, workspace_id, action, args):
        self.calls.append(("desktop_action", workspace_id, action, args))
        return {"ok": True}

    def has_busy_computeruse_session(self, workspace_id):
        self.calls.append(("busy", workspace_id))
        return False

    def has_busy_session(self, workspace_id):
        self.calls.append(("busy_session", workspace_id))
        return self.busy

    def busy_workspace_ids(self, workspace_ids):
        self.calls.append(("busy_workspaces", workspace_ids))
        return set(workspace_ids) if self.busy else set()

    async def abort_busy_computeruse(self, workspace_id):
        self.calls.append(("abort_computeruse", workspace_id))
        return [uuid.UUID(int=1)]


async def _fake_emit(event, data, workspace_id):
    return None


def test_constructor_injects_ports():
    resolver = _FakeResolver()
    gateway = _FakeHarnessGateway()
    svc = RunnerService(
        None,
        credential_resolver=resolver,
        harness_gateway=gateway,
        frontend_bus=_fake_emit,
    )
    assert svc.sio is None
    assert svc._credential_resolver is resolver
    assert svc._harness() is gateway
    assert svc._frontend_bus is _fake_emit


def test_constructor_defaults_and_positional():
    svc_default = RunnerService()
    assert svc_default.sio is None
    assert svc_default._credential_resolver is None
    assert svc_default._harness_gateway is None
    assert svc_default._harness() is get_harness_gateway()
    assert svc_default._frontend_bus is None
    sentinel = object()
    svc_positional = RunnerService(sentinel)
    assert svc_positional.sio is sentinel


def test_registered_harness_gateway_is_the_harness_adapter():
    from apps.harness.runner_gateway import RunnerHarnessGateway

    assert isinstance(get_harness_gateway(), RunnerHarnessGateway)


def test_harness_reply_and_stream_routing_use_gateway():
    gateway = _FakeHarnessGateway()
    svc = RunnerService(harness_gateway=gateway)
    result = {"request_id": "r1", "workspace_id": "w"}
    chunk = {"request_id": "r2", "workspace_id": "w"}
    assert svc._route_harness_reply("harness:stat_result", result) is True
    svc._validate_runner_file_chunk = MagicMock(return_value=True)
    assert svc._route_harness_reply("harness:read_file_chunk", chunk) is True
    assert svc._route_harness_reply("terminal:output", {}) is None
    assert gateway.calls == [("result", result), ("chunk", chunk)]


def test_fail_streams_for_runner_matches_owned_workspaces():
    gateway = _FakeHarnessGateway()
    svc = RunnerService(harness_gateway=gateway)
    runner_id = uuid.uuid4()
    owned, foreign = uuid.uuid4(), uuid.uuid4()
    svc.workspaces = MagicMock()
    svc.workspaces.get_runner_id.side_effect = lambda ws: (
        runner_id if ws == owned else uuid.uuid4()
    )
    svc.fail_streams_for_runner(str(runner_id))
    [(kind, matches, error)] = gateway.calls
    assert kind == "fail_streams"
    assert error == f"runner {runner_id} disconnected"
    assert matches(str(owned)) is True
    assert matches(str(foreign)) is False
    assert matches("not-a-uuid") is False


def test_fail_streams_for_runner_rejects_invalid_runner_id():
    gateway = _FakeHarnessGateway()
    RunnerService(harness_gateway=gateway).fail_streams_for_runner("bogus")
    assert gateway.calls == []


def test_viewer_rpc_maps_harness_failures_to_conflict():
    from apps.runners.exceptions import HarnessActionError
    from common.exceptions import ConflictError

    gateway = _FakeHarnessGateway()
    svc = RunnerService(harness_gateway=gateway)
    workspace_id = uuid.uuid4()
    assert asyncio.run(svc._viewer_rpc(workspace_id, "status", {})) == {"ok": True}
    assert gateway.calls == [("desktop_action", str(workspace_id), "status", {})]

    for error in (HarnessActionError("down"), TimeoutError()):
        async def _fail(*args, error=error):
            raise error

        gateway.desktop_action = _fail
        with pytest.raises(ConflictError, match="could not be confirmed"):
            asyncio.run(svc._viewer_rpc(workspace_id, "status", {}))


def test_take_desktop_control_checks_availability_before_gateway():
    from common.exceptions import ConflictError

    gateway = _FakeHarnessGateway()
    svc = RunnerService(harness_gateway=gateway)
    workspace = types.SimpleNamespace(
        id=uuid.uuid4(), status="running", active_operation=None, current_task_id=None
    )
    assert asyncio.run(svc.take_desktop_control(workspace)) == [uuid.UUID(int=1)]
    assert gateway.calls == [("abort_computeruse", workspace.id)]

    gateway.calls.clear()
    workspace.active_operation = "stopping"
    with pytest.raises(ConflictError):
        asyncio.run(svc.take_desktop_control(workspace))
    assert gateway.calls == []


@pytest.mark.django_db(transaction=True)
def test_lifecycle_admissions_ask_gateway_for_busy_agents(runner, user):
    """Capture, recreate and auto-stop take the busy check from the port."""
    from datetime import timedelta

    from asgiref.sync import async_to_sync
    from django.utils import timezone

    from apps.runners.models import Task
    from apps.runners.tests.test_image_versions import make_line, qemu_workspace
    from common.exceptions import ConflictError

    runner.organization.workspace_auto_stop_timeout_minutes = 1
    runner.organization.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    _, (v1,) = make_line(runner, user)
    workspace = qemu_workspace(runner, user, v1)
    workspace.last_activity_at = timezone.now() - timedelta(hours=1)
    workspace.save(update_fields=["last_activity_at"])
    gateway = _FakeHarnessGateway(busy=True)
    svc = RunnerService(harness_gateway=gateway)

    with pytest.raises(ConflictError, match="agent is active"):
        async_to_sync(svc.create_image_artifact)(workspace.id, "capture")
    with pytest.raises(ConflictError, match="agent is active"):
        async_to_sync(svc.recreate_workspace)(workspace.id, v1.id)
    with pytest.raises(ConflictError, match="no longer eligible"):
        async_to_sync(svc.stop_workspace)(workspace.id, auto_stop=True)

    assert gateway.calls == [("busy_session", workspace.id)] * 3
    workspace.refresh_from_db()
    assert workspace.active_operation is None
    assert not Task.objects.filter(workspace=workspace).exists()


def test_constructor_repositories_and_stores():
    svc = RunnerService()
    assert svc.runners is not None
    assert svc.workspaces is not None
    assert svc.tasks is not None
    assert svc.image_instances is not None
    assert svc.image_definitions is not None
    assert svc.build_jobs is not None
    assert svc.processes is not None
    assert isinstance(svc._process_pending, dict)
    assert isinstance(svc._git_pending, dict)
    assert isinstance(svc._pending_process_verify, dict)


def test_ensure_definition_mutable_rejects_frozen_definition():
    from apps.runners.models import ImageDefinition
    from common.exceptions import ConflictError

    frozen = types.SimpleNamespace(status=ImageDefinition.Status.DELETED)
    svc = RunnerService()
    with pytest.raises(ConflictError):
        svc._ensure_definition_mutable(frozen)
    with pytest.raises(ConflictError):
        svc.deactivate_runner_image(
            types.SimpleNamespace(image_definition=frozen, runner=object())
        )


def test_repository_delegation_methods_mocked():
    svc = RunnerService(harness_gateway=_FakeHarnessGateway())
    artifact_id = uuid.uuid4()
    svc.workspaces = MagicMock()
    workspace = types.SimpleNamespace(id=artifact_id)
    svc.workspaces.get_by_id.return_value = workspace

    assert svc.get_workspace_or_none(artifact_id) is workspace
    svc.workspaces.get_by_id.assert_called_with(artifact_id)


@pytest.mark.parametrize("busy", [False, True])
def test_workspace_list_reads_activity_in_one_gateway_batch(busy):
    """A workspace list must never perform a harness query per workspace."""
    gateway = _FakeHarnessGateway(busy=busy)
    svc = RunnerService(harness_gateway=gateway)
    workspaces = [types.SimpleNamespace(id=uuid.uuid4()) for _ in range(3)]
    svc.workspaces = MagicMock()
    svc.workspaces.list_for_scope.return_value = workspaces

    assert svc.list_workspaces() == workspaces
    assert gateway.calls == [("busy_workspaces", [ws.id for ws in workspaces])]
    assert all(ws.has_active_harness_session is busy for ws in workspaces)


def test_missing_workspace_and_empty_list_do_not_query_harness():
    """There is no harness activity to resolve when no workspace was found."""
    gateway = _FakeHarnessGateway()
    svc = RunnerService(harness_gateway=gateway)
    svc.workspaces = MagicMock()
    svc.workspaces.get_by_id.return_value = None
    svc.workspaces.list_for_scope.return_value = []

    assert svc.list_workspaces() == []
    assert svc.get_workspace_or_none(uuid.uuid4()) is None
    assert gateway.calls == []


def test_workspace_read_propagates_activity_lookup_failure():
    """A failed activity read must never silently report an idle workspace."""
    gateway = _FakeHarnessGateway()
    gateway.busy_workspace_ids = MagicMock(side_effect=RuntimeError("unavailable"))
    svc = RunnerService(harness_gateway=gateway)
    svc.workspaces = MagicMock()
    svc.workspaces.get_by_id.return_value = types.SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(RuntimeError, match="unavailable"):
        svc.get_workspace_or_none(uuid.uuid4())


def test_pending_registries_are_instance_local():
    first, second = RunnerService(), RunnerService()
    registries = ("_process_pending", "_git_pending", "_pending_process_verify")
    for name in registries:
        getattr(first, name)["request"] = object()
        assert getattr(second, name) == {}


def test_facade_constants_reachable():
    svc = RunnerService()
    assert "list_repos" in svc.GIT_OPERATIONS
    assert "list_repos" in svc.GIT_READ_OPERATIONS
    assert isinstance(svc._GIT_ALLOWED_ARGS, dict)
    assert svc.BUILD_LOG_MAX_CHARS == 200_000
    assert svc._PROCESS_RPC_TIMEOUT_SECONDS == 30
    assert svc._STREAM_CHUNK_SIZE == 64 * 1024
    assert isinstance(RunnerService._active_terminals, dict)
    assert isinstance(RunnerService._active_desktops, dict)
