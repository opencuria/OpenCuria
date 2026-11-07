"""App sessions and resolving ``<process>.opencuria.local`` to a workspace port."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from apps.harness.access.base import ExecResult
from apps.mobile_apps import backends
from apps.mobile_apps.backends import AppBackendService, BackendError
from apps.mobile_apps.gateway_protocol import BackendAddress
from apps.mobile_apps.models import App
from apps.runners.repositories import WorkspaceLaunchState
from common.exceptions import ConflictError, NotFoundError

from .conftest import AppWorld, build_world

SS = """\
LISTEN 0 511 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=800,fd=3))
LISTEN 0 511 *:3000 *:* users:(("node",pid=1201,fd=19))
"""


def listening(session_pids: str, sockets: str = SS) -> str:
    return f"{sockets}--opencuria-sessions--\n{session_pids}"


ALIVE = listening("1200 1200\n1201 1200\n800 800\n")
STARTING = listening("1200 1200\n800 800\n", sockets="")
DEAD = listening("800 800\n", sockets="")


def process(name="api", *, kind="persistent", status="running", pid=1200):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        kind=kind,
        status=status,
        pid=pid,
        exit_code=None,
        log_path="/workspace/.opencuria/processes/p.log",
    )


class FakeRunners:
    """The runner service as the backend service uses it."""

    def __init__(self, *processes, status="running", owner=True, available=True):
        self.processes = list(processes)
        self.state = WorkspaceLaunchState(status, None, "online", True, None)
        self.owner = owner
        self.available = available
        self.resumed: list[uuid.UUID] = []
        self.restarted: list[uuid.UUID] = []
        self.touched: list[uuid.UUID] = []
        # The backend reaches several runner domain services; one fake serves all.
        self.workspace_access = self.workspace_lifecycle = self
        self.ownership = self.process_manager = self

    def is_active_workspace_owner(self, user_id, workspace_id, *, organization_id):
        return self.owner

    def get_workspace_launch_state(self, workspace_id):
        return self.state

    def is_workspace_available(self, workspace_id):
        return self.available

    async def resume_workspace(self, workspace_id):
        self.resumed.append(workspace_id)
        self.state = self.state._replace(status="running")

    async def list_processes(self, workspace_id, *, kinds=None, live=True, **_):
        return [p for p in self.processes if kinds is None or p.kind in kinds]

    async def get_process(self, workspace_id, process_id, **_):
        return next(p for p in self.processes if p.id == process_id)

    async def restart_process(self, workspace_id, process_id, **_):
        found = await self.get_process(workspace_id, process_id)
        found.status, found.pid = "running", 1200
        self.restarted.append(process_id)
        return found

    async def touch_workspace_activity(self, workspace_id, **_):
        self.touched.append(workspace_id)


class FakeWorkspace:
    """Accessor double: answers the listening-ports probe from a script."""

    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.probes = 0
        self.opened: list[tuple[str, int]] = []

    async def exec_wait(self, command, workdir="/workspace", env=None, timeout=None):
        self.probes += 1
        output = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return ExecResult(exit_code=0, stdout=output)

    async def open_tcp(self, host, port, timeout=None):
        self.opened.append((host, port))
        return SimpleNamespace(
            workspace_id="ws", connection_id=uuid.uuid4().hex, aclose=_noop
        )


async def _noop():
    return None


def make_app(world: AppWorld, *, workspace=True) -> App:
    return App.objects.create(
        organization=world.org,
        owner=world.owner,
        workspace=world.workspace if workspace else None,
        slug=f"todo-{uuid.uuid4().hex[:6]}",
        name="Todo",
    )


def service_for(runners: FakeRunners, workspace: FakeWorkspace) -> AppBackendService:
    async def accessor(_workspace_id):
        return workspace

    return AppBackendService(runners=lambda: runners, accessor_factory=accessor)


def claims_for(world: AppWorld, app: App) -> backends.SessionClaims:
    return backends.SessionClaims(
        app_id=app.id, organization_id=world.org.id, owner_id=world.owner.id
    )


# -- Sessions --------------------------------------------------------------


def test_session_round_trip_is_bound_to_the_app(app_world):
    app = make_app(app_world)
    service = service_for(FakeRunners(), FakeWorkspace(ALIVE))
    session = service.issue_session(
        app.id, organization_id=app_world.org.id, owner_id=app_world.owner.id
    )
    assert session.path == f"/ws/app-backends/{app.id}"
    claims = service.verify_session(session.token, str(app.id))
    assert claims == claims_for(app_world, app)

    with pytest.raises(BackendError) as foreign:
        service.verify_session(session.token, str(uuid.uuid4()))
    assert foreign.value.status == 401
    for forged in ("", "nope", session.token[:-2] + "xx"):
        with pytest.raises(BackendError):
            service.verify_session(forged, str(app.id))


def test_expired_sessions_are_refused(app_world, monkeypatch):
    app = make_app(app_world)
    service = service_for(FakeRunners(), FakeWorkspace(ALIVE))
    token = service.issue_session(
        app.id, organization_id=app_world.org.id, owner_id=app_world.owner.id
    ).token
    monkeypatch.setattr(backends, "SESSION_TTL", timedelta(seconds=-1))
    with pytest.raises(BackendError, match="Open the app from OpenCuria"):
        service.verify_session(token, str(app.id))


def test_sessions_need_an_own_app_with_a_workspace(app_world):
    service = service_for(FakeRunners(), FakeWorkspace(ALIVE))
    with pytest.raises(NotFoundError):
        service.issue_session(
            make_app(app_world).id,
            organization_id=app_world.org.id,
            owner_id=app_world.stranger.id,
        )
    with pytest.raises(ConflictError):
        service.issue_session(
            make_app(app_world, workspace=False).id,
            organization_id=app_world.org.id,
            owner_id=app_world.owner.id,
        )


# -- Resolving -------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_running_process_resolves_to_its_listening_port():
    world = await _world()
    app = await _app(world)
    api = process()
    runners, workspace = FakeRunners(api, process("web", pid=999)), FakeWorkspace(ALIVE)
    service = service_for(runners, workspace)

    target = await service.resolve(claims_for(world, app), BackendAddress("api", 80))

    assert (target.process_id, target.port, target.workspace_id) == (
        api.id,
        3000,
        world.workspace.id,
    )
    assert runners.touched == [world.workspace.id]
    # The detected port is reused for the next requests.
    await service.resolve(claims_for(world, app), BackendAddress("api", 80))
    assert workspace.probes == 1
    stream = await service.open_stream(target)
    assert workspace.opened == [("localhost", 3000)]
    await stream.aclose()


@pytest.mark.django_db(transaction=True)
async def test_stopped_process_and_workspace_are_started(monkeypatch):
    world = await _world()
    app = await _app(world)
    api = process(status="exited", pid=None)
    runners = FakeRunners(api, status="stopped")
    service = service_for(runners, FakeWorkspace(STARTING, ALIVE))
    monkeypatch.setattr(backends.asyncio, "sleep", _instant)

    target = await service.resolve(claims_for(world, app), BackendAddress("api", 80))

    assert runners.resumed == [world.workspace.id]
    assert runners.restarted == [api.id]
    assert target.port == 3000


@pytest.mark.django_db(transaction=True)
async def test_unknown_and_agent_only_processes_are_not_found():
    world = await _world()
    app = await _app(world)
    runners = FakeRunners(process("web"), process("api", kind="temp"))
    service = service_for(runners, FakeWorkspace(ALIVE))

    with pytest.raises(BackendError) as missing:
        await service.resolve(claims_for(world, app), BackendAddress("api", 80))
    assert missing.value.status == 404
    assert "process_start(name='api', for_user=true)" in missing.value.message
    assert "Available: web." in missing.value.message


@pytest.mark.django_db(transaction=True)
async def test_crashed_process_reports_its_exit(monkeypatch):
    world = await _world()
    app = await _app(world)
    api = process()
    runners = FakeRunners(api)

    async def exited(workspace_id, process_id, **_):
        api.status, api.exit_code = "exited", 1
        return api

    runners.get_process = exited
    service = service_for(runners, FakeWorkspace(DEAD))

    with pytest.raises(BackendError) as crashed:
        await service.resolve(claims_for(world, app), BackendAddress("api", 80))
    # One more start was tried before reporting the crash.
    assert runners.restarted == [api.id]
    assert crashed.value.status == 502
    assert "exited with code 1" in crashed.value.message
    assert "/workspace/.opencuria/processes/p.log" in crashed.value.message


@pytest.mark.django_db(transaction=True)
async def test_process_that_died_since_the_heartbeat_restarts(monkeypatch):
    world = await _world()
    app = await _app(world)
    api = process()  # Still "running" in the database.
    runners = FakeRunners(api)
    real_get = runners.get_process

    async def died_once(workspace_id, process_id, **_):
        current = await real_get(workspace_id, process_id)
        if not runners.restarted:
            current.status, current.exit_code = "exited", 137
        return current

    runners.get_process = died_once
    monkeypatch.setattr(backends.asyncio, "sleep", _instant)
    service = service_for(runners, FakeWorkspace(DEAD, STARTING, ALIVE))

    target = await service.resolve(claims_for(world, app), BackendAddress("api", 80))

    assert runners.restarted == [api.id]
    assert target.port == 3000


@pytest.mark.django_db(transaction=True)
async def test_start_in_flight_is_awaited_not_restarted(monkeypatch):
    world = await _world()
    app = await _app(world)
    api = process(pid=None)  # An agent's restart: RUNNING until the pid confirms.
    runners = FakeRunners(api)
    real_get = runners.get_process
    polls = 0

    async def confirms_later(workspace_id, process_id, **_):
        nonlocal polls
        polls += 1
        current = await real_get(workspace_id, process_id)
        if polls >= 3:
            current.pid = 1200
        return current

    runners.get_process = confirms_later
    monkeypatch.setattr(backends.asyncio, "sleep", _instant)
    service = service_for(runners, FakeWorkspace(ALIVE))

    target = await service.resolve(claims_for(world, app), BackendAddress("api", 80))

    assert runners.restarted == []
    assert target.port == 3000


@pytest.mark.django_db(transaction=True)
async def test_start_that_never_confirms_is_restarted(monkeypatch):
    world = await _world()
    app = await _app(world)
    api = process(pid=None)
    runners = FakeRunners(api)
    monkeypatch.setattr(backends, "PROCESS_START_WAIT_SECONDS", 0.0)
    service = service_for(runners, FakeWorkspace(ALIVE))

    await service.resolve(claims_for(world, app), BackendAddress("api", 80))

    assert runners.restarted == [api.id]


@pytest.mark.django_db(transaction=True)
async def test_offline_runner_answers_at_once():
    world = await _world()
    app = await _app(world)
    runners = FakeRunners(process())
    runners.state = runners.state._replace(runner_status="offline")
    service = service_for(runners, FakeWorkspace(ALIVE))

    with pytest.raises(BackendError) as offline:
        await service.resolve(claims_for(world, app), BackendAddress("api", 80))
    assert (offline.value.status, offline.value.retry_after) == (503, 30)
    assert "runner is offline" in offline.value.message


async def test_long_connections_keep_the_workspace_active(monkeypatch):
    monkeypatch.setattr(backends, "ACTIVITY_TOUCH_SECONDS", 0.01)
    runners = FakeRunners()
    service = service_for(runners, FakeWorkspace(ALIVE))
    workspace_id = uuid.uuid4()

    async with service.keep_active(workspace_id):
        await asyncio.sleep(0.1)
    touched = len(runners.touched)
    await asyncio.sleep(0.05)

    assert touched >= 2
    assert set(runners.touched) == {workspace_id}
    assert len(runners.touched) == touched  # Stops with the connection.


@pytest.mark.django_db(transaction=True)
async def test_port_that_never_listens_is_retryable(monkeypatch):
    world = await _world()
    app = await _app(world)
    monkeypatch.setattr(backends, "PORT_WAIT_SECONDS", 0.05)
    service = service_for(FakeRunners(process()), FakeWorkspace(ALIVE))

    with pytest.raises(BackendError) as waiting:
        await service.resolve(claims_for(world, app), BackendAddress("api", 5000))
    assert (waiting.value.status, waiting.value.retry_after) == (503, 3)
    assert "port 5000" in waiting.value.message


@pytest.mark.django_db(transaction=True)
async def test_foreign_or_deleted_workspaces_are_not_reachable():
    world = await _world()
    app = await _app(world)
    service = service_for(FakeRunners(process(), owner=False), FakeWorkspace(ALIVE))
    with pytest.raises(BackendError) as foreign:
        await service.resolve(claims_for(world, app), BackendAddress("api", 80))
    assert foreign.value.status == 404

    gone = backends.SessionClaims(
        app_id=uuid.uuid4(), organization_id=world.org.id, owner_id=world.owner.id
    )
    with pytest.raises(BackendError) as missing_app:
        await service.resolve(gone, BackendAddress("api", 80))
    assert missing_app.value.code == "backend_app_not_found"


async def test_websocket_budget_refuses_instead_of_queueing(monkeypatch):
    monkeypatch.setattr(backends, "WEBSOCKETS_PER_WORKSPACE", 1)
    workspace = FakeWorkspace(ALIVE)
    service = service_for(FakeRunners(), workspace)
    target = backends.BackendTarget(
        workspace_id=uuid.uuid4(),
        process_id=uuid.uuid4(),
        process_name="api",
        pid=1200,
        port=3000,
        address=BackendAddress("api", 80),
    )
    first = await service.open_stream(target, websocket=True)
    with pytest.raises(BackendError) as busy:
        await service.open_stream(target, websocket=True)
    assert busy.value.status == 503
    await first.aclose()
    await (await service.open_stream(target, websocket=True)).aclose()


async def _instant(_seconds):
    return None


async def _world() -> AppWorld:
    from asgiref.sync import sync_to_async

    return await sync_to_async(build_world)()


async def _app(world: AppWorld) -> App:
    from asgiref.sync import sync_to_async

    return await sync_to_async(make_app)(world)
