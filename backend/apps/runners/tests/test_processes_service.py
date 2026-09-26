"""Tests for RunnerService background-process logic."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from apps.runners.enums import ProcessStatus, RunnerStatus, WorkspaceStatus
from apps.runners.exceptions import RunnerOfflineError, WorkspaceStateError
from apps.runners.models import WorkspaceProcess
from apps.runners.services import RunnerService
from common.exceptions import ConflictError, NotFoundError


@pytest.fixture
def sio_mock() -> AsyncMock:
    """Mock Socket.IO server."""
    return AsyncMock()


@pytest.fixture
def service(sio_mock: AsyncMock) -> RunnerService:
    """RunnerService with a mocked Socket.IO server."""
    return RunnerService(sio_server=sio_mock)


def _make_process(service: RunnerService, workspace, **kwargs) -> WorkspaceProcess:
    """Create a running process record via the repository."""
    defaults: dict = {
        "process_id": uuid.uuid4(),
        "workspace": workspace,
        "command": "sleep 60",
        "workdir": "/workspace",
        "name": "sleeper",
    }
    defaults.update(kwargs)
    return service.processes.create(**defaults)


def _feed_reply(
    service: RunnerService,
    event: str,
    runner,
    workspace_id,
    request_id: str,
    extra: dict,
) -> None:
    """Simulate a runner reply arriving on the Socket.IO thread."""
    data = {
        "request_id": request_id,
        "workspace_id": str(workspace_id),
        **extra,
    }
    service.handle_process_reply(event, data, runner_id=str(runner.id))


@pytest.mark.django_db(transaction=True)
class TestStartProcess:
    @pytest.mark.asyncio
    async def test_start_happy_path(self, service, runner, workspace, user):
        """Start should persist a record and resolve pid/log_path from the reply."""
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        pushes: list[tuple] = []
        service._forward_to_frontend = lambda *a: pushes.append(a)  # type: ignore[method-assign]

        async def _emit(runner_arg, event, payload):
            assert event == "harness:process_start"
            assert payload["command"] == "sleep 60"
            _feed_reply(
                service,
                "harness:process_start_result",
                runner_arg,
                workspace.id,
                payload["request_id"],
                {
                    "process_id": payload["process_id"],
                    "pid": 4242,
                    "log_path": ".opencuria/processes/abc.log",
                    "status": "running",
                },
            )

        service._emit_to_runner.side_effect = _emit

        process = await service.start_process(
            workspace.id,
            "sleep 60",
            workdir="/workspace",
            env={},
            name="sleeper",
            user=user,
        )

        assert process.pid == 4242
        assert process.log_path == ".opencuria/processes/abc.log"
        assert process.status == ProcessStatus.RUNNING
        assert WorkspaceProcess.objects.filter(id=process.id).exists()
        assert pushes
        assert pushes[0][0] == "process:status_changed"

    @pytest.mark.asyncio
    async def test_start_runner_error_marks_failed(
        self, service, runner, workspace, user
    ):
        """Runner error replies should mark the record failed and raise."""
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]

        async def _emit(runner_arg, event, payload):
            _feed_reply(
                service,
                "harness:process_start_result",
                runner_arg,
                workspace.id,
                payload["request_id"],
                {"process_id": payload["process_id"], "error": "boom"},
            )

        service._emit_to_runner.side_effect = _emit

        with pytest.raises(ConflictError):
            await service.start_process(workspace.id, "sleep 60", name="sleeper", user=user)

        failed = WorkspaceProcess.objects.exclude(
            status=ProcessStatus.RUNNING
        ).first()
        assert failed is not None
        assert failed.status == ProcessStatus.FAILED

    @pytest.mark.asyncio
    async def test_start_rejects_foreign_runner_reply(
        self, service, runner, workspace, organization, user
    ):
        """Replies from a runner that does not own the workspace are dropped."""
        from apps.runners.models import Runner
        from common.utils import hash_token

        other = Runner.objects.create(
            name="other",
            api_token_hash=hash_token("other-token"),
            status=RunnerStatus.ONLINE,
            sid="other-sid",
            organization=organization,
            available_runtimes=["docker"],
        )
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]

        async def _emit(runner_arg, event, payload):
            # Send the reply as if it came from the *other* runner.
            _feed_reply(
                service,
                "harness:process_start_result",
                other,
                workspace.id,
                payload["request_id"],
                {"process_id": payload["process_id"], "pid": 1},
            )

        service._emit_to_runner.side_effect = _emit
        service._PROCESS_RPC_TIMEOUT_SECONDS = 1

        with pytest.raises(ConflictError):
            await service.start_process(workspace.id, "sleep 60", name="sleeper", user=user)

    @pytest.mark.asyncio
    async def test_start_runner_offline(self, service, offline_runner, user):
        """Dispatch to an offline runner should raise RunnerOfflineError."""
        from apps.runners.models import Workspace

        workspace = Workspace.objects.create(
            runner=offline_runner,
            name="offline ws",
            status=WorkspaceStatus.RUNNING,
            created_by=user,
        )
        with pytest.raises(RunnerOfflineError):
            await service.start_process(workspace.id, "sleep 60", name="sleeper", user=user)

    @pytest.mark.asyncio
    async def test_start_requires_running_workspace(
        self, service, stopped_workspace, user
    ):
        """Non-running workspaces cannot start processes."""
        with pytest.raises(WorkspaceStateError):
            await service.start_process(stopped_workspace.id, "sleep 60", name="sleeper", user=user)

    @pytest.mark.asyncio
    async def test_start_unknown_workspace(self, service, user):
        """Unknown workspace IDs raise WorkspaceNotFoundError."""
        with pytest.raises(NotFoundError):
            await service.start_process(uuid.uuid4(), "sleep 60", name="sleeper", user=user)


@pytest.mark.django_db(transaction=True)
class TestStopProcess:
    @pytest.mark.asyncio
    async def test_stop_happy_path(self, service, runner, workspace, user):
        """Stop should RPC the runner and mark the record killed."""
        process = _make_process(service, workspace)
        service.processes.update_status(process.id, status="running", pid=4242)
        process.refresh_from_db()

        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        pushes: list[tuple] = []
        service._forward_to_frontend = lambda *a: pushes.append(a)  # type: ignore[method-assign]

        async def _emit(runner_arg, event, payload):
            assert event == "harness:process_stop"
            assert payload["process_id"] == str(process.id)
            _feed_reply(
                service,
                "harness:process_stop_result",
                runner_arg,
                workspace.id,
                payload["request_id"],
                {
                    "process_id": str(process.id),
                    "stopped": True,
                    "status": "killed",
                    "exit_code": None,
                    "pid": 4242,
                },
            )

        service._emit_to_runner.side_effect = _emit

        stopped = await service.stop_process(workspace.id, process.id)
        assert stopped.status == ProcessStatus.KILLED
        assert pushes
        assert pushes[0][0] == "process:status_changed"

    @pytest.mark.asyncio
    async def test_stop_idempotent_for_finished(self, service, workspace):
        process = _make_process(service, workspace)
        service.processes.mark_finished(
            process.id, status=ProcessStatus.EXITED, exit_code=0
        )
        emit = AsyncMock()
        service._emit_to_runner = emit  # type: ignore[method-assign]

        result = await service.stop_process(workspace.id, process.id)
        assert result.status == ProcessStatus.EXITED
        emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_not_found_verifies_then_retries_on_running(
        self, service, runner, workspace
    ):
        """A 'not found' stop reattaches live processes and retries the stop."""
        process = _make_process(service, workspace)
        service.processes.update_status(process.id, status="running", pid=4242)
        process.refresh_from_db()
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        state: dict = {"first_stop_id": None}

        async def _emit(runner_arg, event, payload):
            if event == "harness:process_verify":
                _feed_reply(
                    service,
                    "harness:process_verify_result",
                    runner_arg,
                    workspace.id,
                    payload["request_id"],
                    {
                        "processes": [
                            {
                                "process_id": str(process.id),
                                "status": "running",
                                "exit_code": None,
                                "pid": 4242,
                            }
                        ]
                    },
                )
            elif event == "harness:process_stop":
                if state["first_stop_id"] is None:
                    state["first_stop_id"] = payload["request_id"]
                if payload["request_id"] == state["first_stop_id"]:
                    _feed_reply(
                        service,
                        "harness:process_stop_result",
                        runner_arg,
                        workspace.id,
                        payload["request_id"],
                        {
                            "process_id": str(process.id),
                            "stopped": False,
                            "error": "Background process abc not found for workspace",
                        },
                    )
                else:
                    _feed_reply(
                        service,
                        "harness:process_stop_result",
                        runner_arg,
                        workspace.id,
                        payload["request_id"],
                        {
                            "process_id": str(process.id),
                            "stopped": True,
                            "status": "killed",
                            "exit_code": None,
                            "pid": 4242,
                        },
                    )

        service._emit_to_runner.side_effect = _emit

        stopped = await service.stop_process(workspace.id, process.id)
        assert stopped.status == ProcessStatus.KILLED
        events = [
            call.args[1] for call in service._emit_to_runner.call_args_list
        ]
        assert events.count("harness:process_stop") == 2
        assert "harness:process_verify" in events

    @pytest.mark.asyncio
    async def test_stop_not_found_with_confirmed_exit_marks_exited(
        self, service, runner, workspace
    ):
        """A 'not found' stop with verify=exited marks the row EXITED."""
        process = _make_process(service, workspace)
        service.processes.update_status(process.id, status="running", pid=4242)
        process.refresh_from_db()
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]

        async def _emit(runner_arg, event, payload):
            if event == "harness:process_verify":
                _feed_reply(
                    service,
                    "harness:process_verify_result",
                    runner_arg,
                    workspace.id,
                    payload["request_id"],
                    {
                        "processes": [
                            {
                                "process_id": str(process.id),
                                "status": "exited",
                                "exit_code": 3,
                                "pid": 4242,
                            }
                        ]
                    },
                )
            else:
                _feed_reply(
                    service,
                    "harness:process_stop_result",
                    runner_arg,
                    workspace.id,
                    payload["request_id"],
                    {
                        "process_id": str(process.id),
                        "stopped": False,
                        "error": "Background process abc not found for workspace",
                    },
                )

        service._emit_to_runner.side_effect = _emit

        stopped = await service.stop_process(workspace.id, process.id)
        assert stopped.status == ProcessStatus.EXITED
        assert stopped.exit_code == 3

    @pytest.mark.asyncio
    async def test_stop_not_found_with_verify_timeout_stays_running(
        self, service, runner, workspace
    ):
        """A failed verify (timeout) keeps the row RUNNING (fail-open)."""
        process = _make_process(service, workspace)
        service.processes.update_status(process.id, status="running", pid=4242)
        process.refresh_from_db()
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]

        async def _emit(runner_arg, event, payload):
            if event == "harness:process_verify":
                return  # never reply -> live-timeout -> ConflictError
            _feed_reply(
                service,
                "harness:process_stop_result",
                runner_arg,
                workspace.id,
                payload["request_id"],
                {
                    "process_id": str(process.id),
                    "stopped": False,
                    "error": "Background process abc not found for workspace",
                },
            )

        service._emit_to_runner.side_effect = _emit

        with patch.object(service, "_PROCESS_LIVE_TIMEOUT_SECONDS", 0.05):
            result = await service.stop_process(workspace.id, process.id)
        assert result.status == ProcessStatus.RUNNING
        result.refresh_from_db()
        assert result.status == ProcessStatus.RUNNING

    @pytest.mark.asyncio
    async def test_stop_unknown_process(self, service, workspace):
        """Unknown process IDs raise NotFoundError."""
        with pytest.raises(NotFoundError):
            await service.stop_process(workspace.id, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_stop_foreign_workspace_process(self, service, workspace, runner, user):
        """Processes of another workspace are not visible (404)."""
        from apps.runners.models import Workspace

        other_ws = Workspace.objects.create(
            runner=runner,
            name="other ws",
            status=WorkspaceStatus.RUNNING,
            created_by=user,
        )
        process = _make_process(service, other_ws)
        with pytest.raises(NotFoundError):
            await service.stop_process(workspace.id, process.id)


@pytest.mark.django_db(transaction=True)
class TestListProcesses:
    @pytest.mark.asyncio
    async def test_live_false_returns_db_rows_without_runner_rpc(
        self, service, workspace
    ):
        """UI polling can use heartbeat-updated DB state without runner RPC."""
        process = _make_process(service, workspace)
        service.processes.update_status(
            process.id, status=ProcessStatus.RUNNING, pid=4242
        )
        emit = AsyncMock()
        service._emit_to_runner = emit  # type: ignore[method-assign]

        rows = await service.list_processes(workspace.id, live=False)

        assert [row.id for row in rows] == [process.id]
        assert rows[0].status == ProcessStatus.RUNNING
        emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_false_keeps_stale_database_status_explicitly(
        self, service, workspace
    ):
        """DB-only results can be stale between 15-second heartbeats."""
        process = _make_process(service, workspace)
        service.processes.update_status(
            process.id, status=ProcessStatus.RUNNING, pid=4242
        )
        process.refresh_from_db()

        rows = await service.list_processes(workspace.id, live=False)

        assert rows[0].status == ProcessStatus.RUNNING
        assert rows[0].pid == 4242

    @pytest.mark.asyncio
    async def test_live_defaults_to_existing_runner_reconciliation(
        self, service, runner, workspace
    ):
        """Agent/MCP consumers retain live reconciliation unless opting out."""
        process = _make_process(service, workspace)
        service.processes.update_status(
            process.id, status=ProcessStatus.RUNNING, pid=4242
        )
        process.refresh_from_db()

        async def _emit(runner_arg, event, payload):
            assert event == "harness:process_list"
            _feed_reply(
                service,
                "harness:process_list_result",
                runner_arg,
                workspace.id,
                payload["request_id"],
                {"processes": [{"process_id": str(process.id), "status": "running"}]},
            )

        service._emit_to_runner = AsyncMock(  # type: ignore[method-assign]
            side_effect=_emit
        )

        rows = await service.list_processes(workspace.id)

        assert [row.id for row in rows] == [process.id]
        service._emit_to_runner.assert_awaited_once()


@pytest.mark.django_db
class TestReconcile:
    def test_runner_reports_exited(self, service, runner, workspace):
        """Exited reports update the DB record and push to the frontend."""
        process = _make_process(service, workspace)
        service.processes.update_status(process.id, status="running", pid=4242)
        process.refresh_from_db()

        with patch.object(service, "_forward_to_frontend") as forward:
            changed, vanished = service.reconcile_workspace_processes(
                str(workspace.id),
                [
                    {
                        "process_id": str(process.id),
                        "status": "exited",
                        "exit_code": 3,
                        "pid": 4242,
                    }
                ],
            )

        assert changed == [process.id]
        assert vanished == []
        process.refresh_from_db()
        assert process.status == ProcessStatus.EXITED
        assert process.exit_code == 3
        assert process.ended_at is not None
        forward.assert_called_once()
        assert forward.call_args[0][0] == "process:status_changed"

    def test_runner_restart_empty_list_defers_to_verify(self, service, runner, workspace):
        """A missing process is a vanished candidate, not immediately exited."""
        process = _make_process(service, workspace)
        service.processes.update_status(process.id, status="running", pid=4242)
        process.refresh_from_db()

        changed, vanished = service.reconcile_workspace_processes(
            str(workspace.id), []
        )

        assert changed == []
        assert [row.id for row in vanished] == [process.id]
        process.refresh_from_db()
        assert process.status == ProcessStatus.RUNNING
        assert process.ended_at is None

    def test_running_processes_stay_running(self, service, runner, workspace):
        """Processes the runner still tracks stay running without pushes."""
        process = _make_process(service, workspace)
        service.processes.update_status(process.id, status="running", pid=4242)
        process.refresh_from_db()

        with patch.object(service, "_forward_to_frontend") as forward:
            changed, vanished = service.reconcile_workspace_processes(
                str(workspace.id),
                [
                    {
                        "process_id": str(process.id),
                        "status": "running",
                        "exit_code": None,
                        "pid": 4242,
                    }
                ],
            )

        assert changed == []
        assert vanished == []
        forward.assert_not_called()

    def test_pending_start_without_pid_is_skipped(self, service, runner, workspace):
        """Records without a confirmed pid are skipped until the runner ACKs."""
        process = _make_process(service, workspace)
        # pid stays None: start RPC not yet acknowledged.
        changed, vanished = service.reconcile_workspace_processes(
            str(workspace.id), []
        )
        assert changed == []
        assert vanished == []
        process.refresh_from_db()
        assert process.status == ProcessStatus.RUNNING

    def test_heartbeat_stashes_vanished_for_async_verify(
        self, service, runner, workspace
    ):
        """Heartbeat must not mark vanished rows exited (sync has no RPC)."""
        process = _make_process(service, workspace)
        service.processes.update_status(process.id, status="running", pid=4242)
        process.refresh_from_db()

        auto_stop = AsyncMock(return_value=None)
        with (
            patch.object(service, "auto_stop_inactive_workspaces", new=auto_stop),
            patch.object(
                service.workspaces,
                "list_by_runner",
                wraps=service.workspaces.list_by_runner,
            ) as list_by_runner,
        ):
            service.handle_heartbeat(
                runner=runner,
                workspaces=[
                    {
                        "workspace_id": str(workspace.id),
                        "status": "running",
                        "runtime_type": "docker",
                        "processes": [],
                    }
                ],
            )

        list_by_runner.assert_called_once_with(runner.id)
        auto_stop.assert_awaited_once()
        assert auto_stop.await_args.kwargs["workspaces"][0].id == workspace.id
        process.refresh_from_db()
        assert process.status == ProcessStatus.RUNNING
        assert [row.id for row in service._pending_process_verify[str(workspace.id)]] == [
            process.id
        ]


@pytest.mark.django_db(transaction=True)
class TestReverifyVanished:
    def _running_process(self, service, workspace, pid=4242):
        process = _make_process(service, workspace)
        # Refresh the in-memory instance: update_status writes via
        # QuerySet.update (no in-memory sync), and the verify payload is
        # built from these attributes (pid must be set).
        service.processes.update_status(process.id, status="running", pid=pid)
        process.refresh_from_db()
        return process

    def _verify_reply(self, service, runner, workspace, statuses):
        async def _emit(runner_arg, event, payload):
            assert event == "harness:process_verify"
            _feed_reply(
                service,
                "harness:process_verify_result",
                runner_arg,
                workspace.id,
                payload["request_id"],
                {"processes": statuses},
            )

        service._emit_to_runner = AsyncMock(side_effect=_emit)  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_verify_running_revives_row(self, service, runner, workspace):
        """A reattached 'running' answer keeps the row RUNNING with fresh pid."""
        process = self._running_process(service, workspace)
        self._verify_reply(
            service,
            runner,
            workspace,
            [
                {
                    "process_id": str(process.id),
                    "status": "running",
                    "exit_code": None,
                    "pid": 9999,
                }
            ],
        )
        with patch.object(service, "_forward_to_frontend"):
            changed = await service.reverify_vanished_processes(
                workspace.id, [process]
            )
        assert changed == [process.id]
        process.refresh_from_db()
        assert process.status == ProcessStatus.RUNNING
        assert process.pid == 9999
        assert process.ended_at is None

    @pytest.mark.asyncio
    async def test_verify_exited_marks_exited(self, service, runner, workspace):
        """A confirmed 'exited' answer marks the row EXITED with the code."""
        process = self._running_process(service, workspace)
        self._verify_reply(
            service,
            runner,
            workspace,
            [
                {
                    "process_id": str(process.id),
                    "status": "exited",
                    "exit_code": 3,
                    "pid": 4242,
                }
            ],
        )
        changed = await service.reverify_vanished_processes(
            workspace.id, [process]
        )
        assert changed == [process.id]
        process.refresh_from_db()
        assert process.status == ProcessStatus.EXITED
        assert process.exit_code == 3

    @pytest.mark.asyncio
    async def test_verify_timeout_keeps_running(self, service, runner, workspace):
        """A verify timeout leaves the row RUNNING (fail-open, DB fallback)."""
        process = self._running_process(service, workspace)

        async def _emit(runner_arg, event, payload):
            return  # never reply -> timeout

        service._emit_to_runner = AsyncMock(side_effect=_emit)  # type: ignore[method-assign]
        with patch.object(service, "_PROCESS_LIVE_TIMEOUT_SECONDS", 0.05):
            changed = await service.reverify_vanished_processes(
                workspace.id, [process]
            )
        assert changed == []
        process.refresh_from_db()
        assert process.status == ProcessStatus.RUNNING

    @pytest.mark.asyncio
    async def test_reconcile_vanished_drains_stash(self, service, runner, workspace):
        """The heartbeat stash is drained even when the runner is offline."""
        process = self._running_process(service, workspace)
        service._pending_process_verify[str(workspace.id)] = [process]
        # Runner lost its sid -> offline path, rows stay RUNNING.
        runner.sid = ""
        runner.save(update_fields=["sid"])
        changed = await service.reconcile_vanished_processes(runner)
        assert changed == []
        assert service._pending_process_verify == {}
        process.refresh_from_db()
        assert process.status == ProcessStatus.RUNNING


@pytest.mark.django_db
class TestSweepUnconfirmed:
    def test_stale_pid_none_row_is_failed(self, service, workspace):
        """A stale RUNNING row without pid becomes FAILED (never RUNNING forever)."""
        from django.utils import timezone as tz

        process = _make_process(service, workspace)
        assert process.pid is None
        old = tz.now() - timedelta(seconds=3600)
        type(process).objects.filter(id=process.id).update(started_at=old)
        with patch.object(service, "_forward_to_frontend"):
            changed = service.sweep_unconfirmed_processes(str(workspace.id))
        assert changed == [process.id]
        process.refresh_from_db()
        assert process.status == ProcessStatus.FAILED

    def test_fresh_pid_none_row_is_kept(self, service, workspace):
        """A fresh pid=None row (start in flight) is kept RUNNING."""
        process = _make_process(service, workspace)
        changed = service.sweep_unconfirmed_processes(str(workspace.id))
        assert changed == []
        process.refresh_from_db()
        assert process.status == ProcessStatus.RUNNING


@pytest.mark.django_db
class TestWorkspaceLifecycleKillsProcesses:
    def _running_process(self, service, workspace):
        process = _make_process(service, workspace)
        service.processes.update_status(process.id, status="running", pid=4242)
        process.refresh_from_db()
        return process

    def test_mark_processes_killed(self, service, workspace):
        """mark_processes_killed transitions running records to killed."""
        process = self._running_process(service, workspace)
        count = service.mark_processes_killed(
            str(workspace.id), reason="workspace_stopped"
        )
        assert count == 1
        process.refresh_from_db()
        assert process.status == ProcessStatus.KILLED
        assert process.ended_at is not None

    def test_stopped_handler_kills_processes(self, service, runner, workspace):
        """workspace:stopped should kill tracked processes."""
        from apps.runners.enums import TaskType
        from apps.runners.models import Task

        process = self._running_process(service, workspace)
        task = Task.objects.create(
            runner=runner,
            workspace=workspace,
            type=TaskType.STOP_WORKSPACE,
        )
        service.handle_workspace_stopped(
            str(task.id), str(workspace.id), runner_id=str(runner.id)
        )
        process.refresh_from_db()
        assert process.status == ProcessStatus.KILLED

    def test_removed_handler_kills_processes(self, service, runner, workspace):
        """workspace:removed should kill tracked processes."""
        from apps.runners.enums import TaskType
        from apps.runners.models import Task

        process = self._running_process(service, workspace)
        task = Task.objects.create(
            runner=runner,
            workspace=workspace,
            type=TaskType.REMOVE_WORKSPACE,
        )
        service.handle_workspace_removed(
            str(task.id), str(workspace.id), runner_id=str(runner.id)
        )
        process.refresh_from_db()
        assert process.status == ProcessStatus.KILLED
