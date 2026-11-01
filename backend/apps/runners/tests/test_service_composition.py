"""Independent runner services communicate through constructor-injected ports."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.runners.enums import WorkspaceStatus
from apps.runners.services import RunnerRepositories, RunnerService
from apps.runners.services.domains.process_manager import ProcessManagerService
from apps.runners.services.infra.ownership import OwnershipService
from apps.runners.services.infra.rpc_registry import RpcRegistryService
from apps.runners.services.session_state import InteractiveSessionState
from common.exceptions import ConflictError


def process_service(transport: AsyncMock) -> ProcessManagerService:
    """Build only the process domain, without constructing a RunnerService."""
    return ProcessManagerService(
        processes=Mock(),
        workspaces=Mock(),
        frontend_bus=Mock(),
        harness=Mock(),
        ownership=Mock(),
        rpc_registry=RpcRegistryService(),
        runner_transport=transport,
    )


@pytest.mark.asyncio
async def test_process_rpc_composes_transport_and_reply_registry() -> None:
    """A transport sends the request; a separately owned registry resolves it."""
    transport = AsyncMock()
    service = process_service(transport)
    runner = object()
    received = {"pid": 123}

    async def deliver(actual_runner, event, payload):
        assert actual_runner is runner
        assert event == "harness:process_start"
        assert payload == {"request_id": "request"}
        service.rpc_registry.resolve_process_future("request", received)

    transport.emit_to_runner.side_effect = deliver
    result = await service._await_process_result(
        request_id="request",
        event="harness:process_start",
        payload={"request_id": "request"},
        runner=runner,
        timeout=1,
    )

    assert result == received
    assert service.rpc_registry.process_pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "transport", "cancel"])
async def test_process_rpc_cleans_registry_on_unsuccessful_delivery(failure) -> None:
    """Failed and cancelled sends cannot leave orphaned correlation futures."""
    transport = AsyncMock()
    service = process_service(transport)
    pending = []

    async def deliver(*_args):
        pending.append(service.rpc_registry.process_pending["request"])
        if failure == "transport":
            raise RuntimeError("connection lost")
        if failure == "cancel":
            raise asyncio.CancelledError()

    transport.emit_to_runner.side_effect = deliver
    expected = {
        "timeout": ConflictError,
        "transport": RuntimeError,
        "cancel": asyncio.CancelledError,
    }[failure]
    with pytest.raises(expected):
        await service._await_process_result(
            request_id="request",
            event="harness:process_start",
            payload={},
            runner=object(),
            timeout=0.001,
        )

    assert service.rpc_registry.process_pending == {}
    assert len(pending) == 1 and pending[0].cancelled()


@pytest.mark.asyncio
async def test_ownership_service_rejects_foreign_desktop_runner() -> None:
    """Authorization is independently testable without transport or lifecycle."""
    owner, stranger, workspace_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspaces = Mock()
    workspaces.get_runner_id.return_value = owner
    service = OwnershipService(
        workspaces=workspaces,
        state=InteractiveSessionState(),
        operation_label=lambda operation: operation or "busy",
    )

    assert await service.desktop_event_owned_by_runner(str(workspace_id), str(owner))
    assert not await service.desktop_event_owned_by_runner(
        str(workspace_id), str(stranger)
    )
    assert not await service.desktop_event_owned_by_runner("bad", str(owner))


def test_ownership_service_rejects_lifecycle_hold() -> None:
    """A lifecycle hold remains an explicit admission dependency."""
    service = OwnershipService(
        workspaces=Mock(),
        state=InteractiveSessionState(),
        operation_label=lambda operation: operation.replace("_", " "),
    )
    workspace = SimpleNamespace(
        id=uuid.uuid4(),
        status=WorkspaceStatus.RUNNING,
        active_operation="capturing_image",
        current_task_id=uuid.uuid4(),
    )
    with pytest.raises(ConflictError, match="capturing image"):
        service.ensure_workspace_available(workspace)


def test_composition_owns_all_mutable_state_per_instance() -> None:
    """Two facades never share RPC, credentials, terminals or desktop epochs."""
    first, second = RunnerService(), RunnerService()
    first.session_store.state.terminals["workspace"] = "terminal"
    first.session_store.state.desktops["workspace"] = {"port": 5900}
    first.session_store.state.runner_epochs["runner"] = "epoch"
    first.rpc_registry.process_pending["request"] = object()
    first.rpc_registry.git_pending["request"] = object()
    first.credential_sync.pending_inject.add(("runner", "workspace"))
    first.heartbeat_reconciler.pending_process_verify["workspace"] = []
    first.git_operations.git_locks["workspace"] = asyncio.Lock()

    assert second.session_store.state == InteractiveSessionState()
    assert second.rpc_registry.process_pending == {}
    assert second.rpc_registry.git_pending == {}
    assert second.credential_sync.pending_inject == set()
    assert second.heartbeat_reconciler.pending_process_verify == {}
    assert second.git_operations.git_locks == {}
    assert first.ownership.state is first.session_store.state
    assert first.interactive_sessions.state is first.session_store.state


def test_repository_injection_reaches_only_explicit_consumers() -> None:
    """Composition can replace persistence without building a partial facade."""
    workspaces = Mock()
    workspaces.get_by_id.return_value = None
    service = RunnerService(
        repositories=replace(RunnerRepositories(), workspaces=workspaces)
    )
    assert service.get_workspace_or_none(uuid.uuid4()) is None
    assert service.ownership.workspaces is workspaces
    assert service.process_manager.workspaces is workspaces
    assert service.rpc_registry.__dict__ == {"process_pending": {}, "git_pending": {}}


@pytest.mark.asyncio
async def test_reply_acceptance_tracks_live_registry_ownership() -> None:
    """Only a validated reply that reaches a live waiter is acknowledged."""
    service = process_service(AsyncMock())
    future = asyncio.get_running_loop().create_future()
    service.rpc_registry.process_pending["request"] = future
    payload = {"request_id": "request", "workspace_id": str(uuid.uuid4())}
    service.ownership.validate_harness_workspace_runner.return_value = False

    assert service.rpc_registry.owns_request("process", "request")
    assert not service.handle_process_reply(
        "harness:process_start_result", payload, runner_id=str(uuid.uuid4())
    )
    assert not future.done()
    service.ownership.validate_harness_workspace_runner.return_value = True
    assert service.handle_process_reply(
        "harness:process_start_result", payload, runner_id=str(uuid.uuid4())
    )
    assert future.result() is payload
    assert not service.rpc_registry.owns_request("process", "request")
    assert not service.handle_process_reply("harness:process_start_result", payload)
    assert not service.rpc_registry.owns_request("git", "unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_id", ["", None, "not-a-uuid"])
async def test_authenticated_process_reply_requires_workspace(workspace_id) -> None:
    """An authenticated sender cannot skip ownership by omitting its workspace."""
    service = process_service(AsyncMock())
    future = asyncio.get_running_loop().create_future()
    service.rpc_registry.process_pending["request"] = future
    assert not service.handle_process_reply(
        "harness:process_start_result",
        {"request_id": "request", "workspace_id": workspace_id},
        runner_id=str(uuid.uuid4()),
    )
    service.ownership.validate_harness_workspace_runner.assert_not_called()
    assert not future.done()
    future.cancel()
