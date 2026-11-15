"""Shared Git request validation for REST input and internal runner dispatch."""

from __future__ import annotations

import os
import re


def validate_repo_path(repo_path: str) -> str:
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


def validate_branch(value: object, *, field: str = "branch") -> str:
    """Validate a branch or remote label before runner dispatch."""
    cleaned = str(value or "").strip()
    if not cleaned or len(cleaned) > 255:
        raise ValueError(f"Invalid {field}")
    if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
        raise ValueError(f"Invalid {field}")
    return cleaned


def validate_commit_hash(value: object) -> str:
    """Validate a hex commit hash (full or abbreviated, min 4 chars)."""
    cleaned = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{4,64}", cleaned):
        raise ValueError("Invalid commit hash (must be 4-64 hex chars)")
    return cleaned


def validate_stash_selector(value: object) -> str:
    """Validate a stash selector (``stash@{n}``) fail-closed."""
    cleaned = str(value or "").strip()
    if not re.fullmatch(r"stash@\{\d{1,9}\}", cleaned):
        raise ValueError("Invalid stash (must be stash@{n})")
    return cleaned


def validate_file_paths(raw: object, *, operation: str) -> list[str]:
    """Validate exact repo-relative filenames without stripping whitespace."""
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"paths is required for operation {operation!r}")
    if len(raw) > 256:
        raise ValueError("Too many paths (max 256)")
    for path in raw:
        if not isinstance(path, str) or not path:
            raise ValueError("paths must contain non-empty strings")
        if len(path) > 256:
            raise ValueError("path too long (max 256 chars)")
        if "\x00" in path or "\n" in path or "\r" in path:
            raise ValueError(f"Invalid path: {path!r}")
    return raw


GIT_ALLOWED_ARGS: dict[str, frozenset] = {
    "list_repos": frozenset(),
    "repo_snapshot": frozenset(),
    "repo_history": frozenset({"history_limit", "history_skip", "branch"}),
    "working_diff": frozenset(),
    "commit_details": frozenset({"commit"}),
    "file_content": frozenset({"path", "revision"}),
    "stage_patch": frozenset(
        {
            "path",
            "patch",
            "expected_index_revision",
            "expected_worktree_revision",
        }
    ),
    "unstage_patch": frozenset(
        {
            "path",
            "patch",
            "old_path",
            "expected_head_revision",
            "expected_index_revision",
            "expected_worktree_revision",
        }
    ),
    "discard_patch": frozenset(
        {
            "path",
            "patch",
            "expected_index_revision",
            "expected_worktree_revision",
        }
    ),
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


def validate_git_args(
    operation: str,
    args: dict,
) -> dict:
    """Validate + whitelist operation args; never pass free env/argv.

    Unknown keys (``env``, ``author_*``, ``args``/``argv``, aliases
    like ``hash``/``path``/``new``/``message_b64``) raise ValueError.
    Commit identity is added by the service after validation.
    """
    incoming = dict(args or {})
    allowed = GIT_ALLOWED_ARGS.get(operation)
    if allowed is None:
        raise ValueError(f"Unknown git operation: {operation!r}")
    for key in incoming:
        if key not in allowed:
            raise ValueError(
                f"Unknown argument {key!r} for git operation {operation!r}"
            )
    if operation == "repo_history":
        out: dict = {}
        if "history_limit" in incoming:
            try:
                limit = int(incoming["history_limit"])
            except (TypeError, ValueError) as exc:
                raise ValueError("history_limit must be an integer (1-500)") from exc
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
            out["branch"] = validate_branch(incoming["branch"])
        return out
    if operation in {"list_repos", "repo_snapshot"}:
        return {}
    if operation == "commit_details":
        commit = str(incoming.get("commit") or "").strip()
        if not commit:
            raise ValueError("commit is required for commit_details")
        return {"commit": validate_commit_hash(commit)}
    if operation in {
        "file_content",
        "stage_patch",
        "unstage_patch",
        "discard_patch",
    }:
        paths = {"path": incoming.get("path")}
        if operation == "unstage_patch":
            paths["old_path"] = incoming.get("old_path", paths["path"])
        for path in paths.values():
            if not isinstance(path, str) or not path or len(path) > 1024:
                raise ValueError("path is required (maximum 1024 characters)")
            if path.startswith("/") or any(
                part in {"..", ".git"} for part in path.split("/")
            ):
                raise ValueError("path must be inside the repository")
            if any(char in path for char in "\x00\r\n"):
                raise ValueError("Invalid file path")
        path = paths["path"]
        if operation == "file_content":
            revision = incoming.get("revision", "index")
            if not isinstance(revision, str) or not (
                revision in {"index", "head", "worktree"}
                or re.fullmatch(r"[0-9a-fA-F]{4,64}\^?", revision)
            ):
                raise ValueError("Invalid file revision")
            return {"path": path, "revision": revision}
        patch = incoming.get("patch")
        if (
            not isinstance(patch, str)
            or not patch.startswith("@@ ")
            or len(patch.encode("utf-8")) > 200 * 1024
        ):
            raise ValueError("Expected unified diff hunks (maximum 200 KiB)")
        out = {**paths, "patch": patch}
        revision_keys = ["expected_index_revision", "expected_worktree_revision"]
        if operation == "unstage_patch":
            revision_keys.append("expected_head_revision")
        for key in revision_keys:
            value = incoming.get(key)
            if not isinstance(value, str) or not re.fullmatch(
                r"[0-9a-f]{64}|missing", value
            ):
                raise ValueError(f"Invalid {key}")
            out[key] = value
        return out
    if operation in {"stage", "unstage", "discard"}:
        paths = validate_file_paths(incoming.get("paths"), operation=operation)
        return {"paths": paths}
    if operation == "commit":
        message = str(incoming.get("message", "") or "")
        if not message.strip():
            raise ValueError("message is required for commit")
        if len(message) > 10000:
            raise ValueError("message too long (max 10000 chars)")
        return {"message": message}
    if operation in {"fetch", "pull", "push", "sync"}:
        out = {}
        if incoming.get("remote") not in (None, ""):
            out["remote"] = validate_branch(incoming["remote"], field="remote")
        if operation == "pull" and incoming.get("branch") not in (None, ""):
            if incoming.get("remote") in (None, ""):
                raise ValueError("remote is required when branch is set for pull")
            out["branch"] = validate_branch(incoming["branch"])
        if operation == "push" and incoming.get("set_upstream") is not None:
            out["set_upstream"] = bool(incoming["set_upstream"])
        return out
    if operation == "checkout_branch":
        branch = str(incoming.get("branch", "") or "").strip()
        if not branch:
            raise ValueError("branch is required for checkout_branch")
        return {"branch": validate_branch(branch)}
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
            out["local_name"] = validate_branch(local, field="local_name")
        return out
    if operation == "checkout_commit":
        commit = str(incoming.get("commit", "") or "").strip()
        if not commit:
            raise ValueError("commit is required for checkout_commit")
        return {"commit": validate_commit_hash(commit)}
    if operation == "create_branch":
        branch = str(incoming.get("branch", "") or "").strip()
        if not branch:
            raise ValueError("branch is required for create_branch")
        out = {"branch": validate_branch(branch)}
        if incoming.get("start_point") not in (None, ""):
            out["start_point"] = validate_branch(
                incoming["start_point"], field="start_point"
            )
        if incoming.get("checkout") is not None:
            out["checkout"] = bool(incoming["checkout"])
        return out
    if operation == "rename_branch":
        new = str(incoming.get("new_branch", "") or "").strip()
        if not new:
            raise ValueError("new_branch is required for rename_branch")
        out = {"new_branch": validate_branch(new, field="new_branch")}
        old = str(incoming.get("old_branch", "") or "").strip()
        if old:
            out["old_branch"] = validate_branch(old, field="old_branch")
        return out
    if operation == "delete_branch":
        branch = str(incoming.get("branch", "") or "").strip()
        if not branch:
            raise ValueError("branch is required for delete_branch")
        return {"branch": validate_branch(branch)}
    if operation == "merge_into_current":
        branch = str(incoming.get("branch", "") or "").strip()
        if not branch:
            raise ValueError("branch is required for merge_into_current")
        out = {"branch": validate_branch(branch)}
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
        out = {"target": validate_branch(target, field="target")}
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
        return {"stash": validate_stash_selector(stash)}
    if operation == "stash_branch":
        stash = str(incoming.get("stash", "") or "").strip()
        if not stash:
            raise ValueError("stash is required for stash_branch")
        branch = str(incoming.get("branch", "") or "").strip()
        if not branch:
            raise ValueError("branch is required for stash_branch")
        return {
            "stash": validate_stash_selector(stash),
            "branch": validate_branch(branch),
        }
    if operation == "working_diff":
        return {}
    raise ValueError(f"Unknown git operation: {operation!r}")
