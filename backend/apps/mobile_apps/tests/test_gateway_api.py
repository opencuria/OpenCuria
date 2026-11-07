"""Gateway sessions over REST, the ASGI gateway and publish-time backend hints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import pytest
from asgiref.sync import async_to_sync

from apps.accounts.models import APIKeyPermission
from apps.mobile_apps import gateway
from apps.mobile_apps.backends import AppBackendService, BackendError, BackendTarget
from apps.mobile_apps.export import backend_labels
from apps.mobile_apps.gateway_protocol import SESSION_HEADER
from apps.mobile_apps.services import AppService

from .conftest import FakeReader, SocketStream, build_files
from .test_backends import FakeRunners, make_app, process

RUN = APIKeyPermission.WORKSPACES_PROCESSES_RUN.value


def session_url(app_id) -> str:
    return f"/api/v1/apps/{app_id}/gateway-session/"


def test_owner_gets_a_gateway_session(app_world):
    app = make_app(app_world)
    response = app_world.client(permissions=[RUN]).post(session_url(app.id))
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == f"/ws/app-backends/{app.id}"
    assert body["domain"] == "opencuria.local"
    claims = AppBackendService.verify_session(body["token"], str(app.id))
    assert claims.owner_id == app_world.owner.id


def test_gateway_sessions_need_permission_ownership_and_a_workspace(app_world):
    app = make_app(app_world)
    denied = app_world.client(permissions=[APIKeyPermission.HARNESS_READ.value]).post(
        session_url(app.id)
    )
    assert denied.status_code == 403
    foreign = app_world.client(app_world.stranger, permissions=[RUN]).post(
        session_url(app.id)
    )
    assert foreign.status_code == 404
    orphan = make_app(app_world, workspace=False)
    gone = app_world.client(permissions=[RUN]).post(session_url(orphan.id))
    assert gone.status_code == 409


# -- ASGI gateway ------------------------------------------------------------


class StubService:
    """Backend service double for the ASGI adapter."""

    def __init__(self, port: int, *, error: BackendError | None = None) -> None:
        self.port = port
        self.error = error
        self.forgotten: list[BackendTarget] = []

    verify_session = staticmethod(AppBackendService.verify_session)

    async def resolve(self, claims, address):
        if self.error:
            raise self.error
        return BackendTarget(
            workspace_id=uuid.uuid4(),
            process_id=uuid.uuid4(),
            process_name="api",
            pid=1,
            port=self.port,
            address=address,
        )

    async def open_stream(self, target, *, websocket=False):
        reader, writer = await asyncio.open_connection("127.0.0.1", target.port)
        return SocketStream(reader, writer)

    def forget(self, target):
        self.forgotten.append(target)

    @contextlib.asynccontextmanager
    async def keep_active(self, workspace_id):
        yield


async def call_gateway(path: str, token: str = "", method: str = "GET") -> list[dict]:
    inbox: asyncio.Queue = asyncio.Queue()
    inbox.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    headers = [(SESSION_HEADER.encode(), token.encode())] if token else []
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
    }
    await gateway.app_backend_gateway(scope, inbox.get, send)
    return sent


def token_for(world, app) -> str:
    return (
        AppBackendService(runners=lambda: FakeRunners())
        .issue_session(app.id, organization_id=world.org.id, owner_id=world.owner.id)
        .token
    )


@pytest.mark.django_db(transaction=True)
async def test_gateway_forwards_authorized_requests(workspace_process, monkeypatch):
    from asgiref.sync import sync_to_async

    from .conftest import build_world

    world = await sync_to_async(build_world)()
    app = await sync_to_async(make_app)(world)
    token = await sync_to_async(token_for)(world, app)
    monkeypatch.setattr(
        gateway, "get_app_backend_service", lambda: StubService(workspace_process.port)
    )

    sent = await call_gateway(f"/ws/app-backends/{app.id}/api/80/echo", token)
    assert sent[0]["status"] == 200
    assert workspace_process.requests[0]["path_qs"] == "/echo"

    refused = await call_gateway(f"/ws/app-backends/{app.id}/api/80/echo")
    assert refused[0]["status"] == 401
    assert json.loads(refused[1]["body"])["code"] == "backend_session_invalid"

    invalid = await call_gateway(f"/ws/app-backends/{app.id}/Bad_Name/80/", token)
    assert invalid[0]["status"] == 404


@pytest.mark.django_db(transaction=True)
async def test_gateway_reports_backend_errors_and_unreachable_ports(monkeypatch):
    from asgiref.sync import sync_to_async

    from .conftest import build_world

    world = await sync_to_async(build_world)()
    app = await sync_to_async(make_app)(world)
    token = await sync_to_async(token_for)(world, app)
    starting = BackendError("Starting", status=503, code="busy", retry_after=3)
    monkeypatch.setattr(
        gateway, "get_app_backend_service", lambda: StubService(1, error=starting)
    )
    sent = await call_gateway(f"/ws/app-backends/{app.id}/api/80/", token)
    assert sent[0]["status"] == 503
    assert (b"retry-after", b"3") in sent[0]["headers"]

    # Nothing listens on the resolved port: 502 and the port is re-detected.
    stub = StubService(_free_port())
    monkeypatch.setattr(gateway, "get_app_backend_service", lambda: stub)
    sent = await call_gateway(f"/ws/app-backends/{app.id}/api/80/", token)
    assert sent[0]["status"] == 502
    assert len(stub.forgotten) == 1


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# -- Publishing -------------------------------------------------------------


def test_bundle_backend_hosts_are_detected():
    bundle = b'x="http://api.opencuria.local/todos";y="ws://Chat.opencuria.local:81"'
    assert backend_labels([bundle, b"v2.api.opencuria.local opencuria.local"]) == (
        "api",
        "chat",
    )


def test_publish_warns_about_backends_the_workspace_lacks(app_world):
    files, package = build_files(
        bundle=b'fetch("http://api.opencuria.local/x");'
        b'new WebSocket("ws://chat.opencuria.local");'
        b'fetch("http://store.opencuria.local")'
    )
    runners = FakeRunners(process("api"), process("chat", kind="temp"))
    result = async_to_sync(
        AppService(reader=FakeReader(files, package), runners=lambda: runners).publish
    )(
        owner_id=app_world.owner.id,
        organization_id=app_world.org.id,
        workspace_id=app_world.workspace.id,
        project_path="/workspace/todo",
    )
    warnings = " ".join(result.warnings)
    assert "api.opencuria.local" not in warnings
    assert "process 'chat' stops when your run ends" in warnings
    assert "has no process named 'store'" in warnings
