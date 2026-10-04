"""Viewer intent identity, permissions and no-resurrection regressions."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from apps.runners.desktop_proxy import _viewer_query, build_vnc_redirect_url
from apps.runners.schemas import DesktopViewerIntentIn
from apps.runners.services import RunnerService
from common.exceptions import ConflictError


def test_identity_scopes_client_to_user_and_workspace():
    workspace, other_workspace, client = [uuid.uuid4() for _ in range(3)]
    identity = RunnerService.viewer_identity(workspace, 1, client)
    assert identity == RunnerService.viewer_identity(workspace, 1, client)
    assert (
        identity["lease_id"]
        != RunnerService.viewer_identity(workspace, 2, client)["lease_id"]
    )
    assert (
        identity["lease_id"]
        != RunnerService.viewer_identity(other_workspace, 1, client)["lease_id"]
    )
    assert identity["owner_id"] == "1"
    assert uuid.UUID(identity["lease_id"]).version == 5


@pytest.mark.parametrize("revision", [0, -1, True, "1", 1.5])
def test_revision_must_be_positive_integer(revision):
    with pytest.raises(ValidationError):
        DesktopViewerIntentIn(viewer_client_id=uuid.uuid4(), intent_revision=revision)


def test_redirect_preserves_viewer_in_nested_websocket_path():
    workspace, client = str(uuid.uuid4()), str(uuid.uuid4())
    redirect = build_vnc_redirect_url(workspace, "jwt", client, 7)
    outer = parse_qs(urlparse(redirect).query)
    nested = parse_qs(urlparse(outer["path"][0]).query)
    assert outer["viewer_client_id"] == nested["viewer_client_id"] == [client]
    assert nested["intent_revision"] == ["7"]
    assert _viewer_query(urlparse(outer["path"][0]).query) == {
        "viewer_client_id": client,
        "intent_revision": 7,
    }
    assert _viewer_query("token=jwt") == {}
    assert _viewer_query("viewer_client_id=bad&intent_revision=1") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "expired", "released", "closing"])
async def test_renew_does_not_recreate_ended_intent(state):
    service = RunnerService(sio_server=Mock())
    service._viewer_workspace = AsyncMock()
    service._viewer_rpc = AsyncMock(
        return_value={"lease_state": state, "revision": 3, "epoch": "epoch"}
    )
    with pytest.raises(ConflictError):
        await service.renew_desktop(
            uuid.uuid4(),
            user=SimpleNamespace(id=1),
            organization_id=uuid.uuid4(),
            viewer_client_id=uuid.uuid4(),
            intent_revision=3,
        )
    assert [call.args[1] for call in service._viewer_rpc.await_args_list] == [
        "lease_status"
    ]


@pytest.mark.asyncio
async def test_renew_only_existing_matching_revision_and_status_is_private():
    service = RunnerService(sio_server=Mock())
    service._viewer_workspace = AsyncMock()
    service._viewer_rpc = AsyncMock(
        side_effect=[
            {"lease_state": "held", "revision": 4, "epoch": "e"},
            {"lease_state": "held", "revision": 4, "epoch": "e"},
            {"lease_state": "held", "revision": 4, "epoch": "e", "owner_id": "secret"},
        ]
    )
    workspace, org, client = [uuid.uuid4() for _ in range(3)]
    kwargs = dict(
        user=SimpleNamespace(id=1), organization_id=org, viewer_client_id=client
    )
    result = await service.renew_desktop(workspace, **kwargs, intent_revision=4)
    assert result == {"viewer_lease_state": "held", "revision": 4, "epoch": "e"}
    assert service._viewer_rpc.await_args_list[1].args[1] == "renew"
    assert service._viewer_rpc.await_args_list[1].args[2]["epoch"] == "e"
    result = await service.viewer_desktop_status(workspace, **kwargs)
    assert result["viewer_lease_state"] == "held"
    assert "owner_id" not in result and "lease_id" not in result
    service._viewer_workspace.assert_awaited()


@pytest.mark.asyncio
async def test_service_auth_rejects_other_owner_and_org(monkeypatch):
    from apps.organizations.services import OrganizationService
    from apps.runners.exceptions import WorkspaceNotFoundError

    org = uuid.uuid4()
    workspace = SimpleNamespace(
        created_by_id=2, runner=SimpleNamespace(organization_id=org, is_online=True)
    )
    service = RunnerService(sio_server=Mock())
    service.get_workspace = Mock(return_value=workspace)
    service._ensure_workspace_available = Mock()
    monkeypatch.setattr(OrganizationService, "require_membership", Mock())
    role = Mock(return_value="member")
    monkeypatch.setattr(OrganizationService, "get_user_role", role)
    user = SimpleNamespace(id=1)
    with pytest.raises(WorkspaceNotFoundError):
        await service._viewer_workspace(uuid.uuid4(), user=user, organization_id=org)
    role.return_value = "admin"
    assert (
        await service._viewer_workspace(uuid.uuid4(), user=user, organization_id=org)
        is workspace
    )
    with pytest.raises(WorkspaceNotFoundError):
        await service._viewer_workspace(
            uuid.uuid4(), user=user, organization_id=uuid.uuid4()
        )


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["start", "stop", "renew", "status"])
def test_mcp_requires_terminal_permission(action):
    from apps.mcp_app.server import _call_viewer_desktop

    api_key = SimpleNamespace(has_permission=lambda permission: False)
    result = _call_viewer_desktop(api_key, uuid.uuid4(), {}, action)
    assert "Permission denied" in result[0].text


@pytest.mark.asyncio
async def test_desktop_events_preserve_process_aggregates(monkeypatch):
    """Partial viewer events must not erase MCP or remaining viewer holders."""
    from apps.runners import sio_server

    service = RunnerService(sio_server=Mock())
    workspace = str(uuid.uuid4())
    emit = AsyncMock()
    monkeypatch.setattr(sio_server, "emit_to_frontend", emit)
    service._desktop_event_owned_by_runner = AsyncMock(return_value=True)
    service.tasks.get_by_id = Mock(return_value=None)
    await service.handle_desktop_process(
        workspace,
        6901,
        "ip",
        "net",
        runner_id="runner",
        viewer=True,
        mcp=True,
        holder_count=3,
        generation=4,
        epoch="runner-epoch",
    )
    assert service.get_desktop_info(workspace)["mcp"] is True
    assert emit.await_args.args[1]["holder_count"] == 3
    await service.handle_desktop_started(
        None, workspace, 6901, "ip", "net", runner_id="runner"
    )
    assert service.get_desktop_info(workspace)["mcp"] is True
    assert service.get_desktop_info(workspace)["generation"] == 4
    await service.handle_desktop_viewer_released(
        str(uuid.uuid4()),
        workspace,
        runner_id="runner",
        viewer_held=True,
        mcp=True,
        holder_count=2,
        generation=4,
        epoch="runner-epoch",
    )
    summary = emit.await_args.args[1]
    assert summary["viewer_held"] is True
    assert summary["mcp_active"] is True
    assert summary["holder_count"] == 2
    assert summary["epoch"] == "runner-epoch"
    assert service.get_desktop_info(workspace)["viewer"] is True
    await service.handle_desktop_process(
        workspace,
        6901,
        "ip",
        "net",
        runner_id="runner",
        mcp=False,
        holder_count=0,
        generation=5,
        epoch="new-epoch",
    )
    assert service.get_desktop_info(workspace)["mcp"] is False
    assert emit.await_args.args[1]["generation"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event,method",
    [
        ("desktop:started", "handle_desktop_started"),
        ("desktop:process", "handle_desktop_process"),
        ("desktop:viewer_released", "handle_desktop_viewer_released"),
    ],
)
async def test_socket_events_propagate_aggregate_metadata(monkeypatch, event, method):
    import socketio

    from apps.runners import sio_server

    server = socketio.AsyncServer(async_mode="asgi")
    sio_server._register_event_handlers(server)
    service = SimpleNamespace(**{method: AsyncMock()})
    monkeypatch.setattr(sio_server, "get_runner_service", lambda: service)
    monkeypatch.setattr(
        sio_server, "_require_runner_id", AsyncMock(return_value="runner")
    )
    await server.handlers["/"][event](
        "sid",
        {
            "workspace_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "mcp_active": True,
            "holder_count": 3,
            "generation": 8,
            "epoch": "epoch",
            "viewer_held": True,
        },
    )
    kwargs = getattr(service, method).await_args.kwargs
    assert kwargs["mcp"] is True
    assert kwargs["holder_count"] == 3
    assert kwargs["generation"] == 8
    assert kwargs["epoch"] == "epoch"
    if event == "desktop:viewer_released":
        assert kwargs["viewer_held"] is True
