"""Full git orchestration (Step 7 services group).

Canonical home for the git orchestration previously living on
``WorkspaceService`` in :mod:`src.service`:

- the ``_git_lock_map`` keyed lock map (never-evict semantics;
  ``_git_locks`` / ``_git_locks_guard`` stay readable/writable as live
  aliases onto the map's dict/guard so ``WorkspaceService`` property
  aliases and tests poking ``service._git_locks[...]`` keep working),
- ``_git_exec`` / ``_git_realpath_contained`` /
  ``_git_verify_metadata_paths`` / ``_git_verify_repo_root`` /
  ``_git_resolve_repo_root`` / ``_git_discover_repos`` /
  ``list_git_repositories``,
- snapshot / diff / history assembly (``_git_list_entry``,
  ``_git_repo_snapshot_no_log``, ``_git_repo_history``,
  ``_git_merge_state``, ``_git_working_diff``, ``_git_diff_paths``,
  ``_git_diff_numstat_raw``, ``_git_join_diff_parts``,
  ``_git_untracked_entry``, ``_git_untracked_fallback``,
  ``_git_commit_details``),
- the RPC entry point (``execute_git_operation`` /
  ``_execute_git_operation_inner``) plus every ``_git_op_*`` mutation /
  read / network / branch / merge helper.

``src.git`` stays the pure lib (argv builders, wrappers, validation,
parsing, env, askpass); all runtime interaction and serialisation
lives here, imported as ``git_ops`` (same as ``src.service`` did).

``WorkspaceService`` keeps thin delegates (same names/signatures/
messages) plus a ``git`` property onto the manager and
``_git_lock_map`` / ``_git_locks`` / ``_git_locks_guard`` aliases, so
existing callers and tests keep working.

Workspace resolution is injected so this module never imports
``src.service`` (no dependency cycle):

- ``runtimes``: runtime backends by type (kept for introspection;
  resolution itself goes through the callables below).
- ``get_cached``: ``(workspace_id) -> WorkspaceInfo``; raises
  ``ValueError("... not found")`` for unknown ids.
- ``get_runtime``: ``(workspace_id) -> RuntimeBackend``; raises
  ``RuntimeError`` for unknown runtimes.

Extraction owner: Step 7 (verbatim move; ``Step 5`` lock mechanics via
the canonical ``src.services.exec_kernel.KeyedLockMap`` are preserved).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Callable
from typing import Any

import structlog

from .. import git as git_ops
from ..models import WorkspaceInfo
from ..runtime.base import RuntimeBackend
from .exec_kernel import KeyedLockMap

logger = structlog.get_logger(__name__)


class GitService:
    """Owns git orchestration for workspace repositories.

    Injected dependencies (all mirror ``WorkspaceService`` helpers):

    - ``runtimes``: runtime backends by type (kept for introspection;
      resolution itself goes through the callables below).
    - ``get_cached``: ``(workspace_id) -> WorkspaceInfo``; raises
      ``ValueError("... not found")`` for unknown ids.
    - ``get_runtime``: ``(workspace_id) -> RuntimeBackend``; raises
      ``RuntimeError`` for unknown runtimes.
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {}
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        # Serialises concurrent git snapshot + mutation requests per
        # workspace/repo (same never-evict rationale as the background
        # and desktop lock maps: dropping a lock object while a holder
        # waits would hand the next caller a different lock). Step 5:
        # the get-or-create never-evict mechanics live in the canonical
        # ``KeyedLockMap`` (``src.services.exec_kernel``);
        # ``_git_locks`` / ``_git_locks_guard`` stay readable/writable
        # as live aliases onto the map's dict/guard so
        # ``WorkspaceService`` property aliases and tests poking
        # ``service._git_locks[...]`` keep working.
        self._git_lock_map: KeyedLockMap = KeyedLockMap()

    @property
    def _git_locks(self) -> dict[tuple[uuid.UUID, str], asyncio.Lock]:
        """Alias onto the git lock map's underlying dict (live)."""
        return self._git_lock_map.locks

    @_git_locks.setter
    def _git_locks(self, value: dict) -> None:
        self._git_lock_map.locks.clear()
        self._git_lock_map.locks.update(value)

    @property
    def _git_locks_guard(self) -> asyncio.Lock:
        """Alias onto the git lock map's guard lock."""
        return self._git_lock_map.guard

    @_git_locks_guard.setter
    def _git_locks_guard(self, value: asyncio.Lock) -> None:
        self._git_lock_map.guard = value

    # ── Git operations (dumb-executor git service) ──────────────────────

    async def _git_lock(
        self, workspace_id: uuid.UUID, repo_root: str
    ) -> asyncio.Lock:
        """Return the serialising lock for one workspace/repo pair.

        The entry is created once and retained for the lifetime of the
        runner process (bounded by ever-seen workspace/repo pairs;
        cleared on restart). It is never dropped: dropping after release
        cannot observe queued waiters via the public ``asyncio.Lock``
        API — ``release()`` only schedules the first waiter's wakeup,
        and the waiter sets its locked state later — so a drop in that
        window hands a third caller a different lock object while the
        woken waiter still references the old one, silently breaking
        serialisation. One small in-memory ``asyncio.Lock`` per
        workspace/repo pair is the accepted trade-off for correctness.

        Step 5: mechanics live in the canonical ``KeyedLockMap``
        (``src.services.exec_kernel``); Step 7 moves full git
        orchestration here (``src.services.git_service``).
        """
        return await self._git_lock_map.get((workspace_id, repo_root))

    async def _git_exec(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        argv: list[str],
        *,
        workdir: str,
        env: dict[str, str],
        timeout: float,
        check_git: bool = False,
    ) -> tuple[int, str]:
        """Run one git argv inside the workspace with a timeout.

        Every git argv runs behind the fixed sourcing wrapper
        (:func:`src.git.git_exec_wrapper_argv`) so the persistent
        credential file is sourced and ``GITHUB_TOKEN`` is inherited by
        git and its askpass child — without ever appearing in argv, URLs
        or logs.  User-controlled git arguments stay separate argv
        elements behind ``exec "$@"`` and are never shell-interpreted.
        Only non-git probes (``realpath``/``find``/``test``/``rm``) run
        unwrapped.  Raises :class:`GitError` with code ``missing_git``
        when the git binary is absent.
        """
        if argv[:1] == ["git"]:
            command = git_ops.git_exec_wrapper_argv(list(argv))
        else:
            command = list(argv)
        try:
            exit_code, output = await asyncio.wait_for(
                runtime.exec_command_wait(
                    instance_id,
                    command=command,
                    workdir=workdir,
                    env=env,
                ),
                timeout,
            )
        except asyncio.TimeoutError as exc:
            raise git_ops.GitError("timeout", "Git operation timed out") from exc
        # Missing-binary detection is deliberately narrow: only a nonzero
        # exit of 126/127 *plus* a binary marker counts as missing_git.
        # Plain "not found" text from successful (exit 0) or exit-1 git
        # output — e.g. filenames or git's own messages — must stay a
        # normal operation result, never a false-positive missing_git.
        # _git_exec wraps git argv (sh -c wrapper) but inspects the
        # unwrapped argv, so both wrapped ["git", ...] and bare
        # ["realpath", "-m", ...] probes are covered here.
        lowered = output.lower() if isinstance(output, str) else ""
        missing_markers = (
            "command not found",
            "not recognized",
            "no such file or directory",
            "no such file",
            "not found",
        )
        is_git_argv = argv[:1] == ["git"]
        is_realpath_probe = argv[:1] == ["realpath", "-m"]
        if (
            exit_code in (126, 127)
            and (is_git_argv or is_realpath_probe)
            and any(m in lowered for m in missing_markers)
        ):
            # Only treat as missing-git when the marker refers to the
            # binary under test, not to repo content leaking into output.
            if is_git_argv or "realpath" in lowered:
                raise git_ops.GitError(
                    "missing_git",
                    "git is not available inside the workspace",
                    exit_code=exit_code,
                    stderr=output,
                )
        if check_git and exit_code != 0 and "not a git repository" in lowered:
            raise git_ops.GitError(
                "not_a_repo",
                "Path is not inside a git repository",
                exit_code=exit_code,
                stderr=output,
            )
        return exit_code, output if isinstance(output, str) else str(output)

    async def _git_realpath_contained(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        path: str,
        env: dict[str, str],
        *,
        context: str,
        display: str | None = None,
    ) -> str:
        """Resolve *path* via ``realpath -m`` and enforce ``/workspace``.

        *context* is ``"repo"`` for worktree roots and ``"metadata"`` for
        gitdir/common-dir values.  Fail-closed: realpath errors, empty or
        multiline output, and containment escapes all raise.  Messages only
        ever carry ``/workspace`` paths (never external host paths):
        metadata failures echo *display* (the repo root), never *path*.
        """
        exit_code, output = await self._git_exec(
            runtime,
            instance_id,
            ["realpath", "-m", path],
            workdir="/workspace",
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        safe_display = display if display is not None else path
        if exit_code != 0:
            if context == "repo":
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Could not resolve repo path: {safe_display}",
                    exit_code=exit_code,
                    stderr="",
                )
            raise git_ops.GitError(
                "unsafe_repository",
                f"Repository metadata outside workspace: {safe_display}",
                exit_code=exit_code,
                stderr="",
            )
        resolved = git_ops.parse_single_path_output(output)
        if resolved is None:
            if context == "repo":
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Could not resolve repo path: {safe_display}",
                    exit_code=exit_code,
                    stderr="",
                )
            raise git_ops.GitError(
                "unsafe_repository",
                f"Repository metadata outside workspace: {safe_display}",
                exit_code=exit_code,
                stderr="",
            )
        if not git_ops.is_workspace_path(resolved):
            raise git_ops.GitError(
                "unsafe_repository",
                f"Repository metadata outside workspace: {safe_display}",
                exit_code=exit_code,
                stderr="",
            )
        return resolved

    async def _git_verify_metadata_paths(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> str:
        """Verify gitdir + common-dir live under ``/workspace`` (fail-closed).

        *repo_root* is an already containment-checked ``/workspace`` path
        used as ``workdir`` and as the only path echoed in errors.  All
        rev-parse outputs must be single-line; empty/multiline/unexpected
        values reject.  ``--absolute-git-dir`` must be absolute; common-dir
        prefers ``--path-format=absolute`` with a plain ``--git-common-dir``
        fallback (relative values resolve against the verified git dir —
        see below — never against the worktree root).  Both values are
        passed through ``realpath -m`` containment.  Returns the verified
        (realpath) git dir.  No shell concatenation: all probes are fixed
        argv lists.
        """
        exit_code, output = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--absolute-git-dir"],
            workdir=repo_root,
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a git repository: {repo_root}",
                exit_code=exit_code,
                stderr="",
            )
        git_dir_raw = git_ops.parse_single_path_output(output)
        if git_dir_raw is None or not git_dir_raw.startswith("/"):
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a git repository: {repo_root}",
                exit_code=exit_code,
                stderr="",
            )
        git_dir = await self._git_realpath_contained(
            runtime, instance_id, git_dir_raw, env,
            context="metadata", display=repo_root,
        )
        # Common dir: absolute preferred, plain fallback for older git.
        # ``--path-format`` only affects following args, so it must precede
        # ``--git-common-dir``.  The plain (no --path-format) output is
        # relative to the *process cwd* on old git (prefix-relative per
        # setup.c/relative_path: "../.git" observed from a subdir of a
        # normal repo, "../../external-gitdir" from a separate-git-dir
        # root).  The cwd git ran in is *repo_root* — the worktree root
        # here, so prefix is empty — but when the plain fallback engages,
        # a relative value may also encode the in-gitdir ``commondir``
        # indirection ("../.." inside a linked worktree gitdir).  Git
        # resolves the commondir file relative to the git dir (see
        # get_common_dir_noenv: "%s/commondir" + "%s/<data>" joined onto
        # gitdir), never relative to the worktree root, so both candidate
        # bases (cwd + git dir) are checked: either both must resolve
        # under /workspace (fail-closed) or the candidate rejects.
        common_candidate: str | None = None
        exit_code_c, output_c = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            workdir=repo_root,
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_c == 0:
            parsed = git_ops.parse_single_path_output(output_c)
            if parsed is None:
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Path is not a git repository: {repo_root}",
                    exit_code=exit_code_c,
                    stderr="",
                )
            if parsed.startswith("/"):
                common_candidate = parsed
            else:
                # Explicit --path-format=absolute must yield an absolute
                # path; anything else is unexpected (or hostile) output.
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Path is not a git repository: {repo_root}",
                    exit_code=exit_code_c,
                    stderr="",
                )
        else:
            exit_code_f, output_f = await self._git_exec(
                runtime,
                instance_id,
                ["git", "rev-parse", "--git-common-dir"],
                workdir=repo_root,
                env=env,
                timeout=git_ops.GIT_READ_TIMEOUT_S,
            )
            if exit_code_f != 0:
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Path is not a git repository: {repo_root}",
                    exit_code=exit_code_f,
                    stderr="",
                )
            parsed_f = git_ops.parse_single_path_output(output_f)
            if parsed_f is None:
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Path is not a git repository: {repo_root}",
                    exit_code=exit_code_f,
                    stderr="",
                )
            if parsed_f.startswith("/"):
                common_candidate = os.path.normpath(parsed_f)
            else:
                # Old-git plain fallback: the value is cwd-relative (the
                # cwd being *repo_root*), but the same ".."-shaped value
                # can also encode a gitdir-relative commondir indirection
                # (git joins commondir content onto the git dir, see
                # get_common_dir_noenv).  Resolve against BOTH bases and
                # require both contained: realpath containment below still
                # runs on the chosen candidate, and the extra base check
                # here closes the gap where resolving only against
                # repo_root would normalise "../.." to "/".
                cwd_candidate = os.path.normpath(
                    os.path.join(repo_root, parsed_f)
                )
                gitdir_candidate = os.path.normpath(
                    os.path.join(git_dir, parsed_f)
                )
                if not (
                    git_ops.is_workspace_path(cwd_candidate)
                    and git_ops.is_workspace_path(gitdir_candidate)
                ):
                    raise git_ops.GitError(
                        "unsafe_repository",
                        f"Repository metadata outside workspace: {repo_root}",
                        exit_code=exit_code_f,
                        stderr="",
                    )
                common_candidate = cwd_candidate
        if common_candidate is None or not common_candidate.startswith("/"):
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a git repository: {repo_root}",
                exit_code=exit_code_c,
                stderr="",
            )
        await self._git_realpath_contained(
            runtime, instance_id, common_candidate, env,
            context="metadata", display=repo_root,
        )
        return git_dir

    async def _git_verify_repo_root(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        resolved: str,
        env: dict[str, str],
    ) -> str:
        """Verify *resolved* is a repo root with in-workspace metadata.

        Central root+metadata check shared by explicit resolution and
        discovery: ``--show-toplevel`` must equal *resolved*, then gitdir +
        common-dir must resolve under ``/workspace``.  *resolved* must
        already be a ``/workspace`` path (realpath-contained by the caller).
        """
        exit_code, output = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--show-toplevel"],
            workdir=resolved,
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a git repository: {resolved}",
                exit_code=exit_code,
                stderr="",
            )
        toplevel = git_ops.parse_single_path_output(output)
        if toplevel is None or toplevel != resolved:
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a repository root: {resolved}",
                exit_code=exit_code,
                stderr="",
            )
        await self._git_verify_metadata_paths(
            runtime, instance_id, resolved, env
        )
        return resolved

    async def _git_resolve_repo_root(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_arg: str,
        env: dict[str, str],
    ) -> str:
        """Resolve *repo_arg* to a verified repository root (fail-closed).

        Steps: normalise under ``/workspace`` → ``realpath -m`` symlink
        containment (a realpath failure is fatal — no unsandboxed
        fallback, symlinks must resolve) → ``git rev-parse
        --show-toplevel`` must equal the resolved path (no
        ``.git``-outside access, no subdirectories) → gitdir + common-dir
        must resolve under ``/workspace`` (no external ``.git`` file,
        symlink, ``--separate-git-dir``, worktree or submodule metadata).
        See :meth:`_git_verify_repo_root` for the shared check.
        """
        normalized = git_ops.normalize_repo_arg(repo_arg)
        resolved = await self._git_realpath_contained(
            runtime, instance_id, normalized, env, context="repo"
        )
        # Extra lexical guard so ``ValueError`` (invalid_argument) still
        # surfaces for direct escapes even if realpath mapping changes.
        if not git_ops.is_workspace_path(resolved):
            raise ValueError(f"Repo path escapes /workspace: {repo_arg!r}")
        return await self._git_verify_repo_root(
            runtime, instance_id, resolved, env
        )

    async def _git_discover_repos(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env: dict[str, str],
    ) -> list[str]:
        """Discover repository roots under ``/workspace`` (capped).

        Detects a repo directly in ``/workspace`` plus cloned repos at any
        depth (``find -prune`` never descends into ``.git`` internals).
        Every candidate goes through the central root+metadata check
        (:meth:`_git_verify_repo_root`); unsafe/unresolvable candidates
        (external gitdir/common-dir, symlink escapes, subdirectories) are
        omitted fail-closed.
        """
        roots: list[str] = []
        try:
            verified = await self._git_verify_repo_root(
                runtime, instance_id, "/workspace", env
            )
        except (git_ops.GitError, ValueError):
            verified = None
        if verified is not None:
            roots.append("/workspace")
        exit_code, output = await self._git_exec(
            runtime,
            instance_id,
            git_ops.discovery_find_args("/workspace"),
            workdir="/workspace",
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code == 0 and output.strip():
            candidates = git_ops.discovery_repo_roots(output)
            for candidate in candidates:
                if candidate in roots:
                    continue
                try:
                    resolved = await self._git_realpath_contained(
                        runtime, instance_id, candidate, env, context="repo"
                    )
                except (git_ops.GitError, ValueError):
                    # Fail closed: unresolvable candidates are skipped,
                    # never trusted unresolved.
                    continue
                if not git_ops.is_workspace_path(resolved):
                    continue
                if "/.git/" in resolved:
                    continue
                # The workspace root itself is only listed once (verified
                # above); skip the duplicate find hit.
                if resolved == "/workspace":
                    continue
                try:
                    await self._git_verify_repo_root(
                        runtime, instance_id, resolved, env
                    )
                except (git_ops.GitError, ValueError):
                    continue
                roots.append(resolved)
                if len(roots) >= git_ops.GIT_DISCOVERY_MAX_REPOS:
                    break
        return sorted(set(roots))[: git_ops.GIT_DISCOVERY_MAX_REPOS]

    async def list_git_repositories(
        self, workspace_id: uuid.UUID
    ) -> list[str]:
        """Return discovered repository roots for *workspace_id* (internal)."""
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        async with git_ops.github_askpass_context(runtime, info.instance_id):
            env = git_ops.build_git_env()
            return await self._git_discover_repos(runtime, info.instance_id, env)

    # -- git snapshot helpers -------------------------------------------------

    async def _git_list_entry(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Build a lightweight list entry for one repository root.

        Only ``rev-parse --abbrev-ref HEAD`` + ``rev-parse HEAD`` (no
        status, no log, no for-each-ref).  Detached HEAD (abbrev ``HEAD``)
        and unborn repos (no ``HEAD`` commit) map to
        ``current_branch=None``; unborn additionally maps to
        ``head_hash=None``.
        """
        timeout = git_ops.GIT_READ_TIMEOUT_S
        exit_code_b, abbrev_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        current_branch: str | None = None
        if exit_code_b == 0 and abbrev_out.strip():
            head_name = abbrev_out.strip().splitlines()[0].strip()
            # Detached HEAD reports literally "HEAD".
            current_branch = None if head_name in {"", "HEAD"} else head_name
        exit_code_h, head_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "HEAD"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        head_hash: str | None = None
        if exit_code_h == 0 and head_out.strip():
            head_hash = head_out.strip().splitlines()[0].strip() or None
        if head_hash is None:
            # Unborn repo: no commit exists, so no branch is meaningful
            # even when rev-parse --abbrev-ref echoed one.
            current_branch = None
        name = repo_root.rstrip("/").rsplit("/", 1)[-1] or "workspace"
        return {
            "id": repo_root,
            "name": name,
            "path": repo_root,
            "current_branch": current_branch,
            "head_hash": head_hash,
        }

    async def _git_repo_snapshot_no_log(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Build the snapshot dict for one repository root (no history)."""
        timeout = git_ops.GIT_READ_TIMEOUT_S

        exit_code, status_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "status", "--porcelain=v2", "--branch",
             "--untracked-files=all", "-z"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        changes: list[dict[str, Any]] = []
        branch_header: dict[str, Any] = {
            "oid": None, "head": None, "upstream": None, "ahead": 0, "behind": 0,
        }
        status_ok = exit_code == 0
        if status_ok:
            changes, branch_header = git_ops.changes_from_status_v2(status_out)
        else:
            # Fall back to v1 when v2 is unavailable (old git).
            exit_code1, v1_out = await self._git_exec(
                runtime,
                instance_id,
                ["git", "status", "--porcelain=v1", "-b", "-z"],
                workdir=repo_root,
                env=env,
                timeout=timeout,
            )
            if exit_code1 != 0:
                raise git_ops.GitError(
                    "git_failed",
                    "git status failed",
                    exit_code=exit_code,
                    stderr=status_out,
                )
            exit_code_h, head_out = await self._git_exec(
                runtime,
                instance_id,
                ["git", "rev-parse", "HEAD"],
                workdir=repo_root,
                env=env,
                timeout=timeout,
            )
            head_hash = head_out.strip().splitlines()[0].strip() if exit_code_h == 0 else ""
            changes, branch_header = git_ops.parse_status_v1(v1_out, head_hash)
            branch_header["oid"] = head_hash or None

        if branch_header.get("oid") is None:
            exit_code_h, head_out = await self._git_exec(
                runtime,
                instance_id,
                ["git", "rev-parse", "HEAD"],
                workdir=repo_root,
                env=env,
                timeout=timeout,
            )
            if exit_code_h == 0 and head_out.strip():
                branch_header["oid"] = head_out.strip().splitlines()[0].strip()

        # Branch list with upstream tracking + remote refs.
        us = git_ops._GIT_US
        rs = git_ops._GIT_RS
        ref_format = (
            f"%(refname){us}%(refname:short){us}%(objectname){us}"
            f"%(objecttype){us}%(upstream:short){us}%(upstream:track){us}%(HEAD)"
        )
        exit_code_r, refs_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "for-each-ref", f"--format={ref_format}{rs}",
             "refs/heads", "refs/remotes", "refs/tags"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        branches: list[dict[str, Any]] = []
        remote_refs: list[dict[str, Any]] = []
        if exit_code_r == 0:
            parsed_branches, parsed_remotes = git_ops.parse_for_each_ref(refs_out)
            for item in parsed_branches:
                ahead, behind = git_ops.parse_ahead_behind(item.get("track", ""))
                branches.append(
                    {
                        "name": item["name"],
                        "tip_hash": item["tip_hash"],
                        "upstream": item.get("upstream"),
                        "ahead": ahead,
                        "behind": behind,
                    }
                )
            branches.sort(key=lambda item: item["name"])
            for item in parsed_remotes:
                remote_refs.append({"name": item["name"], "tip_hash": item["tip_hash"]})
            remote_refs.sort(key=lambda item: item["name"])

        exit_code_m, remotes_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "remote"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        remotes: list[str] = []
        if exit_code_m == 0:
            remotes = sorted(
                line.strip() for line in remotes_out.splitlines() if line.strip()
            )

        exit_code_d, default_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        default_remote: str | None = None
        if exit_code_d == 0 and default_out.strip():
            default_remote = default_out.strip().splitlines()[0].strip() or None

        # Merge / rebase / cherry-pick state.
        merge_state = await self._git_merge_state(runtime, instance_id, repo_root, env)

        name = repo_root.rstrip("/").rsplit("/", 1)[-1] or "workspace"
        return {
            "id": repo_root,
            "name": name,
            "path": repo_root,
            "current_branch": branch_header.get("head"),
            "head_hash": branch_header.get("oid"),
            "branches": branches,
            "remote_refs": remote_refs,
            "remotes": remotes,
            "default_remote": default_remote,
            "upstream": branch_header.get("upstream"),
            "ahead": int(branch_header.get("ahead", 0)),
            "behind": int(branch_header.get("behind", 0)),
            "merge_state": merge_state,
            "changes": [
                {
                    "path": item["path"],
                    "old_path": item.get("old_path"),
                    "status": item["status"],
                    "staged": item.get("staged") is not None,
                    "staged_kind": item.get("staged"),
                    "unstaged": item.get("unstaged"),
                    "conflict": item.get("conflict"),
                    "diff": [],
                }
                for item in changes
            ],
        }

    async def _git_repo_history(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        *,
        limit: int = git_ops.GIT_HISTORY_DEFAULT_LIMIT,
        skip: int = 0,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Return one paginated history page for a repository root.

        Only ``git log --max-count limit+1 --skip skip`` (``--all`` when
        *branch* is None, else ``refs/heads/<branch>``).  The extra probe
        commit yields ``has_more``.
        """
        timeout = git_ops.GIT_READ_TIMEOUT_S
        capped_limit = max(1, min(int(limit), git_ops.GIT_HISTORY_MAX_LIMIT))
        capped_skip = max(0, int(skip))
        # Paginated history (newest first) + has_more probe.
        # ``--all --date-order`` covers every local branch, remote-tracking
        # ref, tag and stash entry — like vscode-git-graph — while detached
        # HEAD commits stay included (HEAD is an implicit starting point).
        # ``--exclude`` precedes ``--all`` (option order matters) to hide
        # only the internal notes fan-out (refs/notes/*); stash (refs/stash)
        # remains visible on purpose.
        us = git_ops._GIT_US
        rs = git_ops._GIT_RS
        log_format = (
            f"%H{us}%h{us}%P{us}%aN{us}%aE{us}%aI{us}"
            f"%cN{us}%cE{us}%cI{us}%s{us}%b{rs}"
        )
        argv = [
            "git", "log", "--exclude=refs/notes/*",
        ]
        if branch is not None:
            argv.append(f"refs/heads/{branch}")
        else:
            argv.append("--all")
        argv += [
            "--date-order", f"--format={log_format}",
            f"--max-count={capped_limit + 1}", f"--skip={capped_skip}",
        ]
        exit_code_l, log_out = await self._git_exec(
            runtime,
            instance_id,
            argv,
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        commits: list[dict[str, Any]] = []
        has_more = False
        if exit_code_l == 0 and log_out.strip():
            parsed = git_ops.parse_log_us_rs(log_out)
            has_more = len(parsed) > capped_limit
            for item in parsed[:capped_limit]:
                commits.append(
                    {
                        "hash": item["hash"],
                        "message": item["message"],
                        "body": item.get("body", ""),
                        "author": item.get("author", ""),
                        "author_email": item.get("author_email", ""),
                        "timestamp": item.get("author_date", ""),
                        "author_date": item.get("author_date", ""),
                        "committer": item.get("committer", ""),
                        "committer_email": item.get("committer_email", ""),
                        "committer_date": item.get("committer_date", ""),
                        "parents": item.get("parents", []),
                    }
                )
        if exit_code_l != 0:
            # Unborn repo: no commits exist yet — empty page, not an error.
            exit_code_h, _ = await self._git_exec(
                runtime, instance_id,
                ["git", "rev-parse", "--verify", "HEAD"],
                workdir=repo_root, env=env, timeout=timeout,
            )
            if exit_code_h != 0:
                return {
                    "commits": [],
                    "has_more": False,
                    "history_skip": capped_skip,
                    "history_limit": capped_limit,
                }
            raise git_ops.GitError(
                "git_failed",
                "git log failed",
                exit_code=exit_code_l,
                stderr=log_out,
            )
        return {
            "commits": commits,
            "has_more": has_more,
            "history_skip": capped_skip,
            "history_limit": capped_limit,
        }

    async def _git_merge_state(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Detect MERGE_HEAD / rebase / cherry-pick state files.

        Re-resolves the verified git dir (already sandbox-checked before
        every operation) fail-closed so the ``test -e`` probes below can
        never touch external metadata.  Any verification failure yields
        the neutral (no-merge) state.
        """
        state: dict[str, Any] = {
            "merging": False,
            "rebasing": False,
            "cherry_picking": False,
        }
        exit_code, git_dir_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--absolute-git-dir"],
            workdir=repo_root,
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            return state
        git_dir_raw = git_ops.parse_single_path_output(git_dir_out)
        if git_dir_raw is None or not git_dir_raw.startswith("/"):
            return state
        try:
            git_dir = await self._git_realpath_contained(
                runtime, instance_id, git_dir_raw, env,
                context="metadata", display=repo_root,
            )
        except (git_ops.GitError, ValueError):
            return state
        checks = {
            "merging": ["MERGE_HEAD"],
            "rebasing": ["rebase-merge", "rebase-apply"],
            "cherry_picking": ["CHERRY_PICK_HEAD"],
        }
        for flag, names in checks.items():
            for name in names:
                exit_code_t, _ = await self._git_exec(
                    runtime,
                    instance_id,
                    ["test", "-e", f"{git_dir}/{name}"],
                    workdir=repo_root,
                    env=env,
                    timeout=git_ops.GIT_READ_TIMEOUT_S,
                )
                if exit_code_t == 0:
                    state[flag] = True
                    break
        return state

    # -- git diff assembly -----------------------------------------------------

    async def _git_working_diff(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        changes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build staged/unstaged/untracked diff entries for a repo."""
        timeout = git_ops.GIT_READ_TIMEOUT_S
        if changes is None:
            exit_code, status_out = await self._git_exec(
                runtime,
                instance_id,
                ["git", "status", "--porcelain=v2", "--branch",
                 "--untracked-files=all", "-z"],
                workdir=repo_root,
                env=env,
                timeout=timeout,
            )
            if exit_code != 0:
                raise git_ops.GitError(
                    "git_failed", "git status failed",
                    exit_code=exit_code, stderr=status_out,
                )
            changes, _ = git_ops.changes_from_status_v2(status_out)

        staged_entries: list[dict[str, Any]] = []
        unstaged_entries: list[dict[str, Any]] = []

        staged_paths = sorted(
            {item["path"] for item in changes if item.get("staged") is not None}
        )
        unstaged_paths = sorted(
            {
                item["path"]
                for item in changes
                if item.get("unstaged") not in (None,)
            }
        )

        if staged_paths:
            staged_patches = await self._git_diff_paths(
                runtime, instance_id, repo_root, env,
                ["git", "diff", "--cached", "--no-color", "--no-ext-diff",
                 "--src-prefix=a/", "--dst-prefix=b/",
                 f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
                 "--patch", "--no-commit-id", "--", *staged_paths],
            )
            staged_numstat = await self._git_diff_numstat_raw(
                runtime, instance_id, repo_root, env,
                ["git", "diff", "--cached", "--no-color", "--no-ext-diff",
                 "--numstat", "-z", "--", *staged_paths],
                ["git", "diff", "--cached", "--no-color", "--no-ext-diff",
                 "--raw", "-z", "--", *staged_paths],
            )
            staged_entries = self._git_join_diff_parts(staged_patches, staged_numstat)

        if unstaged_paths:
            tracked_unstaged = sorted(
                item["path"]
                for item in changes
                if item.get("unstaged") not in (None, "untracked")
            )
            untracked = sorted(
                item["path"]
                for item in changes
                if item.get("unstaged") == "untracked"
            )
            combined: list[dict[str, Any]] = []
            if tracked_unstaged:
                entries = await self._git_diff_paths(
                    runtime, instance_id, repo_root, env,
                    ["git", "diff", "--no-color", "--no-ext-diff",
                     "--src-prefix=a/", "--dst-prefix=b/",
                     f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
                     "--patch", "--no-commit-id", "--", *tracked_unstaged],
                )
                numstat = await self._git_diff_numstat_raw(
                    runtime, instance_id, repo_root, env,
                    ["git", "diff", "--no-color", "--no-ext-diff",
                     "--numstat", "-z", "--", *tracked_unstaged],
                    ["git", "diff", "--no-color", "--no-ext-diff",
                     "--raw", "-z", "--", *tracked_unstaged],
                )
                combined.extend(self._git_join_diff_parts(entries, numstat))
            for rel in untracked:
                combined.append(
                    await self._git_untracked_entry(
                        runtime, instance_id, repo_root, env, rel
                    )
                )
            combined.sort(key=lambda item: item["new_path"])
            unstaged_entries = combined

        staged_entries.sort(key=lambda item: item["new_path"])
        return {"staged": staged_entries, "unstaged": unstaged_entries}

    async def _git_diff_paths(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        argv: list[str],
    ) -> dict[str, str]:
        """Run a patch diff argv and split per-file patches."""
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git diff failed",
                exit_code=exit_code, stderr=output,
            )
        return git_ops.split_patch_per_file(output)

    async def _git_diff_numstat_raw(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        numstat_argv: list[str],
        raw_argv: list[str],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Run numstat + raw diff argvs and parse them."""
        exit_code_n, numstat_out = await self._git_exec(
            runtime, instance_id, numstat_argv,
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        numstat: dict[str, dict[str, Any]] = {}
        if exit_code_n == 0:
            numstat = git_ops.parse_numstat_z(numstat_out)
        exit_code_r, raw_out = await self._git_exec(
            runtime, instance_id, raw_argv,
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        raw: dict[str, dict[str, Any]] = {}
        if exit_code_r == 0:
            raw = git_ops.parse_raw_z(raw_out)
        return numstat, raw

    def _git_join_diff_parts(
        self,
        patches: dict[str, str],
        numstat_raw: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Join patch/numstat/raw parts into file change dicts."""
        numstat, raw = numstat_raw
        return git_ops.build_file_changes(raw=raw, numstat=numstat, patches=patches)

    async def _git_untracked_entry(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        rel: str,
    ) -> dict[str, Any]:
        """Render an untracked path as an added-file diff entry."""
        rel_validated = git_ops.normalize_relative_path(rel)
        timeout = git_ops.GIT_READ_TIMEOUT_S
        exit_code, output = await self._git_exec(
            runtime, instance_id,
            ["git", "diff", "--no-index", "--no-color", "--no-ext-diff",
             "--src-prefix=a/", "--dst-prefix=b/",
             f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
             "--patch", "--", "/dev/null", rel_validated],
            workdir=repo_root, env=env, timeout=timeout,
        )
        # --no-index exits 1 when a diff exists; that is the happy path.
        if exit_code not in {0, 1}:
            # Fall back to a capped direct read when diff fails (e.g. the
            # path is a directory or unreadable).
            return await self._git_untracked_fallback(
                runtime, instance_id, repo_root, env, rel_validated
            )
        patches = git_ops.split_patch_per_file(output)
        # --no-index labels paths oddly; take the single patch if present.
        patch_text = next(iter(patches.values()), "")
        hunks, has_textual = git_ops.parse_patch_hunks(patch_text[: git_ops.GIT_MAX_DIFF_BYTES + 64])
        truncated = len(patch_text.encode("utf-8", "ignore")) > git_ops.GIT_MAX_DIFF_BYTES
        binary = bool(patch_text) and not has_textual and "Binary files " in patch_text
        return {
            "old_path": rel_validated,
            "new_path": rel_validated,
            "status": "A",
            "additions": sum(
                1 for hunk in hunks for line in hunk["lines"] if line["type"] == "add"
            ),
            "deletions": 0,
            "binary": binary,
            "truncated": truncated,
            "has_textual_diff": has_textual and not binary,
            "diff": hunks,
        }

    async def _git_untracked_fallback(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        rel: str,
    ) -> dict[str, Any]:
        """Render an unreadable/odd untracked path without patch text."""
        return {
            "old_path": rel,
            "new_path": rel,
            "status": "A",
            "additions": 0,
            "deletions": 0,
            "binary": False,
            "truncated": False,
            "has_textual_diff": False,
            "diff": [],
        }

    async def _git_commit_details(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        commit_hash: str,
    ) -> dict[str, Any]:
        """Build commit details with file changes, numstat and hunks."""
        timeout = git_ops.GIT_READ_TIMEOUT_S
        full_hash = git_ops.validate_commit_hash(commit_hash)
        us = "\x1f"
        rs = "\x1e"
        header_format = (
            f"%H{us}%P{us}%aN{us}%aE{us}%aI{us}%cN{us}%cE{us}%cI{us}%s{us}%b{rs}"
        )
        exit_code, header_out = await self._git_exec(
            runtime, instance_id,
            ["git", "show", "--no-patch", f"--format={header_format}", full_hash, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "unknown_commit", f"Unknown commit: {commit_hash}",
                exit_code=exit_code, stderr=header_out,
            )
        fields = header_out.split(rs)[0].split(us)
        if len(fields) < 10:
            raise git_ops.GitError(
                "unknown_commit", f"Unknown commit: {commit_hash}",
                exit_code=exit_code, stderr=header_out,
            )
        full, parents_raw, author, author_email, author_date = fields[0].strip(), fields[1], fields[2].strip(), fields[3].strip(), fields[4].strip()
        committer, committer_email, committer_date = fields[5].strip(), fields[6].strip(), fields[7].strip()
        subject, body = fields[8].strip(), fields[9].strip("\n")
        exit_code_r, parents_check = await self._git_exec(
            runtime, instance_id,
            ["git", "rev-list", "--parents", "-n", "1", full],
            workdir=repo_root, env=env, timeout=timeout,
        )
        parents = parents_raw.split() if parents_raw.strip() else []
        if exit_code_r == 0 and parents_check.strip():
            tokens = parents_check.strip().split()
            parents = tokens[1:] if len(tokens) > 1 else []

        is_root = len(parents) == 0
        if is_root:
            numstat_argv = ["git", "diff-tree", "--root", "--no-commit-id",
                            "--numstat", "-z", "-r", "-M", full, "--"]
            raw_argv = ["git", "diff-tree", "--root", "--no-commit-id",
                        "--raw", "-z", "-r", "-M", full, "--"]
            patch_argv = ["git", "show", "--no-color", "--no-ext-diff",
                          "--pretty=format:", "--patch",
                          f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
                          "--src-prefix=a/", "--dst-prefix=b/", "-M", full, "--"]
        else:
            numstat_argv = ["git", "diff-tree", "--no-commit-id",
                            "--numstat", "-z", "-r", "-M",
                            f"{parents[0]}", full, "--"]
            raw_argv = ["git", "diff-tree", "--no-commit-id",
                        "--raw", "-z", "-r", "-M",
                        f"{parents[0]}", full, "--"]
            patch_argv = ["git", "diff", "--no-color", "--no-ext-diff",
                          f"{parents[0]}", full, "--patch",
                          f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
                          "--src-prefix=a/", "--dst-prefix=b/", "-M", "--"]
        numstat, raw = await self._git_diff_numstat_raw(
            runtime, instance_id, repo_root, env, numstat_argv, raw_argv
        )
        exit_code_p, patch_out = await self._git_exec(
            runtime, instance_id, patch_argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        patches = git_ops.split_patch_per_file(patch_out) if exit_code_p == 0 else {}
        file_changes = git_ops.build_file_changes(raw=raw, numstat=numstat, patches=patches)
        return {
            "hash": full,
            "message": subject,
            "body": body,
            "author": author,
            "author_email": author_email,
            "author_date": author_date,
            "committer": committer,
            "committer_email": committer_email,
            "committer_date": committer_date,
            "parents": parents,
            "file_changes": file_changes,
        }

    # -- git RPC entry point ---------------------------------------------------

    async def execute_git_operation(
        self,
        workspace_id: uuid.UUID,
        operation: str,
        repo_path: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one whitelisted git operation inside a workspace.

        Single RPC entry point for productive git integration.  All repo
        paths are resolved fail-closed under ``/workspace`` and verified
        as repository roots.  Mutations return a fresh ``snapshot`` so the
        backend/webapp can update atomically.

        Args:
            workspace_id: Target workspace.
            operation: Whitelisted operation name (see
                :data:`src.git.GIT_OPERATIONS`).
            repo_path: Absolute repo path under ``/workspace``.  Omitted
                for ``list_repos`` (discovers all repos).
            args: Operation-specific arguments (paths, branch names,
                messages, pagination cursors).

        Returns:
            JSON-serialisable dict with ``ok`` plus ``snapshot`` /
            ``repos`` / ``diff`` / ``details`` payloads, or a structured
            error (``ok=False``, ``code``, ``message``, ``stderr``).
        """
        params = dict(args or {})
        if operation not in git_ops.GIT_OPERATIONS:
            return {
                "ok": False,
                "code": "unknown_operation",
                "message": f"Unknown git operation: {operation}",
                "stderr": "",
                "exit_code": None,
            }
        try:
            assert self._get_cached is not None and self._get_runtime is not None
            info = self._get_cached(workspace_id)
            runtime = self._get_runtime(workspace_id)
            if not info.instance_id:
                raise RuntimeError("Workspace has no instance assigned")
        except (ValueError, RuntimeError) as exc:
            return git_ops.git_error_payload(exc)

        timeout = git_ops.git_timeout_for(operation)
        try:
            outcome = await asyncio.wait_for(
                self._execute_git_operation_inner(
                    workspace_id, info.instance_id, runtime,
                    operation, repo_path, params,
                ),
                timeout + 30.0,
            )
            if not isinstance(outcome, dict):
                return {
                    "ok": False, "code": "git_failed",
                    "message": "Git operation returned no result",
                    "stderr": "", "exit_code": None,
                }
            outcome.setdefault("ok", True)
            return outcome
        except asyncio.TimeoutError:
            return {
                "ok": False, "code": "timeout",
                "message": "Git operation timed out",
                "stderr": "", "exit_code": None,
            }
        except Exception as exc:  # noqa: BLE001 - contract is structured errors
            return git_ops.git_error_payload(exc)

    async def _execute_git_operation_inner(
        self,
        workspace_id: uuid.UUID,
        instance_id: str,
        runtime: RuntimeBackend,
        operation: str,
        repo_path: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the git operation body (locks + auth + dispatch).

        The Git RPC accepts no caller-provided env and no unknown
        operation-specific args: both are rejected fail-closed via
        :func:`src.git.check_git_args_allowed` before any askpass/repo
        work, so ``env``/``GIT_CONFIG_*``/``PATH``/token injection via a
        future REST/MCP caller is impossible.  Auth comes only from the
        secure base env plus the persistent workspace credential file
        (sourced inside the workspace by the exec wrapper) and the
        throwaway askpass script derived from it.
        """
        timeout = git_ops.git_timeout_for(operation)
        # Defense-in-depth strict allow-list (mirrors the backend map).
        # Runs before any askpass probe or repo command.
        git_ops.check_git_args_allowed(operation, params)
        # Pull contract: branch without remote is a strict reject before
        # any askpass/repo work (mirrors backend/MCP/REST validation).
        if operation == "pull" and params.get("branch") not in (None, ""):
            remote = params.get("remote")
            if remote is None or str(remote).strip() == "":
                raise ValueError("remote is required when branch is set for pull")
        async with git_ops.github_askpass_context(
            runtime, instance_id
        ) as askpass:
            env = git_ops.build_git_env(askpass_script=askpass)
            if operation == "list_repos":
                repos = await self._git_discover_repos(runtime, instance_id, env)
                entries: list[dict[str, Any]] = []
                for root in repos:
                    lock = await self._git_lock(workspace_id, root)
                    async with lock:
                        entries.append(
                            await self._git_list_entry(
                                runtime, instance_id, root, env
                            )
                        )
                return {"ok": True, "repos": entries}

            # All other operations require an explicit repository root.
            if not repo_path:
                raise ValueError(f"repo_path is required for operation {operation!r}")
            repo_root = await self._git_resolve_repo_root(
                runtime, instance_id, repo_path, env
            )
            if operation in ("repo_snapshot", "repo_history"):
                lock = await self._git_lock(workspace_id, repo_root)
                async with lock:
                    if operation == "repo_snapshot":
                        snapshot = await self._git_repo_snapshot_no_log(
                            runtime, instance_id, repo_root, env
                        )
                        history = await self._git_repo_history(
                            runtime, instance_id, repo_root, env,
                            limit=git_ops.GIT_HISTORY_PAGE_SIZE,
                            skip=0,
                        )
                        snapshot.update(history)
                        return {"ok": True, "snapshot": snapshot}
                    branch = git_ops.validate_optional_branch(
                        params.get("branch")
                    )
                    if branch is not None and not await self._git_verify_branch_exists(
                        runtime, instance_id, repo_root, env, branch
                    ):
                        raise git_ops.GitError(
                            "unknown_branch", f"Unknown branch: {branch}",
                            exit_code=None, stderr="",
                        )
                    try:
                        history_limit = int(
                            params.get(
                                "history_limit",
                                git_ops.GIT_HISTORY_DEFAULT_LIMIT,
                            )
                        )
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Invalid history_limit: {params.get('history_limit')!r}"
                        ) from exc
                    try:
                        history_skip = int(params.get("history_skip", 0))
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Invalid history_skip: {params.get('history_skip')!r}"
                        ) from exc
                    history = await self._git_repo_history(
                        runtime, instance_id, repo_root, env,
                        limit=history_limit, skip=history_skip,
                        branch=branch,
                    )
                    return {"ok": True, "repo_path": repo_root, **history}
            lock = await self._git_lock(workspace_id, repo_root)
            async with lock:
                handler = {
                    "working_diff": self._git_op_working_diff,
                    "commit_details": self._git_op_commit_details,
                    "stage": self._git_op_stage,
                    "unstage": self._git_op_unstage,
                    "discard": self._git_op_discard,
                    "commit": self._git_op_commit,
                    "fetch": self._git_op_fetch,
                    "pull": self._git_op_pull,
                    "push": self._git_op_push,
                    "sync": self._git_op_sync,
                    "checkout_branch": self._git_op_checkout_branch,
                    "checkout_commit": self._git_op_checkout_commit,
                    "checkout_remote_branch": self._git_op_checkout_remote_branch,
                    "create_branch": self._git_op_create_branch,
                    "rename_branch": self._git_op_rename_branch,
                    "delete_branch": self._git_op_delete_branch,
                    "merge_into_current": self._git_op_merge_into_current,
                    "merge_current_into": self._git_op_merge_current_into,
                    "merge_abort": self._git_op_merge_abort,
                }[operation]
                return await handler(
                    runtime, instance_id, repo_root, env, params, timeout,
                    workspace_id=workspace_id,
                )

    async def _git_fresh_snapshot(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Return a fresh single-repo snapshot after a mutation."""
        snapshot = await self._git_repo_snapshot_no_log(
            runtime, instance_id, repo_root, env
        )
        history = await self._git_repo_history(
            runtime, instance_id, repo_root, env,
            limit=git_ops.GIT_HISTORY_PAGE_SIZE, skip=0,
        )
        snapshot.update(history)
        return snapshot

    def _git_mutation_result(
        self, snapshot: dict[str, Any], **extra: Any
    ) -> dict[str, Any]:
        """Wrap a mutation outcome with its fresh snapshot."""
        return {"ok": True, "snapshot": snapshot, **extra}

    # -- read operations --------------------------------------------------------

    async def _git_op_working_diff(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Return staged vs unstaged working-tree diffs."""
        diff = await self._git_working_diff(runtime, instance_id, repo_root, env)
        return {"ok": True, "repo_path": repo_root, "diff": diff}

    async def _git_op_commit_details(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Return full details for one commit."""
        commit = params.get("commit") or ""
        if not commit:
            raise ValueError("commit is required for commit_details")
        details = await self._git_commit_details(
            runtime, instance_id, repo_root, env, str(commit)
        )
        return {"ok": True, "repo_path": repo_root, "details": details}

    # -- staging operations -----------------------------------------------------

    @staticmethod
    def _git_require_paths(params: dict[str, Any], operation: str) -> list[str]:
        """Extract and validate repo-relative paths from *params*."""
        raw = params.get("paths", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"paths is required for operation {operation!r}")
        paths = [git_ops.normalize_relative_path(str(item)) for item in raw]
        if len(paths) > 256:
            raise ValueError("Too many paths (max 256)")
        return paths

    async def _git_op_stage(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Stage paths via ``git add -- <paths>``."""
        paths = self._git_require_paths(params, "stage")
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "add", "--", *paths],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git add failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_unstage(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Unstage paths (unborn-safe via ``rm --cached`` fallback)."""
        paths = self._git_require_paths(params, "unstage")
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "restore", "--staged", "--", *paths],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0 and "could not resolve HEAD" in output:
            # Unborn HEAD: nothing to restore from; staged additions are
            # dropped from the index instead (matches VS Code behaviour).
            exit_code, output = await self._git_exec(
                runtime, instance_id, ["git", "rm", "--cached", "-r", "--", *paths],
                workdir=repo_root, env=env, timeout=timeout,
            )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git unstage failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_discard(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Discard worktree changes; untracked paths are cleaned exactly."""
        paths = self._git_require_paths(params, "discard")
        exit_code, status_out = await self._git_exec(
            runtime, instance_id,
            ["git", "status", "--porcelain=v2", "--branch",
             "--untracked-files=all", "-z"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git status failed",
                exit_code=exit_code, stderr=status_out,
            )
        changes, _ = git_ops.changes_from_status_v2(status_out)
        by_path = {item["path"]: item for item in changes}
        to_restore: list[str] = []
        to_clean: list[str] = []
        for rel in paths:
            state = by_path.get(rel)
            if state is None:
                continue
            if state.get("unstaged") == "untracked":
                to_clean.append(rel)
            else:
                to_restore.append(rel)
        if to_restore:
            exit_code, output = await self._git_exec(
                runtime, instance_id,
                ["git", "restore", "--worktree", "--", *to_restore],
                workdir=repo_root, env=env, timeout=timeout,
            )
            if exit_code != 0:
                raise git_ops.GitError(
                    "git_failed", "git discard failed",
                    exit_code=exit_code, stderr=output,
                )
        for rel in to_clean:
            exit_code, output = await self._git_exec(
                runtime, instance_id, ["git", "clean", "-f", "--", rel],
                workdir=repo_root, env=env, timeout=timeout,
            )
            if exit_code != 0:
                raise git_ops.GitError(
                    "git_failed", "git clean failed",
                    exit_code=exit_code, stderr=output,
                )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    # -- commit ------------------------------------------------------------------

    async def _git_op_commit(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Commit the index only; message passed as a single argv element.

        Git argv runs behind the fixed sourcing wrapper (persistent
        credentials only), and both runtimes preserve the argv boundary
        behind ``exec "$@"`` (Docker exec argv, SSH single-quote
        escaping), so ``-m <message>`` cannot inject flags or shell
        operators.  Plain ``message`` is the transport (argv-safe).
        """
        text = str(params.get("message", "") or "")
        if not text.strip():
            raise ValueError("message is required for commit")
        # Identity fallback per missing field only: an existing
        # user.name/user.email repo value is respected as-is; only the
        # missing side is supplied via ``-c`` from the backend fallback.
        extra: list[str] = []
        exit_code_n, _ = await self._git_exec(
            runtime, instance_id, ["git", "config", "--get", "user.name"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        exit_code_e, _ = await self._git_exec(
            runtime, instance_id, ["git", "config", "--get", "user.email"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_n != 0 or exit_code_e != 0:
            # Backend-supplied fallback identity; only actually used
            # fields are validated here before ``-c`` argv use (no
            # NUL/CR/LF, length-capped).
            if exit_code_n != 0:
                author_name = git_ops.validate_author_name(
                    str(params.get("author_name", "") or "opencuria"),
                )
                extra += ["-c", f"user.name={author_name}"]
            if exit_code_e != 0:
                author_email = git_ops.validate_author_email(
                    str(params.get("author_email", "") or "opencuria@localhost"),
                )
                extra += ["-c", f"user.email={author_email}"]
        commit_argv = ["git", *extra, "commit", "--quiet",
                       "--allow-empty-message", "-m", text]
        exit_code, output = await self._git_exec(
            runtime, instance_id, commit_argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "nothing to commit" in lowered or "no changes added" in lowered:
                raise git_ops.GitError(
                    "nothing_to_commit", "Nothing to commit",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git commit failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    # -- network operations --------------------------------------------------------

    def _git_remote_arg(self, params: dict[str, Any]) -> str | None:
        """Return the validated remote name, if any."""
        remote = params.get("remote")
        if remote is None or str(remote).strip() == "":
            return None
        name = str(remote).strip()
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
            raise ValueError(f"Invalid remote: {remote!r}")
        return name

    async def _git_op_fetch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Fetch with prune (non-interactive, PAT via askpass)."""
        argv = ["git", "fetch", "--prune"]
        remote = self._git_remote_arg(params)
        if remote:
            argv.append(remote)
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "network_failed", "git fetch failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_current_branch(
        self, runtime, instance_id, repo_root, env
    ) -> str | None:
        """Return the current branch name, or None when detached/unborn."""
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "branch", "--show-current"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            return None
        name = output.strip().splitlines()[0].strip() if output.strip() else ""
        return name or None

    async def _git_op_pull(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Pull following repo config (ff/merge), never interactive."""
        argv = ["git", "pull", "--no-edit"]
        remote = self._git_remote_arg(params)
        branch = params.get("branch")
        if branch not in (None, "") and remote is None:
            raise ValueError("remote is required when branch is set for pull")
        if remote:
            argv.append(remote)
            if branch:
                argv.append(git_ops.validate_branch_name(str(branch)))
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "no tracking information" in lowered or "no upstream" in lowered:
                raise git_ops.GitError(
                    "no_upstream", "No upstream configured for the current branch",
                    exit_code=exit_code, stderr=output,
                )
            if "conflict" in lowered or "automatic merge failed" in lowered:
                snapshot = await self._git_fresh_snapshot(
                    runtime, instance_id, repo_root, env
                )
                result = self._git_mutation_result(snapshot, repo_path=repo_root)
                result["conflict"] = True
                result["ok"] = False
                result["code"] = "conflict"
                result["message"] = "Merge conflict — resolve or run merge_abort"
                return result
            raise git_ops.GitError(
                "network_failed", "git pull failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_push(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Push following the branch upstream when one exists.

        Matches VS Code/Git semantics: with an upstream configured and
        no explicit remote (and ``set_upstream`` not true), run plain
        ``git push`` so branch push config/upstream wins.  An explicit
        remote is honoured as ``git push <remote>``.  Without upstream
        (or when ``set_upstream`` is true) set it via
        ``git push -u <explicit remote or origin> <current>``.
        """
        current = await self._git_current_branch(runtime, instance_id, repo_root, env)
        if current is None:
            raise git_ops.GitError(
                "detached_head", "Cannot push while HEAD is detached",
                exit_code=None, stderr="",
            )
        explicit_remote = self._git_remote_arg(params)
        exit_code_u, upstream_out = await self._git_exec(
            runtime, instance_id,
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        has_upstream = exit_code_u == 0 and bool(upstream_out.strip())
        set_upstream = bool(params.get("set_upstream"))
        if has_upstream and not set_upstream:
            if explicit_remote is not None:
                argv = ["git", "push", explicit_remote]
            else:
                argv = ["git", "push"]
        else:
            argv = ["git", "push", "-u", explicit_remote or "origin", current]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "network_failed", "git push failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_sync(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Sync = pull, then push when ahead of upstream."""
        pull_result = await self._git_op_pull(
            runtime, instance_id, repo_root, env, params, timeout
        )
        if pull_result.get("ok") is False:
            return pull_result
        snapshot = pull_result.get("snapshot", {})
        if int(snapshot.get("ahead", 0)) > 0:
            return await self._git_op_push(
                runtime, instance_id, repo_root, env, params, timeout
            )
        return pull_result

    # -- branch operations ---------------------------------------------------------

    async def _git_verify_branch_exists(
        self, runtime, instance_id, repo_root, env, branch: str
    ) -> bool:
        """Return True when local branch *branch* exists."""
        exit_code, _ = await self._git_exec(
            runtime, instance_id,
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        return exit_code == 0

    async def _git_verify_ref_exists(
        self, runtime, instance_id, repo_root, env, ref: str
    ) -> bool:
        """Return True when *ref* resolves (branch, tag or commit)."""
        exit_code, _ = await self._git_exec(
            runtime, instance_id,
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        return exit_code == 0

    async def _git_op_checkout_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Checkout a local branch (dirty-worktree errors surface)."""
        branch = git_ops.validate_branch_name(str(params.get("branch", "")))
        exit_code_c, _ = await self._git_exec(
            runtime, instance_id, ["git", "check-ref-format", "--branch", branch],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_c != 0:
            raise ValueError(f"Invalid branch: {branch!r}")
        if not await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, branch
        ):
            raise git_ops.GitError(
                "unknown_branch", f"Unknown branch: {branch}",
                exit_code=None, stderr="",
            )
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "checkout", branch, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "commit your changes or stash them" in lowered:
                raise git_ops.GitError(
                    "dirty_worktree",
                    "Working tree has uncommitted changes",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git checkout failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_checkout_commit(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Detach HEAD at *commit* (dirty-worktree errors surface)."""
        commit = git_ops.validate_commit_hash(str(params.get("commit", "")))
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "checkout", "--detach", commit, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "commit your changes or stash them" in lowered:
                raise git_ops.GitError(
                    "dirty_worktree",
                    "Working tree has uncommitted changes",
                    exit_code=exit_code, stderr=output,
                )
            if "unknown revision" in lowered or "bad revision" in lowered:
                raise git_ops.GitError(
                    "unknown_commit", f"Unknown commit: {commit}",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git checkout failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_checkout_remote_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Track a remote branch locally (fetch + checkout/create)."""
        remote_ref = git_ops.validate_remote_ref(str(params.get("remote_ref", "")))
        # Default local branch: strip only the remote prefix
        # ("origin/feat/x" -> "feat/x", like `git switch <remote_ref>`).
        _remote, _, _short = remote_ref.partition("/")
        local_raw = params.get("local_name")
        local = (
            git_ops.validate_local_branch(str(local_raw))
            if local_raw not in (None, "")
            else git_ops.validate_local_branch(_short)
        )
        exit_code_f, fetch_out = await self._git_exec(
            runtime, instance_id, ["git", "fetch", _remote],
            workdir=repo_root, env=env, timeout=git_ops.GIT_NETWORK_TIMEOUT_S,
        )
        if exit_code_f != 0:
            raise git_ops.GitError(
                "network_failed", "git fetch failed",
                exit_code=exit_code_f, stderr=fetch_out,
            )
        exit_code_r, _ = await self._git_exec(
            runtime, instance_id,
            ["git", "show-ref", "--verify", "--quiet",
             f"refs/remotes/{remote_ref}"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_r != 0:
            raise git_ops.GitError(
                "unknown_branch", f"Unknown remote branch: {remote_ref}",
                exit_code=exit_code_r, stderr="",
            )
        if await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, local
        ):
            argv = ["git", "checkout", local, "--"]
        else:
            argv = ["git", "checkout", "-b", local, "--track", remote_ref, "--"]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if (
                "commit your changes or stash them" in lowered
                or "overwritten by checkout" in lowered
            ):
                raise git_ops.GitError(
                    "dirty_worktree",
                    "Working tree has uncommitted changes",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git checkout failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_create_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Create a branch; optionally check it out (``checkout=True``)."""
        branch = git_ops.validate_branch_name(str(params.get("branch", "")))
        exit_code_c, _ = await self._git_exec(
            runtime, instance_id, ["git", "check-ref-format", "--branch", branch],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_c != 0:
            raise ValueError(f"Invalid branch: {branch!r}")
        if await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, branch
        ):
            raise git_ops.GitError(
                "branch_exists", f"Branch already exists: {branch}",
                exit_code=None, stderr="",
            )
        start = params.get("start_point")
        argv = ["git", "branch", "--", branch]
        if start:
            start_ref = str(start).strip()
            # Accept hashes or validated branch names as start points.
            try:
                start_ref = git_ops.validate_commit_hash(start_ref)
            except ValueError:
                start_ref = git_ops.validate_branch_name(start_ref, field="start_point")
            if not await self._git_verify_ref_exists(
                runtime, instance_id, repo_root, env, start_ref
            ):
                raise git_ops.GitError(
                    "unknown_commit", f"Unknown start point: {start}",
                    exit_code=None, stderr="",
                )
            argv.append(start_ref)
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git branch failed",
                exit_code=exit_code, stderr=output,
            )
        if params.get("checkout"):
            exit_code_o, output_o = await self._git_exec(
                runtime, instance_id, ["git", "checkout", branch, "--"],
                workdir=repo_root, env=env, timeout=timeout,
            )
            if exit_code_o != 0:
                raise git_ops.GitError(
                    "git_failed", "git checkout failed",
                    exit_code=exit_code_o, stderr=output_o,
                )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_rename_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Rename a branch (defaults to the current branch)."""
        old = str(params.get("old_branch") or "").strip()
        new = git_ops.validate_branch_name(str(params.get("new_branch", "")), field="new_branch")
        exit_code_c, _ = await self._git_exec(
            runtime, instance_id, ["git", "check-ref-format", "--branch", new],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_c != 0:
            raise ValueError(f"Invalid branch: {new!r}")
        if await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, new
        ):
            raise git_ops.GitError(
                "branch_exists", f"Branch already exists: {new}",
                exit_code=None, stderr="",
            )
        if old:
            old_validated = git_ops.validate_branch_name(old, field="old_branch")
            if not await self._git_verify_branch_exists(
                runtime, instance_id, repo_root, env, old_validated
            ):
                raise git_ops.GitError(
                    "unknown_branch", f"Unknown branch: {old_validated}",
                    exit_code=None, stderr="",
                )
            argv = ["git", "branch", "-m", "--", old_validated, new]
        else:
            current = await self._git_current_branch(runtime, instance_id, repo_root, env)
            if current is None:
                raise git_ops.GitError(
                    "detached_head", "Cannot rename while HEAD is detached",
                    exit_code=None, stderr="",
                )
            argv = ["git", "branch", "-m", "--", new]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git branch rename failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_delete_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Safe-delete a branch (``-d`` only; current branch protected)."""
        branch = git_ops.validate_branch_name(str(params.get("branch", "")))
        current = await self._git_current_branch(runtime, instance_id, repo_root, env)
        if current is not None and current == branch:
            raise git_ops.GitError(
                "current_branch", "Cannot delete the current branch",
                exit_code=None, stderr="",
            )
        if not await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, branch
        ):
            raise git_ops.GitError(
                "unknown_branch", f"Unknown branch: {branch}",
                exit_code=None, stderr="",
            )
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "branch", "-d", "--", branch],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "not fully merged" in lowered:
                raise git_ops.GitError(
                    "not_merged", f"Branch is not fully merged: {branch}",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git branch delete failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    # -- merge operations ----------------------------------------------------------

    def _git_merge_msg(self, params: dict[str, Any], default: str) -> list[str]:
        """Return ``-m <message>`` argv for merges (single argv element)."""
        message = params.get("message")
        if message is None or str(message).strip() == "":
            return []
        text = str(message)
        if len(text) > 4096:
            raise ValueError("merge message too long (max 4096 chars)")
        return ["-m", text]

    async def _git_op_merge_into_current(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Merge *branch* into the current branch (fast-forward allowed)."""
        branch = git_ops.validate_branch_name(str(params.get("branch", "")))
        if not await self._git_verify_ref_exists(
            runtime, instance_id, repo_root, env, branch
        ):
            raise git_ops.GitError(
                "unknown_branch", f"Unknown branch: {branch}",
                exit_code=None, stderr="",
            )
        argv = ["git", "merge", "--no-edit", *self._git_merge_msg(params, branch),
                "--", branch]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        if exit_code != 0:
            lowered = output.lower()
            if "conflict" in lowered or "automatic merge failed" in lowered:
                result = self._git_mutation_result(snapshot, repo_path=repo_root)
                result["conflict"] = True
                result["ok"] = False
                result["code"] = "conflict"
                result["message"] = "Merge conflict — resolve or run merge_abort"
                result["stderr"] = git_ops._redact(output)
                return result
            raise git_ops.GitError(
                "git_failed", "git merge failed",
                exit_code=exit_code, stderr=output,
            )
        result = self._git_mutation_result(snapshot, repo_path=repo_root)
        result["conflict"] = False
        return result

    async def _git_op_merge_current_into(
        self, runtime, instance_id, repo_root, env, params, timeout,
        workspace_id: uuid.UUID | None = None, **_: Any
    ) -> dict[str, Any]:
        """Merge the current branch into *target* and return to the start branch.

        The target branch is checked out temporarily and the original
        (source) branch is merged into it.  On success the runner checks
        back out to the original branch.  On conflict it *stays* on the
        target so the user can resolve/abort there — the result payload
        reports ``stayed_on_target=True`` and the conflict flag.  On a
        non-conflict merge failure the runner makes a best-effort return
        to the source branch (conflict state, if any, stays for manual
        resolution).
        """
        target = git_ops.validate_branch_name(str(params.get("target", "")), field="target")
        if not await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, target
        ):
            raise git_ops.GitError(
                "unknown_branch", f"Unknown branch: {target}",
                exit_code=None, stderr="",
            )
        source = await self._git_current_branch(runtime, instance_id, repo_root, env)
        if source is None:
            raise git_ops.GitError(
                "detached_head", "Cannot merge while HEAD is detached",
                exit_code=None, stderr="",
            )
        if source == target:
            raise ValueError("source and target branches must differ")
        exit_code_o, output_o = await self._git_exec(
            runtime, instance_id, ["git", "checkout", target, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code_o != 0:
            lowered = output_o.lower()
            if "commit your changes or stash them" in lowered:
                raise git_ops.GitError(
                    "dirty_worktree",
                    "Working tree has uncommitted changes",
                    exit_code=exit_code_o, stderr=output_o,
                )
            raise git_ops.GitError(
                "git_failed", "git checkout failed",
                exit_code=exit_code_o, stderr=output_o,
            )
        argv = ["git", "merge", "--no-edit", *self._git_merge_msg(params, source),
                "--", source]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            snapshot = await self._git_fresh_snapshot(
                runtime, instance_id, repo_root, env
            )
            if "conflict" in lowered or "automatic merge failed" in lowered:
                result = self._git_mutation_result(snapshot, repo_path=repo_root)
                result["conflict"] = True
                result["stayed_on_target"] = True
                result["target"] = target
                result["source"] = source
                result["ok"] = False
                result["code"] = "conflict"
                result["message"] = (
                    f"Merge conflict on {target} — resolve or run merge_abort"
                )
                result["stderr"] = git_ops._redact(output)
                return result
            # Non-conflict failure: best-effort return to the source branch.
            try:
                await self._git_exec(
                    runtime, instance_id, ["git", "checkout", source, "--"],
                    workdir=repo_root, env=env, timeout=timeout,
                )
            except Exception:  # noqa: BLE001 - best effort return
                pass
            raise git_ops.GitError(
                "git_failed", "git merge failed",
                exit_code=exit_code, stderr=output,
            )
        # Success: return to the original branch.
        exit_code_b, output_b = await self._git_exec(
            runtime, instance_id, ["git", "checkout", source, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code_b != 0:
            raise git_ops.GitError(
                "git_failed",
                f"Merged into {target} but could not return to {source}",
                exit_code=exit_code_b, stderr=output_b,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        result = self._git_mutation_result(snapshot, repo_path=repo_root)
        result["conflict"] = False
        result["stayed_on_target"] = False
        result["target"] = target
        result["source"] = source
        return result

    async def _git_op_merge_abort(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Abort an in-progress merge (no-op error when none active)."""
        state = await self._git_merge_state(runtime, instance_id, repo_root, env)
        if not state.get("merging"):
            raise git_ops.GitError(
                "no_merge", "No merge in progress",
                exit_code=None, stderr="",
            )
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "merge", "--abort"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git merge --abort failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

