"""Tests for runner git integration (service + websocket, fakes only).

Covers the runner-side contract of the productive git integration:
discovery/path-security, metadata sandbox (external gitdir / linked
worktree / symlink escapes reject with ``unsafe_repository`` and leak no
external path), status staged/unstaged/untracked/conflicts/rename,
list/snapshot/history split, diff/root/binary/untracked, all mutations
happy path, invalid branch/path, detached/unborn, auth redaction and
non-interactive env, operation serialisation, and the websocket
``git:operation`` handler (happy/error/cancel).

No real networks are used: a ``FakeGitRuntime`` emulates ``git`` argv
inside a workspace, backed by real temporary git repositories on the test
host where end-to-end behaviour matters (status/list/snapshot/history/diff/mutations).
The service talks to the runtime only via ``exec_command_wait`` argv, so
the fake translates argv into local ``git -C <repo>`` subprocess calls.
External-path tests use a sibling temp dir (``tmp/ext-*``) that the fake
maps *outside* the ``/workspace`` namespace, so "no leak" assertions are
deterministic and never overfit the host ``/tmp`` layout.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from src import git as git_ops
from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.models import WorkspaceInfo
from src.service import WorkspaceService


# ---------------------------------------------------------------------------
# Host git-environment isolation (module setup/teardown).
# ---------------------------------------------------------------------------
#
# Every git subprocess below (``_make_repo``/``_git_config_identity``/
# ``_commit_all``/``FakeGitRuntime._run_git``) inherits ``os.environ``.
# A leaked host ``GIT_*`` variable (``GIT_DIR``/``GIT_WORK_TREE``/
# ``GIT_INDEX_FILE``/``GIT_AUTHOR_*``/``GIT_COMMITTER_*``/``GIT_CONFIG_*``)
# or an ambient global gitconfig (``HOME``/``XDG_CONFIG_HOME``) silently
# redirects those scratch repos: ``GIT_INDEX_FILE=.git/index`` breaks
# worktree/submodule setup with ``fatal: .git/index: index file open
# failed: Not a directory``, and ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` (or a
# global ``user.name``) overrides the commit-identity expectations.  The
# full suite (or a ``bash -lc`` pre-commit hook) can export such variables,
# while the single file run stays green — classic test pollution.
# Scrub them here for the whole module and restore afterwards.


_GIT_ENV_SNAPSHOT: dict[str, str] | None = None
_GIT_EMPTY_HOME: str | None = None


def setup_module(module: object | None = None) -> None:
    """Snapshot and sanitize host git env for this module (pytest entry)."""
    global _GIT_ENV_SNAPSHOT, _GIT_EMPTY_HOME
    if _GIT_ENV_SNAPSHOT is not None:
        return
    _GIT_ENV_SNAPSHOT = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("GIT_"):
            del os.environ[key]
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
    os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    _GIT_EMPTY_HOME = tempfile.mkdtemp(prefix="oc-git-nohome-")
    os.environ["HOME"] = _GIT_EMPTY_HOME
    os.environ["XDG_CONFIG_HOME"] = _GIT_EMPTY_HOME


def teardown_module(module: object | None = None) -> None:
    """Restore the host git env snapshot (pytest entry)."""
    global _GIT_ENV_SNAPSHOT, _GIT_EMPTY_HOME
    if _GIT_ENV_SNAPSHOT is None:
        return
    os.environ.clear()
    os.environ.update(_GIT_ENV_SNAPSHOT)
    _GIT_ENV_SNAPSHOT = None
    if _GIT_EMPTY_HOME is not None:
        shutil.rmtree(_GIT_EMPTY_HOME, ignore_errors=True)
        _GIT_EMPTY_HOME = None


# unittest-runner aliases (same functions, guarded against double-run).
setUpModule = setup_module
tearDownModule = teardown_module


# ---------------------------------------------------------------------------
# Fake runtime: translates workspace argv into local git subprocess calls.
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: str) -> tuple[int, str]:
    """Run real git locally for *args* in *cwd*; return (exit, output)."""
    proc = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class FakeGitRuntime:
    """Emulate workspace git via local subprocesses (no network)."""

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root
        self.calls: list[tuple[list[str], str | None, dict | None]] = []
        self.delay: dict[str, float] = {}
        self.entered = 0
        self.max_entered = 0
        self.hold_release: asyncio.Event | None = None

    def _resolve_cwd(self, workdir: str | None) -> str:
        if not workdir:
            return self.workspace_root
        # Map /workspace/... to the temp dir.
        if workdir == "/workspace":
            return self.workspace_root
        if workdir.startswith("/workspace/"):
            candidate = os.path.join(self.workspace_root, workdir[len("/workspace/") :])
            return candidate
        return workdir

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        argv = [str(c) for c in command]
        # The service wraps git argv in a fixed sourcing wrapper; unwrap
        # for local emulation (env already arrives as the mapping arg).
        inner = git_ops.unwrap_git_exec_argv(argv)
        probe_argv = inner if inner is not None else argv
        self.calls.append((argv, workdir, dict(env or {})))
        cwd = self._resolve_cwd(workdir)

        # Optional serialisation probe: hold inside one call.
        if self.hold_release is not None and probe_argv[:2] == ["git", "status"]:
            self.entered += 1
            self.max_entered = max(self.max_entered, self.entered)
            try:
                await asyncio.wait_for(self.hold_release.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            self.entered -= 1

        for prefix, delay in self.delay.items():
            if " ".join(probe_argv).startswith(prefix):
                await asyncio.sleep(delay)

        if probe_argv[:2] == ["realpath", "-m"]:
            # Emulate realpath -m: lexically for missing paths, resolving
            # on-disk symlinks like the real binary (needed for .git
            # symlink/external-gitdir tests).
            target = probe_argv[2] if len(probe_argv) > 2 else ""
            if target.startswith("/workspace/") or target == "/workspace":
                rel_in = "" if target == "/workspace" else target[len("/workspace/") :]
                mapped = os.path.join(self.workspace_root, rel_in)
                try:
                    resolved_host = os.path.realpath(mapped)
                except Exception:
                    resolved_host = os.path.normpath(mapped)
                # Map back to /workspace namespace when inside the root.
                try:
                    rel = os.path.relpath(resolved_host, self.workspace_root)
                except ValueError:
                    return 0, resolved_host + "\n"
                if rel.startswith(".."):
                    return 0, resolved_host + "\n"
                return 0, "/workspace" if rel == "." else f"/workspace/{rel}" + "\n"
            try:
                resolved_ext = os.path.realpath(target)
            except Exception:
                resolved_ext = os.path.normpath(target)
            # Map host-rooted results back into /workspace namespace so
            # containment checks see container paths.
            if resolved_ext == self.workspace_root or resolved_ext.startswith(
                self.workspace_root + os.sep
            ):
                try:
                    rel2 = os.path.relpath(resolved_ext, self.workspace_root)
                except ValueError:
                    return 0, resolved_ext + "\n"
                return (
                    0,
                    "/workspace" if rel2 == "." else f"/workspace/{rel2}" + "\n",
                )
            return 0, resolved_ext + "\n"
        if probe_argv == ["test", "-s", "/root/.opencuria-env.sh"] or (
            len(probe_argv) == 3
            and probe_argv[0] == "sh"
            and "opencuria-env" in probe_argv[2]
            and "GITHUB_TOKEN" in probe_argv[2]
        ):
            return 1, ""
        if probe_argv[:3] == ["sh", "-c", "test -s /root/.opencuria-env.sh"]:
            return 1, ""
        if (
            probe_argv[:2] == ["sh", "-lc"]
            and "mktemp" in probe_argv[2]
            and "askpass" in probe_argv[2]
        ):
            return 1, ""
        if (
            probe_argv[:2] == ["sh", "-c"]
            and "mktemp" in probe_argv[2]
            and "askpass" in probe_argv[2]
        ):
            return 1, ""
        if probe_argv[:1] == ["test"]:
            # test -e <gitdir>/<flag> and test -f/-s probes: resolve to host.
            path = probe_argv[-1]
            if path.startswith("/workspace/"):
                host = os.path.join(self.workspace_root, path[len("/workspace/") :])
            elif path.startswith("/tmp/") or path.startswith("/root/"):
                return 1, ""
            else:
                host = os.path.join(cwd, path)
            return (0, "") if os.path.exists(host) else (1, "")
        if probe_argv[:1] == ["rm"]:
            return 0, ""
        if probe_argv[:1] == ["find"]:
            # Emulate: find /workspace -maxdepth 4 -name .git -prune -print0.
            # Real find matches FILES as well as dirs (worktrees/submodules
            # use a ".git" FILE), and -maxdepth applies to the MATCH path:
            # `-name .git` hits live at depth+1, so a ".git" directly in
            # /workspace is depth 1 and a repo at depth 4 yields
            # ".../d/.git" at depth 5 — which real `find -maxdepth 4`
            # does NOT report.  The fake mirrors that: only candidates
            # whose own depth is <= maxdepth are reported.
            maxdepth = 4
            try:
                if "-maxdepth" in probe_argv:
                    maxdepth = int(
                        probe_argv[probe_argv.index("-maxdepth") + 1]
                    )
            except (ValueError, IndexError):
                pass
            roots: list[str] = []
            for dirpath, dirnames, filenames in os.walk(
                self.workspace_root, followlinks=False
            ):
                rel = os.path.relpath(dirpath, self.workspace_root)
                depth = 0 if rel == "." else len(rel.split(os.sep))
                if depth > maxdepth:
                    dirnames[:] = []
                    continue
                ws_path = "/workspace" if rel == "." else f"/workspace/{rel}"
                # .git as a real dir (classic repo) ...
                if ".git" in dirnames:
                    hit = ws_path + "/.git"
                    if hit.count("/") - 1 <= maxdepth:
                        roots.append(hit)
                    dirnames.remove(".git")
                # ... or as a FILE (linked worktree .git, submodule
                # gitfile, separate-git-dir pointer) or symlink.
                gitfile_host = os.path.join(dirpath, ".git")
                if os.path.isfile(gitfile_host) or os.path.islink(
                    gitfile_host
                ):
                    hit = ws_path + "/.git"
                    if hit.count("/") - 1 <= maxdepth and hit not in roots:
                        roots.append(hit)
                if depth >= maxdepth:
                    dirnames[:] = []
            return 0, "\x00".join(roots) + ("\x00" if roots else "")
        if probe_argv[:1] == ["git"]:
            os.makedirs(cwd, exist_ok=True)
            code, output = await asyncio.to_thread(_run_git, probe_argv, cwd)
            # Map host temp paths back into the /workspace namespace so
            # rev-parse --show-toplevel/--absolute-git-dir/--git-common-dir
            # comparisons in the service (which expects container paths)
            # succeed.
            if any(
                flag in probe_argv
                for flag in (
                    "--show-toplevel",
                    "--absolute-git-dir",
                    "--git-common-dir",
                    "--git-dir",
                )
            ):
                output = output.replace(self.workspace_root, "/workspace")
            return code, output
        if probe_argv[:2] == ["sh", "-lc"]:
            return 1, ""
        if probe_argv[:2] == ["sh", "-c"]:
            return 1, ""
        return 1, f"unsupported: {probe_argv!r}"


def _make_repo(path: str, *, branch: str = "main") -> str:
    os.makedirs(path, exist_ok=True)
    proc = subprocess.run(
        ["git", "init", "-b", branch, path], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    subprocess.run(
        ["git", "-C", path, "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", path, "config", "user.name", "Tester"],
        check=True,
        capture_output=True,
    )
    (Path(path) / "README.md").write_text("# test\n")
    subprocess.run(["git", "-C", path, "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", path, "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return path


def _service_for(root: str) -> tuple[WorkspaceService, FakeGitRuntime, uuid.UUID]:
    runtime = FakeGitRuntime(root)
    service = WorkspaceService(runtimes={"docker": runtime}, settings=RunnerSettings())
    workspace_id = uuid.uuid4()
    service._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    return service, runtime, workspace_id


def _ws_repo(root: str, name: str) -> str:
    """Workspace-namespaced repo path (``/workspace/<name>``)."""
    return f"/workspace/{name}"


def _git_config_identity(repo: str) -> None:
    """Set a local commit identity for a scratch repo (no global state)."""
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "Tester"],
        check=True,
        capture_output=True,
    )


def _commit_all(repo: str, message: str) -> None:
    """Commit staged state in a scratch repo (helper for security tests)."""
    subprocess.run(
        ["git", "-C", repo, "commit", "-m", message],
        check=True,
        capture_output=True,
    )


def _no_external_leak(payload: object, *external_markers: str) -> bool:
    """Return True when no external marker leaks into *payload*.

    Markers are the host-side external gitdir paths (sibling temp dirs
    outside the workspace mapping).  The service must echo only
    ``/workspace`` paths in errors.
    """
    blob = str(payload)
    return all(marker not in blob for marker in external_markers if marker)


def _inner_argv(call: tuple) -> list[str]:
    """Unwrap one recorded runtime call to its inner git argv (or argv)."""
    argv = call[0]
    return git_ops.unwrap_git_exec_argv(argv) or argv


def _had_data_command(
    runtime: FakeGitRuntime, *, after: int, verbs: tuple[str, ...] = ("status", "diff", "add")
) -> bool:
    """Return True when a status/diff/mutation argv ran after *after*."""
    for call in runtime.calls[after:]:
        inner = _inner_argv(call)
        if inner[:1] == ["git"] and len(inner) > 1 and inner[1] in verbs:
            return True
    return False


class _OldGitCommonDirRuntime(FakeGitRuntime):
    """Fake git<2.34: ``--path-format`` unknown, plain common-dir served.

    Fails ``--path-format=absolute --git-common-dir`` like git without
    ``--path-format`` support (exit 129) and answers the plain
    ``--git-common-dir`` fallback with the canned *plain_common* value.
    All other argv delegate to :class:`FakeGitRuntime` (real git).
    """

    def __init__(self, workspace_root: str, plain_common: str) -> None:
        super().__init__(workspace_root)
        self.plain_common = plain_common

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        argv = [str(c) for c in command]
        inner = git_ops.unwrap_git_exec_argv(argv) or argv
        if inner[:2] == ["git", "rev-parse"] and "--path-format=absolute" in inner:
            self.calls.append((argv, workdir, dict(env or {})))
            return 129, "error: unknown option `path-format'\n"
        if inner == ["git", "rev-parse", "--git-common-dir"]:
            self.calls.append((argv, workdir, dict(env or {})))
            return 0, self.plain_common
        return await super().exec_command_wait(
            instance_id, command, workdir, env
        )


class _ScriptedMetadataRuntime(FakeGitRuntime):
    """Fake with scripted rev-parse metadata outputs (pure escape tests).

    *git_dir_output* / *common_output* are served verbatim for the
    preferred probes (absolute common-dir), so hostile multiline/empty
    values can be exercised without touching the filesystem.  ``realpath``
    failures are injected via *realpath_fail_prefixes*: any
    ``realpath -m`` target starting with such a prefix returns exit 1.
    Everything else delegates to :class:`FakeGitRuntime`.
    """

    def __init__(
        self,
        workspace_root: str,
        *,
        git_dir_output: str | None = None,
        common_output: str | None = None,
        serve_plain_common: str | None = None,
        realpath_fail_prefixes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(workspace_root)
        self.git_dir_output = git_dir_output
        self.common_output = common_output
        self.serve_plain_common = serve_plain_common
        self.realpath_fail_prefixes = realpath_fail_prefixes

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        argv = [str(c) for c in command]
        inner = git_ops.unwrap_git_exec_argv(argv) or argv
        if inner == ["git", "rev-parse", "--absolute-git-dir"]:
            if self.git_dir_output is not None:
                self.calls.append((argv, workdir, dict(env or {})))
                return 0, self.git_dir_output
        if inner == [
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ]:
            if self.common_output is not None:
                self.calls.append((argv, workdir, dict(env or {})))
                return 0, self.common_output
            # Simulate old git when only a plain value is scripted.
            if self.serve_plain_common is not None:
                self.calls.append((argv, workdir, dict(env or {})))
                return 129, "error: unknown option `path-format'\n"
        if inner == ["git", "rev-parse", "--git-common-dir"]:
            if self.serve_plain_common is not None:
                self.calls.append((argv, workdir, dict(env or {})))
                return 0, self.serve_plain_common
        if inner[:2] == ["realpath", "-m"]:
            target = inner[2] if len(inner) > 2 else ""
            if target.startswith(self.realpath_fail_prefixes):
                self.calls.append((argv, workdir, dict(env or {})))
                return 1, ""
        return await super().exec_command_wait(
            instance_id, command, workdir, env
        )


# ---------------------------------------------------------------------------
# Pure parser/command unit tests.
# ---------------------------------------------------------------------------


class GitHelperUnitTests(unittest.TestCase):
    def test_normalize_repo_arg_rejects_escape(self) -> None:
        self.assertEqual(git_ops.normalize_repo_arg(None), "/workspace")
        self.assertEqual(git_ops.normalize_repo_arg("/workspace/a"), "/workspace/a")
        with self.assertRaises(ValueError):
            git_ops.normalize_repo_arg("/etc/passwd")
        with self.assertRaises(ValueError):
            git_ops.normalize_repo_arg("/workspace/../etc")
        with self.assertRaises(ValueError):
            git_ops.normalize_repo_arg("/workspace\x00evil")

    def test_normalize_relative_path(self) -> None:
        self.assertEqual(git_ops.normalize_relative_path("a/b.txt"), "a/b.txt")
        with self.assertRaises(ValueError):
            git_ops.normalize_relative_path("/abs/path")
        with self.assertRaises(ValueError):
            git_ops.normalize_relative_path("../escape")
        with self.assertRaises(ValueError):
            git_ops.normalize_relative_path("")

    def test_validate_branch_name(self) -> None:
        self.assertEqual(git_ops.validate_branch_name("feature/x"), "feature/x")
        for bad in ["-foo", "..", "a..b", "a b", "a~b", "a:b", "@{1}", "", "a//b"]:
            with self.assertRaises(ValueError, msg=bad):
                git_ops.validate_branch_name(bad)

    def test_validate_commit_hash(self) -> None:
        self.assertEqual(git_ops.validate_commit_hash("AbC123"), "abc123")
        with self.assertRaises(ValueError):
            git_ops.validate_commit_hash("xyz")
        with self.assertRaises(ValueError):
            git_ops.validate_commit_hash("")

    def test_build_git_env_non_interactive(self) -> None:
        env = git_ops.build_git_env()
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_PAGER"], "cat")
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])
        self.assertIn("accept-new", env["GIT_SSH_COMMAND"])
        env2 = git_ops.build_git_env(askpass_script="/tmp/x.sh")
        self.assertEqual(env2["GIT_ASKPASS"], "/tmp/x.sh")
        # SSH never gets an askpass pointer: BatchMode key auth only, the
        # PAT must never answer an SSH/host-key/passphrase prompt.
        self.assertNotIn("SSH_ASKPASS", env2)
        self.assertNotIn("SSH_ASKPASS_REQUIRE", env2)

    def test_build_git_env_rejects_external_env(self) -> None:
        # The Git RPC accepts no caller-provided env: injection of
        # GIT_CONFIG_*/PATH/tokens via args must be impossible, so the
        # builder takes no free env at all.
        with self.assertRaises(TypeError):
            git_ops.build_git_env({"GITHUB_TOKEN": "ghp_secret"})  # type: ignore[call-arg]

    def test_git_exec_wrapper_sources_persistent_env(self) -> None:
        from src.git import _GIT_EXEC_WRAPPER_SCRIPT  # noqa: PLC2701 - fixed script under test

        argv = git_ops.git_exec_wrapper_argv(["git", "status"])
        self.assertEqual(argv[:3], ["sh", "-c", _GIT_EXEC_WRAPPER_SCRIPT])
        self.assertEqual(argv[3], "opencuria-git-exec")
        self.assertEqual(argv[4:], ["git", "status"])
        # Conditional sourcing: POSIX sh aborts `.<missing>` with exit 2,
        # so the fixed script must guard with `[ -f ... ]`.
        self.assertIn("[ -f /root/.opencuria-env.sh ]", argv[2])
        # Real shell execution with a missing credential file: the wrapper
        # must still run the git/printf argv (exit 0).  POSIX `sh`
        # (dash) aborts an unguarded `. <missing>` with exit 2 inside
        # containers without credentials; use a guaranteed-missing path
        # (TMPDIR, never /root/...) so the test holds on any host.
        missing = os.path.join(tempfile.gettempdir(), "opencuria-no-such-env-xyz.sh")
        try:
            os.unlink(missing)
        except OSError:
            pass
        guarded = argv[2].replace("/root/.opencuria-env.sh", missing)
        self.assertIn(f"[ -f {missing} ]", guarded)
        probe = subprocess.run(
            [argv[0], argv[1], guarded, argv[3], "printf", "wrapper-ok:%s", "yes"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(probe.returncode, 0)
        self.assertIn("wrapper-ok:yes", probe.stdout)
        old_broken_script = f'. {missing} 2>/dev/null; exec "$@"'
        old = subprocess.run(
            [
                "dash",
                "-c",
                old_broken_script,
                argv[3],
                "printf",
                "wrapper-ok:%s",
                "yes",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # The legacy unguarded dot-form is the reported blocker: dash
        # aborts on the missing file with exit 2, never reaching exec.
        self.assertEqual(old.returncode, 2)
        self.assertNotIn("wrapper-ok:yes", old.stdout)
        # Real git argv behind the wrapper with no credential file.
        git_probe = subprocess.run(
            [*argv[:4], "git", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(git_probe.returncode, 0)
        self.assertIn("git version", git_probe.stdout + git_probe.stderr)
        # User input stays separate argv elements behind exec "$@".
        evil = git_ops.git_exec_wrapper_argv(
            ["git", "commit", "-m", "x'; rm -rf /; echo '"]
        )
        self.assertIn("x'; rm -rf /; echo '", evil)
        self.assertNotIn("rm -rf", evil[2])
        self.assertEqual(git_ops.unwrap_git_exec_argv(argv), ["git", "status"])
        self.assertIsNone(git_ops.unwrap_git_exec_argv(["git", "status"]))

    def test_git_allowed_args_map(self) -> None:
        # Runner allow-list mirrors the backend map (defense-in-depth at
        # the trust boundary); commit carries backend-injected identity.
        self.assertEqual(git_ops.GIT_ALLOWED_ARGS["list_repos"], frozenset())
        self.assertEqual(git_ops.GIT_ALLOWED_ARGS["repo_snapshot"], frozenset())
        self.assertEqual(
            git_ops.GIT_ALLOWED_ARGS["repo_history"],
            frozenset({"history_limit", "history_skip", "branch"}),
        )
        self.assertEqual(
            git_ops.GIT_ALLOWED_ARGS["checkout_remote_branch"],
            frozenset({"remote_ref", "local_name"}),
        )
        self.assertNotIn("snapshot", git_ops.GIT_ALLOWED_ARGS)
        self.assertEqual(git_ops.GIT_HISTORY_DEFAULT_LIMIT, 50)
        self.assertEqual(git_ops.GIT_HISTORY_PAGE_SIZE, 50)
        self.assertEqual(git_ops.GIT_ALLOWED_ARGS["working_diff"], frozenset())
        self.assertEqual(
            git_ops.GIT_ALLOWED_ARGS["commit"],
            frozenset({"message", "author_name", "author_email"}),
        )
        self.assertIn("message", git_ops.GIT_ALLOWED_ARGS["commit"])
        self.assertNotIn("message_b64", git_ops.GIT_ALLOWED_ARGS["commit"])
        self.assertNotIn("env", git_ops.GIT_ALLOWED_ARGS["commit"])
        with self.assertRaises(ValueError):
            git_ops.check_git_args_allowed("commit", {"message_b64": "eA=="})
        with self.assertRaises(ValueError):
            git_ops.check_git_args_allowed("repo_snapshot", {"env": {}})
        with self.assertRaises(ValueError):
            git_ops.check_git_args_allowed("working_diff", {"paths": ["a"]})
        # Valid keys pass.
        git_ops.check_git_args_allowed("repo_history", {"history_limit": 2})
        git_ops.check_git_args_allowed(
            "commit", {"message": "m", "author_name": "T", "author_email": "t@e.c"}
        )

    def test_validate_author_identity(self) -> None:
        name, email = git_ops.validate_author_identity(" T ", " t@example.com ")
        self.assertEqual((name, email), ("T", "t@example.com"))
        for bad_name, bad_email in [
            ("", "t@example.com"),
            ("T", ""),
            ("a\nb", "t@example.com"),
            ("T", "a\rb@c"),
            ("T\x00x", "t@example.com"),
            ("x" * 256, "t@example.com"),
            ("T", "x" * 321 + "@e.c"),
            ("T", "no-at-sign"),
        ]:
            with self.assertRaises(ValueError, msg=f"{bad_name!r}/{bad_email!r}"):
                git_ops.validate_author_identity(bad_name, bad_email)

    def test_askpass_script_body_strict(self) -> None:
        body = git_ops.GIT_ASKPASS_SCRIPT_BODY
        self.assertIn("x-access-token", body)
        self.assertIn("GITHUB_TOKEN", body)
        # Unknown prompts must not yield the token: nonzero exit, no echo.
        self.assertIn("exit 1", body)
        # Real shell behaviour: username/password answered, unknown denied.
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write("#!/bin/sh\n" + body + "\n")
            path = handle.name
        try:
            os.chmod(path, 0o700)
            user = subprocess.run(
                [path, "Username for 'https://github.com':"],
                capture_output=True,
                text=True,
                timeout=30,
                env={"GITHUB_TOKEN": "SECRET-TOKEN-123", "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(user.returncode, 0)
            self.assertIn("x-access-token", user.stdout)
            self.assertNotIn("SECRET-TOKEN-123", user.stdout)
            pw_env = {"GITHUB_TOKEN": "SECRET-TOKEN-123", "PATH": "/usr/bin:/bin"}
            pw = subprocess.run(
                [path, "Password for 'https://x-access-token@github.com':"],
                capture_output=True,
                text=True,
                timeout=30,
                env=pw_env,
            )
            self.assertEqual(pw.returncode, 0)
            self.assertIn("SECRET-TOKEN-123", pw.stdout)
            unknown = subprocess.run(
                [path, "Enter passphrase for key '/root/.ssh/id_ed25519':"],
                capture_output=True,
                text=True,
                timeout=30,
                env=pw_env,
            )
            self.assertNotEqual(unknown.returncode, 0)
            self.assertNotIn("SECRET-TOKEN-123", unknown.stdout)
            hostkey = subprocess.run(
                [path, "Are you sure you want to continue connecting (yes/no)?"],
                capture_output=True,
                text=True,
                timeout=30,
                env=pw_env,
            )
            self.assertNotEqual(hostkey.returncode, 0)
            self.assertNotIn("SECRET-TOKEN-123", hostkey.stdout)
        finally:
            os.unlink(path)

    def test_redaction(self) -> None:
        payload = git_ops.git_error_payload(
            git_ops.GitError(
                "network_failed",
                "fetch failed",
                exit_code=1,
                stderr="https://x-access-token:ghp_abc123@github.com/o/r.git denied",
            )
        )
        self.assertNotIn("ghp_abc123", payload["stderr"])
        self.assertIn("***", payload["stderr"])
        # Generic user:secret@ URLs are redacted even without PAT prefixes.
        generic = git_ops.git_error_payload(
            git_ops.GitError(
                "network_failed",
                "fetch failed",
                exit_code=1,
                stderr="https://deploy:s3cr3t-p@ssw0rd@git.example.com/o/r.git denied",
            )
        )
        self.assertNotIn("s3cr3t-p@ssw0rd", generic["stderr"])
        self.assertIn("https://***@git.example.com/o/r.git", generic["stderr"])

    def test_discovery_helpers(self) -> None:
        argv = git_ops.discovery_find_args()
        self.assertEqual(argv[:2], ["find", "/workspace"])
        self.assertIn("-prune", argv)
        self.assertIn("-print0", argv)
        out = "/workspace/a/.git\x00/workspace/b/.git\x00"
        self.assertEqual(
            git_ops.discovery_repo_roots(out),
            ["/workspace/a", "/workspace/b"],
        )
        evil = "/workspace/a/.git\x00/etc/x/.git\x00"
        self.assertEqual(git_ops.discovery_repo_roots(evil), ["/workspace/a"])

    def test_status_v2_parsing_staged_unstaged_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "r"))
            Path(repo, "staged.txt").write_text("s\n")
            subprocess.run(["git", "-C", repo, "add", "staged.txt"], check=True)
            Path(repo, "README.md").write_text("# modified\n")
            Path(repo, "untracked.txt").write_text("u\n")
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "status",
                    "--porcelain=v2",
                    "--branch",
                    "--untracked-files=all",
                    "-z",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            changes, branch = git_ops.changes_from_status_v2(out)
            by_path = {c["path"]: c for c in changes}
            self.assertEqual(by_path["staged.txt"]["staged"], "A")
            self.assertIsNone(by_path["staged.txt"]["unstaged"])
            self.assertEqual(by_path["README.md"]["unstaged"], "M")
            self.assertIsNone(by_path["README.md"]["staged"])
            self.assertEqual(by_path["untracked.txt"]["unstaged"], "untracked")
            self.assertEqual(branch["head"], "main")

    def test_status_v2_parsing_rename_and_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "r"))
            Path(repo, "old.txt").write_text("same\n")
            subprocess.run(["git", "-C", repo, "add", "old.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "add old"],
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "-C", repo, "mv", "old.txt", "new.txt"], check=True)
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "status",
                    "--porcelain=v2",
                    "--branch",
                    "--untracked-files=all",
                    "-z",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            changes, _ = git_ops.changes_from_status_v2(out)
            renamed = next(c for c in changes if c["path"] == "new.txt")
            self.assertEqual(renamed["staged"], "R")
            self.assertEqual(renamed["old_path"], "old.txt")

            # Conflict fixture: divergent branches merged.
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "rename done"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", "side"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# side\n")
            subprocess.run(
                ["git", "-C", repo, "commit", "-am", "side"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# main\n")
            subprocess.run(
                ["git", "-C", repo, "commit", "-am", "main2"],
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "-C", repo, "merge", "side"], capture_output=True)
            out2 = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "status",
                    "--porcelain=v2",
                    "--branch",
                    "--untracked-files=all",
                    "-z",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            changes2, _ = git_ops.changes_from_status_v2(out2)
            conflicted = next(c for c in changes2 if c["path"] == "README.md")
            self.assertEqual(conflicted["conflict"], "UU")

    def test_parse_patch_hunks_binary(self) -> None:
        hunks, has_text = git_ops.parse_patch_hunks(
            "diff --git a/b b/b\nBinary files a/b and b/b differ\n"
        )
        self.assertEqual(hunks, [])
        self.assertFalse(has_text)
        hunks2, has_text2 = git_ops.parse_patch_hunks("@@ -1 +1 @@\n-foo\n+bar\n")
        self.assertTrue(has_text2)
        self.assertEqual(hunks2[0]["lines"][0], {"type": "del", "content": "foo"})

    # --- diff parser fix: numstat -z exact format ---------------------------

    def test_numstat_two_empty_files_no_swallow(self) -> None:
        # Two normal ``0 0`` records must stay two files; the second
        # bare field is never a rename target.
        stats = git_ops.parse_numstat_z("0\t0\tempty.txt\x000\t0\tnext-bare\x00")
        self.assertEqual(
            stats,
            {
                "empty.txt": {
                    "additions": 0,
                    "deletions": 0,
                    "binary": False,
                    "old_path": None,
                },
                "next-bare": {
                    "additions": 0,
                    "deletions": 0,
                    "binary": False,
                    "old_path": None,
                },
            },
        )

    def test_numstat_two_empty_files_real_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# test\n")
            subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            Path(repo, "empty1.txt").write_text("")
            Path(repo, "empty2.txt").write_text("")
            subprocess.run(
                ["git", "-C", repo, "add", "empty1.txt", "empty2.txt"],
                check=True,
            )
            out = subprocess.run(
                ["git", "-C", repo, "diff", "--cached", "--numstat", "-z"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            stats = git_ops.parse_numstat_z(out)
            self.assertEqual(
                stats,
                {
                    "empty1.txt": {
                        "additions": 0,
                        "deletions": 0,
                        "binary": False,
                        "old_path": None,
                    },
                    "empty2.txt": {
                        "additions": 0,
                        "deletions": 0,
                        "binary": False,
                        "old_path": None,
                    },
                },
            )

    def test_numstat_tab_filename_real_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# test\n")
            subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            tabbed = os.path.join(repo, "a\tb.txt")
            Path(tabbed).write_text("v1\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            out = subprocess.run(
                ["git", "-C", repo, "diff", "--cached", "--numstat", "-z"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            stats = git_ops.parse_numstat_z(out)
            self.assertIn("a\tb.txt", stats)
            self.assertEqual(stats["a\tb.txt"]["additions"], 1)
            self.assertEqual(stats["a\tb.txt"]["deletions"], 0)
            self.assertFalse(stats["a\tb.txt"]["binary"])
            self.assertIsNone(stats["a\tb.txt"]["old_path"])

    def test_numstat_rename_copy_binary_real_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            Path(repo, "old.txt").write_text("same\nline2\nline3\nline4\n")
            Path(repo, "README.md").write_text("# test\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "-C", repo, "mv", "old.txt", "new.txt"], check=True)
            out = subprocess.run(
                ["git", "-C", repo, "diff", "--cached", "--numstat", "-z"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            stats = git_ops.parse_numstat_z(out)
            self.assertEqual(
                stats,
                {
                    "new.txt": {
                        "additions": 0,
                        "deletions": 0,
                        "binary": False,
                        "old_path": "old.txt",
                    },
                },
            )
            subprocess.run(
                ["git", "-C", repo, "reset", "-q", "HEAD"],
                check=True,
            )
            # Binary marker survives as zero counts + binary flag.
            Path(repo, "blob.bin").write_bytes(bytes(range(256)) * 64)
            subprocess.run(["git", "-C", repo, "add", "blob.bin"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "bin"],
                check=True,
                capture_output=True,
            )
            Path(repo, "blob.bin").write_bytes(bytes(reversed(range(256))) * 64)
            out2 = subprocess.run(
                ["git", "-C", repo, "diff", "--numstat", "-z"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            stats2 = git_ops.parse_numstat_z(out2)
            self.assertTrue(stats2["blob.bin"]["binary"])
            self.assertEqual(stats2["blob.bin"]["additions"], 0)
            self.assertEqual(stats2["blob.bin"]["deletions"], 0)

    def test_numstat_malformed_fail_safe(self) -> None:
        # Garbage never raises and never fabricates keys.
        self.assertEqual(git_ops.parse_numstat_z(""), {})
        self.assertEqual(git_ops.parse_numstat_z("bogus\x00"), {})
        self.assertEqual(
            git_ops.parse_numstat_z("bogus\x001\t0\tok.txt\x00"),
            {
                "ok.txt": {
                    "additions": 1,
                    "deletions": 0,
                    "binary": False,
                    "old_path": None,
                },
            },
        )
        # Non-numeric counts are dropped.
        self.assertEqual(git_ops.parse_numstat_z("x\ty\tbad.txt\x00"), {})
        # Truncated rename anchor (missing new path) is dropped entirely.
        self.assertEqual(git_ops.parse_numstat_z("1\t0\t\x00only-old\x00"), {})
        # Bare NUL field without tabs is skipped, not glued to a record.
        self.assertEqual(
            git_ops.parse_numstat_z("1\t1\ta.txt\x00lonely\x00"),
            {
                "a.txt": {
                    "additions": 1,
                    "deletions": 1,
                    "binary": False,
                    "old_path": None,
                },
            },
        )

    # --- diff parser fix: split_patch_per_file resolution -------------------

    def test_split_patch_spaces_filename_real_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            target = os.path.join(repo, "my file with spaces.txt")
            Path(target).write_text("hello\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "add"],
                check=True,
                capture_output=True,
            )
            Path(target).write_text("hello\nworld\n")
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "diff",
                    "--no-color",
                    "--no-ext-diff",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    "--unified=3",
                    "--patch",
                    "--no-commit-id",
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo,
            ).stdout
            patches = git_ops.split_patch_per_file(out)
            self.assertEqual(list(patches), ["my file with spaces.txt"])
            hunks, has_text = git_ops.parse_patch_hunks(
                patches["my file with spaces.txt"]
            )
            self.assertTrue(has_text)
            flat = [ln["content"] for h in hunks for ln in h["lines"]]
            self.assertIn("world", flat)

    def test_split_patch_rename_token_mismatch_with_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            Path(repo, "old.txt").write_text("same\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "add"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "mv", "old.txt", "new name with many words.txt"],
                check=True,
            )
            Path(repo, "new name with many words.txt").write_text("same\nmore\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "diff",
                    "--cached",
                    "--no-color",
                    "--no-ext-diff",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    "--unified=3",
                    "--patch",
                    "--no-commit-id",
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo,
            ).stdout
            patches = git_ops.split_patch_per_file(out)
            # No midpoint guessing: the hunk belongs to the exact new path.
            self.assertEqual(list(patches), ["new name with many words.txt"])
            self.assertIn("@@ ", patches["new name with many words.txt"])
            self.assertIn("+more", patches["new name with many words.txt"])
            numstat_out = subprocess.run(
                ["git", "-C", repo, "diff", "--cached", "--numstat", "-z"],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo,
            ).stdout
            raw_out = subprocess.run(
                ["git", "-C", repo, "diff", "--cached", "--raw", "-z"],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo,
            ).stdout
            raw = git_ops.parse_raw_z(raw_out)
            numstat = git_ops.parse_numstat_z(numstat_out)
            self.assertIn("new name with many words.txt", raw)
            self.assertIn("new name with many words.txt", numstat)
            changes = git_ops.build_file_changes(
                raw=raw, numstat=numstat, patches=patches
            )
            self.assertEqual(len(changes), 1)
            entry = changes[0]
            self.assertEqual(entry["new_path"], "new name with many words.txt")
            self.assertEqual(entry["old_path"], "old.txt")
            self.assertTrue(entry["diff"])

    def test_split_patch_deleted_space_path_real_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            target = os.path.join(repo, "a file with spaces.txt")
            Path(target).write_text("content\n")
            Path(repo, "README.md").write_text("# t\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            os.unlink(target)
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "diff",
                    "--no-color",
                    "--no-ext-diff",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    "--unified=3",
                    "--patch",
                    "--no-commit-id",
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo,
            ).stdout
            patches = git_ops.split_patch_per_file(out)
            self.assertEqual(list(patches), ["a file with spaces.txt"])
            self.assertIn("-content", patches["a file with spaces.txt"])
            raw = git_ops.parse_raw_z(
                subprocess.run(
                    ["git", "-C", repo, "diff", "--raw", "-z"],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=repo,
                ).stdout
            )
            numstat = git_ops.parse_numstat_z(
                subprocess.run(
                    ["git", "-C", repo, "diff", "--numstat", "-z"],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=repo,
                ).stdout
            )
            changes = git_ops.build_file_changes(
                raw=raw, numstat=numstat, patches=patches
            )
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["new_path"], "a file with spaces.txt")
            self.assertEqual(changes[0]["status"], "D")

    def test_split_patch_special_names_real_git(self) -> None:
        cases = [
            ('quo"te.txt', 'quo"te.txt'),
            ("back\\slash.txt", "back\\slash.txt"),
            ("ünïcode-ß.txt", "ünïcode-ß.txt"),
        ]
        for filename, expected in cases:
            with self.subTest(filename=filename):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = os.path.join(tmp, "r")
                    subprocess.run(
                        ["git", "init", "-b", "main", repo],
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "-C", repo, "config", "user.email", "t@example.com"],
                        check=True,
                        capture_output=True,
                    )
                    subprocess.run(
                        ["git", "-C", repo, "config", "user.name", "T"],
                        check=True,
                        capture_output=True,
                    )
                    Path(os.path.join(repo, filename)).write_text("v1\n")
                    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
                    subprocess.run(
                        ["git", "-C", repo, "commit", "-m", "add"],
                        check=True,
                        capture_output=True,
                    )
                    Path(os.path.join(repo, filename)).write_text("v2\nmore\n")
                    out = subprocess.run(
                        [
                            "git",
                            "-C",
                            repo,
                            "diff",
                            "--no-color",
                            "--no-ext-diff",
                            "--src-prefix=a/",
                            "--dst-prefix=b/",
                            "--unified=3",
                            "--patch",
                            "--no-commit-id",
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                        cwd=repo,
                    ).stdout
                    patches = git_ops.split_patch_per_file(out)
                    self.assertEqual(list(patches), [expected])
                    self.assertIn("+more", patches[expected])

    def test_split_patch_newline_filename_real_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# test\n")
            subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            newline_name = "line1\nline2.txt"
            Path(os.path.join(repo, newline_name)).write_text("v1\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "diff",
                    "--cached",
                    "--no-color",
                    "--no-ext-diff",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    "--unified=3",
                    "--patch",
                    "--no-commit-id",
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo,
            ).stdout
            patches = git_ops.split_patch_per_file(out)
            self.assertEqual(list(patches), [newline_name])
            numstat = git_ops.parse_numstat_z(
                subprocess.run(
                    ["git", "-C", repo, "diff", "--cached", "--numstat", "-z"],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=repo,
                ).stdout
            )
            self.assertIn(newline_name, numstat)

    def test_split_patch_pure_rename_and_binary_real_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            Path(repo, "old short").write_text("same content here\nline2\nline3\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "add"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "mv", "old short", "new name with many words"],
                check=True,
            )
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "diff",
                    "--cached",
                    "--no-color",
                    "--no-ext-diff",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    "--patch",
                    "--no-commit-id",
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo,
            ).stdout
            patches = git_ops.split_patch_per_file(out)
            self.assertEqual(list(patches), ["new name with many words"])
            raw = git_ops.parse_raw_z(
                subprocess.run(
                    ["git", "-C", repo, "diff", "--cached", "--raw", "-z"],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=repo,
                ).stdout
            )
            numstat = git_ops.parse_numstat_z(
                subprocess.run(
                    ["git", "-C", repo, "diff", "--cached", "--numstat", "-z"],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=repo,
                ).stdout
            )
            changes = git_ops.build_file_changes(
                raw=raw, numstat=numstat, patches=patches
            )
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["new_path"], "new name with many words")
            self.assertEqual(changes[0]["old_path"], "old short")
            self.assertEqual(changes[0]["status"], "R")
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            Path(repo, "blob.bin").write_bytes(bytes(range(256)) * 10)
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "bin"],
                check=True,
                capture_output=True,
            )
            Path(repo, "blob.bin").write_bytes(bytes(reversed(range(256))) * 10)
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "diff",
                    "--no-color",
                    "--no-ext-diff",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    "--patch",
                    "--no-commit-id",
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo,
            ).stdout
            patches = git_ops.split_patch_per_file(out)
            self.assertEqual(list(patches), ["blob.bin"])
            numstat = git_ops.parse_numstat_z(
                subprocess.run(
                    ["git", "-C", repo, "diff", "--numstat", "-z"],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=repo,
                ).stdout
            )
            raw = git_ops.parse_raw_z(
                subprocess.run(
                    ["git", "-C", repo, "diff", "--raw", "-z"],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=repo,
                ).stdout
            )
            changes = git_ops.build_file_changes(
                raw=raw, numstat=numstat, patches=patches
            )
            self.assertEqual(len(changes), 1)
            self.assertTrue(changes[0]["binary"])
            self.assertFalse(changes[0]["has_textual_diff"])

    def test_split_patch_multi_file_no_cross_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            subprocess.run(
                ["git", "init", "-b", "main", repo],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "T"],
                check=True,
                capture_output=True,
            )
            Path(repo, "a file.txt").write_text("aaa\n")
            Path(repo, "b file.txt").write_text("bbb\n")
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "add"],
                check=True,
                capture_output=True,
            )
            Path(repo, "a file.txt").write_text("aaa\nAAA\n")
            Path(repo, "b file.txt").write_text("bbb\nBBB\n")
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "diff",
                    "--no-color",
                    "--no-ext-diff",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    "--unified=3",
                    "--patch",
                    "--no-commit-id",
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo,
            ).stdout
            patches = git_ops.split_patch_per_file(out)
            self.assertEqual(sorted(patches), ["a file.txt", "b file.txt"])
            self.assertIn("+AAA", patches["a file.txt"])
            self.assertNotIn("+BBB", patches["a file.txt"])
            self.assertIn("+BBB", patches["b file.txt"])
            self.assertNotIn("+AAA", patches["b file.txt"])

    def test_split_patch_malformed_drops_safely(self) -> None:
        # Unquoted 3-token header (ambiguous spaces) must not guess.
        dropped = git_ops.split_patch_per_file(
            "diff --git a/a b c d/e\nold mode 100644\nnew mode 100755\n"
        )
        self.assertEqual(dropped, {})
        # Unterminated C-quote drops the section, keeps nothing.
        dropped2 = git_ops.split_patch_per_file(
            'diff --git "a/broken "b/fixed"\nindex 123..456 100644\n'
            '--- "a/broken\n+++ "b/fixed"\n@@ -1 +1 @@\n-a\n+b\n'
        )
        self.assertEqual(dropped2, {})
        # raw/numstat still supply the file without a patch.
        changes = git_ops.build_file_changes(
            raw={"real.txt": {"status": "M", "score": "", "old_path": None}},
            numstat={
                "real.txt": {
                    "additions": 1,
                    "deletions": 1,
                    "binary": False,
                    "old_path": None,
                }
            },
            patches=dropped,
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["new_path"], "real.txt")
        self.assertEqual(changes[0]["diff"], [])

    def test_build_file_changes_union_status_old_path(self) -> None:
        raw = {
            "new name.txt": {"status": "R", "score": "050", "old_path": "old.txt"},
            "gone.txt": {"status": "D", "score": "", "old_path": None},
        }
        numstat = {
            "new name.txt": {
                "additions": 2,
                "deletions": 1,
                "binary": False,
                "old_path": "old.txt",
            },
            "gone.txt": {
                "additions": 0,
                "deletions": 3,
                "binary": False,
                "old_path": None,
            },
        }
        patches = {
            "new name.txt": (
                "diff --git a/old.txt b/new name.txt\n"
                "index 123..456 100644\n--- a/old.txt\n+++ b/new name.txt\t\n"
                "@@ -1 +1,2 @@\n same\n+more\n"
            ),
        }
        changes = git_ops.build_file_changes(raw=raw, numstat=numstat, patches=patches)
        by_path = {c["new_path"]: c for c in changes}
        self.assertEqual(sorted(by_path), ["gone.txt", "new name.txt"])
        renamed = by_path["new name.txt"]
        self.assertEqual(renamed["status"], "R")
        self.assertEqual(renamed["old_path"], "old.txt")
        self.assertEqual(renamed["additions"], 2)
        self.assertEqual(renamed["deletions"], 1)
        self.assertTrue(renamed["diff"])
        deleted = by_path["gone.txt"]
        self.assertEqual(deleted["status"], "D")
        self.assertEqual(deleted["old_path"], "gone.txt")
        self.assertEqual(deleted["diff"], [])

    def test_operation_whitelist(self) -> None:
        for op in ("list_repos", "repo_snapshot", "repo_history", "checkout_remote_branch", "stage", "merge_abort", "sync", "commit_details"):
            self.assertIn(op, git_ops.GIT_OPERATIONS)
        self.assertNotIn("exec", git_ops.GIT_OPERATIONS)


# ---------------------------------------------------------------------------
# Service-level tests against real temporary git repos via FakeGitRuntime.
# ---------------------------------------------------------------------------


class GitDiscoverySecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_repos_discovers_multiple_repos_and_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo-a"))
            _make_repo(os.path.join(tmp, "repo-b"))
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(ws_id, "list_repos", None, {})
            self.assertTrue(result["ok"])
            paths = sorted(r["path"] for r in result["repos"])
            self.assertEqual(paths, ["/workspace/repo-a", "/workspace/repo-b"])

    async def test_repo_path_outside_workspace_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/etc/passwd", {}
            )
            self.assertFalse(result["ok"])
            # No git argv may run for an invalid path (wrapped or not).
            self.assertEqual(
                [
                    c
                    for c in _runtime.calls
                    if "rev-parse" in (git_ops.unwrap_git_exec_argv(c[0]) or c[0])
                ],
                [],
            )

    async def test_subdirectory_of_repo_rejected_as_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            os.makedirs(os.path.join(tmp, "repo", "sub"), exist_ok=True)
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/repo/sub", {}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "not_a_repo")

    async def test_not_a_repo_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "plain"), exist_ok=True)
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/plain", {}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "not_a_repo")

    async def test_unknown_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(ws_id, "rm -rf", None, {})
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "unknown_operation")


class GitMetadataSandboxTests(unittest.IsolatedAsyncioTestCase):
    """Metadata sandbox: gitdir/common-dir must stay under /workspace.

    Explicit operations on repos whose git metadata escapes reject with
    ``unsafe_repository`` (never echoing the host external path), run no
    status/diff/mutation argv after the metadata probes, and discovery
    omits the unsafe candidate while keeping normal repos.
    """

    async def test_external_separate_gitdir_rejected_and_omitted(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory(prefix="oc-ext-") as ext,
        ):
            _make_repo(os.path.join(tmp, "good"))
            ext_git = os.path.join(ext, "mydir.git")
            evil_host = os.path.join(tmp, "evil")
            proc = subprocess.run(
                [
                    "git",
                    "init",
                    "-b",
                    "main",
                    "--separate-git-dir",
                    ext_git,
                    evil_host,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            _git_config_identity(evil_host)
            Path(evil_host, "f.txt").write_text("x\n")
            subprocess.run(["git", "-C", evil_host, "add", "f.txt"], check=True)
            _commit_all(evil_host, "i")
            service, runtime, ws_id = _service_for(tmp)
            before = len(runtime.calls)
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/evil", {}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "unsafe_repository")
            self.assertTrue(_no_external_leak(result, ext, ext_git))
            self.assertIn("/workspace/evil", str(result.get("message", "")))
            self.assertFalse(_had_data_command(runtime, after=before))
            snap = await service.execute_git_operation(ws_id, "list_repos", None, {})
            self.assertTrue(snap["ok"])
            paths = sorted(r["path"] for r in snap["repos"])
            self.assertIn("/workspace/good", paths)
            self.assertNotIn("/workspace/evil", paths)
            # A mutation on the unsafe repo is rejected before any write.
            before_mut = len(runtime.calls)
            staged = await service.execute_git_operation(
                ws_id, "stage", "/workspace/evil", {"paths": ["f.txt"]}
            )
            self.assertFalse(staged["ok"])
            self.assertEqual(staged["code"], "unsafe_repository")
            self.assertTrue(_no_external_leak(staged, ext, ext_git))
            self.assertFalse(_had_data_command(runtime, after=before_mut))

    async def test_internal_linked_worktree_accepted_and_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main_host = _make_repo(os.path.join(tmp, "main"))
            wt_host = os.path.join(tmp, "wt1")
            proc = subprocess.run(
                ["git", "-C", main_host, "worktree", "add", wt_host],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            service, _runtime, ws_id = _service_for(tmp)
            for repo_arg in ("/workspace/main", "/workspace/wt1"):
                result = await service.execute_git_operation(
                    ws_id, "working_diff", repo_arg, {}
                )
                self.assertTrue(result["ok"], f"{repo_arg}: {result}")
            snap = await service.execute_git_operation(ws_id, "list_repos", None, {})
            self.assertTrue(snap["ok"])
            paths = sorted(r["path"] for r in snap["repos"])
            self.assertEqual(paths, ["/workspace/main", "/workspace/wt1"])

    async def test_external_main_linked_worktree_rejected_and_omitted(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory(prefix="oc-ext-") as ext,
        ):
            _make_repo(os.path.join(tmp, "good"))
            emain = os.path.join(ext, "emain")
            proc = subprocess.run(
                ["git", "init", "-b", "main", emain],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            _git_config_identity(emain)
            Path(emain, "f.txt").write_text("x\n")
            subprocess.run(["git", "-C", emain, "add", "f.txt"], check=True)
            _commit_all(emain, "i")
            linked_host = os.path.join(tmp, "linked")
            proc = subprocess.run(
                ["git", "-C", emain, "worktree", "add", linked_host],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            service, runtime, ws_id = _service_for(tmp)
            before = len(runtime.calls)
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/linked", {}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "unsafe_repository")
            self.assertTrue(_no_external_leak(result, ext, emain))
            self.assertFalse(_had_data_command(runtime, after=before))
            snap = await service.execute_git_operation(ws_id, "list_repos", None, {})
            self.assertTrue(snap["ok"])
            paths = sorted(r["path"] for r in snap["repos"])
            self.assertIn("/workspace/good", paths)
            self.assertNotIn("/workspace/linked", paths)

    async def test_git_symlink_escape_rejected_and_omitted(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory(prefix="oc-ext-") as ext,
        ):
            _make_repo(os.path.join(tmp, "good"))
            victim_host = os.path.join(tmp, "victim")
            proc = subprocess.run(
                ["git", "init", "-b", "main", victim_host],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            gitdir_host = os.path.join(victim_host, ".git")
            probe = subprocess.run(
                ["git", "-C", victim_host, "rev-parse", "--absolute-git-dir"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            # Replace the real .git dir with a symlink escaping the
            # workspace.  git then refuses the repo ("not a git
            # repository"); either code is fail-closed as long as no
            # external path leaks and discovery omits the candidate.
            import shutil

            shutil.rmtree(gitdir_host)
            os.symlink(ext, gitdir_host)
            service, runtime, ws_id = _service_for(tmp)
            before = len(runtime.calls)
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/victim", {}
            )
            self.assertFalse(result["ok"])
            self.assertIn(result["code"], ("unsafe_repository", "not_a_repo"))
            self.assertTrue(_no_external_leak(result, ext))
            self.assertFalse(_had_data_command(runtime, after=before))
            snap = await service.execute_git_operation(ws_id, "list_repos", None, {})
            self.assertTrue(snap["ok"])
            paths = sorted(r["path"] for r in snap["repos"])
            self.assertIn("/workspace/good", paths)
            self.assertNotIn("/workspace/victim", paths)

    async def test_symlinked_worktree_path_rejected_and_omitted(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory(prefix="oc-ext-") as ext,
        ):
            _make_repo(os.path.join(tmp, "good"))
            realrepo = os.path.join(ext, "realrepo")
            proc = subprocess.run(
                ["git", "init", "-b", "main", realrepo],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            os.symlink(realrepo, os.path.join(tmp, "linkrepo"))
            service, runtime, ws_id = _service_for(tmp)
            before = len(runtime.calls)
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/linkrepo", {}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "unsafe_repository")
            self.assertTrue(_no_external_leak(result, ext, realrepo))
            self.assertFalse(_had_data_command(runtime, after=before))
            snap = await service.execute_git_operation(ws_id, "list_repos", None, {})
            self.assertTrue(snap["ok"])
            paths = sorted(r["path"] for r in snap["repos"])
            self.assertIn("/workspace/good", paths)
            self.assertNotIn("/workspace/linkrepo", paths)

    async def test_malformed_metadata_outputs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            cases = [
                {"git_dir_output": ""},
                {"git_dir_output": "/workspace/repo/.git\n/workspace/evil\n"},
                {"git_dir_output": "/workspace/repo/.git\x00"},
                {"common_output": ""},
                {"common_output": "/workspace/repo/.git\n/evil\n"},
                {"common_output": "relative/path\n"},
            ]
            for kwargs in cases:
                with self.subTest(kwargs=kwargs):
                    runtime = _ScriptedMetadataRuntime(tmp, **kwargs)  # type: ignore[arg-type]
                    service = WorkspaceService(
                        runtimes={"docker": runtime},
                        settings=RunnerSettings(),
                    )
                    ws_id = uuid.uuid4()
                    service._cache[ws_id] = WorkspaceInfo(
                        workspace_id=ws_id,
                        instance_id="instance-1",
                        status="running",
                        runtime_type="docker",
                    )
                    before = len(runtime.calls)
                    result = await service.execute_git_operation(
                        ws_id, "working_diff", "/workspace/repo", {}
                    )
                    self.assertFalse(result["ok"], kwargs)
                    self.assertEqual(result["code"], "not_a_repo", kwargs)
                    self.assertFalse(
                        _had_data_command(runtime, after=before),
                        f"data command ran after malformed metadata: {kwargs}",
                    )

    async def test_realpath_failures_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            # Repo-root realpath failure: not_a_repo (unresolvable root).
            runtime = _ScriptedMetadataRuntime(
                tmp, realpath_fail_prefixes=("/workspace/repo",)
            )
            service = WorkspaceService(
                runtimes={"docker": runtime}, settings=RunnerSettings()
            )
            ws_id = uuid.uuid4()
            service._cache[ws_id] = WorkspaceInfo(
                workspace_id=ws_id,
                instance_id="instance-1",
                status="running",
                runtime_type="docker",
            )
            before = len(runtime.calls)
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/repo", {}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "not_a_repo")
            self.assertFalse(_had_data_command(runtime, after=before))
            # Metadata realpath failure: unsafe_repository (fail closed,
            # only the repo root echoed).
            runtime2 = _ScriptedMetadataRuntime(
                tmp, realpath_fail_prefixes=("/workspace/repo/.git",)
            )
            service2 = WorkspaceService(
                runtimes={"docker": runtime2}, settings=RunnerSettings()
            )
            ws_id2 = uuid.uuid4()
            service2._cache[ws_id2] = WorkspaceInfo(
                workspace_id=ws_id2,
                instance_id="instance-1",
                status="running",
                runtime_type="docker",
            )
            result2 = await service2.execute_git_operation(
                ws_id2, "working_diff", "/workspace/repo", {}
            )
            self.assertFalse(result2["ok"])
            self.assertEqual(result2["code"], "unsafe_repository")
            self.assertIn("/workspace/repo", str(result2.get("message", "")))

    async def test_old_git_common_dir_fallback_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            # Relative ".git" resolves under the workspace: accepted.
            runtime = _OldGitCommonDirRuntime(tmp, ".git\n")
            service = WorkspaceService(
                runtimes={"docker": runtime}, settings=RunnerSettings()
            )
            ws_id = uuid.uuid4()
            service._cache[ws_id] = WorkspaceInfo(
                workspace_id=ws_id,
                instance_id="instance-1",
                status="running",
                runtime_type="docker",
            )
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/repo", {}
            )
            self.assertTrue(result["ok"], result)
            # Relative escapes and absolute external values reject with
            # unsafe_repository and leak no host path.
            for plain in (
                "../../external-gitdir\n",
                "/tmp/oc-ext-evil.git\n",
            ):
                with self.subTest(plain=plain):
                    runtime_bad = _OldGitCommonDirRuntime(tmp, plain)
                    service_bad = WorkspaceService(
                        runtimes={"docker": runtime_bad},
                        settings=RunnerSettings(),
                    )
                    ws_bad = uuid.uuid4()
                    service_bad._cache[ws_bad] = WorkspaceInfo(
                        workspace_id=ws_bad,
                        instance_id="instance-1",
                        status="running",
                        runtime_type="docker",
                    )
                    before = len(runtime_bad.calls)
                    bad = await service_bad.execute_git_operation(
                        ws_bad, "working_diff", "/workspace/repo", {}
                    )
                    self.assertFalse(bad["ok"])
                    self.assertEqual(bad["code"], "unsafe_repository")
                    self.assertTrue(_no_external_leak(bad, "/tmp/oc-ext-evil.git"))
                    self.assertFalse(
                        _had_data_command(runtime_bad, after=before)
                    )

    async def test_internal_local_submodule_accepted_and_discovered(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main_host = _make_repo(os.path.join(tmp, "main"))
            sub_src = _make_repo(os.path.join(tmp, "subsrc"))
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    main_host,
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    sub_src,
                    "sub",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            subprocess.run(
                ["git", "-C", main_host, "add", ".gitmodules", "sub"],
                check=True,
                capture_output=True,
            )
            _commit_all(main_host, "add sub")
            service, _runtime, ws_id = _service_for(tmp)
            for repo_arg in ("/workspace/main", "/workspace/main/sub"):
                result = await service.execute_git_operation(
                    ws_id, "working_diff", repo_arg, {}
                )
                self.assertTrue(result["ok"], f"{repo_arg}: {result}")
            snap = await service.execute_git_operation(ws_id, "list_repos", None, {})
            self.assertTrue(snap["ok"])
            paths = sorted(r["path"] for r in snap["repos"])
            self.assertIn("/workspace/main", paths)
            self.assertIn("/workspace/main/sub", paths)


class GitMetadataHelperTests(unittest.TestCase):
    """Unit tests for the metadata-sandbox path helpers."""

    def test_parse_single_path_output(self) -> None:
        self.assertEqual(
            git_ops.parse_single_path_output("/workspace/a/.git\n"),
            "/workspace/a/.git",
        )
        self.assertEqual(
            git_ops.parse_single_path_output("  /workspace/a  \n"),
            "/workspace/a",
        )
        for bad in (
            "",
            "   \n",
            "/workspace/a\n/workspace/b\n",
            "/workspace/a\x00",
        ):
            with self.subTest(bad=repr(bad)):
                self.assertIsNone(git_ops.parse_single_path_output(bad))
        self.assertIsNone(git_ops.parse_single_path_output("/a\n/b\n"))
        self.assertIsNone(git_ops.parse_single_path_output("line1\r\nline2"))
        self.assertIsNone(git_ops.parse_single_path_output(123))  # type: ignore[arg-type]

    def test_is_workspace_path(self) -> None:
        self.assertTrue(git_ops.is_workspace_path("/workspace"))
        self.assertTrue(git_ops.is_workspace_path("/workspace/a"))
        self.assertTrue(git_ops.is_workspace_path("/workspace/a/../b"))
        self.assertFalse(git_ops.is_workspace_path("/workspace/../etc"))
        self.assertFalse(git_ops.is_workspace_path("/workspace/.."))
        self.assertFalse(git_ops.is_workspace_path("/etc/passwd"))
        self.assertFalse(git_ops.is_workspace_path("/workspace\x00evil"))
        self.assertFalse(git_ops.is_workspace_path("/workspace/a\nb"))
        self.assertFalse(git_ops.is_workspace_path("/workspace/a\rb"))
        self.assertFalse(git_ops.is_workspace_path(""))
        self.assertFalse(git_ops.is_workspace_path(None))  # type: ignore[arg-type]
        self.assertFalse(git_ops.is_workspace_path("/workspacespoof"))
        self.assertFalse(git_ops.is_workspace_path("/workspace-link"))


class GitSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """Split list/snapshot/history contract (no aggregate snapshot)."""

    async def test_list_repos_returns_light_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", "feature"],
                check=True,
                capture_output=True,
            )
            Path(repo, "feature.txt").write_text("f\n")
            subprocess.run(["git", "-C", repo, "add", "feature.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "feature tip"],
                check=True,
                capture_output=True,
            )
            service, runtime, ws_id = _service_for(tmp)
            before = len(runtime.calls)
            result = await service.execute_git_operation(ws_id, "list_repos", None, {})
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(result["repos"]), 1)
            entry = result["repos"][0]
            self.assertEqual(entry["path"], "/workspace/repo")
            self.assertEqual(entry["id"], "/workspace/repo")
            self.assertEqual(entry["name"], "repo")
            self.assertEqual(entry["current_branch"], "feature")
            self.assertRegex(entry["head_hash"], r"^[0-9a-f]{40}$")
            # Light entries carry no heavy payloads.
            self.assertEqual(
                set(entry), {"id", "name", "path", "current_branch", "head_hash"}
            )
            # No status/log/for-each-ref argv for the list path.
            heavies = []
            for call in runtime.calls[before:]:
                inner = git_ops.unwrap_git_exec_argv(call[0]) or call[0]
                if inner[:1] == ["git"] and len(inner) > 1:
                    if inner[1] in {"status", "log", "for-each-ref"}:
                        heavies.append(inner)
            self.assertEqual(heavies, [])

    async def test_list_repos_discovers_multiple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo-a"))
            _make_repo(os.path.join(tmp, "repo-b"))
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(ws_id, "list_repos", None, {})
            self.assertTrue(result["ok"])
            paths = sorted(r["path"] for r in result["repos"])
            self.assertEqual(paths, ["/workspace/repo-a", "/workspace/repo-b"])

    async def test_repo_snapshot_merges_first_history_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            # Second commit + staged/unstaged/untracked mix.
            Path(repo, "feature.txt").write_text("f\n")
            subprocess.run(["git", "-C", repo, "add", "feature.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "second"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# changed\n")
            Path(repo, "new-staged.txt").write_text("s\n")
            subprocess.run(["git", "-C", repo, "add", "new-staged.txt"], check=True)
            Path(repo, "untracked.txt").write_text("u\n")

            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "repo_snapshot", _ws_repo(tmp, "repo"), {}
            )
            self.assertTrue(result["ok"], result)
            snap = result["snapshot"]
            self.assertEqual(snap["path"], "/workspace/repo")
            self.assertEqual(snap["current_branch"], "main")
            self.assertRegex(snap["head_hash"], r"^[0-9a-f]{40}$")
            self.assertTrue(any(b["name"] == "main" for b in snap["branches"]))
            self.assertGreaterEqual(len(snap["commits"]), 2)
            first = snap["commits"][0]
            self.assertRegex(first["hash"], r"^[0-9a-f]{40}$")
            self.assertIn("parents", first)
            self.assertIn("timestamp", first)
            self.assertEqual(snap["history_limit"], 50)
            self.assertEqual(snap["history_skip"], 0)
            self.assertIn("has_more", snap)
            paths = {c["path"]: c for c in snap["changes"]}
            self.assertTrue(paths["new-staged.txt"]["staged"])
            self.assertFalse(paths["untracked.txt"]["staged"])
            self.assertIn("merge_state", snap)

    async def test_repo_snapshot_needs_repo_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(ws_id, "repo_snapshot", None, {})
            self.assertFalse(result["ok"])
            self.assertIn(result["code"], ("invalid_argument", "not_a_repo"))

    async def test_repo_history_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            for i in range(5):
                Path(repo, f"f{i}.txt").write_text(f"{i}\n")
                subprocess.run(["git", "-C", repo, "add", f"f{i}.txt"], check=True)
                subprocess.run(
                    ["git", "-C", repo, "commit", "-m", f"c{i}"],
                    check=True,
                    capture_output=True,
                )
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id,
                "repo_history",
                _ws_repo(tmp, "repo"),
                {"history_limit": 2, "history_skip": 1},
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(result["commits"]), 2)
            self.assertTrue(result["has_more"])
            self.assertEqual(result["history_skip"], 1)
            self.assertEqual(result["history_limit"], 2)
            self.assertEqual(result["repo_path"], "/workspace/repo")

    async def test_repo_history_branch_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", "feature"],
                check=True,
                capture_output=True,
            )
            Path(repo, "feature.txt").write_text("f\n")
            subprocess.run(["git", "-C", repo, "add", "feature.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "feature tip"],
                check=True,
                capture_output=True,
            )
            feature_tip = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", repo, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            Path(repo, "main2.txt").write_text("m\n")
            subprocess.run(["git", "-C", repo, "add", "main2.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "main tip"],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            only_feature = await service.execute_git_operation(
                ws_id, "repo_history", repo_arg, {"branch": "feature"}
            )
            self.assertTrue(only_feature["ok"], only_feature)
            feature_hashes = {c["hash"] for c in only_feature["commits"]}
            self.assertIn(feature_tip, feature_hashes)
            # main-only page must not contain the feature tip.
            only_main = await service.execute_git_operation(
                ws_id, "repo_history", repo_arg, {"branch": "main"}
            )
            self.assertTrue(only_main["ok"], only_main)
            self.assertNotIn(
                feature_tip, {c["hash"] for c in only_main["commits"]}
            )
            # Unknown branch filter is a structured error.
            bad = await service.execute_git_operation(
                ws_id, "repo_history", repo_arg, {"branch": "nope-missing"}
            )
            self.assertFalse(bad["ok"])
            self.assertEqual(bad["code"], "unknown_branch")

    async def test_repo_history_caps_and_unborn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            service, _runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            capped = await service.execute_git_operation(
                ws_id, "repo_history", repo_arg, {"history_limit": 9999}
            )
            self.assertTrue(capped["ok"], capped)
            self.assertEqual(capped["history_limit"], 500)
            zero = await service.execute_git_operation(
                ws_id, "repo_history", repo_arg, {"history_limit": 0}
            )
            self.assertTrue(zero["ok"], zero)
            self.assertEqual(zero["history_limit"], 1)
            neg = await service.execute_git_operation(
                ws_id, "repo_history", repo_arg, {"history_skip": -5}
            )
            self.assertTrue(neg["ok"], neg)
            self.assertEqual(neg["history_skip"], 0)
            bad_type = await service.execute_git_operation(
                ws_id, "repo_history", repo_arg, {"history_limit": "many"}
            )
            self.assertFalse(bad_type["ok"])
            self.assertEqual(bad_type["code"], "invalid_argument")

            fresh_dir = os.path.join(tmp, "fresh")
            os.makedirs(fresh_dir)
            subprocess.run(
                ["git", "init", "-b", "main", fresh_dir],
                check=True,
                capture_output=True,
            )
            empty = await service.execute_git_operation(
                ws_id, "repo_history", "/workspace/fresh", {}
            )
            self.assertTrue(empty["ok"], empty)
            self.assertEqual(empty["commits"], [])
            self.assertFalse(empty["has_more"])

    async def test_repo_history_includes_all_branches_remotes_and_stash(self) -> None:
        """History spans every ref (all branches incl. remotes + stash).

        Regression test: the history previously listed only ``HEAD`` (the
        current branch).  It must instead cover all refs — like
        vscode-git-graph — while internal ``refs/notes/*`` fan-out stays
        hidden and detached commits remain included via HEAD.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            _git_config_identity(repo)
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", "feature"],
                check=True,
                capture_output=True,
            )
            Path(repo, "feature.txt").write_text("f\n")
            subprocess.run(["git", "-C", repo, "add", "feature.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "feature tip"],
                check=True,
                capture_output=True,
            )
            feature_tip = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", repo, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# stashed\n")
            subprocess.run(
                ["git", "-C", repo, "stash", "push", "-m", "stash entry"],
                check=True,
                capture_output=True,
            )
            stash_tip = subprocess.run(
                ["git", "-C", repo, "rev-parse", "refs/stash"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            # A remote-tracking ref must show up as well (a fetched remote
            # branch has no local commits of its own — simulate the fetch
            # result directly so the test stays offline and deterministic).
            subprocess.run(
                ["git", "-C", repo, "update-ref",
                 "refs/remotes/origin/feature", feature_tip],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "notes", "add", "-m", "hidden note", "HEAD"],
                check=True,
                capture_output=True,
            )
            notes_tip = subprocess.run(
                ["git", "-C", repo, "rev-parse", "refs/notes/commits"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "repo_history", _ws_repo(tmp, "repo"), {}
            )
            self.assertTrue(result["ok"], result)
            by_hash = {c["hash"]: c for c in result["commits"]}
            self.assertIn(feature_tip, by_hash)
            self.assertIn(stash_tip, by_hash)
            self.assertNotIn(notes_tip, by_hash)
            # Remote refs still surface via repo_snapshot (not history).
            snap = await service.execute_git_operation(
                ws_id, "repo_snapshot", _ws_repo(tmp, "repo"), {}
            )
            self.assertTrue(snap["ok"], snap)
            self.assertTrue(
                any(
                    r["name"] == "origin/feature"
                    for r in snap["snapshot"]["remote_refs"]
                )
            )

    async def test_repo_snapshot_unborn_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = os.path.join(tmp, "fresh")
            os.makedirs(repo_dir)
            subprocess.run(
                ["git", "init", "-b", "main", repo_dir],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            listed = await service.execute_git_operation(
                ws_id, "list_repos", None, {}
            )
            self.assertTrue(listed["ok"])
            entry = next(
                r for r in listed["repos"] if r["path"] == "/workspace/fresh"
            )
            self.assertIsNone(entry["current_branch"])
            self.assertIsNone(entry["head_hash"])
            result = await service.execute_git_operation(
                ws_id, "repo_snapshot", "/workspace/fresh", {}
            )
            self.assertTrue(result["ok"], result)
            snap = result["snapshot"]
            self.assertEqual(snap["current_branch"], "main")
            self.assertIsNone(snap["head_hash"])
            self.assertEqual(snap["commits"], [])

    async def test_list_repos_detached_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            head = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", repo, "checkout", "--detach", "HEAD"],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "working_diff", _ws_repo(tmp, "repo"), {}
            )
            self.assertTrue(result["ok"])
            snap_result = await service.execute_git_operation(
                ws_id, "list_repos", None, {}
            )
            entry = next(
                r for r in snap_result["repos"] if r["path"] == "/workspace/repo"
            )
            self.assertIsNone(entry["current_branch"])
            self.assertEqual(entry["head_hash"], head)
            snap = await service.execute_git_operation(
                ws_id, "repo_snapshot", _ws_repo(tmp, "repo"), {}
            )
            self.assertTrue(snap["ok"], snap)
            self.assertIsNone(snap["snapshot"]["current_branch"])
            self.assertEqual(snap["snapshot"]["head_hash"], head)


class GitDiffTests(unittest.IsolatedAsyncioTestCase):
    async def test_working_diff_staged_vs_unstaged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            Path(repo, "staged.txt").write_text("one\n")
            subprocess.run(["git", "-C", repo, "add", "staged.txt"], check=True)
            Path(repo, "README.md").write_text("# changed\nline\n")
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "working_diff", _ws_repo(tmp, "repo"), {}
            )
            self.assertTrue(result["ok"])
            staged = {f["new_path"]: f for f in result["diff"]["staged"]}
            unstaged = {f["new_path"]: f for f in result["diff"]["unstaged"]}
            self.assertIn("staged.txt", staged)
            self.assertIn("README.md", unstaged)
            self.assertNotIn("README.md", staged)
            self.assertTrue(staged["staged.txt"]["has_textual_diff"])
            self.assertTrue(unstaged["README.md"]["diff"])

    async def test_working_diff_untracked_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            Path(repo, "notes.txt").write_text("hello\nworld\n")
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "working_diff", _ws_repo(tmp, "repo"), {}
            )
            self.assertTrue(result["ok"])
            unstaged = {f["new_path"]: f for f in result["diff"]["unstaged"]}
            self.assertIn("notes.txt", unstaged)
            entry = unstaged["notes.txt"]
            self.assertEqual(entry["status"], "A")
            flat = [line["content"] for hunk in entry["diff"] for line in hunk["lines"]]
            self.assertIn("hello", flat)

    async def test_working_diff_binary_marked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            Path(repo, "blob.bin").write_bytes(bytes(range(256)) * 64)
            subprocess.run(["git", "-C", repo, "add", "blob.bin"], check=True)
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "working_diff", _ws_repo(tmp, "repo"), {}
            )
            self.assertTrue(result["ok"])
            staged = {f["new_path"]: f for f in result["diff"]["staged"]}
            self.assertIn("blob.bin", staged)
            self.assertFalse(staged["blob.bin"]["has_textual_diff"])

    async def test_commit_details_root_and_regular(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            root = subprocess.run(
                ["git", "-C", repo, "rev-list", "--max-parents=0", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            Path(repo, "second.txt").write_text("two\n")
            subprocess.run(["git", "-C", repo, "add", "second.txt"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "commit",
                    "-m",
                    "second subject",
                    "-m",
                    "body line",
                ],
                check=True,
                capture_output=True,
            )
            head = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            service, _runtime, ws_id = _service_for(tmp)
            details = await service.execute_git_operation(
                ws_id,
                "commit_details",
                _ws_repo(tmp, "repo"),
                {"commit": head},
            )
            self.assertTrue(details["ok"])
            self.assertEqual(details["details"]["hash"], head)
            self.assertEqual(details["details"]["message"], "second subject")
            self.assertIn("body line", details["details"]["body"])
            self.assertTrue(details["details"]["file_changes"])
            root_details = await service.execute_git_operation(
                ws_id,
                "commit_details",
                _ws_repo(tmp, "repo"),
                {"commit": root},
            )
            self.assertTrue(root_details["ok"])
            self.assertEqual(root_details["details"]["parents"], [])

    async def test_argv_only_no_shell_concat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, runtime, ws_id = _service_for(tmp)
            await service.execute_git_operation(
                ws_id, "working_diff", _ws_repo(tmp, "repo"), {}
            )
            for argv, _workdir, _env in runtime.calls:
                self.assertIsInstance(argv, list)
                # Git invocations run behind the fixed sourcing wrapper
                # (sh -c '<fixed script>' + git argv); the wrapper script
                # itself is constant and user input stays separate argv
                # elements behind exec "$@".  Non-git probes may use fixed
                # ["sh", ...] scripts — those never carry user input.
                inner = git_ops.unwrap_git_exec_argv(argv)
                if inner is not None:
                    self.assertEqual(inner[:1], ["git"])
                    for token in inner:
                        self.assertNotIn(";", token)
                        self.assertNotIn("&&", token)
                        self.assertNotIn("|", token)
                elif argv[:1] == ["git"]:
                    for token in argv:
                        self.assertNotIn(";", token)
                        self.assertNotIn("&&", token)
                        self.assertNotIn("|", token)


class GitMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_unstage_discard_commit_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            Path(repo, "work.txt").write_text("v1\n")
            service, _runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")

            staged = await service.execute_git_operation(
                ws_id, "stage", repo_arg, {"paths": ["work.txt"]}
            )
            self.assertTrue(staged["ok"])
            self.assertIn("snapshot", staged)
            snap = staged["snapshot"]
            entry = next(c for c in snap["changes"] if c["path"] == "work.txt")
            self.assertTrue(entry["staged"])

            unstaged = await service.execute_git_operation(
                ws_id, "unstage", repo_arg, {"paths": ["work.txt"]}
            )
            self.assertTrue(unstaged["ok"])
            entry2 = next(
                c for c in unstaged["snapshot"]["changes"] if c["path"] == "work.txt"
            )
            self.assertFalse(entry2["staged"])

            # Discard a tracked modification.
            Path(repo, "README.md").write_text("# dirty\n")
            discarded = await service.execute_git_operation(
                ws_id, "discard", repo_arg, {"paths": ["README.md"]}
            )
            self.assertTrue(discarded["ok"])
            self.assertEqual(Path(repo, "README.md").read_text(), "# test\n")

            # Discard an untracked file removes exactly that path.
            Path(repo, "trash.txt").write_text("x\n")
            Path(repo, "keep.txt").write_text("keep\n")
            discarded2 = await service.execute_git_operation(
                ws_id, "discard", repo_arg, {"paths": ["trash.txt"]}
            )
            self.assertTrue(discarded2["ok"])
            self.assertFalse(Path(repo, "trash.txt").exists())
            self.assertTrue(Path(repo, "keep.txt").exists())

            # Commit only the index.
            Path(repo, "commit-me.txt").write_text("c\n")
            await service.execute_git_operation(
                ws_id, "stage", repo_arg, {"paths": ["commit-me.txt"]}
            )
            committed = await service.execute_git_operation(
                ws_id,
                "commit",
                repo_arg,
                {
                    "message": "add commit-me",
                    "author_name": "T",
                    "author_email": "t@example.com",
                },
            )
            self.assertTrue(committed["ok"])
            log = subprocess.run(
                ["git", "-C", repo, "log", "--oneline", "-1"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("add commit-me", log)

    async def test_commit_respects_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            Path(repo, "a.txt").write_text("a\n")
            service, runtime, ws_id = _service_for(tmp)
            await service.execute_git_operation(
                ws_id, "stage", _ws_repo(tmp, "repo"), {"paths": ["a.txt"]}
            )
            await service.execute_git_operation(
                ws_id, "commit", _ws_repo(tmp, "repo"), {"message": "m1"}
            )
            commit_argv = next(
                git_ops.unwrap_git_exec_argv(c[0]) or c[0]
                for c in runtime.calls
                if (git_ops.unwrap_git_exec_argv(c[0]) or c[0])[:2] == ["git", "commit"]
            )
            self.assertNotIn("user.name=opencuria", " ".join(commit_argv))

    async def _commit_identity_case(
        self,
        tmpdir: str,
        *,
        repo_name: str | None,
        repo_email: str | None,
        fallback_name: str = "Fallback Name",
        fallback_email: str = "fallback@example.com",
    ) -> tuple[str, list[str]]:
        """Stage+commit in a repo with partial identity; return author + argv."""
        repo = os.path.join(tmpdir, "repo")
        os.makedirs(repo, exist_ok=True)
        subprocess.run(
            ["git", "init", "-b", "main", repo],
            check=True,
            capture_output=True,
        )
        if repo_name is not None:
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", repo_name],
                check=True,
                capture_output=True,
            )
        if repo_email is not None:
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", repo_email],
                check=True,
                capture_output=True,
            )
        Path(repo, "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", repo, "add", "seed.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                repo,
                "-c",
                "user.name=Seeder",
                "-c",
                "user.email=seeder@example.com",
                "commit",
                "-m",
                "seed",
            ],
            check=True,
            capture_output=True,
        )
        Path(repo, "next.txt").write_text("next\n")
        service, runtime, ws_id = _service_for(tmpdir)
        repo_arg = _ws_repo(tmpdir, "repo")
        staged = await service.execute_git_operation(
            ws_id, "stage", repo_arg, {"paths": ["next.txt"]}
        )
        self.assertTrue(staged["ok"])
        result = await service.execute_git_operation(
            ws_id,
            "commit",
            repo_arg,
            {
                "message": "identity check",
                "author_name": fallback_name,
                "author_email": fallback_email,
            },
        )
        self.assertTrue(result["ok"], msg=str(result))
        author = subprocess.run(
            ["git", "-C", repo, "show", "--no-patch", "--format=%an%x00%ae", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        commit_argv = next(
            git_ops.unwrap_git_exec_argv(c[0]) or c[0]
            for c in runtime.calls
            if (git_ops.unwrap_git_exec_argv(c[0]) or c[0])[:1] == ["git"]
            and "commit" in (git_ops.unwrap_git_exec_argv(c[0]) or c[0])
        )
        return author, commit_argv

    async def test_commit_partial_identity_repo_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            author, commit_argv = await self._commit_identity_case(
                tmp,
                repo_name="Repo Name",
                repo_email=None,
            )
            self.assertEqual(author, "Repo Name\x00fallback@example.com")
            self.assertNotIn("user.name=Fallback Name", " ".join(commit_argv))
            self.assertIn("user.email=fallback@example.com", " ".join(commit_argv))

    async def test_commit_partial_identity_repo_email_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            author, commit_argv = await self._commit_identity_case(
                tmp,
                repo_name=None,
                repo_email="repo@example.com",
            )
            self.assertEqual(author, "Fallback Name\x00repo@example.com")
            self.assertIn("user.name=Fallback Name", " ".join(commit_argv))
            self.assertNotIn("user.email=repo@example.com", " ".join(commit_argv))

    async def test_commit_identity_both_present_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            author, commit_argv = await self._commit_identity_case(
                tmp,
                repo_name="Repo Name",
                repo_email="repo@example.com",
            )
            self.assertEqual(author, "Repo Name\x00repo@example.com")
            self.assertNotIn("-c", commit_argv)

    async def test_commit_identity_both_missing_uses_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            author, commit_argv = await self._commit_identity_case(
                tmp,
                repo_name=None,
                repo_email=None,
            )
            self.assertEqual(author, "Fallback Name\x00fallback@example.com")
            joined = " ".join(commit_argv)
            self.assertIn("user.name=Fallback Name", joined)
            self.assertIn("user.email=fallback@example.com", joined)

    async def test_commit_message_with_quotes_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            Path(repo, "a.txt").write_text("a\n")
            service, _runtime, ws_id = _service_for(tmp)
            await service.execute_git_operation(
                ws_id, "stage", _ws_repo(tmp, "repo"), {"paths": ["a.txt"]}
            )
            evil = "fix '; rm -rf /; echo '"
            result = await service.execute_git_operation(
                ws_id, "commit", _ws_repo(tmp, "repo"), {"message": evil}
            )
            self.assertTrue(result["ok"])
            subject = subprocess.run(
                ["git", "-C", repo, "log", "-1", "--format=%s"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(subject, evil)

    async def test_unstage_unborn_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = os.path.join(tmp, "fresh")
            os.makedirs(repo_dir)
            subprocess.run(
                ["git", "init", "-b", "main", repo_dir],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "config", "user.email", "t@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo_dir, "config", "user.name", "T"],
                check=True,
            )
            Path(repo_dir, "a.txt").write_text("a\n")
            service, _runtime, ws_id = _service_for(tmp)
            staged = await service.execute_git_operation(
                ws_id, "stage", "/workspace/fresh", {"paths": ["a.txt"]}
            )
            self.assertTrue(staged["ok"])
            unstaged = await service.execute_git_operation(
                ws_id, "unstage", "/workspace/fresh", {"paths": ["a.txt"]}
            )
            self.assertTrue(unstaged["ok"])

    async def test_branch_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, _runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")

            created = await service.execute_git_operation(
                ws_id, "create_branch", repo_arg, {"branch": "feature/a"}
            )
            self.assertTrue(created["ok"])
            self.assertTrue(
                any(b["name"] == "feature/a" for b in created["snapshot"]["branches"])
            )

            checked = await service.execute_git_operation(
                ws_id, "checkout_branch", repo_arg, {"branch": "feature/a"}
            )
            self.assertTrue(checked["ok"])
            self.assertEqual(checked["snapshot"]["current_branch"], "feature/a")

            renamed = await service.execute_git_operation(
                ws_id, "rename_branch", repo_arg, {"new_branch": "feature/b"}
            )
            self.assertTrue(renamed["ok"])
            self.assertEqual(renamed["snapshot"]["current_branch"], "feature/b")

            back = await service.execute_git_operation(
                ws_id, "checkout_branch", repo_arg, {"branch": "main"}
            )
            self.assertTrue(back["ok"])
            deleted = await service.execute_git_operation(
                ws_id, "delete_branch", repo_arg, {"branch": "feature/b"}
            )
            self.assertTrue(deleted["ok"])
            self.assertFalse(
                any(b["name"] == "feature/b" for b in deleted["snapshot"]["branches"])
            )

    async def test_invalid_branch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, runtime, ws_id = _service_for(tmp)
            before = len(runtime.calls)
            result = await service.execute_git_operation(
                ws_id,
                "create_branch",
                _ws_repo(tmp, "repo"),
                {"branch": "-evil"},
            )
            self.assertFalse(result["ok"])
            # Validation happens before any branch-mutating argv runs.
            branch_calls = [
                c
                for c in runtime.calls[before:]
                if (git_ops.unwrap_git_exec_argv(c[0]) or c[0])[:2] == ["git", "branch"]
                and "-evil" in (git_ops.unwrap_git_exec_argv(c[0]) or c[0])
            ]
            self.assertEqual(branch_calls, [])

    async def test_delete_current_branch_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "delete_branch", _ws_repo(tmp, "repo"), {"branch": "main"}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "current_branch")

    async def test_merge_into_current_and_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", "side"],
                check=True,
                capture_output=True,
            )
            Path(repo, "side.txt").write_text("s\n")
            subprocess.run(["git", "-C", repo, "add", "side.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "side work"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            merged = await service.execute_git_operation(
                ws_id,
                "merge_into_current",
                _ws_repo(tmp, "repo"),
                {"branch": "side"},
            )
            self.assertTrue(merged["ok"])
            self.assertFalse(merged.get("conflict", False))
            self.assertTrue(Path(repo, "side.txt").exists())

    async def test_merge_conflict_then_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", "side"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# side\n")
            subprocess.run(
                ["git", "-C", repo, "commit", "-am", "side"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# main\n")
            subprocess.run(
                ["git", "-C", repo, "commit", "-am", "main2"],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            merged = await service.execute_git_operation(
                ws_id, "merge_into_current", repo_arg, {"branch": "side"}
            )
            self.assertFalse(merged["ok"])
            self.assertEqual(merged["code"], "conflict")
            self.assertTrue(merged.get("conflict"))
            self.assertIn("snapshot", merged)
            aborted = await service.execute_git_operation(
                ws_id, "merge_abort", repo_arg, {}
            )
            self.assertTrue(aborted["ok"])

    async def test_merge_current_into_returns_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", "target"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            Path(repo, "main-work.txt").write_text("m\n")
            subprocess.run(["git", "-C", repo, "add", "main-work.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "main work"],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id,
                "merge_current_into",
                _ws_repo(tmp, "repo"),
                {"target": "target"},
            )
            self.assertTrue(result["ok"])
            self.assertFalse(result.get("stayed_on_target", True))
            # Runner checked back out to the source branch.
            current = subprocess.run(
                ["git", "-C", repo, "branch", "--show-current"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(current, "main")

    async def test_merge_current_into_stays_on_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", "target"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# target\n")
            subprocess.run(
                ["git", "-C", repo, "commit", "-am", "target change"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            Path(repo, "README.md").write_text("# main\n")
            subprocess.run(
                ["git", "-C", repo, "commit", "-am", "main change"],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id,
                "merge_current_into",
                _ws_repo(tmp, "repo"),
                {"target": "target"},
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "conflict")
            self.assertTrue(result.get("stayed_on_target"))
            on_disk = subprocess.run(
                ["git", "-C", repo, "branch", "--show-current"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(on_disk, "target")

    async def test_checkout_commit_detaches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            head = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id,
                "checkout_commit",
                _ws_repo(tmp, "repo"),
                {"commit": head},
            )
            self.assertTrue(result["ok"])
            self.assertIsNone(result["snapshot"]["current_branch"])
            self.assertEqual(result["snapshot"]["head_hash"], head)

    async def test_fetch_push_pull_sync_with_local_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            remote = os.path.join(tmp, "remote.git")
            subprocess.run(
                ["git", "init", "--bare", remote],
                check=True,
                capture_output=True,
            )
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "remote", "add", "origin", remote],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "push", "-u", "origin", "main"],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            fetched = await service.execute_git_operation(ws_id, "fetch", repo_arg, {})
            self.assertTrue(fetched["ok"])
            Path(repo, "more.txt").write_text("more\n")
            subprocess.run(["git", "-C", repo, "add", "more.txt"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-m", "more"],
                check=True,
                capture_output=True,
            )
            pushed = await service.execute_git_operation(ws_id, "push", repo_arg, {})
            self.assertTrue(pushed["ok"])
            pulled = await service.execute_git_operation(ws_id, "pull", repo_arg, {})
            self.assertTrue(pulled["ok"])
            synced = await service.execute_git_operation(ws_id, "sync", repo_arg, {})
            self.assertTrue(synced["ok"])

    def _push_argv_calls(self, runtime: FakeGitRuntime) -> list[list[str]]:
        """Return unwrapped ``git push`` argv in call order."""
        calls: list[list[str]] = []
        for argv, _workdir, _env in runtime.calls:
            inner = git_ops.unwrap_git_exec_argv(argv) or argv
            if inner[:2] == ["git", "push"]:
                calls.append(inner)
        return calls

    async def test_push_respects_existing_upstream_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin.git")
            fork = os.path.join(tmp, "fork.git")
            subprocess.run(
                ["git", "init", "--bare", origin], check=True, capture_output=True
            )
            subprocess.run(
                ["git", "init", "--bare", fork], check=True, capture_output=True
            )
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "remote", "add", "origin", origin],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "remote", "add", "fork", fork],
                check=True,
            )
            # Upstream points at fork; origin must not be forced.
            subprocess.run(
                ["git", "-C", repo, "push", "-u", "fork", "main"],
                check=True,
                capture_output=True,
            )
            service, runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            result = await service.execute_git_operation(ws_id, "push", repo_arg, {})
            self.assertTrue(result["ok"], msg=str(result))
            push_calls = self._push_argv_calls(runtime)
            self.assertEqual(push_calls, [["git", "push"]])

    async def test_push_first_push_sets_origin_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin.git")
            subprocess.run(
                ["git", "init", "--bare", origin], check=True, capture_output=True
            )
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "remote", "add", "origin", origin],
                check=True,
            )
            service, runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            result = await service.execute_git_operation(ws_id, "push", repo_arg, {})
            self.assertTrue(result["ok"], msg=str(result))
            push_calls = self._push_argv_calls(runtime)
            self.assertEqual(push_calls, [["git", "push", "-u", "origin", "main"]])
            upstream = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "rev-parse",
                    "--abbrev-ref",
                    "--symbolic-full-name",
                    "@{u}",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(upstream, "origin/main")

    async def test_push_explicit_remote_with_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin.git")
            fork = os.path.join(tmp, "fork.git")
            subprocess.run(
                ["git", "init", "--bare", origin], check=True, capture_output=True
            )
            subprocess.run(
                ["git", "init", "--bare", fork], check=True, capture_output=True
            )
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "remote", "add", "origin", origin],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "remote", "add", "fork", fork],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "push", "-u", "fork", "main"],
                check=True,
                capture_output=True,
            )
            service, runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            result = await service.execute_git_operation(
                ws_id, "push", repo_arg, {"remote": "fork"}
            )
            self.assertTrue(result["ok"], msg=str(result))
            push_calls = self._push_argv_calls(runtime)
            self.assertEqual(push_calls, [["git", "push", "fork"]])

    async def test_push_set_upstream_true_resets_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin.git")
            fork = os.path.join(tmp, "fork.git")
            subprocess.run(
                ["git", "init", "--bare", origin], check=True, capture_output=True
            )
            subprocess.run(
                ["git", "init", "--bare", fork], check=True, capture_output=True
            )
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "remote", "add", "origin", origin],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "remote", "add", "fork", fork],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "push", "-u", "fork", "main"],
                check=True,
                capture_output=True,
            )
            service, runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            result = await service.execute_git_operation(
                ws_id,
                "push",
                repo_arg,
                {"remote": "origin", "set_upstream": True},
            )
            self.assertTrue(result["ok"], msg=str(result))
            push_calls = self._push_argv_calls(runtime)
            self.assertEqual(push_calls, [["git", "push", "-u", "origin", "main"]])
            upstream = subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "rev-parse",
                    "--abbrev-ref",
                    "--symbolic-full-name",
                    "@{u}",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(upstream, "origin/main")

    async def test_push_detached_head_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            head = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", repo, "checkout", "--detach", head],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "push", _ws_repo(tmp, "repo"), {}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "detached_head")

    async def test_pull_no_upstream_structured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", "lonely"],
                check=True,
                capture_output=True,
            )
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id, "pull", _ws_repo(tmp, "repo"), {}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "no_upstream")


class GitAuthEnvTests(unittest.IsolatedAsyncioTestCase):
    async def test_git_env_rejects_caller_env_strict(self) -> None:
        # Unknown `env` args are rejected fail-closed (invalid_argument)
        # before any git argv runs — never silently ignored.
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, runtime, ws_id = _service_for(tmp)
            before = len(runtime.calls)
            result = await service.execute_git_operation(
                ws_id,
                "working_diff",
                _ws_repo(tmp, "repo"),
                {
                    "env": {
                        "GITHUB_TOKEN": "ghp_super_secret_value",
                        "GIT_CONFIG_COUNT": "1",
                    }
                },
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "invalid_argument")
            self.assertEqual(runtime.calls[before:], [])

    async def test_strict_args_rejected_before_git_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            cases = [
                ("list_repos", None, {"bogus": 1}),
                ("repo_snapshot", repo_arg, {"bogus": 1}),
                ("repo_history", repo_arg, {"history_limit": 2, "bogus": 1}),
                ("working_diff", repo_arg, {"bogus": 1}),
                ("commit_details", repo_arg, {"commit": "abc123", "hash": "abc123"}),
                ("stage", repo_arg, {"paths": ["a"], "path": ["b"]}),
                ("commit", repo_arg, {"message": "m", "message_b64": "bQ=="}),
                ("commit", repo_arg, {"message": "m", "env": {}}),
                ("fetch", repo_arg, {"remote": "origin", "branch": "main"}),
                ("pull", repo_arg, {"remote": "origin", "set_upstream": True}),
                ("push", repo_arg, {"remote": "origin", "branch": "main"}),
                ("create_branch", repo_arg, {"branch": "x", "new": "y"}),
                ("rename_branch", repo_arg, {"new_branch": "a", "new": "b"}),
                ("merge_current_into", repo_arg, {"target": "a", "branch": "b"}),
                ("merge_abort", repo_arg, {"force": True}),
            ]
            for operation, repo, args in cases:
                before = len(runtime.calls)
                result = await service.execute_git_operation(
                    ws_id,
                    operation,
                    repo,
                    dict(args),
                )
                self.assertFalse(result["ok"], msg=f"{operation} {args}")
                self.assertEqual(
                    result["code"], "invalid_argument", msg=f"{operation} {args}"
                )
                self.assertEqual(
                    runtime.calls[before:],
                    [],
                    msg=f"{operation} {args} must not run git",
                )
            # Commit keeps backend-injected identity keys allowed.
            before_ok = len(runtime.calls)
            Path(os.path.join(tmp, "repo", "strict-ok.txt")).write_text("x\n")
            await service.execute_git_operation(
                ws_id, "stage", repo_arg, {"paths": ["strict-ok.txt"]}
            )
            ok_result = await service.execute_git_operation(
                ws_id,
                "commit",
                repo_arg,
                {
                    "message": "strict ok",
                    "author_name": "T",
                    "author_email": "t@example.com",
                },
            )
            self.assertTrue(ok_result["ok"])
            self.assertGreater(len(runtime.calls), before_ok)

    async def test_commit_author_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            # Remove repo identity so the fallback path is exercised.
            subprocess.run(
                ["git", "-C", repo, "config", "--unset", "user.name"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "--unset", "user.email"],
                check=True,
                capture_output=True,
            )
            service, runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            Path(repo, "au.txt").write_text("a\n")
            await service.execute_git_operation(
                ws_id, "stage", repo_arg, {"paths": ["au.txt"]}
            )
            cases = [
                {
                    "message": "m",
                    "author_name": "bad\nname",
                    "author_email": "t@example.com",
                },
                {
                    "message": "m",
                    "author_name": "T",
                    "author_email": "bad\n@example.com",
                },
                {
                    "message": "m",
                    "author_name": "x" * 256,
                    "author_email": "t@example.com",
                },
                {"message": "m", "author_name": "T", "author_email": "no-at-sign"},
            ]
            for args in cases:
                before = len(runtime.calls)
                result = await service.execute_git_operation(
                    ws_id, "commit", repo_arg, dict(args)
                )
                self.assertFalse(result["ok"], msg=str(args))
                self.assertEqual(result["code"], "invalid_argument", msg=str(args))
                commit_calls = [
                    c
                    for c in runtime.calls[before:]
                    if (git_ops.unwrap_git_exec_argv(c[0]) or c[0])[:2]
                    == ["git", "commit"]
                ]
                self.assertEqual(commit_calls, [], msg=str(args))

    async def test_askpass_uses_persistent_token_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, runtime, ws_id = _service_for(tmp)
            await service.execute_git_operation(
                ws_id,
                "working_diff",
                _ws_repo(tmp, "repo"),
                {},
            )
            probes = [
                argv
                for argv, _w, _e in runtime.calls
                if argv[:3] == ["sh", "-c", "test -s /root/.opencuria-env.sh"]
                or (
                    len(argv) == 4
                    and argv[:2] == ["sh", "-c"]
                    and "GITHUB_TOKEN" in argv[2]
                )
            ]
            self.assertTrue(probes)
            for argv in probes:
                self.assertIn("/root/.opencuria-env.sh", " ".join(argv))

    async def test_askpass_created_outside_workspace_and_removed(self) -> None:
        created: list[str] = []
        created_bodies: list[str] = []
        created_envs: list[dict] = []
        removed: list[str] = []

        class AskpassRuntime(FakeGitRuntime):
            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                argv = [str(c) for c in command]
                self.calls.append((argv, workdir, dict(env or {})))
                inner = git_ops.unwrap_git_exec_argv(argv) or argv
                if inner[:3] == ["sh", "-c", "test -s /root/.opencuria-env.sh"]:
                    return 0, ""
                if inner[:2] == ["sh", "-lc"] and "mktemp" in inner[2]:
                    created.append("/tmp/opencuria-git-askpass-TEST.sh")
                    created_bodies.append(inner[2])
                    created_envs.append(dict(env or {}))
                    return 0, "/tmp/opencuria-git-askpass-TEST.sh\n"
                if inner[:2] == ["sh", "-c"] and "mktemp" in inner[2]:
                    created.append("/tmp/opencuria-git-askpass-TEST.sh")
                    created_bodies.append(inner[2])
                    created_envs.append(dict(env or {}))
                    return 0, "/tmp/opencuria-git-askpass-TEST.sh\n"
                if inner[:2] == ["rm", "-f"]:
                    removed.extend(inner[2:])
                    return 0, ""
                if (
                    len(inner) == 4
                    and inner[:2] == ["sh", "-c"]
                    and "GITHUB_TOKEN" in inner[2]
                ):
                    # Persistent-token probe: pretend a token exists so the
                    # askpass path is exercised (no token value in argv).
                    return 0, ""
                return await super().exec_command_wait(
                    instance_id, command, workdir, env
                )

        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            runtime = AskpassRuntime(tmp)
            service = WorkspaceService(
                runtimes={"docker": runtime}, settings=RunnerSettings()
            )
            ws_id = uuid.uuid4()
            service._cache[ws_id] = WorkspaceInfo(
                workspace_id=ws_id,
                instance_id="instance-1",
                status="running",
                runtime_type="docker",
            )
            await service.execute_git_operation(
                ws_id,
                "working_diff",
                _ws_repo(tmp, "repo"),
                {},
            )
            self.assertTrue(created)
            for path in created:
                self.assertTrue(path.startswith("/tmp/"))
                self.assertFalse(path.startswith("/workspace"))
                self.assertIn(path, removed)
            # Only GIT_ASKPASS is exported; SSH never gets the PAT script.
            git_envs = [env for _a, _w, env in runtime.calls if env]
            self.assertTrue(git_envs)
            for env in git_envs:
                self.assertNotIn("SSH_ASKPASS", env)
                self.assertNotIn("SSH_ASKPASS_REQUIRE", env)
            askpass_envs = [env for env in git_envs if "GIT_ASKPASS" in env]
            self.assertTrue(askpass_envs)
            for env in askpass_envs:
                self.assertTrue(env["GIT_ASKPASS"].startswith("/tmp/"))
            # The created script answers only GitHub prompts; unknown
            # prompts exit nonzero with no token.  `created_bodies` holds
            # the full `sh -c` constructor command: assert the embedded
            # fixed askpass body (single-quoted) carries the strict tail,
            # and that no catch-all token echo remains.
            self.assertTrue(created_bodies)
            for body in created_bodies:
                self.assertIn("exit 1", body)
                self.assertIn(git_ops.GIT_ASKPASS_SCRIPT_BODY.split(";;")[0][:40], body)

    async def test_missing_git_structured(self) -> None:
        class NoGitRuntime:
            def __init__(self) -> None:
                self.calls: list[tuple[list[str], str | None, dict | None]] = []

            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                argv = [str(c) for c in command]
                self.calls.append((argv, workdir, dict(env or {})))
                inner = git_ops.unwrap_git_exec_argv(argv) or argv
                if inner[:1] == ["git"]:
                    return 127, "git: command not found\n"
                if inner[:2] == ["realpath", "-m"]:
                    return 0, inner[2] + "\n"
                return 0, ""

        with tempfile.TemporaryDirectory():
            service = WorkspaceService(
                runtimes={"docker": NoGitRuntime()}, settings=RunnerSettings()
            )
            ws_id = uuid.uuid4()
            service._cache[ws_id] = WorkspaceInfo(
                workspace_id=ws_id,
                instance_id="instance-1",
                status="running",
                runtime_type="docker",
            )
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/repo", {}
            )
            # Resolution itself needs git; any structured failure is fine.
            self.assertFalse(result["ok"])
            self.assertIn("code", result)

    async def test_pull_branch_without_remote_invalid_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, runtime, ws_id = _service_for(tmp)
            before = len(runtime.calls)
            result = await service.execute_git_operation(
                ws_id, "pull", _ws_repo(tmp, "repo"), {"branch": "main"}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "invalid_argument")
            self.assertIn("remote", result["message"])
            self.assertEqual(runtime.calls[before:], [])

    async def test_not_found_text_without_missing_exit_stays_normal(self) -> None:
        class NotFoundTextRuntime:
            def __init__(self, root: str) -> None:
                self.root = root
                self.calls: list = []

            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                argv = [str(c) for c in command]
                self.calls.append((argv, workdir, dict(env or {})))
                inner = git_ops.unwrap_git_exec_argv(argv) or argv
                if inner[:1] == ["git"]:
                    # Successful git output mentioning "not found" plus a
                    # real repo file literally named with that phrase must
                    # never become missing_git.
                    return 0, "warning: path 'not found notes.txt' has no upstream\n"
                if inner[:2] == ["realpath", "-m"]:
                    return 0, inner[2] + "\n"
                return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            Path(os.path.join(repo, "not found notes.txt")).write_text("x\n")
            service = WorkspaceService(
                runtimes={"docker": NotFoundTextRuntime(tmp)},
                settings=RunnerSettings(),
            )
            ws_id = uuid.uuid4()
            service._cache[ws_id] = WorkspaceInfo(
                workspace_id=ws_id,
                instance_id="instance-1",
                status="running",
                runtime_type="docker",
            )
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/repo", {}
            )
            # Exit 0 with "not found" text is a normal result, not missing_git.
            self.assertNotEqual(result.get("code"), "missing_git")

    async def test_exit1_not_found_text_stays_normal(self) -> None:
        class Exit1Runtime:
            def __init__(self) -> None:
                self.calls: list = []

            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                argv = [str(c) for c in command]
                self.calls.append((argv, workdir, dict(env or {})))
                inner = git_ops.unwrap_git_exec_argv(argv) or argv
                if inner[:1] == ["git"]:
                    return 1, "error: path 'not found notes.txt' did not match\n"
                if inner[:2] == ["realpath", "-m"]:
                    return 0, inner[2] + "\n"
                return 0, ""

        with tempfile.TemporaryDirectory():
            service = WorkspaceService(
                runtimes={"docker": Exit1Runtime()}, settings=RunnerSettings()
            )
            ws_id = uuid.uuid4()
            service._cache[ws_id] = WorkspaceInfo(
                workspace_id=ws_id,
                instance_id="instance-1",
                status="running",
                runtime_type="docker",
            )
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/repo", {}
            )
            # Exit 1 with "not found" text is a normal git failure, not missing_git.
            self.assertNotEqual(result.get("code"), "missing_git")

    async def test_missing_binary_127_stays_missing_git(self) -> None:
        class NoGitRuntime127:
            def __init__(self) -> None:
                self.calls: list = []

            async def exec_command_wait(
                self, instance_id, command, workdir=None, env=None
            ):
                argv = [str(c) for c in command]
                self.calls.append((argv, workdir, dict(env or {})))
                inner = git_ops.unwrap_git_exec_argv(argv) or argv
                if inner[:1] == ["git"]:
                    return 127, "git: command not found\n"
                if inner[:2] == ["realpath", "-m"]:
                    return 0, inner[2] + "\n"
                return 0, ""

        with tempfile.TemporaryDirectory():
            service = WorkspaceService(
                runtimes={"docker": NoGitRuntime127()},
                settings=RunnerSettings(),
            )
            ws_id = uuid.uuid4()
            service._cache[ws_id] = WorkspaceInfo(
                workspace_id=ws_id,
                instance_id="instance-1",
                status="running",
                runtime_type="docker",
            )
            result = await service.execute_git_operation(
                ws_id, "working_diff", "/workspace/repo", {}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result.get("code"), "missing_git")


class GitSerialisationTests(unittest.IsolatedAsyncioTestCase):
    async def test_operations_serialised_per_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, runtime, ws_id = _service_for(tmp)
            runtime.hold_release = asyncio.Event()
            repo_arg = _ws_repo(tmp, "repo")
            first = asyncio.create_task(
                service.execute_git_operation(ws_id, "working_diff", repo_arg, {})
            )
            await asyncio.sleep(0.2)
            second = asyncio.create_task(
                service.execute_git_operation(ws_id, "working_diff", repo_arg, {})
            )
            await asyncio.sleep(0.2)
            # The second op must wait behind the first's status call.
            self.assertEqual(runtime.max_entered, 1)
            runtime.hold_release.set()
            results = await asyncio.gather(first, second)
            self.assertTrue(all(r["ok"] for r in results))


# ---------------------------------------------------------------------------
# Websocket handler tests.
# ---------------------------------------------------------------------------


def _ws_interface(service) -> WebSocketInterface:
    interface = WebSocketInterface(service, RunnerSettings())
    interface._sio.emit = AsyncMock()
    return interface


class GitWebsocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_git_operation_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(os.path.join(tmp, "repo"))
            service, _runtime, ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "git-1",
                    "operation": "working_diff",
                    "repo_path": "/workspace/repo",
                    "args": {},
                }
            )
            task = interface._running_tasks.get("git:git-1")
            if task is not None:
                await task
            event, payload = interface._sio.emit.await_args.args
            self.assertEqual(event, "git:operation_result")
            self.assertEqual(payload["request_id"], "git-1")
            self.assertEqual(payload["workspace_id"], str(ws_id))
            self.assertTrue(payload["ok"])
            self.assertIn("diff", payload)
            self.assertNotIn("git:git-1", interface._running_tasks)

    async def test_git_operation_invalid_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _runtime, _ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            await handler(
                {
                    "workspace_id": "not-a-uuid",
                    "request_id": "git-bad",
                    "operation": "list_repos",
                }
            )
            event, payload = interface._sio.emit.await_args.args
            self.assertEqual(event, "git:operation_result")
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "invalid_workspace_id")

    async def test_git_operation_service_error_still_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _runtime, ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "git-err",
                    "operation": "working_diff",
                    "repo_path": "/workspace/does-not-exist",
                    "args": {},
                }
            )
            task = interface._running_tasks.get("git:git-err")
            if task is not None:
                await task
            event, payload = interface._sio.emit.await_args.args
            self.assertEqual(event, "git:operation_result")
            self.assertFalse(payload["ok"])
            self.assertIn("code", payload)

    async def test_git_operation_cancel(self) -> None:
        service = WorkspaceService(runtimes={}, settings=RunnerSettings())

        async def _slow(*args, **kwargs):
            await asyncio.sleep(30)
            return {"ok": True}  # pragma: no cover

        service.execute_git_operation = _slow  # type: ignore[assignment]
        interface = _ws_interface(service)
        ws_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["git:operation"]
        await handler(
            {
                "workspace_id": str(ws_id),
                "request_id": "git-cancel",
                "operation": "fetch",
                "repo_path": "/workspace/repo",
                "args": {},
            }
        )
        self.assertIn("git:git-cancel", interface._running_tasks)
        cancel = interface._sio.handlers["/"]["harness:cancel"]
        await cancel({"request_id": "git-cancel"})
        task = interface._running_tasks.get("git:git-cancel")
        if task is not None:  # pragma: no branch
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertNotIn("git:git-cancel", interface._running_tasks)

    async def test_git_operation_network_timeout_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, runtime, ws_id = _service_for(tmp)
            runtime.delay["git fetch"] = 0.05
            interface = _ws_interface(service)
            # Handler exists and routes network ops through the service with
            # the extended timeout budget.
            self.assertIn("git:operation", interface._sio.handlers["/"])
            self.assertGreaterEqual(git_ops.git_timeout_for("fetch"), 60.0)
            self.assertLessEqual(git_ops.git_timeout_for("repo_snapshot"), 60.0)
            self.assertGreaterEqual(
                git_ops.git_timeout_for("checkout_remote_branch"), 60.0
            )
            self.assertTrue(git_ops.is_network_operation("checkout_remote_branch"))
            self.assertFalse(git_ops.is_network_operation("repo_snapshot"))


class GitCheckoutRemoteBranchTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkout_remote_simple_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin")
            subprocess.run(
                ["git", "init", "-b", "main", origin], check=True, capture_output=True
            )
            _git_config_identity(origin)
            Path(origin, "README.md").write_text("# origin\n")
            subprocess.run(["git", "-C", origin, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", origin, "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", origin, "checkout", "-b", "shared"],
                check=True,
                capture_output=True,
            )
            Path(origin, "s.txt").write_text("s\n")
            subprocess.run(["git", "-C", origin, "add", "s.txt"], check=True)
            subprocess.run(
                ["git", "-C", origin, "commit", "-m", "shared tip"],
                check=True,
                capture_output=True,
            )
            tip = subprocess.run(
                ["git", "-C", origin, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", origin, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            clone = os.path.join(tmp, "clone")
            subprocess.run(
                ["git", "clone", origin, clone], check=True, capture_output=True
            )
            _git_config_identity(clone)
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id,
                "checkout_remote_branch",
                _ws_repo(tmp, "clone"),
                {"remote_ref": "origin/shared"},
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["snapshot"]["current_branch"], "shared")
            self.assertEqual(result["snapshot"]["head_hash"], tip)
            self.assertTrue(
                any(
                    b["name"] == "shared"
                    for b in result["snapshot"]["branches"]
                )
            )
            # Idempotent: existing local branch just checks out.
            again = await service.execute_git_operation(
                ws_id,
                "checkout_remote_branch",
                _ws_repo(tmp, "clone"),
                {"remote_ref": "origin/shared"},
            )
            self.assertTrue(again["ok"], again)
            self.assertEqual(again["snapshot"]["current_branch"], "shared")
            # Explicit local_name override.
            renamed = await service.execute_git_operation(
                ws_id,
                "checkout_remote_branch",
                _ws_repo(tmp, "clone"),
                {"remote_ref": "origin/shared", "local_name": "local-copy"},
            )
            self.assertTrue(renamed["ok"], renamed)
            self.assertEqual(renamed["snapshot"]["current_branch"], "local-copy")

    async def test_checkout_remote_hierarchical_branch(self) -> None:
        """Regression: remote refs with slashes (origin/feat/x) must check out."""
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin")
            subprocess.run(
                ["git", "init", "-b", "main", origin], check=True, capture_output=True
            )
            _git_config_identity(origin)
            Path(origin, "README.md").write_text("# origin\n")
            subprocess.run(["git", "-C", origin, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", origin, "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", origin, "checkout", "-b", "feat/nested"],
                check=True,
                capture_output=True,
            )
            Path(origin, "n.txt").write_text("n\n")
            subprocess.run(["git", "-C", origin, "add", "n.txt"], check=True)
            subprocess.run(
                ["git", "-C", origin, "commit", "-m", "nested tip"],
                check=True,
                capture_output=True,
            )
            tip = subprocess.run(
                ["git", "-C", origin, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", origin, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            clone = os.path.join(tmp, "clone")
            subprocess.run(
                ["git", "clone", origin, clone], check=True, capture_output=True
            )
            _git_config_identity(clone)
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id,
                "checkout_remote_branch",
                _ws_repo(tmp, "clone"),
                {"remote_ref": "origin/feat/nested"},
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["snapshot"]["current_branch"], "feat/nested")
            self.assertEqual(result["snapshot"]["head_hash"], tip)
            self.assertTrue(
                any(
                    b["name"] == "feat/nested"
                    for b in result["snapshot"]["branches"]
                )
            )

    async def test_checkout_remote_unknown_and_invalid_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(os.path.join(tmp, "repo"))
            subprocess.run(
                ["git", "-C", repo, "remote", "add", "origin", os.path.join(tmp, "repo")],
                check=True,
                capture_output=True,
            )
            service, runtime, ws_id = _service_for(tmp)
            repo_arg = _ws_repo(tmp, "repo")
            missing = await service.execute_git_operation(
                ws_id,
                "checkout_remote_branch",
                repo_arg,
                {"remote_ref": "origin/does-not-exist"},
            )
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["code"], "unknown_branch")
            for bad in ("noslash", "-origin/x", "", "origin/"):
                before = len(runtime.calls)
                bad_result = await service.execute_git_operation(
                    ws_id,
                    "checkout_remote_branch",
                    repo_arg,
                    {"remote_ref": bad},
                )
                self.assertFalse(bad_result["ok"], msg=bad)
                self.assertEqual(bad_result["code"], "invalid_argument", msg=bad)
                fetch_calls = [
                    c
                    for c in runtime.calls[before:]
                    if (git_ops.unwrap_git_exec_argv(c[0]) or c[0])[:2]
                    == ["git", "fetch"]
                ]
                self.assertEqual(fetch_calls, [], msg=bad)

    async def test_checkout_remote_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin")
            subprocess.run(
                ["git", "init", "-b", "main", origin], check=True, capture_output=True
            )
            _git_config_identity(origin)
            Path(origin, "README.md").write_text("# origin\n")
            subprocess.run(["git", "-C", origin, "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", origin, "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", origin, "checkout", "-b", "other"],
                check=True,
                capture_output=True,
            )
            Path(origin, "README.md").write_text("# other\n")
            subprocess.run(["git", "-C", origin, "commit", "-am", "other"],
                           check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", origin, "checkout", "main"],
                check=True,
                capture_output=True,
            )
            clone = os.path.join(tmp, "clone")
            subprocess.run(
                ["git", "clone", origin, clone], check=True, capture_output=True
            )
            _git_config_identity(clone)
            # Dirty tracked modification that checkout would overwrite.
            Path(clone, "README.md").write_text("# dirty-local\n")
            service, _runtime, ws_id = _service_for(tmp)
            result = await service.execute_git_operation(
                ws_id,
                "checkout_remote_branch",
                _ws_repo(tmp, "clone"),
                {"remote_ref": "origin/other"},
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "dirty_worktree")

    async def test_remote_ref_validators(self) -> None:
        self.assertEqual(git_ops.validate_remote_ref("origin/foo"), "origin/foo")
        self.assertEqual(git_ops.validate_remote_ref("  origin/foo  "), "origin/foo")
        self.assertEqual(
            git_ops.validate_remote_ref("origin/feat/harness-bestof-hardening"),
            "origin/feat/harness-bestof-hardening",
        )
        for bad in ("noslash", "-origin/x", "", "origin/", "/x", "x" * 256):
            with self.assertRaises(ValueError, msg=bad):
                git_ops.validate_remote_ref(bad)
        self.assertIsNone(git_ops.validate_optional_branch(None))
        self.assertEqual(git_ops.validate_optional_branch("main"), "main")
        with self.assertRaises(ValueError):
            git_ops.validate_optional_branch("-bad")


class GitWebsocketValidationTests(unittest.IsolatedAsyncioTestCase):
    """Trust-boundary validation for the git:operation websocket handler.

    No real git/network: service fakes only.
    """

    async def test_missing_request_id_logs_only_no_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _runtime, ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "operation": "list_repos",
                    "args": {},
                }
            )
            interface._sio.emit.assert_not_awaited()
            self.assertEqual(interface._running_tasks, {})

    async def test_empty_request_id_logs_only_no_task_key_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _runtime, ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "   ",
                    "operation": "list_repos",
                }
            )
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "",
                    "operation": "list_repos",
                }
            )
            interface._sio.emit.assert_not_awaited()
            # Empty IDs must never register a "git:" task key.
            self.assertNotIn("git:", interface._running_tasks)
            self.assertEqual(interface._running_tasks, {})

    async def test_missing_operation_structured_invalid_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _runtime, ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "git-no-op",
                    "args": {},
                }
            )
            event, payload = interface._sio.emit.await_args.args
            self.assertEqual(event, "git:operation_result")
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "invalid_request")
            self.assertEqual(payload["request_id"], "git-no-op")
            self.assertEqual(payload["workspace_id"], str(ws_id))
            self.assertIn("operation", payload)
            self.assertEqual(interface._running_tasks, {})

    async def test_unknown_operation_structured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, runtime, ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            before = len(runtime.calls)
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "git-unknown",
                    "operation": "rm -rf",
                    "args": {},
                }
            )
            event, payload = interface._sio.emit.await_args.args
            self.assertEqual(event, "git:operation_result")
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "unknown_operation")
            self.assertEqual(payload["request_id"], "git-unknown")
            self.assertEqual(payload["operation"], "rm -rf")
            self.assertEqual(payload["workspace_id"], str(ws_id))
            # Rejected before any task/git argv runs.
            self.assertEqual(runtime.calls[before:], [])
            self.assertEqual(interface._running_tasks, {})

    async def test_non_dict_args_structured_invalid_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, runtime, ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            before = len(runtime.calls)
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "git-bad-args",
                    "operation": "list_repos",
                    "args": ["history_limit", 2],
                }
            )
            event, payload = interface._sio.emit.await_args.args
            self.assertEqual(event, "git:operation_result")
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "invalid_argument")
            self.assertEqual(payload["request_id"], "git-bad-args")
            self.assertEqual(payload["operation"], "list_repos")
            self.assertEqual(payload["workspace_id"], str(ws_id))
            self.assertEqual(runtime.calls[before:], [])
            self.assertEqual(interface._running_tasks, {})

    async def test_non_string_repo_path_structured_invalid_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, runtime, ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            before = len(runtime.calls)
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "git-bad-repo",
                    "operation": "working_diff",
                    "repo_path": {"path": "/workspace/repo"},
                    "args": {},
                }
            )
            event, payload = interface._sio.emit.await_args.args
            self.assertEqual(event, "git:operation_result")
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "invalid_argument")
            self.assertEqual(payload["request_id"], "git-bad-repo")
            self.assertEqual(runtime.calls[before:], [])
            self.assertEqual(interface._running_tasks, {})

    async def test_duplicate_request_id_rejected_conflict(self) -> None:
        service = WorkspaceService(runtimes={}, settings=RunnerSettings())

        async def _slow(*args, **kwargs):
            await asyncio.sleep(30)
            return {"ok": True}  # pragma: no cover

        service.execute_git_operation = _slow  # type: ignore[assignment]
        interface = _ws_interface(service)
        ws_id = uuid.uuid4()
        handler = interface._sio.handlers["/"]["git:operation"]
        await handler(
            {
                "workspace_id": str(ws_id),
                "request_id": "git-dup",
                "operation": "fetch",
                "repo_path": "/workspace/repo",
                "args": {},
            }
        )
        first = interface._running_tasks.get("git:git-dup")
        self.assertIsNotNone(first)
        # Second request with the same ID must not overwrite the task
        # (cancel ambiguity) — it gets a structured conflict instead.
        await handler(
            {
                "workspace_id": str(ws_id),
                "request_id": "git-dup",
                "operation": "fetch",
                "repo_path": "/workspace/repo",
                "args": {},
            }
        )
        self.assertIs(interface._running_tasks.get("git:git-dup"), first)
        event, payload = interface._sio.emit.await_args.args
        self.assertEqual(event, "git:operation_result")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "conflict")
        self.assertEqual(payload["request_id"], "git-dup")
        self.assertEqual(payload["operation"], "fetch")
        # Cleanup: cancel still resolves to no leak and no final result.
        cancel = interface._sio.handlers["/"]["harness:cancel"]
        await cancel({"request_id": "git-dup"})
        task = interface._running_tasks.get("git:git-dup")
        if task is not None:  # pragma: no branch
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertNotIn("git:git-dup", interface._running_tasks)

    async def test_result_echo_keys_cannot_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _runtime, ws_id = _service_for(tmp)
            other_id = uuid.uuid4()

            async def _evil(*args, **kwargs):
                return {
                    "ok": True,
                    "workspace_id": str(other_id),
                    "request_id": "evil",
                    "operation": "push",
                    "diff": {},
                }

            service.execute_git_operation = _evil  # type: ignore[assignment]
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "git-echo",
                    "operation": "working_diff",
                    "repo_path": "/workspace/repo",
                    "args": {},
                }
            )
            task = interface._running_tasks.get("git:git-echo")
            if task is not None:
                await task
            event, payload = interface._sio.emit.await_args.args
            self.assertEqual(event, "git:operation_result")
            self.assertEqual(payload["request_id"], "git-echo")
            self.assertEqual(payload["workspace_id"], str(ws_id))
            self.assertEqual(payload["operation"], "working_diff")

    async def test_harness_cancel_empty_request_id_no_touch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _runtime, ws_id = _service_for(tmp)
            interface = _ws_interface(service)
            handler = interface._sio.handlers["/"]["git:operation"]
            await handler(
                {
                    "workspace_id": str(ws_id),
                    "request_id": "git-keep",
                    "operation": "list_repos",
                    "args": {},
                }
            )
            task = interface._running_tasks.get("git:git-keep")
            self.assertIsNotNone(task)
            cancel = interface._sio.handlers["/"]["harness:cancel"]
            await cancel({"request_id": ""})
            await cancel({})
            # Empty cancels must not pop unrelated task keys.
            self.assertIn("git:git-keep", interface._running_tasks)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


class GitOutputRedactionTests(unittest.TestCase):
    def test_error_payload_never_leaks_token(self) -> None:
        err = git_ops.GitError(
            "network_failed",
            "push failed",
            exit_code=1,
            stderr="remote: https://oauth2:ghp_leaked@github.com/x.git denied",
        )
        payload = git_ops.git_error_payload(err)
        blob = str(payload)
        self.assertNotIn("ghp_leaked", blob)

    def test_error_payload_redacts_generic_userinfo_url(self) -> None:
        err = git_ops.GitError(
            "network_failed",
            "fetch failed",
            exit_code=1,
            stderr="fatal: https://ci-user:random-secret-987@git.example.com/o/r.git denied",
        )
        payload = git_ops.git_error_payload(err)
        self.assertNotIn("random-secret-987", payload["stderr"])
        self.assertIn("https://***@git.example.com/o/r.git", payload["stderr"])


if __name__ == "__main__":
    unittest.main()
