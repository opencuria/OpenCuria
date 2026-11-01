"""Git reads return metadata to the app and bounded patches to commit suggestions."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from src import git as git_ops
from src.models import WorkspaceInfo
from src.services.git_service import GitService
from tests.test_git_operations import FakeGitRuntime, _inner_argv, _make_repo, _run_git
from tests.test_git_operations import setup_module as isolate_git
from tests.test_git_operations import teardown_module as restore_git


@pytest.fixture(scope="module", autouse=True)
def isolated_git_environment() -> Iterator[None]:
    """Use disposable repositories without ambient Git configuration."""
    isolate_git()
    try:
        yield
    finally:
        restore_git()


def _git(repo: Path, *args: str) -> str:
    """Run a successful local Git command and return its output."""
    code, output = _run_git(["git", *args], str(repo))
    assert code == 0, output
    return output


@pytest.fixture
def service(tmp_path: Path) -> tuple[Path, GitService, FakeGitRuntime, uuid.UUID]:
    """Exercise the public runner operation over real disposable repositories."""
    repo = tmp_path / "repo"
    _make_repo(str(repo))
    runtime = FakeGitRuntime(str(tmp_path))
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(
        workspace_id=workspace_id, instance_id="instance-1", status="running"
    )
    return repo, GitService(lambda _: info, lambda _: runtime), runtime, workspace_id


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("quote_paths", ["true", "false"])
@pytest.mark.parametrize("include_patch", [False, True])
def test_combined_diff_preserves_paths_and_metadata(
    service, cached: bool, quote_paths: str, include_patch: bool
) -> None:
    """Renames, binary/mode changes, deletes and metadata-like paths stay intact."""
    repo, _, _, _ = service
    _git(repo, "config", "core.quotepath", quote_paths)
    names = [
        "old name.txt",
        "delete with spaces.txt",
        "mode.py",
        ":100644 100644 x y M",
        "1\t2\tname\nwith newline",
        'quote"\\ä.txt',
        "a file with spaces.txt",
        "another file with spaces.txt",
    ]
    for name in names:
        (repo / name).write_text(f"before {name!r}\n" * 20)
    (repo / "binary.bin").write_bytes(bytes(range(256)))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixtures")

    (repo / names[0]).rename(repo / "new\tname\nwith spaces.txt")
    (repo / names[1]).unlink()
    (repo / names[2]).chmod(0o755)
    for index, name in enumerate(names[3:]):
        (repo / name).write_text(f"before {name!r}\n" * 20 + f"after-{index}\n")
    (repo / "binary.bin").write_bytes(bytes(range(255, -1, -1)))
    for name in ("empty1.txt", "empty2.txt"):
        (repo / name).touch()
    (repo / "link").symlink_to("README.md")
    if cached:
        _git(repo, "add", "-A")
    args = (["--cached"] if cached else []) + (["--patch"] if include_patch else [])
    output = _git(repo, "diff", "--raw", "--numstat", "-z", *args)

    actual = git_ops.parse_diff_z(output)

    by_path = {row["new_path"]: row for row in actual}
    assert by_path[names[1]]["status"] == "D"
    assert by_path[names[1]]["deletions"] == 20
    if include_patch:
        assert "-before" in by_path[names[1]]["patch"]
    else:
        assert all("patch" not in row for row in actual)
    assert by_path[names[2]]["additions"] == 0
    assert by_path[names[2]]["deletions"] == 0
    for index, name in enumerate(names[3:]):
        assert by_path[name]["additions"] == 1
        if include_patch:
            assert [
                line
                for line in by_path[name]["patch"].splitlines()
                if line.startswith("+after")
            ] == [f"+after-{index}"]
    assert {row["new_path"] for row in actual} >= set(names[3:])
    if cached:
        renamed = next(row for row in actual if row["status"] == "R")
        assert renamed["old_path"] == names[0]
        assert renamed["new_path"] == "new\tname\nwith spaces.txt"
        if include_patch:
            assert "rename to" in renamed["patch"]
        for name in ("empty1.txt", "empty2.txt"):
            assert by_path[name]["status"] == "A"
            assert by_path[name]["additions"] == by_path[name]["deletions"] == 0
    assert next(row for row in actual if row["new_path"] == "binary.bin")["binary"]


@pytest.mark.parametrize("copy", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_combined_diff_renames_and_copies(service, copy: bool, changed: bool) -> None:
    """Pure and edited renames/copies retain exact paths, counts and patch text."""
    repo, _, _, _ = service
    old, new = "old short.txt", "new name with many words.txt"
    original = "same content\n" * 20
    (repo / old).write_text(original)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "original")
    (repo / new).write_text(original + ("more\n" if changed else ""))
    if not copy:
        (repo / old).unlink()
    _git(repo, "add", "-A")

    rows = git_ops.parse_diff_z(
        _git(
            repo,
            "diff",
            "--cached",
            "--raw",
            "--numstat",
            "--patch",
            "-z",
            "-C",
            "--find-copies-harder",
        )
    )

    assert len(rows) == 1
    row = rows[0]
    assert (row["old_path"], row["new_path"]) == (old, new)
    assert row["status"] == ("C" if copy else "R")
    assert row["additions"] == int(changed)
    assert row["deletions"] == 0
    assert ("+more" in row["patch"]) == changed


@pytest.mark.parametrize(
    "malformed",
    [
        "bogus\x00",
        "x\ty\tbad.txt\x00",
        "1\t0\t\x00only-old\x00",
        "1\t0\t\x00\x00new.txt\x00",
    ],
)
def test_malformed_statistics_do_not_replace_raw_metadata(malformed: str) -> None:
    """Incomplete or invalid statistics never create a file or lose raw status."""
    rows = git_ops.parse_diff_z(":100644 100644 a b M\x00real.txt\x00" + malformed)
    assert rows == [
        {
            "old_path": "real.txt",
            "new_path": "real.txt",
            "status": "M",
            "additions": 0,
            "deletions": 0,
            "binary": False,
        }
    ]


@pytest.mark.parametrize(
    "patch",
    [
        "diff --git a/a b c d/e\nold mode 100644\nnew mode 100755\n",
        'diff --git "a/broken "b/fixed"\nindex 123..456 100644\n'
        '--- "a/broken\n+++ "b/fixed"\n@@ -1 +1 @@\n-a\n+b\n',
    ],
)
def test_ambiguous_patch_paths_keep_metadata_without_misattribution(patch: str) -> None:
    """Unparseable patches cannot invent paths or hide valid file statistics."""
    rows = git_ops.parse_diff_z(
        ":100644 100644 a b M\x00real.txt\x001\t1\treal.txt\x00\x00" + patch
    )
    assert rows == [
        {
            "old_path": "real.txt",
            "new_path": "real.txt",
            "status": "M",
            "additions": 1,
            "deletions": 1,
            "binary": False,
        }
    ]


def test_combined_diff_preserves_limits(service) -> None:
    """Combining streams retains the per-file byte and per-response patch caps."""
    repo, _, _, _ = service
    for index in range(git_ops.GIT_MAX_DIFF_FILES_WITH_PATCH + 2):
        (repo / f"file-{index:03}.txt").write_text("new\n")
    (repo / "000-large.txt").write_text("large\n" * git_ops.GIT_MAX_DIFF_BYTES)
    _git(repo, "add", ".")
    output = _git(repo, "diff", "--cached", "--raw", "--numstat", "--patch", "-z")

    actual = git_ops.parse_diff_z(output)

    assert actual[0]["patch"].endswith("…[truncated]")
    assert len(actual[0]["patch"].encode()) < git_ops.GIT_MAX_DIFF_BYTES + 32
    assert (
        sum("patch" in row for row in actual) == git_ops.GIT_MAX_DIFF_FILES_WITH_PATCH
    )
    assert "patch" not in actual[-1]
    assert actual[-1]["additions"] == 1


def test_empty_combined_diff() -> None:
    """An unchanged tree has no metadata or patch separator."""
    assert git_ops.parse_diff_z("") == []


@pytest.mark.parametrize("files", [1, 20, 101])
async def test_working_diff_guest_exec_budget(service, files: int) -> None:
    """A mixed working diff needs one traversal per side, independent of files."""
    repo, git, runtime, workspace_id = service
    for index in range(files):
        (repo / f"file-{index}.txt").write_text("staged\n")
    _git(repo, "add", ".")
    for index in range(files):
        (repo / f"file-{index}.txt").write_text("unstaged\n")

    result = await git.execute_git_operation(
        workspace_id, "working_diff", "/workspace/repo"
    )

    assert result["ok"], result
    for side in ("staged", "unstaged"):
        rows = result["diff"][side]
        assert len(rows) == files
        assert all(row["additions"] == 1 for row in rows)
        assert all(row["deletions"] == (side == "unstaged") for row in rows)
        assert sum("patch" in row for row in rows) == min(
            files, git_ops.GIT_MAX_DIFF_FILES_WITH_PATCH
        )
        assert all(f"+{side}" in row["patch"] for row in rows if "patch" in row)
    diffs = [
        _inner_argv(call)
        for call in runtime.calls
        if _inner_argv(call)[:2] == ["git", "diff"]
    ]
    assert len(diffs) == 2
    assert len(runtime.calls) == 7  # Includes auth, status and containment probes.


@pytest.mark.parametrize("kind", ["root", "regular", "merge"])
async def test_commit_details_use_one_diff(service, kind: str) -> None:
    """Root, regular and merge commits retain their first-parent diff semantics."""
    repo, git, runtime, workspace_id = service
    root = _git(repo, "rev-parse", "HEAD").strip()
    if kind != "root":
        (repo / "second.txt").write_text("second\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "second")
    if kind == "merge":
        _git(repo, "checkout", "-b", "feature", root)
        (repo / "feature.txt").write_text("feature\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "feature")
        _git(repo, "checkout", "main")
        _git(repo, "merge", "--no-ff", "feature", "-m", "merge")
    head = _git(repo, "rev-parse", "HEAD").strip()
    result = await git.execute_git_operation(
        workspace_id, "commit_details", "/workspace/repo", {"commit": head}
    )

    assert result["ok"], result
    expected_path = {
        "root": "README.md",
        "regular": "second.txt",
        "merge": "feature.txt",
    }[kind]
    rows = result["details"]["file_changes"]
    assert rows == [
        {
            "old_path": expected_path,
            "new_path": expected_path,
            "status": "A",
            "additions": 1,
            "deletions": 0,
            "binary": False,
        }
    ]
    assert len([call for call in runtime.calls if "--raw" in _inner_argv(call)]) == 1
    assert all("--patch" not in _inner_argv(call) for call in runtime.calls)


async def test_working_diff_preserves_conflicts(service) -> None:
    """Unmerged metadata stays available alongside ordinary file patches."""
    repo, git, _, workspace_id = service
    (repo / "regular.txt").write_text("before\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "regular file")
    _git(repo, "checkout", "-b", "feature")
    (repo / "README.md").write_text("feature\n")
    _git(repo, "commit", "-am", "feature")
    _git(repo, "checkout", "main")
    (repo / "README.md").write_text("main\n")
    _git(repo, "commit", "-am", "main")
    code, _ = _run_git(["git", "merge", "feature"], str(repo))
    assert code == 1
    (repo / "regular.txt").write_text("staged\n")
    _git(repo, "add", "regular.txt")
    (repo / "regular.txt").write_text("unstaged\n")

    result = await git.execute_git_operation(
        workspace_id, "working_diff", "/workspace/repo"
    )

    assert result["ok"], result
    staged = result["diff"]["staged"]
    assert [
        (row["new_path"], row["additions"], row["deletions"]) for row in staged
    ] == [("README.md", 0, 0), ("regular.txt", 1, 1)]
    unstaged = {row["new_path"]: row for row in result["diff"]["unstaged"]}
    assert unstaged["README.md"]["status"] == "M"
    assert "+unstaged" in unstaged["regular.txt"]["patch"]


async def test_combined_diff_failure_contract(service, monkeypatch) -> None:
    """Working diff errors are redacted; commit detail diff reads stay best effort."""
    repo, git, runtime, workspace_id = service
    (repo / "README.md").write_text("changed\n")
    original_exec = runtime.exec_command_wait

    async def fail_diff(*args: object, **kwargs: object) -> tuple[int, str]:
        command = kwargs["command"]
        inner = git_ops.unwrap_git_exec_argv(command) or command
        if "--raw" in inner:
            return 128, "failed https://user:secret@example.test/repo"
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(runtime, "exec_command_wait", fail_diff)
    failed = await git.execute_git_operation(
        workspace_id, "working_diff", "/workspace/repo"
    )
    assert failed["ok"] is False
    assert failed["code"] == "git_failed"
    assert failed["exit_code"] == 128
    assert "secret" not in failed["stderr"]
    head = _git(repo, "rev-parse", "HEAD").strip()
    detail = await git.execute_git_operation(
        workspace_id, "commit_details", "/workspace/repo", {"commit": head}
    )
    assert detail["ok"], detail
    assert detail["details"]["file_changes"] == []


async def test_combined_diff_cancellation_releases_repo_lock(
    service, monkeypatch
) -> None:
    """Cancellation stops waiting and allows the next repository read."""
    repo, git, runtime, workspace_id = service
    (repo / "README.md").write_text("changed\n")
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    original_exec = runtime.exec_command_wait

    async def hold_diff(*args: object, **kwargs: object) -> tuple[int, str]:
        command = kwargs["command"]
        inner = git_ops.unwrap_git_exec_argv(command) or command
        if inner[:2] == ["git", "diff"]:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(runtime, "exec_command_wait", hold_diff)
    task = asyncio.create_task(
        git.execute_git_operation(workspace_id, "working_diff", "/workspace/repo")
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    monkeypatch.setattr(runtime, "exec_command_wait", original_exec)
    result = await asyncio.wait_for(
        git.execute_git_operation(workspace_id, "working_diff", "/workspace/repo"), 5
    )
    assert result["ok"], result
