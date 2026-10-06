import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.mcp_app.server import _call_create_image_artifact
from common.exceptions import ConflictError, NotFoundError


@pytest.mark.parametrize("error_type", [None, ConflictError, NotFoundError, ValueError])
def test_capture_reports_outcome_and_always_closes_loop(monkeypatch, error_type):
    workspace = SimpleNamespace(id=uuid.uuid4())
    task = SimpleNamespace(id=uuid.uuid4())
    org_id = uuid.uuid4()
    capture = AsyncMock(return_value=(workspace, task))
    if error_type:
        capture.side_effect = (
            NotFoundError("Workspace", "Capture diagnostic")
            if error_type is NotFoundError
            else error_type("Capture diagnostic")
        )
    monkeypatch.setattr(
        "apps.mcp_app.server._get_owned_workspace_or_error",
        lambda *args: (workspace, None),
    )
    monkeypatch.setattr(
        "apps.runners.sio_server.get_runner_service",
        lambda: SimpleNamespace(create_image_artifact=capture),
    )
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio, "new_event_loop", lambda: loop)
    result = _call_create_image_artifact(
        SimpleNamespace(),
        org_id,
        {"workspace_id": str(workspace.id), "name": "Snapshot"},
    )
    assert loop.is_closed()
    capture.assert_awaited_once_with(
        workspace_id=workspace.id,
        name="Snapshot",
        organization_id=org_id,
        captured_image_id=None,
        message="",
    )
    if error_type:
        assert "Capture diagnostic" in result[0].text
    else:
        assert json.loads(result[0].text) == {
            "task_id": str(task.id),
            "workspace_id": str(workspace.id),
        }


def test_capture_unexpected_error_also_closes_loop(monkeypatch):
    workspace = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(
        "apps.mcp_app.server._get_owned_workspace_or_error",
        lambda *args: (workspace, None),
    )
    monkeypatch.setattr(
        "apps.runners.sio_server.get_runner_service",
        lambda: SimpleNamespace(
            create_image_artifact=AsyncMock(side_effect=RuntimeError("unexpected"))
        ),
    )
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio, "new_event_loop", lambda: loop)
    with pytest.raises(RuntimeError, match="unexpected"):
        _call_create_image_artifact(
            SimpleNamespace(),
            uuid.uuid4(),
            {"workspace_id": str(workspace.id), "name": "Snapshot"},
        )
    assert loop.is_closed()
