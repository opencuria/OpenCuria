"""User process policy can be enforced without REST, runner I/O or persistence."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.runners.enums import ProcessKind, ProcessStatus
from apps.runners.exceptions import WorkspaceNotFoundError
from apps.runners.services import RunnerService
from apps.runners.services.process_controls import (
    ProcessControlService,
    TemporaryProcessControlError,
)
from common.exceptions import ConflictError, NotFoundError


@pytest.fixture
def controls() -> SimpleNamespace:
    """Bind the user controls to a fake execution service and actor."""
    runners = Mock(spec=RunnerService)
    for name in (
        "list_processes",
        "get_process",
        "stop_process",
        "restart_process",
        "delete_process",
    ):
        setattr(runners, name, AsyncMock())
    return SimpleNamespace(
        service=ProcessControlService(runners),
        runners=runners,
        workspace_id=uuid.uuid4(),
        scope={"user": SimpleNamespace(id=42), "organization_id": uuid.uuid4()},
    )


@pytest.mark.parametrize("live", [None, False, True])
async def test_user_visibility_preserves_persistent_rows_and_running_temps(
    controls, live
) -> None:
    """Every persistent status remains visible; only running temps are included."""
    rows = [
        SimpleNamespace(kind=kind, status=status)
        for kind in ProcessKind
        for status in ProcessStatus
    ]
    controls.runners.list_processes.return_value = rows
    result = await controls.service.list_processes(
        controls.workspace_id,
        **controls.scope,
        **({"live": live} if live is not None else {}),
    )
    assert result == rows[:4] + [rows[4]]
    controls.runners.list_processes.assert_awaited_once_with(
        controls.workspace_id, live=bool(live)
    )
    controls.runners.get_workspace_for_user.assert_called_once_with(
        controls.workspace_id, **controls.scope
    )


@pytest.mark.parametrize(
    "method", ["list_processes", "stop_process", "restart_process", "delete_process"]
)
async def test_owner_scope_is_checked_before_any_process_lookup(
    controls, method
) -> None:
    """A non-owner or foreign org cannot trigger a lookup or a runner RPC."""
    controls.runners.get_workspace_for_user.side_effect = WorkspaceNotFoundError(
        str(controls.workspace_id)
    )
    args = () if method == "list_processes" else ("process",)
    with pytest.raises(NotFoundError) as error:
        await getattr(controls.service, method)(
            controls.workspace_id, *args, **controls.scope
        )
    assert error.value.message == f"Workspace '{controls.workspace_id}' not found"
    assert [call[0] for call in controls.runners.mock_calls] == [
        "get_workspace_for_user"
    ]


@pytest.mark.parametrize("method", ["restart_process", "delete_process"])
async def test_agent_temps_are_rejected_before_dispatch(controls, method) -> None:
    """User controls never invoke agent mutations on a temporary process."""
    controls.runners.get_process.return_value = SimpleNamespace(kind="temp")
    with pytest.raises(TemporaryProcessControlError) as error:
        await getattr(controls.service, method)(
            controls.workspace_id, "agent-process", **controls.scope
        )
    assert error.value.code == "validation_error"
    controls.runners.get_process.assert_awaited_once_with(
        controls.workspace_id, "agent-process"
    )
    getattr(controls.runners, method).assert_not_awaited()


@pytest.mark.parametrize(
    "method", ["stop_process", "restart_process", "delete_process"]
)
@pytest.mark.parametrize("id_or_name", ["my-server", uuid.uuid4()])
async def test_user_controls_preserve_identity_and_execution_result(
    controls, method, id_or_name
) -> None:
    """Both names and UUIDs reach execution; restart retains the initiating user."""
    controls.runners.get_process.return_value = SimpleNamespace(kind="persistent")
    execute = getattr(controls.runners, method)
    result = await getattr(controls.service, method)(
        controls.workspace_id, id_or_name, **controls.scope
    )
    assert result is execute.return_value
    execute.assert_awaited_once_with(
        controls.workspace_id,
        id_or_name,
        **({"user": controls.scope["user"]} if method == "restart_process" else {}),
    )


async def test_user_can_stop_temp_without_a_preliminary_live_lookup(controls) -> None:
    """Stopping a temp never depends on get_process or rejects its agent scope."""
    controls.runners.get_process.side_effect = AssertionError("unneeded live lookup")
    await controls.service.stop_process(
        controls.workspace_id, "temp-uuid", **controls.scope
    )
    controls.runners.stop_process.assert_awaited_once_with(
        controls.workspace_id, "temp-uuid"
    )


async def test_live_lookup_failure_prevents_user_mutation(controls) -> None:
    """The existing preflight failure propagates without restarting the process."""
    failure = ConflictError("workspace busy")
    controls.runners.get_process.side_effect = failure
    with pytest.raises(ConflictError) as error:
        await controls.service.restart_process(
            controls.workspace_id, "my-server", **controls.scope
        )
    assert error.value is failure
    controls.runners.restart_process.assert_not_awaited()
