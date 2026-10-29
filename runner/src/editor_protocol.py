"""Bounded text-editor commands executed inside the workspace runtime.

Commands use fixed Python source and JSON argv, never interpolate paths or text
into source. The filesystem and Git checks run inside the workspace as well as
at the RPC boundary, so symlinks cannot turn editor operations into host reads.
"""

from __future__ import annotations

import json

EDITOR_MAX_BYTES = 5 * 1024 * 1024
EDITOR_PATCH_MAX_BYTES = 200 * 1024

_COMMON = r"""
import base64, hashlib, json, os, re, stat, subprocess, sys
MAX_BYTES = 5 * 1024 * 1024
def fail(code, message):
    print(json.dumps({"ok": False, "code": code, "message": message}))
    raise SystemExit(0)
def token(data):
    return "missing" if data is None else hashlib.sha256(data).hexdigest()
def text_result(data):
    if data is not None and len(data) > MAX_BYTES:
        fail("file_too_large", "The editor supports files up to 5 MiB")
    try:
        content = (data or b"").decode("utf-8")
    except UnicodeDecodeError:
        fail("binary_file", "This file is not UTF-8 text")
    if "\0" in content:
        fail("binary_file", "This file is not text")
    return {"ok": True, "content": content, "revision": token(data),
            "exists": data is not None, "size": len(data or b"")}
def checked_path(path, root="/workspace"):
    root = os.path.realpath(root)
    resolved = os.path.realpath(path)
    if resolved == root or not resolved.startswith(root + os.sep):
        fail("unsafe_path", "File must be inside the workspace")
    return resolved
def read_worktree(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        fail("unsafe_path", "Cannot open this file safely")
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            fail("unsupported_file", "The editor supports regular files only")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        fail("file_too_large", "The editor supports files up to 5 MiB")
    return data
"""

EDITOR_FILE_SCRIPT = (
    _COMMON
    + r"""
args = json.loads(sys.argv[1])
path = checked_path(args["path"])
if args["operation"] == "read":
    result = text_result(read_worktree(path))
else:
    temporary = checked_path(args["temporary"])
    try:
        current = read_worktree(path)
        if token(current) != args["expected_revision"]:
            fail("stale_document", "The file changed on disk. Reload it before saving.")
        mode = stat.S_IMODE(os.stat(path).st_mode) if current is not None else 0o644
        data = read_worktree(temporary)
        result = text_result(data)
        # Resolve once more immediately before replacing the directory entry.
        if checked_path(args["path"]) != path:
            fail("stale_document", "The file path changed. Reload it before saving.")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
result["path"] = args["path"]
print(json.dumps(result))
"""
)

EDITOR_GIT_SCRIPT = (
    _COMMON
    + r"""
args = json.loads("".join(sys.argv[1:]))
os.environ["GIT_LITERAL_PATHSPECS"] = "1"
path = args["path"]
root = os.path.realpath(os.getcwd())
full_path = checked_path(os.path.join(root, path), root)
if ".git" in path.split("/"):
    fail("unsafe_path", "Git metadata cannot be edited as a change")
if os.path.islink(os.path.join(root, path)):
    fail("unsupported_file", "The diff editor supports regular files only")
def git(*argv, **kwargs):
    return subprocess.run(["git", *argv], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **kwargs)
def blob(revision, file_path=path):
    if revision == "worktree":
        return read_worktree(full_path)
    if revision not in ("index", "head"):
        valid = git("rev-parse", "--verify", revision + "^{commit}")
        if valid.returncode:
            if revision.endswith("^"):
                parents = git("rev-list", "--parents", "-n", "1", revision[:-1])
                if not parents.returncode and len(parents.stdout.split()) == 1:
                    return None
            fail("invalid_revision", "This commit is not available")
    source = ":" + file_path if revision == "index" else (
        ("HEAD" if revision == "head" else revision) + ":" + file_path)
    info = git("cat-file", "-s", source)
    if info.returncode:
        # A missing HEAD in a new repository and an absent path are empty sides.
        return None
    if int(info.stdout) > MAX_BYTES:
        fail("file_too_large", "The editor supports files up to 5 MiB")
    result = git("cat-file", "blob", source)
    if result.returncode:
        fail("git_failed", "Could not read the Git file")
    return result.stdout
if args["operation"] == "file_content":
    result = text_result(blob(args["revision"]))
else:
    conflicts = git("ls-files", "--unmerged", "--", path)
    if conflicts.returncode or conflicts.stdout:
        fail("conflict", "Resolve the merge conflict before staging selected changes")
    entry = git("ls-files", "--stage", "--", path)
    if entry.stdout.startswith((b"120000 ", b"160000 ")):
        fail("unsupported_file", "The diff editor supports regular files only")
    index = blob("index")
    worktree = blob("worktree")
    head = None
    if args["operation"] == "unstage_patch":
        old_path = args.get("old_path", path)
        checked_path(os.path.join(root, old_path), root)
        if ".git" in old_path.split("/"):
            fail("unsafe_path", "Git metadata cannot be edited as a change")
        head = blob("head", old_path)
        if token(head) != args["expected_head_revision"]:
            fail("stale_document", "HEAD changed. Reload the diff and try again.")
    if token(index) != args["expected_index_revision"] or (
        token(worktree) != args["expected_worktree_revision"]
    ):
        fail("stale_document", "The file or index changed. Reload the diff and try again.")
    # NUL and invalid UTF-8 files are never accepted for a textual patch.
    text_result(index)
    text_result(worktree)
    patch = args["patch"]
    if len(patch.encode("utf-8")) > 200 * 1024 or not patch.startswith("@@ "):
        fail("invalid_argument", "Expected unified diff hunks (maximum 200 KiB)")
    remaining_old = remaining_new = 0
    changed_lines = 0
    lines = patch.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        if line.startswith("@@ "):
            match = re.fullmatch(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@.*", line)
            if not match or remaining_old or remaining_new:
                fail("invalid_argument", "Invalid unified diff hunk")
            remaining_old = int(match.group(1) or "1")
            remaining_new = int(match.group(2) or "1")
        elif line == "\\ No newline at end of file":
            continue
        elif line.startswith((" ", "+", "-")):
            remaining_old -= int(line[0] != "+")
            remaining_new -= int(line[0] != "-")
            changed_lines += int(line[0] != " ")
            if remaining_old < 0 or remaining_new < 0:
                fail("invalid_argument", "Unexpected lines outside a unified diff hunk")
        else:
            fail("invalid_argument", "Only unified diff hunks are accepted")
    if remaining_old or remaining_new or not changed_lines:
        fail("invalid_argument", "Incomplete unified diff hunk")
    # An absent side is a new file to Git, whose first hunk must start at 1.
    # Selecting a later addition/deletion still creates just those selected lines.
    if args["operation"] == "stage_patch" and index is None:
        selected = [line for line in lines if line.startswith("+") or line == "\\ No newline at end of file"]
        count = sum(line.startswith("+") for line in selected)
        patch = "@@ -0,0 +1,%d @@\n" % count + "\n".join(selected) + "\n"
    if args["operation"] == "discard_patch" and worktree is None:
        selected = [line for line in lines if line.startswith("-") or line == "\\ No newline at end of file"]
        count = sum(line.startswith("-") for line in selected)
        patch = "@@ -1,%d +0,0 @@\n" % count + "\n".join(selected) + "\n"
    def quote_path(prefix):
        raw = (prefix + path).encode("utf-8")
        return '"' + ''.join(chr(c) if 32 <= c < 127 and c not in (34, 92)
                             else "\\%03o" % c for c in raw) + '"'
    old = quote_path("a/")
    new = quote_path("b/")
    # Missing sides need /dev/null for partial staging of new/deleted files.
    reverse = args["operation"] in ("unstage_patch", "discard_patch")
    if args["operation"] == "stage_patch" and index is None:
        old = "/dev/null"
    if args["operation"] == "discard_patch" and worktree is None:
        new = "/dev/null"
    header = "--- " + old + "\n+++ " + new + "\n"
    argv = ["apply", "--whitespace=nowarn", "--unidiff-zero"]
    if args["operation"] != "discard_patch":
        argv.append("--cached")
    if reverse:
        argv.append("--reverse")
    applied = git(*argv, "-", input=(header + patch).encode("utf-8"))
    if applied.returncode:
        fail("patch_failed", "The selected change could not be applied. Reload the diff and try again.")
    if args["operation"] == "stage_patch" and worktree is None and blob("index") == b"":
        removed = git("update-index", "--force-remove", "--", path)
        if removed.returncode:
            fail("git_failed", "Could not stage the file deletion")
    if args["operation"] == "unstage_patch" and head is None and blob("index") == b"":
        removed = git("update-index", "--force-remove", "--", path)
        if removed.returncode:
            fail("git_failed", "Could not unstage the added file")
    if args["operation"] == "discard_patch" and index is None and read_worktree(full_path) == b"":
        os.unlink(full_path)
    result = {"ok": True}
print(json.dumps(result))
"""
)


def editor_command(script: str, args: dict) -> list[str]:
    """Build argv while keeping every argument below the OS string limit."""
    payload = json.dumps(args, ensure_ascii=True)
    return [
        "python3",
        "-c",
        script,
        *(payload[start : start + 32768] for start in range(0, len(payload), 32768)),
    ]
