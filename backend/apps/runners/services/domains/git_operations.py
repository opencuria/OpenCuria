"""Whitelisted git operations (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.workspaces``,
``self._git_pending`` / ``self._git_locks`` / ``self._git_locks_guard``
plus sibling helpers (``_emit_to_runner`` via ``RunnerTransportMixin``,
``_ensure_git_dispatchable`` / ``_validate_task_runner`` /
``_validate_harness_workspace_runner`` via ``OwnershipMixin``,
``touch_workspace_activity`` via :class:`WorkspaceLifecycleMixin`).
``_PendingGitRequest`` stays defined in the facade module and is imported
lazily inside ``_await_git_result`` (no circular import).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from ...exceptions import (
    RunnerOfflineError,
    RunnerTimeoutError,
    WorkspaceNotFoundError,
)

logger = logging.getLogger(__name__)


class GitOperationsMixin:
    """Whitelisted git RPCs shared by RunnerService."""

    # ------------------------------------------------------------------
    # Git operations (productive git integration)
    # ------------------------------------------------------------------

    #: Whitelisted git operations forwarded to the runner (mirrors
    #: ``GIT_OPERATIONS`` in ``runner/src/git.py``). Only operation +
    #: typed args are ever dispatched — never free shell/env input.
    GIT_OPERATIONS: tuple[str, ...] = (
        "list_repos",
        "repo_snapshot",
        "repo_history",
        "working_diff",
        "commit_details",
        "stage",
        "unstage",
        "discard",
        "commit",
        "fetch",
        "pull",
        "push",
        "sync",
        "checkout_branch",
        "checkout_commit",
        "checkout_remote_branch",
        "create_branch",
        "rename_branch",
        "delete_branch",
        "merge_into_current",
        "merge_current_into",
        "merge_abort",
        "stash_apply",
        "stash_pop",
        "stash_drop",
        "stash_branch",
    )

    #: Read-only operations (short timeout budget).
    GIT_READ_OPERATIONS = frozenset(
        {"list_repos", "repo_snapshot", "repo_history", "working_diff", "commit_details"}
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
        runner: "Runner",
        timeout: float,
    ) -> dict:
        """Emit a git:operation RPC and wait for its correlated result."""
        from .. import _PendingGitRequest

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._git_pending[request_id] = _PendingGitRequest(
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
                :attr:`GIT_OPERATIONS`). Anything else raises
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
        if operation not in self.GIT_OPERATIONS:
            raise ValueError(f"Unknown git operation: {operation!r}")
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        runner = self._ensure_git_dispatchable(workspace)

        safe_args = self._sanitize_git_args(operation, dict(args or {}), user=user)
        if repo_path is not None:
            self._validate_git_repo_path(repo_path)

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
    def _validate_git_repo_path(repo_path: str) -> str:
        """Basic backend check for repo paths (runner re-validates)."""
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise ValueError("repo_path must be a non-empty string")
        cleaned = repo_path.strip()
        if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
            raise ValueError(f"Invalid repo path: {repo_path!r}")
        normalized = os.path.normpath(cleaned)
        if normalized != "/workspace" and not normalized.startswith("/workspace/"):
            raise ValueError(f"Repo path must be under /workspace: {repo_path!r}")
        if len(cleaned) > 512:
            raise ValueError("repo_path too long (max 512 chars)")
        return cleaned

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

    @classmethod
    def _check_git_args_allowed(cls, operation: str, args: dict) -> None:
        """Reject unknown/caller-controlled arg keys for *operation*."""
        allowed = cls._GIT_ALLOWED_ARGS.get(operation)
        if allowed is None:
            raise ValueError(f"Unknown git operation: {operation!r}")
        for key in args:
            if key not in allowed:
                raise ValueError(
                    f"Unknown argument {key!r} for git operation {operation!r}"
                )

    #: Allowed arg keys per git operation. Anything else (``env``,
    #: ``author_*``, ``args``/``argv``, aliases, typos) raises ValueError —
    #: the service never silently drops caller input.
    _GIT_ALLOWED_ARGS: dict[str, frozenset] = {
        "list_repos": frozenset(),
        "repo_snapshot": frozenset(),
        "repo_history": frozenset({"history_limit", "history_skip", "branch"}),
        "working_diff": frozenset(),
        "commit_details": frozenset({"commit"}),
        "stage": frozenset({"paths"}),
        "unstage": frozenset({"paths"}),
        "discard": frozenset({"paths"}),
        "commit": frozenset({"message"}),
        "fetch": frozenset({"remote"}),
        "pull": frozenset({"remote", "branch"}),
        "push": frozenset({"remote", "set_upstream"}),
        "sync": frozenset({"remote"}),
        "checkout_branch": frozenset({"branch"}),
        "checkout_commit": frozenset({"commit"}),
        "checkout_remote_branch": frozenset({"remote_ref", "local_name"}),
        "create_branch": frozenset({"branch", "start_point", "checkout"}),
        "rename_branch": frozenset({"new_branch", "old_branch"}),
        "delete_branch": frozenset({"branch"}),
        "merge_into_current": frozenset({"branch", "message"}),
        "merge_current_into": frozenset({"target", "message"}),
        "merge_abort": frozenset(),
        "stash_apply": frozenset({"stash"}),
        "stash_pop": frozenset({"stash"}),
        "stash_drop": frozenset({"stash"}),
        "stash_branch": frozenset({"stash", "branch"}),
    }

    @staticmethod
    def _validate_git_branch_name(value: object, *, field: str = "branch") -> str:
        """Backend branch check (schema-equivalent; runner validates strictly)."""
        cleaned = str(value or "").strip()
        if not cleaned or len(cleaned) > 255:
            raise ValueError(f"Invalid {field}")
        if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
            raise ValueError(f"Invalid {field}")
        return cleaned

    @staticmethod
    def _validate_git_commit_hash(value: object) -> str:
        """Validate a hex commit hash (full or abbreviated, min 4 chars)."""
        cleaned = str(value or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{4,64}", cleaned):
            raise ValueError("Invalid commit hash (must be 4-64 hex chars)")
        return cleaned

    @staticmethod
    def _validate_git_stash_selector(value: object) -> str:
        """Validate a stash selector (``stash@{n}``) fail-closed."""
        cleaned = str(value or "").strip()
        if not re.fullmatch(r"stash@\{\d{1,9}\}", cleaned):
            raise ValueError("Invalid stash (must be stash@{n})")
        return cleaned

    @staticmethod
    def _validate_git_file_paths(raw: object, *, operation: str) -> list[str]:
        """Validate repo-relative file paths (schema-equivalent)."""
        items = [raw] if isinstance(raw, str) else raw
        if not isinstance(items, list) or not items:
            raise ValueError(f"paths is required for operation {operation!r}")
        if len(items) > 256:
            raise ValueError("Too many paths (max 256)")
        cleaned: list[str] = []
        for item in items:
            text = str(item).strip()
            if not text:
                raise ValueError("paths must not contain empty entries")
            if len(text) > 256:
                raise ValueError("path too long (max 256 chars)")
            if "\x00" in text or "\n" in text or "\r" in text:
                raise ValueError(f"Invalid path: {item!r}")
            cleaned.append(text)
        return cleaned

    @classmethod
    def _sanitize_git_args(
        cls,
        operation: str,
        args: dict,
        *,
        user=None,
    ) -> dict:
        """Validate + whitelist operation args; never pass free env/argv.

        Unknown keys (``env``, ``author_*``, ``args``/``argv``, aliases
        like ``hash``/``path``/``new``/``message_b64``) raise ValueError.
        The runner needs plain ``message`` only (no ``message_b64``
        public API); ``author_name``/``author_email`` are injected
        server-side from *user* for ``commit`` after validation.
        """
        incoming = dict(args or {})
        cls._check_git_args_allowed(operation, incoming)
        if operation == "repo_history":
            out: dict = {}
            if "history_limit" in incoming:
                try:
                    limit = int(incoming["history_limit"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "history_limit must be an integer (1-500)"
                    ) from exc
                if limit < 1 or limit > 500:
                    raise ValueError("history_limit must be an integer (1-500)")
                out["history_limit"] = limit
            if "history_skip" in incoming:
                try:
                    skip = int(incoming["history_skip"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("history_skip must be an integer") from exc
                if skip < 0:
                    raise ValueError("history_skip must be >= 0")
                out["history_skip"] = skip
            if incoming.get("branch") not in (None, ""):
                out["branch"] = cls._validate_git_branch_name(incoming["branch"])
            return out
        if operation in {"list_repos", "repo_snapshot"}:
            return {}
        if operation == "commit_details":
            commit = str(incoming.get("commit") or "").strip()
            if not commit:
                raise ValueError("commit is required for commit_details")
            return {"commit": cls._validate_git_commit_hash(commit)}
        if operation in {"stage", "unstage", "discard"}:
            paths = cls._validate_git_file_paths(
                incoming.get("paths"), operation=operation
            )
            return {"paths": paths}
        if operation == "commit":
            message = str(incoming.get("message", "") or "")
            if not message.strip():
                raise ValueError("message is required for commit")
            if len(message) > 10000:
                raise ValueError("message too long (max 10000 chars)")
            out = {"message": message}
            if user is not None:
                author_name, author_email = cls.commit_identity_for_user(user)
                out["author_name"] = author_name
                out["author_email"] = author_email
            return out
        if operation in {"fetch", "pull", "push", "sync"}:
            out = {}
            if incoming.get("remote") not in (None, ""):
                out["remote"] = cls._validate_git_branch_name(
                    incoming["remote"], field="remote"
                )
            if operation == "pull" and incoming.get("branch") not in (None, ""):
                if incoming.get("remote") in (None, ""):
                    raise ValueError(
                        "remote is required when branch is set for pull"
                    )
                out["branch"] = cls._validate_git_branch_name(incoming["branch"])
            if operation == "push" and incoming.get("set_upstream") is not None:
                out["set_upstream"] = bool(incoming["set_upstream"])
            return out
        if operation == "checkout_branch":
            branch = str(incoming.get("branch", "") or "").strip()
            if not branch:
                raise ValueError("branch is required for checkout_branch")
            return {"branch": cls._validate_git_branch_name(branch)}
        if operation == "checkout_remote_branch":
            remote_ref = str(incoming.get("remote_ref", "") or "").strip()
            if not remote_ref:
                raise ValueError("remote_ref is required for checkout_remote_branch")
            if (
                len(remote_ref) > 255
                or "\x00" in remote_ref
                or "\n" in remote_ref
                or "\r" in remote_ref
            ):
                raise ValueError("Invalid remote_ref")
            remote, sep, branch_part = remote_ref.partition("/")
            if not sep or not remote or not branch_part:
                raise ValueError("Invalid remote_ref")
            out = {"remote_ref": remote_ref}
            if incoming.get("local_name") not in (None, ""):
                local = str(incoming["local_name"]).strip()
                if not local:
                    raise ValueError("Invalid local_name")
                if len(local) > 255:
                    raise ValueError("Invalid local_name")
                out["local_name"] = cls._validate_git_branch_name(
                    local, field="local_name"
                )
            return out
        if operation == "checkout_commit":
            commit = str(incoming.get("commit", "") or "").strip()
            if not commit:
                raise ValueError("commit is required for checkout_commit")
            return {"commit": cls._validate_git_commit_hash(commit)}
        if operation == "create_branch":
            branch = str(incoming.get("branch", "") or "").strip()
            if not branch:
                raise ValueError("branch is required for create_branch")
            out = {"branch": cls._validate_git_branch_name(branch)}
            if incoming.get("start_point") not in (None, ""):
                out["start_point"] = cls._validate_git_branch_name(
                    incoming["start_point"], field="start_point"
                )
            if incoming.get("checkout") is not None:
                out["checkout"] = bool(incoming["checkout"])
            return out
        if operation == "rename_branch":
            new = str(incoming.get("new_branch", "") or "").strip()
            if not new:
                raise ValueError("new_branch is required for rename_branch")
            out = {"new_branch": cls._validate_git_branch_name(
                new, field="new_branch"
            )}
            old = str(incoming.get("old_branch", "") or "").strip()
            if old:
                out["old_branch"] = cls._validate_git_branch_name(
                    old, field="old_branch"
                )
            return out
        if operation == "delete_branch":
            branch = str(incoming.get("branch", "") or "").strip()
            if not branch:
                raise ValueError("branch is required for delete_branch")
            return {"branch": cls._validate_git_branch_name(branch)}
        if operation == "merge_into_current":
            branch = str(incoming.get("branch", "") or "").strip()
            if not branch:
                raise ValueError("branch is required for merge_into_current")
            out = {"branch": cls._validate_git_branch_name(branch)}
            if incoming.get("message") not in (None, ""):
                message = str(incoming["message"])
                if len(message) > 4096:
                    raise ValueError("message too long (max 4096 chars)")
                out["message"] = message
            return out
        if operation == "merge_current_into":
            target = str(incoming.get("target", "") or "").strip()
            if not target:
                raise ValueError("target is required for merge_current_into")
            out = {"target": cls._validate_git_branch_name(
                target, field="target"
            )}
            if incoming.get("message") not in (None, ""):
                message = str(incoming["message"])
                if len(message) > 4096:
                    raise ValueError("message too long (max 4096 chars)")
                out["message"] = message
            return out
        if operation == "merge_abort":
            return {}
        if operation in {"stash_apply", "stash_pop", "stash_drop"}:
            stash = str(incoming.get("stash", "") or "").strip()
            if not stash:
                raise ValueError(f"stash is required for {operation}")
            return {"stash": cls._validate_git_stash_selector(stash)}
        if operation == "stash_branch":
            stash = str(incoming.get("stash", "") or "").strip()
            if not stash:
                raise ValueError("stash is required for stash_branch")
            branch = str(incoming.get("branch", "") or "").strip()
            if not branch:
                raise ValueError("branch is required for stash_branch")
            return {
                "stash": cls._validate_git_stash_selector(stash),
                "branch": cls._validate_git_branch_name(branch),
            }
        if operation == "working_diff":
            return {}
        raise ValueError(f"Unknown git operation: {operation!r}")
