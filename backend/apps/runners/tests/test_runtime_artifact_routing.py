"""End-to-end in-process routing for trusted runtime artifact RPCs."""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from apps.harness.access.runner_accessor import (
    _ACCESSORS_BY_REQUEST,
    RunnerWorkspaceAccessor,
    create_harness_accessor,
)
from apps.runners.services import RunnerService
from common.utils import hash_token


class _WorkspaceService:
    """Minimal runner service for the WebSocket handler, no guest or network."""

    supported_runtimes: list[str] = []

    def __init__(self) -> None:
        self.ensure_runtime_artifact = AsyncMock(
            return_value={
                "ok": True,
                "path": "/opt/opencuria/runtimes/claude-agent/2.1.292/claude",
                "version": "2.1.292",
                "platform": "linux-x64",
            }
        )


@pytest.fixture
def runner_websocket_interface(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Load the real runner handler with only unrelated host metrics stubbed."""
    # The backend and runner use separate virtualenvs.  ``psutil`` is used by
    # periodic host-metrics code only, which this request/response test never
    # calls, so provide its small import surface when the backend venv lacks it.
    if "psutil" not in sys.modules:
        psutil = types.ModuleType("psutil")
        psutil.cpu_percent = lambda *args, **kwargs: 0  # type: ignore[attr-defined]
        psutil.virtual_memory = lambda: None  # type: ignore[attr-defined]
        psutil.disk_usage = lambda *_args: None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "psutil", psutil)

    runner_root = Path(__file__).resolve().parents[4] / "runner"
    monkeypatch.syspath_prepend(str(runner_root))
    from src.config import RunnerSettings
    from src.interfaces.websocket import WebSocketInterface

    service = _WorkspaceService()
    interface = WebSocketInterface(
        service, RunnerSettings(state_dir=str(tmp_path / "runner-state"))
    )
    interface._sio.emit = AsyncMock()
    return interface, service


@pytest.fixture
def backend_sio_artifact_handler(db, monkeypatch: pytest.MonkeyPatch):
    """Register backend's real runner reply handler and authenticate one SID."""
    from django.contrib.auth import get_user_model

    import apps.runners.sio_server as sio_server
    from apps.organizations.models import Organization
    from apps.runners.enums import RunnerStatus, WorkspaceStatus
    from apps.runners.models import Runner, Workspace

    organization = Organization.objects.create(
        name=f"Artifact RPC {uuid.uuid4().hex[:8]}",
        slug=f"artifact-rpc-{uuid.uuid4().hex[:10]}",
    )
    user = get_user_model().objects.create_user(
        email=f"artifact-rpc-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    runner = Runner.objects.create(
        name="artifact-rpc-runner",
        api_token_hash=hash_token(f"artifact-rpc-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid="artifact-rpc-sid",
        organization=organization,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Artifact RPC Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    service = RunnerService()
    sio = sio_server.socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(sio)
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    monkeypatch.setattr(
        sio,
        "get_session",
        AsyncMock(return_value={"runner_id": str(runner.id)}),
    )

    return {
        "handler": sio.handlers["/"]["workspace:artifact_ensure_result"],
        "service": service,
        "runner": runner,
        "workspace": workspace,
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_runner_handler_reply_routes_through_owned_backend_to_accessor(
    runner_websocket_interface, backend_sio_artifact_handler
) -> None:
    runner_interface, workspace_service = runner_websocket_interface
    backend = backend_sio_artifact_handler
    workspace = backend["workspace"]

    async def runner_emit(event: str, reply: dict) -> None:
        assert event == "workspace:artifact_ensure_result"
        await backend["handler"]("artifact-rpc-sid", reply)

    runner_interface._sio.emit = AsyncMock(side_effect=runner_emit)

    async def backend_emit(event: str, request: dict, *, to: str) -> None:
        assert to == "artifact-rpc-sid"
        assert event == "workspace:artifact_ensure"
        runner_reply = await runner_interface._sio.handlers["/"][event](request)
        assert runner_reply["ok"] is True
        assert workspace_service.ensure_runtime_artifact.await_count == 1

    backend["service"].sio = AsyncMock()
    backend["service"].sio.emit = AsyncMock(side_effect=backend_emit)
    accessor = await create_harness_accessor(backend["service"], str(workspace.id))
    result = await accessor.ensure_runtime_artifact("claude-agent", "2.1.292")

    assert result["ok"] is True
    assert result["workspace_id"] == str(workspace.id)
    assert result["request_id"]
    assert result["version"] == "2.1.292"
    assert result["path"] == ("/opt/opencuria/runtimes/claude-agent/2.1.292/claude")
    request_id = result["request_id"]
    assert request_id not in accessor._pending
    assert request_id not in _ACCESSORS_BY_REQUEST


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_artifact_reply_from_foreign_runner_is_dropped(
    runner_websocket_interface, backend_sio_artifact_handler
) -> None:
    runner_interface, _workspace_service = runner_websocket_interface
    backend = backend_sio_artifact_handler
    workspace_id = str(backend["workspace"].id)
    transport_events: list[tuple[str, dict]] = []

    async def emit(event: str, payload: dict) -> None:
        transport_events.append((event, payload))

    accessor = RunnerWorkspaceAccessor(workspace_id, emit=emit)
    # Ensure a genuine live request is registered, then send the exact reply
    # as if a different (authenticated) runner had emitted it.
    task = asyncio.create_task(
        accessor.ensure_runtime_artifact("claude-agent", "2.1.292")
    )
    await asyncio.sleep(0)
    request_id = transport_events[0][1]["request_id"]
    routed_before = accessor._pending[request_id]
    backend["service"].handle_harness_reply(
        "workspace:artifact_ensure_result",
        {
            "request_id": request_id,
            "workspace_id": workspace_id,
            "ok": True,
            "version": "2.1.292",
        },
        runner_id=str(uuid.uuid4()),
    )
    assert accessor._pending[request_id] is routed_before
    assert not routed_before.done()

    # A subsequent correctly-owned result settles the same waiter.
    backend["service"].handle_harness_reply(
        "workspace:artifact_ensure_result",
        {
            "request_id": request_id,
            "workspace_id": workspace_id,
            "ok": True,
            "version": "2.1.292",
            "path": "/opt/opencuria/runtimes/claude-agent/2.1.292/claude",
        },
        runner_id=str(backend["runner"].id),
    )
    result = await task
    assert result["ok"] is True
    assert request_id not in accessor._pending
    assert request_id not in _ACCESSORS_BY_REQUEST


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_artifact_reply_with_malformed_request_id_cannot_resolve_waiter(
    runner_websocket_interface, backend_sio_artifact_handler
) -> None:
    runner_interface, _workspace_service = runner_websocket_interface
    backend = backend_sio_artifact_handler
    workspace_id = str(backend["workspace"].id)
    emitted: list[tuple[str, dict]] = []

    async def emit(event: str, payload: dict) -> None:
        emitted.append((event, payload))
        if event == "workspace:artifact_ensure":
            # Produce a nominally successful result with another reply ID.
            await backend["handler"](
                "artifact-rpc-sid",
                {
                    "workspace_id": workspace_id,
                    "request_id": "malformed-request-id",
                    "ok": True,
                    "version": "2.1.292",
                    "path": "/opt/opencuria/runtimes/claude-agent/2.1.292/claude",
                },
            )

    accessor = RunnerWorkspaceAccessor(workspace_id, emit=emit)
    task = asyncio.create_task(
        accessor.ensure_runtime_artifact("claude-agent", "2.1.292")
    )
    await asyncio.sleep(0)
    request_id = emitted[0][1]["request_id"]
    assert request_id != "malformed-request-id"
    pending = accessor._pending[request_id]
    assert not pending.done()

    # The real runner handler does echo the caller's ID, and the correct value
    # completes the correlated call; the malformed one above cannot do so.
    reply = await runner_interface._sio.handlers["/"]["workspace:artifact_ensure"](
        emitted[0][1]
    )
    await backend["handler"]("artifact-rpc-sid", reply)
    result = await task
    assert result["request_id"] == request_id
    assert request_id not in accessor._pending
    assert request_id not in _ACCESSORS_BY_REQUEST


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_backend_never_forwards_artifact_reply_to_frontend(
    backend_sio_artifact_handler,
) -> None:
    backend = backend_sio_artifact_handler
    frontend = AsyncMock()
    backend["service"]._frontend_bus = frontend

    await backend["handler"](
        "artifact-rpc-sid",
        {
            "request_id": "no-frontend",
            "workspace_id": str(backend["workspace"].id),
            "ok": True,
            "version": "2.1.292",
        },
    )

    frontend.assert_not_called()
