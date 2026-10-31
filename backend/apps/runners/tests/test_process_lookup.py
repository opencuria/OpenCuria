"""Characterize process identity precedence and workspace/session scoping."""

from __future__ import annotations

import uuid

import pytest

from apps.runners.enums import ProcessStatus
from apps.runners.exceptions import WorkspaceNotFoundError
from apps.runners.models import Workspace, WorkspaceProcess
from apps.runners.services import RunnerService

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def lookup_records(workspace, runner) -> dict:
    """Create colliding names and UUID-shaped names in distinct scopes."""
    own_session = uuid.uuid4()
    other_session = uuid.uuid4()
    foreign_workspace = Workspace.objects.create(
        runner=runner, name="Foreign", created_by=workspace.created_by
    )

    def record(name: str, **fields) -> WorkspaceProcess:
        return WorkspaceProcess.objects.create(
            workspace=fields.pop("workspace", workspace),
            name=name,
            command="sleep 60",
            status=ProcessStatus.EXITED,
            **fields,
        )

    persistent = record("worker")
    own_temp = record("worker", kind="temp", session_id=own_session)
    other_temp = record("worker", kind="temp", session_id=other_session)
    foreign = record("foreign", workspace=foreign_workspace)
    return {
        "workspace": workspace,
        "own_session": own_session,
        "other_session": other_session,
        "absent_session": uuid.uuid4(),
        "persistent": persistent,
        "own_temp": own_temp,
        "other_temp": other_temp,
        "foreign": foreign,
        "uuid_name": record(str(persistent.id)),
        "foreign_uuid_name": record(str(foreign.id)),
        "other_session_uuid_name": record(str(other_temp.id)),
        "case_sensitive": record("Worker"),
    }


@pytest.mark.parametrize(
    "identifier,kind,session,expected",
    [
        ("persistent_id", None, None, "persistent"),
        ("persistent_uuid", None, "own_session", "persistent"),
        ("persistent_id", "persistent", "own_session", "persistent"),
        ("persistent_id", "temp", "own_session", None),
        ("worker", None, None, "persistent"),
        ("worker", None, "own_session", "own_temp"),
        ("worker", None, "other_session", "other_temp"),
        ("worker", None, "absent_session", "persistent"),
        ("worker", "persistent", "own_session", "persistent"),
        ("worker", "temp", "own_session", "own_temp"),
        ("worker", "temp", None, None),
        ("worker", "temp", "absent_session", None),
        ("own_temp_id", None, "own_session", "own_temp"),
        ("own_temp_uuid", "temp", "own_session", "own_temp"),
        ("own_temp_id", None, "other_session", None),
        ("own_temp_id", "persistent", "own_session", None),
        # User controls can address a temp by UUID without an agent session.
        ("own_temp_id", None, None, "own_temp"),
        ("own_temp_id", "temp", None, "own_temp"),
        ("other_temp_id", None, "own_session", "other_session_uuid_name"),
        ("other_temp_id", "persistent", None, "other_session_uuid_name"),
        ("other_temp_id", "temp", "own_session", None),
        ("foreign_id", None, None, "foreign_uuid_name"),
        ("foreign_id", None, "own_session", "foreign_uuid_name"),
        ("foreign", None, None, None),
        ("Worker", None, None, "case_sensitive"),
        ("WORKER", None, None, None),
        ("missing", None, None, None),
    ],
)
async def test_process_resolution_precedence_and_scope(
    lookup_records: dict,
    identifier: str,
    kind: str | None,
    session: str | None,
    expected: str | None,
) -> None:
    """Preserve UUID priority, exact names, session fallback and not-found errors."""
    records = lookup_records
    value: str | uuid.UUID = identifier
    if identifier.endswith(("_id", "_uuid")):
        name, representation = identifier.rsplit("_", 1)
        value = records[name].id
        if representation == "id":
            value = str(value)
    # Exercise the same session string input used by the agent accessor.
    session_id = str(records[session]) if session else None
    service = RunnerService()
    if expected is None:
        with pytest.raises(WorkspaceNotFoundError) as error:
            await service._resolve_process(
                records["workspace"].id, value, kind=kind, session_id=session_id
            )
        assert error.value.identifier == str(value)
    else:
        process = await service._resolve_process(
            records["workspace"].id, value, kind=kind, session_id=session_id
        )
        assert process.id == records[expected].id


async def test_process_lookup_rejects_invalid_session_before_resolving(
    workspace,
) -> None:
    """Malformed session identities keep the existing validation error."""
    with pytest.raises(ValueError, match="Invalid session_id: invalid"):
        await RunnerService()._resolve_process(
            workspace.id, "worker", session_id="invalid"
        )
