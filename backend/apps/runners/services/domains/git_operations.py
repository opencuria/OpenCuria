"""Dispatch validated Git operations and correlate replies from the owning runner."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from ...exceptions import (
    RunnerOfflineError,
    RunnerTimeoutError,
    WorkspaceNotFoundError,
)
from ...git_validation import GIT_ALLOWED_ARGS, validate_git_args, validate_repo_path
from ..infra.rpc_registry import PendingGitRequest

if TYPE_CHECKING:
    from ...models import Runner

logger = logging.getLogger(__name__)


class GitOperationsMixin:
    """Whitelisted git RPCs shared by RunnerService."""

    # ------------------------------------------------------------------
    # Git operations (productive git integration)
    # ------------------------------------------------------------------

    #: Read-only operations (short timeout budget).
    GIT_READ_OPERATIONS = frozenset(
        {
            "list_repos", "repo_snapshot", "repo_history", "working_diff",
            "commit_details", "file_content",
        }
    )

    #: Timeout budgets (seconds): reads are fast; network ops may take
    #: as long as a full clone/fetch round-trip.
    #: Backend waits longer than the runner total outer budgets
    #: (60s reads / 150s network), adding +15s/+30s headroom so the
    #: runner answers first and the backend never times out early.
    _GIT_READ_TIMEOUT_SECONDS = 75
    _GIT_NETWORK_TIMEOUT_SECONDS = 180

    @classmethod
    def git_timeout_for(cls, operation: str) -> float:
        """Return the runner round-trip timeout budget for *operation*."""
        if operation in cls.GIT_READ_OPERATIONS:
            return cls._GIT_READ_TIMEOUT_SECONDS
        return cls._GIT_NETWORK_TIMEOUT_SECONDS

    @staticmethod
    def _git_lock_key(workspace_id: uuid.UUID, repo_path: str | None) -> str:
        """Return the serialisation key for one workspace/repo pair.

        The runner serialises per resolved repository root; the
        ``list_repos`` discovery aggregate locks per repo (see
        :meth:`run_git_operation`). These backend process-local locks
        are best effort only: they serialise concurrent dispatch inside
        this backend process, but multiple backend workers still rely
        on the runner-side lock. ``/workspace`` is the discovery
        aggregate key (runner lock per repo).
        """
        repo = (repo_path or "").strip() or "/workspace"
        return f"{workspace_id}:{repo}"

    async def _git_lock(
        self, workspace_id: uuid.UUID, repo_path: str | None
    ) -> asyncio.Lock:
        """Return the serialising lock for one workspace/repo pair.

        Backend process-local lock (best effort; the runner enforces the
        authoritative per-repo serialisation). Known keys are bounded:
        at most one entry per workspace plus one per repo path ever used,
        and entries are reused, never recreated per call.
        """
        key = self._git_lock_key(workspace_id, repo_path)
        async with self._git_locks_guard:
            lock = self._git_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._git_locks[key] = lock
            return lock

    def handle_git_reply(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> None:
        """Route a git:operation_result reply to its waiter (sync).

        Called from the Socket.IO handler via ``sync_to_async`` after
        :func:`_require_runner_id` rejected unauthenticated sessions.
        The reply must carry ``request_id``, ``workspace_id`` and
        ``operation`` exactly matching the pending request registered by
        :meth:`_await_git_result` (fail-closed trust boundary — also
        enforced when *runner_id* is None, e.g. unit/internal calls).
        Missing/malformed/mismatched replies are logged and dropped —
        never forwarded to the frontend event bus. Runner-sender
        ownership of the reply workspace is always verified before the
        pending future is resolved; unknown ``request_id`` values are
        logged once and dropped.
        """
        if event != "git:operation_result":
            return
        if not isinstance(data, dict):
            logger.warning("git:operation_result rejected: malformed payload")
            return
        raw_request_id = data.get("request_id", "")
        request_id = str(raw_request_id or "")
        if not request_id:
            logger.warning("git:operation_result rejected: missing request_id")
            return
        pending = self._git_pending.get(request_id)
        if pending is None:
            logger.warning("git reply for unknown request_id %s", request_id)
            return
        raw_workspace_id = data.get("workspace_id", "")
        workspace_id = str(raw_workspace_id or "")
        if not workspace_id:
            logger.warning(
                "git:operation_result rejected: missing workspace_id "
                "(request %s)",
                request_id,
            )
            return
        try:
            workspace_uuid = uuid.UUID(workspace_id)
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "git:operation_result rejected: invalid workspace_id %s",
                workspace_id,
            )
            return
        if workspace_id != pending.workspace_id:
            logger.warning(
                "git:operation_result rejected: workspace_id mismatch "
                "(request %s)",
                request_id,
            )
            return
        raw_operation = data.get("operation")
        if not isinstance(raw_operation, str) or not raw_operation:
            logger.warning(
                "git:operation_result rejected: missing operation "
                "(request %s)",
                request_id,
            )
            return
        if raw_operation != pending.operation:
            logger.warning(
                "git:operation_result rejected: operation mismatch "
                "(request %s)",
                request_id,
            )
            return
        if runner_id:
            if not self._validate_harness_workspace_runner(
                workspace_uuid, runner_id
            ):
                return
        else:
            # No sender to check (unit/internal call): the reply workspace
            # must still resolve to a known workspace so a forged reply
            # for a garbage UUID can never resolve a waiter.
            if self.workspaces.get_runner_id(workspace_uuid) is None:
                logger.warning(
                    "git:operation_result rejected: workspace %s not found",
                    workspace_id,
                )
                return
        self._resolve_git_future(request_id, data)

    async def _await_git_result(
        self,
        *,
        request_id: str,
        workspace_id: uuid.UUID,
        operation: str,
        payload: dict,
        runner: Runner,
        timeout: float,
    ) -> dict:
        """Emit a git:operation RPC and wait for its correlated result."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._git_pending[request_id] = PendingGitRequest(
            future=future,
            workspace_id=str(workspace_id),
            operation=operation,
        )
        try:
            await self._emit_to_runner(runner, "git:operation", payload)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise RunnerTimeoutError("git:operation", timeout) from exc
        finally:
            self._git_pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def run_git_operation(
        self,
        workspace_id: uuid.UUID,
        operation: str,
        *,
        repo_path: str | None = None,
        args: dict | None = None,
        user=None,
    ) -> dict:
        """Dispatch one whitelisted git operation to the owning runner.

        Args:
            workspace_id: Target workspace (must be RUNNING, no blocking
                ``active_operation``, owner runner online).
            operation: Whitelisted operation name (see
                ``git_validation.GIT_ALLOWED_ARGS``). Anything else raises
                ``ValueError`` before any runner dispatch.
            repo_path: Absolute repo path under ``/workspace``
                (omitted for ``list_repos`` discovery, sent top-level
                for all other ops). Basic format check only — the
                runner re-validates fail-closed.
            args: Operation-specific typed args (paths, branch names,
                messages). Unknown keys (``env``, ``author_*``,
                ``args``/``argv``, aliases, typos) raise ``ValueError``;
                git auth comes only from injected workspace
                credentials on the runner.
            user: Optional account user for the commit-identity fallback
                (``author_name``/``author_email`` for ``commit`` when the
                repo has no git identity configured).

        Returns:
            The raw runner result dict (``ok`` plus ``repos`` /
            ``snapshot`` / ``diff`` / ``details`` / ``commits``
            payloads, or a structured ``ok: False`` error with
            ``code``/``message``).

        Raises:
            WorkspaceNotFoundError: Unknown workspace.
            WorkspaceStateError: Workspace not running / busy / deleting.
            RunnerOfflineError: Owning runner is offline.
            RunnerTimeoutError: Runner did not answer before the budget.
            ConflictError: Runner call failed (emit error etc.).
            ValueError: Unknown operation or invalid local arguments.
        """
        if operation not in GIT_ALLOWED_ARGS:
            raise ValueError(f"Unknown git operation: {operation!r}")
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        runner = self._ensure_git_dispatchable(workspace)

        safe_args = validate_git_args(operation, dict(args or {}))
        if operation == "commit" and user is not None:
            name, email = self.commit_identity_for_user(user)
            safe_args.update(author_name=name, author_email=email)
        if operation != "list_repos" and repo_path is None:
            raise ValueError(f"repo_path is required for operation {operation!r}")
        if repo_path is not None:
            repo_path = validate_repo_path(repo_path)

        request_id = uuid.uuid4().hex
        payload: dict = {
            "request_id": request_id,
            "workspace_id": str(workspace_id),
            "operation": operation,
            "args": safe_args,
        }
        if repo_path is not None:
            payload["repo_path"] = repo_path

        lock = await self._git_lock(workspace_id, repo_path)
        timeout = self.git_timeout_for(operation)
        async with lock:
            try:
                result = await self._await_git_result(
                    request_id=request_id,
                    workspace_id=workspace_id,
                    operation=operation,
                    payload=payload,
                    runner=runner,
                    timeout=timeout,
                )
            except (RunnerTimeoutError, RunnerOfflineError, RuntimeError):
                raise
            except Exception as exc:
                raise ConflictError(f"Git operation failed: {exc}") from exc
        if not isinstance(result, dict):
            raise ConflictError("Git operation returned no result")
        await self.touch_workspace_activity(workspace_id)
        return result

    @staticmethod
    def commit_identity_for_user(user) -> tuple[str, str]:
        """Return the (author_name, author_email) fallback for *user*.

        The runner only uses these when the repo has no git identity
        configured; they never come from the API client.
        """
        email = str(getattr(user, "email", "") or "").strip()
        full_name = ""
        try:
            full_name = str(user.get_full_name() or "").strip()
        except Exception:
            full_name = ""
        if full_name:
            name = full_name
        else:
            username = str(getattr(user, "username", "") or "").strip()
            if username:
                name = username
            elif "@" in email:
                name = email.split("@", 1)[0]
            else:
                name = "opencuria"
        if not email:
            email = "opencuria@localhost"
        return name[:255], email[:320]
