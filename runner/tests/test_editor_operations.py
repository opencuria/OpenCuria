"""Real Git and filesystem coverage for editor saves and partial changes."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

from src import git as git_ops
from src.models import WorkspaceInfo
from src.services.files import FileManager
from src.services.git_service import GitService
from tests.test_git_operations import FakeGitRuntime, _make_repo
from tests.test_git_operations import setup_module as isolate_git
from tests.test_git_operations import teardown_module as restore_git


@pytest.fixture(scope="module", autouse=True)
def git_environment():
    """Never use the test host's Git credentials or configuration."""
    isolate_git()
    try:
        yield
    finally:
        restore_git()


class EditorRuntime(FakeGitRuntime):
    """Run the production fixed scripts against a translated scratch workspace."""

    async def exec_command_wait(
        self,
        instance_id,
        command,
        workdir=None,
        env=None,
    ):
        inner = git_ops.unwrap_git_exec_argv(command) or command
        if inner[:2] == ["python3", "-c"]:
            script = inner[2].replace(
                'root="/workspace"', f"root={self.workspace_root!r}"
            )
            payload = json.loads("".join(inner[3:]))
            for key in ("path", "temporary"):
                if payload.get(key, "").startswith("/workspace/"):
                    payload[key] = self._resolve_cwd(payload[key])
            argv = ["python3", "-c", script, json.dumps(payload)]
            cwd = self._resolve_cwd(workdir)

            def run():
                result = subprocess.run(
                    argv,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                return result.returncode, result.stdout + result.stderr

            return await asyncio.to_thread(run)
        if inner[:3] == ["rm", "-f", "--"]:
            Path(self._resolve_cwd(inner[-1])).unlink(missing_ok=True)
            return 0, ""
        if inner[:1] == ["git"] and (env or {}).get("GIT_LITERAL_PATHSPECS") == "1":
            command = ["git", "--literal-pathspecs", *inner[1:]]
        return await super().exec_command_wait(
            instance_id,
            command,
            workdir,
            env,
        )

    async def put_archive(self, instance_id, path, data):
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(self._resolve_cwd(path), filter="data")


def services(root: Path):
    runtime = EditorRuntime(str(root))
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(
        workspace_id=workspace_id, instance_id="editor-instance", status="running"
    )
    return (
        GitService(lambda _: info, lambda _: runtime),
        FileManager(get_cached=lambda _: info, get_runtime=lambda _: runtime),
        workspace_id,
    )


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


async def git_content(service, workspace_id, path, revision="index"):
    result = await service.execute_git_operation(
        workspace_id,
        "file_content",
        "/workspace/repo",
        {"path": path, "revision": revision},
    )
    assert result["ok"], result
    return result


async def patch_args(service, workspace_id, path, patch, *, old_path=None):
    index = await git_content(service, workspace_id, path)
    worktree = await git_content(service, workspace_id, path, "worktree")
    result = {
        "path": path,
        "patch": patch,
        "expected_index_revision": index["revision"],
        "expected_worktree_revision": worktree["revision"],
    }
    if old_path is not None:
        head = await git_content(service, workspace_id, old_path, "head")
        result["expected_head_revision"] = head["revision"]
        result["old_path"] = old_path
    return result


async def test_partial_stage_unstage_and_revert_preserve_other_changes(tmp_path):
    repo = Path(_make_repo(str(tmp_path / "repo")))
    path = repo / "source.txt"
    original = "one\ntwo\nthree\nfour\nfive\nsix\n"
    path.write_text(original)
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "Source")
    path.write_text("one\nTWO\nthree\nfour\nFIVE\nsix\n")
    service, _, workspace_id = services(tmp_path)
    first = "@@ -2,1 +2,1 @@\n-two\n+TWO\n"
    args = await patch_args(service, workspace_id, "source.txt", first)
    result = await service.execute_git_operation(
        workspace_id, "stage_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert run_git(repo, "show", ":source.txt") == original.replace("two", "TWO")
    assert path.read_text().endswith("FIVE\nsix\n")
    # Reject stale clicks without applying the same patch to a different state.
    stale = await service.execute_git_operation(
        workspace_id, "discard_patch", "/workspace/repo", args
    )
    assert stale["code"] == "stale_document"
    args = await patch_args(
        service, workspace_id, "source.txt", first, old_path="source.txt"
    )
    result = await service.execute_git_operation(
        workspace_id, "unstage_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert run_git(repo, "show", ":source.txt") == original
    second = "@@ -5,1 +5,1 @@\n-five\n+FIVE\n"
    args = await patch_args(service, workspace_id, "source.txt", second)
    result = await service.execute_git_operation(
        workspace_id, "discard_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert path.read_text() == original.replace("two", "TWO")
    assert run_git(repo, "show", ":source.txt") == original


async def test_new_file_unicode_literal_path_and_missing_sides(tmp_path):
    repo = Path(_make_repo(str(tmp_path / "repo")))
    path = 'é $(touch INJECTED) " name.txt'
    (repo / path).write_text("first\nsecond\n")
    service, _, workspace_id = services(tmp_path)
    root_hash = run_git(repo, "rev-parse", "HEAD").strip()
    root_parent = await git_content(service, workspace_id, "README.md", root_hash + "^")
    assert root_parent["content"] == "" and root_parent["revision"] == "missing"
    missing = await git_content(service, workspace_id, path)
    assert missing["revision"] == "missing"
    args = await patch_args(service, workspace_id, path, "@@ -0,0 +1,1 @@\n+first\n")
    result = await service.execute_git_operation(
        workspace_id, "stage_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert run_git(repo, "show", ":" + path) == "first\n"
    assert (repo / path).read_text() == "first\nsecond\n"
    assert not (repo / "INJECTED").exists()
    (repo / path).unlink()
    assert (await git_content(service, workspace_id, path, "worktree"))[
        "revision"
    ] == "missing"
    run_git(repo, "checkout", "--orphan", "empty")
    assert (await git_content(service, workspace_id, "README.md", "head"))[
        "revision"
    ] == "missing"


async def test_partial_patches_fail_closed(tmp_path):
    repo = Path(_make_repo(str(tmp_path / "repo")))
    service, _, workspace_id = services(tmp_path)
    (repo / "README.md").write_text("changed\n")
    args = await patch_args(
        service, workspace_id, "README.md", "@@ -1 +1 @@\n-wrong\n+new\n"
    )
    failed = await service.execute_git_operation(
        workspace_id, "stage_patch", "/workspace/repo", args
    )
    assert failed["code"] == "patch_failed"
    args["patch"] = "@@ -1 +1 @@\n-# test\n+new\ndiff --git a/other b/other\n"
    failed = await service.execute_git_operation(
        workspace_id, "stage_patch", "/workspace/repo", args
    )
    assert failed["code"] == "invalid_argument"
    args["patch"] = (
        "@@ -1 +1 @@\n-# test\n+new\n"
        "--- a/victim\n+++ b/victim\n@@ -1 +1 @@\n-secret\n+overwritten\n"
    )
    (repo / "victim").write_text("secret\n")
    failed = await service.execute_git_operation(
        workspace_id, "stage_patch", "/workspace/repo", args
    )
    assert failed["code"] == "invalid_argument"
    assert run_git(repo, "show", ":README.md") == "# test\n"
    assert (repo / "victim").read_text() == "secret\n"
    for path in ("../../outside", ".git/config", "/etc/passwd"):
        failed = await service.execute_git_operation(
            workspace_id,
            "file_content",
            "/workspace/repo",
            {"path": path, "revision": "index"},
        )
        assert not failed["ok"]
    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}"
    try:
        outside.write_text("outside secret\n")
        (repo / "escape").symlink_to(outside)
        failed = await service.execute_git_operation(
            workspace_id,
            "file_content",
            "/workspace/repo",
            {"path": "escape", "revision": "worktree"},
        )
        assert failed["code"] == "unsafe_path"
        assert "outside secret" not in str(failed)
    finally:
        outside.unlink(missing_ok=True)


async def test_new_file_later_line_and_complete_unstage(tmp_path):
    repo = Path(_make_repo(str(tmp_path / "repo")))
    (repo / "new").write_text("first\nsecond\nthird\n")
    service, _, workspace_id = services(tmp_path)
    args = await patch_args(service, workspace_id, "new", "@@ -0,0 +2,1 @@\n+second\n")
    result = await service.execute_git_operation(
        workspace_id, "stage_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert run_git(repo, "show", ":new") == "second\n"
    args = await patch_args(
        service, workspace_id, "new", "@@ -0,0 +1,1 @@\n+second\n", old_path="new"
    )
    result = await service.execute_git_operation(
        workspace_id, "unstage_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert run_git(repo, "ls-files", "--", "new") == ""
    assert (repo / "new").read_text() == "first\nsecond\nthird\n"


async def test_unstage_rejects_changed_head_without_touching_index(tmp_path):
    repo = Path(_make_repo(str(tmp_path / "repo")))
    (repo / "README.md").write_text("changed\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "Second")
    second = run_git(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "reset", "--soft", "HEAD^")
    service, _, workspace_id = services(tmp_path)
    args = await patch_args(
        service,
        workspace_id,
        "README.md",
        "@@ -1 +1 @@\n-# test\n+changed\n",
        old_path="README.md",
    )
    run_git(repo, "reset", "--soft", second)
    result = await service.execute_git_operation(
        workspace_id, "unstage_patch", "/workspace/repo", args
    )
    assert result["code"] == "stale_document"
    assert run_git(repo, "show", ":README.md") == "changed\n"


async def test_unstage_selected_renamed_file_content_preserves_rename(tmp_path):
    repo = Path(_make_repo(str(tmp_path / "repo")))
    (repo / "old.txt").write_text("a\nb\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "Old path")
    run_git(repo, "mv", "old.txt", "new.txt")
    (repo / "new.txt").write_text("a\nB\n")
    run_git(repo, "add", "new.txt")
    service, _, workspace_id = services(tmp_path)
    args = await patch_args(
        service,
        workspace_id,
        "new.txt",
        "@@ -2 +2 @@\n-b\n+B\n",
        old_path="old.txt",
    )
    result = await service.execute_git_operation(
        workspace_id, "unstage_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert run_git(repo, "show", ":new.txt") == "a\nb\n"
    assert (repo / "new.txt").read_text() == "a\nB\n"
    assert run_git(repo, "ls-files", "--", "old.txt") == ""
    assert "R100\told.txt\tnew.txt" in run_git(
        repo, "diff", "--cached", "--name-status"
    )


async def test_whitespace_file_paths_remain_exact_for_reads_and_staging(tmp_path):
    repo = Path(_make_repo(str(tmp_path / "repo")))
    exact = " leading and trailing "
    (repo / exact).write_text("original\n")
    (repo / exact.strip()).write_text("sibling must not change\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "Whitespace paths")
    (repo / exact).write_text("edited\n")
    service, _, workspace_id = services(tmp_path)
    index = await git_content(service, workspace_id, exact)
    worktree = await git_content(service, workspace_id, exact, "worktree")
    assert index["content"] == "original\n"
    assert worktree["content"] == "edited\n"
    args = await patch_args(
        service, workspace_id, exact, "@@ -1 +1 @@\n-original\n+edited\n"
    )
    result = await service.execute_git_operation(
        workspace_id, "stage_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert run_git(repo, "show", ":" + exact) == "edited\n"
    assert run_git(repo, "show", ":" + exact.strip()) == "sibling must not change\n"
    result = await service.execute_git_operation(
        workspace_id, "unstage", "/workspace/repo", {"paths": [exact]}
    )
    assert result["ok"], result
    assert run_git(repo, "show", ":" + exact) == "original\n"
    result = await service.execute_git_operation(
        workspace_id, "stage", "/workspace/repo", {"paths": [exact]}
    )
    assert result["ok"], result
    assert run_git(repo, "show", ":" + exact) == "edited\n"


async def test_literal_pathspec_filename_never_stages_another_file(tmp_path):
    repo = Path(_make_repo(str(tmp_path / "repo")))
    exact = ":(glob)*"
    (repo / exact).write_text("literal target\n")
    (repo / "README.md").write_text("unrelated edit\n")
    service, _, workspace_id = services(tmp_path)
    result = await service.execute_git_operation(
        workspace_id, "stage", "/workspace/repo", {"paths": [exact]}
    )
    assert result["ok"], result
    assert run_git(repo, "show", ":" + exact) == "literal target\n"
    assert run_git(repo, "show", ":README.md") == "# test\n"


async def test_file_reads_and_saves_preserve_trailing_filename_spaces(tmp_path):
    _, files, workspace_id = services(tmp_path)
    exact = tmp_path / " padded "
    sibling = tmp_path / " padded"
    exact.write_text("exact file\n")
    sibling.write_text("must stay unchanged\n")
    opened = await files.read_editor_file(workspace_id, "/workspace/ padded ")
    assert opened["content"] == "exact file\n"
    saved = await files.save_editor_file(
        workspace_id, "/workspace/ padded ", "changed\n", opened["revision"]
    )
    assert saved["ok"]
    assert exact.read_text() == "changed\n"
    assert sibling.read_text() == "must stay unchanged\n"


async def test_complete_deletion_stage_and_partial_deleted_file_restore(tmp_path):
    repo = Path(_make_repo(str(tmp_path / "repo")))
    path = repo / "README.md"
    path.unlink()
    service, _, workspace_id = services(tmp_path)
    args = await patch_args(
        service, workspace_id, "README.md", "@@ -1,1 +0,0 @@\n-# test\n"
    )
    result = await service.execute_git_operation(
        workspace_id, "stage_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert run_git(repo, "ls-files", "--", "README.md") == ""
    run_git(repo, "reset", "HEAD", "--", "README.md")
    args = await patch_args(
        service, workspace_id, "README.md", "@@ -1,1 +0,0 @@\n-# test\n"
    )
    result = await service.execute_git_operation(
        workspace_id, "discard_patch", "/workspace/repo", args
    )
    assert result["ok"], result
    assert path.read_text() == "# test\n"


async def test_file_save_preserves_utf8_crlf_mode_and_rejects_conflicts(tmp_path):
    _, files, workspace_id = services(tmp_path)
    path = tmp_path / "script.sh"
    path.write_bytes("#!/bin/sh\r\necho ä\r\n".encode())
    path.chmod(0o751)
    opened = await files.read_editor_file(workspace_id, "/workspace/script.sh")
    assert opened["content"] == "#!/bin/sh\r\necho ä\r\n"
    saved = await files.save_editor_file(
        workspace_id,
        "/workspace/script.sh",
        "#!/bin/sh\r\necho ö\r\n",
        opened["revision"],
    )
    assert saved["ok"]
    assert path.read_bytes() == "#!/bin/sh\r\necho ö\r\n".encode()
    assert path.stat().st_mode & 0o777 == 0o751
    stale = await files.save_editor_file(
        workspace_id, "/workspace/script.sh", "do not overwrite", opened["revision"]
    )
    assert stale["code"] == "stale_document"
    assert "do not overwrite" not in path.read_text()
    assert not list(tmp_path.glob(".opencuria-editor-*"))
    new = await files.read_editor_file(workspace_id, "/workspace/new.txt")
    assert new["revision"] == "missing"
    created = await files.save_editor_file(
        workspace_id, "/workspace/new.txt", "", "missing"
    )
    assert created["ok"] and (tmp_path / "new.txt").read_bytes() == b""


async def test_concurrent_saves_with_same_revision_have_one_winner(tmp_path):
    _, files, workspace_id = services(tmp_path)
    path = tmp_path / "file"
    path.write_text("original\n")
    original = await files.read_editor_file(workspace_id, "/workspace/file")
    results = await asyncio.gather(
        *(
            files.save_editor_file(
                workspace_id, "/workspace/file", content, original["revision"]
            )
            for content in ("first\n", "second\n")
        )
    )
    assert sum(bool(result.get("ok")) for result in results) == 1
    assert sum(result.get("code") == "stale_document" for result in results) == 1
    assert path.read_text() in {"first\n", "second\n"}
    assert not list(tmp_path.glob(".opencuria-editor-*"))


@pytest.mark.parametrize(
    "data,code",
    [
        (b"\x00binary", "binary_file"),
        (b"\xff", "binary_file"),
        (b"x" * (5 * 1024 * 1024 + 1), "file_too_large"),
    ],
    ids=["nul", "invalid-utf8", "too-large"],
)
async def test_file_read_never_returns_partial_or_binary_text(tmp_path, data, code):
    _, files, workspace_id = services(tmp_path)
    (tmp_path / "file").write_bytes(data)
    result = await files.read_editor_file(workspace_id, "/workspace/file")
    assert result["code"] == code
    assert "content" not in result
