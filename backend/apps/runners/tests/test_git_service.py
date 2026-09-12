"""Tests for RunnerService git RPC (whitelisted git:operation dispatch)."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.exceptions import (
    RunnerOfflineError,
    RunnerTimeoutError,
    WorkspaceStateError,
)
from apps.runners.services import RunnerService
from common.exceptions import NotFoundError


@pytest.fixture
def sio_mock() -> AsyncMock:
    """Mock Socket.IO server."""
    return AsyncMock()


@pytest.fixture
def service(sio_mock: AsyncMock) -> RunnerService:
    """RunnerService with a mocked Socket.IO server."""
    return RunnerService(sio_server=sio_mock)


def _feed_git_reply(
    service: RunnerService,
    runner,
    workspace_id,
    request_id: str,
    extra: dict,
) -> None:
    """Simulate a git:operation_result reply arriving on the SIO thread."""
    data = {
        "request_id": request_id,
        "workspace_id": str(workspace_id),
        **extra,
    }
    service.handle_git_reply(
        "git:operation_result", data, runner_id=str(runner.id)
    )


def _emit_ok(service: RunnerService, runner, workspace, payload_extra=None):
    """Install an _emit_to_runner fake that replies with a happy list_repos."""
    seen: list[dict] = []

    async def _emit(runner_arg, event, payload):
        assert event == "git:operation"
        seen.append(payload)
        result = {
            "request_id": payload["request_id"],
            "workspace_id": str(workspace.id),
            "operation": payload["operation"],
            "ok": True,
            "repos": [],
        }
        if payload_extra:
            result.update(payload_extra)
        _feed_git_reply(
            service, runner_arg, workspace.id, payload["request_id"], result
        )

    service._emit_to_runner = AsyncMock(side_effect=_emit)  # type: ignore[method-assign]
    return seen


# Back-compat alias for the ok-reply emit helper.
_emit_ok_snapshot = _emit_ok


@pytest.mark.django_db(transaction=True)
class TestRunGitOperationHappyPath:
    @pytest.mark.asyncio
    async def test_list_repos_dispatch_payload_and_result(
        self, service, runner, workspace
    ):
        """list_repos dispatches a whitelisted payload and returns the result."""
        seen = _emit_ok(service, runner, workspace)

        result = await service.run_git_operation(
            workspace.id,
            "list_repos",
            args={},
        )

        assert result["ok"] is True
        assert result["repos"] == []
        assert len(seen) == 1
        payload = seen[0]
        assert payload["operation"] == "list_repos"
        assert payload["workspace_id"] == str(workspace.id)
        assert payload["args"] == {}
        assert "request_id" in payload
        assert "repo_path" not in payload
        assert "env" not in payload.get("args", {})

    @pytest.mark.asyncio
    async def test_repo_snapshot_dispatch_includes_repo_path(
        self, service, runner, workspace
    ):
        """repo_snapshot forwards repo_path top-level with empty args."""
        seen = _emit_ok(
            service, runner, workspace, payload_extra={"snapshot": {"path": "x"}}
        )

        result = await service.run_git_operation(
            workspace.id,
            "repo_snapshot",
            repo_path="/workspace/repo",
            args={},
        )

        assert result["ok"] is True
        payload = seen[0]
        assert payload["operation"] == "repo_snapshot"
        assert payload["repo_path"] == "/workspace/repo"
        assert payload["args"] == {}

    @pytest.mark.asyncio
    async def test_repo_history_forwards_paging_and_branch(
        self, service, runner, workspace
    ):
        """repo_history forwards history_limit/skip/branch verbatim."""
        seen = _emit_ok(service, runner, workspace)

        await service.run_git_operation(
            workspace.id,
            "repo_history",
            repo_path="/workspace/repo",
            args={"history_limit": 25, "history_skip": 10, "branch": "main"},
        )

        payload = seen[0]
        assert payload["operation"] == "repo_history"
        assert payload["repo_path"] == "/workspace/repo"
        assert payload["args"] == {
            "history_limit": 25,
            "history_skip": 10,
            "branch": "main",
        }

    @pytest.mark.asyncio
    async def test_checkout_remote_branch_forwards_refs(
        self, service, runner, workspace
    ):
        """checkout_remote_branch forwards remote_ref plus optional local_name."""
        seen = _emit_ok(service, runner, workspace)

        await service.run_git_operation(
            workspace.id,
            "checkout_remote_branch",
            repo_path="/workspace/repo",
            args={"remote_ref": "origin/feat", "local_name": "feat"},
        )

        payload = seen[0]
        assert payload["args"] == {
            "remote_ref": "origin/feat",
            "local_name": "feat",
        }

    @pytest.mark.asyncio
    async def test_stage_includes_repo_path(self, service, runner, workspace):
        """Mutations forward repo_path plus typed args."""
        seen = _emit_ok_snapshot(service, runner, workspace)

        await service.run_git_operation(
            workspace.id,
            "stage",
            repo_path="/workspace/repo",
            args={"paths": ["a.txt"]},
        )

        payload = seen[0]
        assert payload["repo_path"] == "/workspace/repo"
        assert payload["args"] == {"paths": ["a.txt"]}

    @pytest.mark.asyncio
    async def test_runner_ok_false_passthrough(self, service, runner, workspace):
        """Runner domain failures (ok:false) are returned verbatim, not raised."""

        async def _emit(runner_arg, event, payload):
            _feed_git_reply(
                service,
                runner_arg,
                workspace.id,
                payload["request_id"],
                {
                    "request_id": payload["request_id"],
                    "workspace_id": str(workspace.id),
                    "operation": "push",
                    "ok": False,
                    "code": "conflict",
                    "message": "merge conflict",
                    "snapshot": {"repos": [{"path": "/workspace/repo"}]},
                },
            )

        service._emit_to_runner = AsyncMock(side_effect=_emit)  # type: ignore[method-assign]

        result = await service.run_git_operation(
            workspace.id, "push", repo_path="/workspace/repo", args={}
        )
        assert result["ok"] is False
        assert result["code"] == "conflict"
        assert result["snapshot"] == {"repos": [{"path": "/workspace/repo"}]}

    @pytest.mark.asyncio
    async def test_per_repo_serialization_keys(self, service, workspace):
        """Lock keys are per workspace/repo; bare list_repos uses /workspace."""
        assert service._git_lock_key(workspace.id, None) == f"{workspace.id}:/workspace"
        assert service._git_lock_key(workspace.id, "/workspace/repo") == (
            f"{workspace.id}:/workspace/repo"
        )
        lock_a = await service._git_lock(workspace.id, "/workspace/a")
        lock_a_again = await service._git_lock(workspace.id, "/workspace/a")
        lock_b = await service._git_lock(workspace.id, "/workspace/b")
        assert lock_a is lock_a_again
        assert lock_a is not lock_b


@pytest.mark.django_db(transaction=True)
class TestCommitIdentityFallback:
    @pytest.mark.asyncio
    async def test_commit_injects_author_from_full_name(
        self, service, runner, workspace, user
    ):
        """Commit args gain author_name/email from the account user."""
        user.first_name = "Ada"
        user.last_name = "Lovelace"
        seen = _emit_ok_snapshot(service, runner, workspace)

        await service.run_git_operation(
            workspace.id,
            "commit",
            repo_path="/workspace/repo",
            args={"message": "hello"},
            user=user,
        )

        assert seen[0]["args"]["message"] == "hello"
        assert seen[0]["args"]["author_name"] == "Ada Lovelace"
        assert seen[0]["args"]["author_email"] == user.email

    def test_commit_identity_email_prefix_fallback(self, service):
        """No name → username/email prefix; no email → localhost fallback."""
        from types import SimpleNamespace

        class _NoName:
            email = "bob@example.com"
            username = ""

            def get_full_name(self):
                return ""

        name, email = RunnerService.commit_identity_for_user(_NoName())
        assert name == "bob"
        assert email == "bob@example.com"

        anon = SimpleNamespace(email="", username="", get_full_name=lambda: "")
        name2, email2 = RunnerService.commit_identity_for_user(anon)
        assert name2 == "opencuria"
        assert email2 == "opencuria@localhost"


@pytest.mark.django_db(transaction=True)
class TestStrictGitArgs:
    @pytest.mark.asyncio
    async def test_unknown_operation_rejected(self, service, workspace):
        """Unknown operations fail before any runner dispatch."""
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="Unknown git operation"):
            await service.run_git_operation(workspace.id, "rm_rf_everything")
        service._emit_to_runner.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "operation,args",
        [
            ("list_repos", {"env": {"GIT_DIR": "/tmp"}}),
            ("repo_snapshot", {"history_limit": 5}),
            ("repo_history", {"remote": "origin"}),
            ("stage", {"paths": ["a"], "env": {}}),
            ("commit", {"message": "x", "author_name": "Mallory"}),
            ("commit", {"message": "x", "author_email": "m@x.y"}),
            ("commit", {"message": "x", "args": {"foo": 1}}),
            ("commit", {"message": "x", "argv": ["git"]}),
            ("stage", {"paths": ["a"], "hash": "abc123"}),
            ("commit", {"message": "x", "message_b64": "eA=="}),
        ],
    )
    async def test_forbidden_and_unknown_args_rejected(
        self, service, runner, workspace, operation, args
    ):
        """env/author/args/argv/aliases never reach the runner."""
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        kwargs: dict = {"args": args}
        if operation in {
            "stage",
            "commit",
            "repo_snapshot",
            "repo_history",
            "checkout_remote_branch",
        }:
            kwargs["repo_path"] = "/workspace/repo"
        with pytest.raises(ValueError, match="Unknown argument|env|author"):
            await service.run_git_operation(workspace.id, operation, **kwargs)
        service._emit_to_runner.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "operation,args",
        [
            ("stage", {}),
            ("commit", {}),
            ("commit_details", {}),
            ("commit_details", {"commit": "zzz"}),
            ("checkout_branch", {}),
            ("checkout_commit", {"commit": "nope"}),
            ("checkout_remote_branch", {}),
            ("checkout_remote_branch", {"remote_ref": ""}),
            ("repo_history", {"history_limit": 0}),
            ("repo_history", {"history_limit": 501}),
            ("repo_history", {"history_skip": -1}),
            ("create_branch", {}),
            ("rename_branch", {}),
            ("delete_branch", {}),
            ("merge_into_current", {}),
            ("merge_current_into", {}),
        ],
    )
    async def test_missing_and_invalid_fields_rejected(
        self, service, runner, workspace, operation, args
    ):
        """Per-operation required fields fail fast with ValueError."""
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(ValueError):
            await service.run_git_operation(
                workspace.id,
                operation,
                repo_path="/workspace/repo",
                args=args,
            )
        service._emit_to_runner.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_invalid_repo_path_rejected(self, service, runner, workspace):
        """Repo paths outside /workspace fail before dispatch."""
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="under /workspace"):
            await service.run_git_operation(
                workspace.id, "stage", repo_path="/etc/passwd", args={"paths": ["a"]}
            )
        service._emit_to_runner.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_branch_and_hash_and_remote_validated(
        self, service, runner, workspace
    ):
        """Branch/hash/remote typos are rejected with clear errors."""
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="branch is required"):
            await service.run_git_operation(
                workspace.id,
                "checkout_branch",
                repo_path="/workspace/repo",
                args={"branch": ""},
            )
        with pytest.raises(ValueError, match="Invalid commit hash"):
            await service.run_git_operation(
                workspace.id,
                "checkout_commit",
                repo_path="/workspace/repo",
                args={"commit": "xyz!"},
            )
        with pytest.raises(ValueError, match="Invalid remote"):
            await service.run_git_operation(
                workspace.id,
                "fetch",
                repo_path="/workspace/repo",
                args={"remote": "   "},
            )
        service._emit_to_runner.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_pull_branch_without_remote_rejected(
        self, service, runner, workspace
    ):
        """pull with branch but no remote is a strict ValueError (no silent drop)."""
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="remote is required"):
            await service.run_git_operation(
                workspace.id,
                "pull",
                repo_path="/workspace/repo",
                args={"branch": "main"},
            )
        service._emit_to_runner.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_git_timeout_budgets_exceed_runner_outer(
        self, service
    ):
        """Backend waits longer than runner outer budgets (60s/150s)."""
        assert service.git_timeout_for("list_repos") == 75
        assert service.git_timeout_for("repo_snapshot") == 75
        assert service.git_timeout_for("repo_history") == 75
        assert service.git_timeout_for("working_diff") == 75
        assert service.git_timeout_for("fetch") == 180
        assert service.git_timeout_for("pull") == 180
        assert service.git_timeout_for("checkout_remote_branch") == 180


@pytest.mark.django_db(transaction=True)
class TestGitDispatchGuards:
    @pytest.mark.asyncio
    async def test_missing_workspace(self, service):
        """Unknown workspace IDs raise NotFoundError."""
        with pytest.raises(NotFoundError):
            await service.run_git_operation(uuid.uuid4(), "list_repos")

    @pytest.mark.asyncio
    async def test_offline_runner(self, service, offline_runner, user):
        """Dispatch to an offline runner raises RunnerOfflineError."""
        from apps.runners.models import Workspace

        workspace = Workspace.objects.create(
            runner=offline_runner,
            name="offline ws",
            status=WorkspaceStatus.RUNNING,
            created_by=user,
        )
        with pytest.raises(RunnerOfflineError):
            await service.run_git_operation(workspace.id, "list_repos")

    @pytest.mark.asyncio
    async def test_runner_without_sid_is_offline(self, service, runner, workspace):
        """A runner with no SID cannot be emitted to (offline)."""
        runner.sid = ""
        runner.save(update_fields=["sid"])
        workspace.refresh_from_db()
        with pytest.raises(RunnerOfflineError):
            await service.run_git_operation(workspace.id, "list_repos")

    @pytest.mark.asyncio
    async def test_not_running_workspace(self, service, stopped_workspace):
        """Non-running workspaces cannot run git operations."""
        with pytest.raises(WorkspaceStateError):
            await service.run_git_operation(stopped_workspace.id, "list_repos")

    @pytest.mark.asyncio
    async def test_active_operation_blocks_git(
        self, service, runner, workspace
    ):
        """A blocking lifecycle operation holds the workspace for git."""
        workspace.active_operation = "restarting"
        workspace.save(update_fields=["active_operation"])
        with pytest.raises(WorkspaceStateError):
            await service.run_git_operation(workspace.id, "list_repos")

    @pytest.mark.asyncio
    async def test_timeout_raises_runner_timeout_error(
        self, service, runner, workspace, monkeypatch
    ):
        """No runner reply within budget raises RunnerTimeoutError."""
        service._emit_to_runner = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr(RunnerService, "git_timeout_for", classmethod(lambda cls, op: 0.02))
        with pytest.raises(RunnerTimeoutError):
            await service.run_git_operation(workspace.id, "list_repos")

    @pytest.mark.asyncio
    async def test_emit_failure_propagates(
        self, service, runner, workspace
    ):
        """Transport errors during emit propagate (RuntimeError, not swallowed)."""

        async def _boom(runner_arg, event, payload):
            raise RuntimeError("socket down")

        service._emit_to_runner = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="socket down"):
            await service.run_git_operation(workspace.id, "list_repos")


@pytest.mark.django_db(transaction=True)
class TestHandleGitReply:
    def _register_pending(
        self, service, workspace_id, operation: str, request_id: str
    ):
        from apps.runners.services import _PendingGitRequest

        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        service._git_pending[request_id] = _PendingGitRequest(
            future=fut,
            workspace_id=str(workspace_id),
            operation=operation,
        )
        return loop, fut

    def test_unknown_request_id_dropped(self, service, runner, workspace):
        """Replies for unknown request_ids resolve nothing."""
        assert service._resolve_git_future("nope", {"ok": True}) is False

    def test_missing_workspace_id_dropped(self, service, runner, workspace):
        """Authenticated replies without workspace_id are dropped."""
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-1"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-1",
                    "operation": "list_repos",
                    "ok": True,
                },
                runner_id=str(runner.id),
            )
            assert not fut.done()
        finally:
            service._git_pending.pop("req-1", None)
            loop.close()

    def test_foreign_runner_reply_dropped(
        self, service, runner, workspace, organization
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
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-2"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-2",
                    "workspace_id": str(workspace.id),
                    "operation": "list_repos",
                    "ok": True,
                },
                runner_id=str(other.id),
            )
            assert not fut.done()
        finally:
            service._git_pending.pop("req-2", None)
            loop.close()

    def test_happy_reply_resolves_future(self, service, runner, workspace):
        """Exact workspace_id + operation match resolves the waiter."""
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-ok"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-ok",
                    "workspace_id": str(workspace.id),
                    "operation": "list_repos",
                    "ok": True,
                },
                runner_id=str(runner.id),
            )
            assert fut.done()
            assert fut.result() == {
                "request_id": "req-ok",
                "workspace_id": str(workspace.id),
                "operation": "list_repos",
                "ok": True,
            }
        finally:
            service._git_pending.pop("req-ok", None)
            loop.close()

    def test_happy_reply_resolves_without_runner_id(
        self, service, runner, workspace
    ):
        """Unit/internal replies (runner_id None) still require exact match."""
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-internal"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-internal",
                    "workspace_id": str(workspace.id),
                    "operation": "list_repos",
                    "ok": True,
                },
                runner_id=None,
            )
            assert fut.done()
        finally:
            service._git_pending.pop("req-internal", None)
            loop.close()

    def test_missing_operation_dropped(self, service, runner, workspace):
        """Socket contract requires operation; missing never resolves."""
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-no-op"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-no-op",
                    "workspace_id": str(workspace.id),
                    "ok": True,
                },
                runner_id=str(runner.id),
            )
            assert not fut.done()
        finally:
            service._git_pending.pop("req-no-op", None)
            loop.close()

    def test_operation_mismatch_dropped(self, service, runner, workspace):
        """Same-runner reply with the wrong operation never resolves."""
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-op-mismatch"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-op-mismatch",
                    "workspace_id": str(workspace.id),
                    "operation": "push",
                    "ok": True,
                },
                runner_id=str(runner.id),
            )
            assert not fut.done()
        finally:
            service._git_pending.pop("req-op-mismatch", None)
            loop.close()

    def test_nonstring_operation_dropped(self, service, runner, workspace):
        """Non-string operation echoes are malformed and dropped."""
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-op-nonstr"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-op-nonstr",
                    "workspace_id": str(workspace.id),
                    "operation": {"name": "list_repos"},
                    "ok": True,
                },
                runner_id=str(runner.id),
            )
            assert not fut.done()
        finally:
            service._git_pending.pop("req-op-nonstr", None)
            loop.close()

    def test_workspace_mismatch_dropped_same_runner(
        self, service, runner, workspace, user
    ):
        """Same-runner reply for another workspace never resolves the waiter."""
        from apps.runners.enums import WorkspaceStatus
        from apps.runners.models import Workspace

        other_workspace = Workspace.objects.create(
            runner=runner,
            name="other ws",
            status=WorkspaceStatus.RUNNING,
            created_by=user,
        )
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-ws-mismatch"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-ws-mismatch",
                    "workspace_id": str(other_workspace.id),
                    "operation": "list_repos",
                    "ok": True,
                },
                runner_id=str(runner.id),
            )
            assert not fut.done()
        finally:
            service._git_pending.pop("req-ws-mismatch", None)
            loop.close()

    def test_workspace_mismatch_dropped_without_runner_id(
        self, service, runner, workspace, user
    ):
        """Internal callers also fail closed on workspace mismatch."""
        from apps.runners.enums import WorkspaceStatus
        from apps.runners.models import Workspace

        other_workspace = Workspace.objects.create(
            runner=runner,
            name="other ws internal",
            status=WorkspaceStatus.RUNNING,
            created_by=user,
        )
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-ws-mismatch-internal"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-ws-mismatch-internal",
                    "workspace_id": str(other_workspace.id),
                    "operation": "list_repos",
                    "ok": True,
                },
                runner_id=None,
            )
            assert not fut.done()
        finally:
            service._git_pending.pop("req-ws-mismatch-internal", None)
            loop.close()

    def test_invalid_workspace_uuid_dropped(self, service, runner, workspace):
        """Malformed workspace_id UUIDs are dropped without resolving."""
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-ws-invalid"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-ws-invalid",
                    "workspace_id": "not-a-uuid",
                    "operation": "list_repos",
                    "ok": True,
                },
                runner_id=str(runner.id),
            )
            assert not fut.done()
        finally:
            service._git_pending.pop("req-ws-invalid", None)
            loop.close()

    def test_mismatched_reply_keeps_pending_for_late_happy_reply(
        self, service, runner, workspace
    ):
        """A dropped mismatch must not consume the waiter; happy retry wins."""
        loop, fut = self._register_pending(
            service, workspace.id, "list_repos", "req-retry"
        )
        try:
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-retry",
                    "workspace_id": str(workspace.id),
                    "operation": "push",
                    "ok": True,
                },
                runner_id=str(runner.id),
            )
            assert not fut.done()
            assert "req-retry" in service._git_pending
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": "req-retry",
                    "workspace_id": str(workspace.id),
                    "operation": "list_repos",
                    "ok": True,
                },
                runner_id=str(runner.id),
            )
            assert fut.done()
        finally:
            service._git_pending.pop("req-retry", None)
            loop.close()

    @pytest.mark.asyncio
    async def test_timeout_cleans_pending(self, service, runner, workspace):
        """Timed-out RPCs pop the pending entry (no leak for late replies)."""
        request_id = "req-timeout-cleanup"

        async def _never_emit(runner_arg, event, payload):
            return None

        service._emit_to_runner = AsyncMock(side_effect=_never_emit)  # type: ignore[method-assign]
        with pytest.raises(RunnerTimeoutError):
            await service._await_git_result(
                request_id=request_id,
                workspace_id=workspace.id,
                operation="list_repos",
                payload={"request_id": request_id},
                runner=runner,
                timeout=0.02,
            )
        assert request_id not in service._git_pending

    @pytest.mark.asyncio
    async def test_await_registers_expected_workspace_and_operation(
        self, service, runner, workspace, monkeypatch
    ):
        """_await_git_result pins expected workspace/operation before emit."""
        from apps.runners.services import _PendingGitRequest

        seen: dict = {}

        async def _capture(runner_arg, event, payload):
            pending = service._git_pending.get(payload["request_id"])
            seen["pending"] = pending
            assert isinstance(pending, _PendingGitRequest)
            assert pending.workspace_id == str(workspace.id)
            assert pending.operation == "list_repos"
            assert event == "git:operation"
            service.handle_git_reply(
                "git:operation_result",
                {
                    "request_id": payload["request_id"],
                    "workspace_id": str(workspace.id),
                    "operation": "list_repos",
                    "ok": True,
                },
                runner_id=str(runner.id),
            )

        monkeypatch.setattr(service, "_emit_to_runner", _capture)
        result = await service._await_git_result(
            request_id="req-register-check",
            workspace_id=workspace.id,
            operation="list_repos",
            payload={"request_id": "req-register-check"},
            runner=runner,
            timeout=5.0,
        )
        assert result["ok"] is True
        assert "req-register-check" not in service._git_pending
        assert seen["pending"].workspace_id == str(workspace.id)
        assert seen["pending"].operation == "list_repos"

    def test_unknown_event_ignored(self, service):
        """Non-git events never touch git futures."""
        service.handle_git_reply("harness:process_start_result", {"request_id": "x"})
        assert "x" not in service._git_pending

    def test_malformed_payload_dropped(self, service, runner):
        """Non-dict / request-less payloads are dropped without resolving."""
        service.handle_git_reply("git:operation_result", "nope", runner_id=str(runner.id))  # type: ignore[arg-type]
        service.handle_git_reply(
            "git:operation_result", {"workspace_id": "x"}, runner_id=str(runner.id)
        )
        service.handle_git_reply(
            "git:operation_result",
            {"request_id": "r", "workspace_id": "not-a-uuid", "ok": True},
            runner_id=str(runner.id),
        )
