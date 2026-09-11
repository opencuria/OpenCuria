"""Dedicated git operations for the runner workspace executor.

The runner stays a "dumb executor": it owns no backend business logic, but
:mod:`service` may encapsulate git command construction and output parsing
here so the WebSocket interface stays a thin adapter.  All commands use
argv lists (never shell concatenation) and run inside the workspace
container/VM via the runtime ``exec_command_wait`` API.
"""

from __future__ import annotations

import contextlib
import os
import re
from typing import Any

# --- protocol limits ----------------------------------------------------------

#: Max repositories discovered under ``/workspace`` (fail-closed limit).
GIT_DISCOVERY_MAX_REPOS = 64

#: Max reported history commits per snapshot request.
GIT_HISTORY_DEFAULT_LIMIT = 200
GIT_HISTORY_MAX_LIMIT = 500

#: Unified diff context lines.
GIT_DIFF_CONTEXT_LINES = 3

#: Per-file textual diff cap (per side-buffer truncation).
GIT_MAX_DIFF_BYTES = 200 * 1024  # 200 KiB
#: Max files with inline patch text per diff response.
GIT_MAX_DIFF_FILES_WITH_PATCH = 100
#: Max raw bytes read when rendering an untracked file as "added".
GIT_MAX_UNTRACKED_BYTES = 200 * 1024

#: stderr bytes returned in structured errors (after redaction).
GIT_MAX_STDERR_BYTES = 8 * 1024

#: Command timeouts (seconds).
GIT_READ_TIMEOUT_S = 30.0
GIT_NETWORK_TIMEOUT_S = 120.0

#: Unit separator / record separator for NUL-hostile transports.
_GIT_US = "\x1f"
_GIT_RS = "\x1e"

_NET_OPS = frozenset({"fetch", "pull", "push", "sync"})
_READ_OPS = frozenset({"snapshot", "working_diff", "commit_details"})

GIT_OPERATIONS = (
    "snapshot",
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
    "create_branch",
    "rename_branch",
    "delete_branch",
    "merge_into_current",
    "merge_current_into",
    "merge_abort",
)

#: Allowed operation-specific arg keys per git operation (defense-in-depth:
#: the runner protocol is a trust boundary, so unknown keys are rejected
#: before any askpass/repo work — mirroring the backend allow-list).
#: ``author_name``/``author_email`` are only ever injected server-side by
#: the backend for ``commit`` (never accepted from an external Git client),
#: so the runner allows them on ``commit`` only.
GIT_ALLOWED_ARGS: dict[str, frozenset] = {
    "snapshot": frozenset({"history_limit", "history_skip"}),
    "working_diff": frozenset(),
    "commit_details": frozenset({"commit"}),
    "stage": frozenset({"paths"}),
    "unstage": frozenset({"paths"}),
    "discard": frozenset({"paths"}),
    "commit": frozenset({"message", "author_name", "author_email"}),
    "fetch": frozenset({"remote"}),
    "pull": frozenset({"remote", "branch"}),
    "push": frozenset({"remote", "set_upstream"}),
    "sync": frozenset({"remote"}),
    "checkout_branch": frozenset({"branch"}),
    "checkout_commit": frozenset({"commit"}),
    "create_branch": frozenset({"branch", "start_point", "checkout"}),
    "rename_branch": frozenset({"new_branch", "old_branch"}),
    "delete_branch": frozenset({"branch"}),
    "merge_into_current": frozenset({"branch", "message"}),
    "merge_current_into": frozenset({"target", "message"}),
    "merge_abort": frozenset(),
}


def check_git_args_allowed(operation: str, params: dict[str, Any]) -> None:
    """Reject unknown operation-specific arg keys fail-closed.

    Raises :class:`ValueError` (surfaced as structured ``invalid_argument``)
    before any askpass/repo work.  Required-shape validation stays in the
    per-operation handlers.
    """
    allowed = GIT_ALLOWED_ARGS.get(operation)
    if allowed is None:
        raise ValueError(f"Unknown git operation: {operation!r}")
    for key in params:
        if key not in allowed:
            raise ValueError(
                f"Unknown argument {key!r} for git operation {operation!r}"
            )


#: Max lengths for the commit identity fallback (``-c user.name/email``).
GIT_AUTHOR_NAME_MAX = 255
GIT_AUTHOR_EMAIL_MAX = 320


def validate_author_name(name: str) -> str:
    """Validate a single commit author name fallback before ``-c`` argv use."""
    if not isinstance(name, str):
        raise ValueError("Invalid author identity")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("author_name must be non-empty")
    if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
        raise ValueError("Invalid author identity")
    if len(cleaned) > GIT_AUTHOR_NAME_MAX:
        raise ValueError("author_name too long (max 255 chars)")
    return cleaned


def validate_author_email(email: str) -> str:
    """Validate a single commit author email fallback before ``-c`` argv use."""
    if not isinstance(email, str):
        raise ValueError("Invalid author identity")
    cleaned = email.strip()
    if not cleaned:
        raise ValueError("author_email must be non-empty")
    if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
        raise ValueError("Invalid author identity")
    if len(cleaned) > GIT_AUTHOR_EMAIL_MAX:
        raise ValueError("author_email too long (max 320 chars)")
    if "@" not in cleaned:
        raise ValueError("Invalid author_email")
    return cleaned


def validate_author_identity(name: str, email: str) -> tuple[str, str]:
    """Validate commit author fallback values before ``-c`` argv use.

    Backend-supplied values are revalidated here (defense-in-depth):
    non-empty, no NUL/CR/LF (argv/header injection), length-capped.
    """
    return validate_author_name(name), validate_author_email(email)

#: Exact ``--``-safe single-path working-tree file state.
_CONFLICT_XY = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})

# Branch names must be single ref path components validated with
# ``git check-ref-format --branch``; anything else is rejected before argv
# construction to keep ``git branch`` argv free of option injection.
_BRANCH_FORBIDDEN_RE = re.compile(r"(?:^|[\\/])\.|\.\.|\s|~|\^|:|\?|\*|\[|@\{|\\\\")
_BRANCH_BAD_CHARS = set(" ~^:?*[]\\")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")

# Full and abbreviated commit hashes.
_HASH_RE = re.compile(r"^[0-9a-f]{4,64}$")

# --- structured git errors ----------------------------------------------------


class GitError(Exception):
    """Structured git failure with a machine-readable ``code``."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.stderr = stderr


def _redact(text: str) -> str:
    """Redact credential-looking material from git output/errors."""
    if not text:
        return ""
    redacted = re.sub(
        r"(?i)(ghp_[A-Za-z0-9_]+|gho_[A-Za-z0-9_]+|"
        r"github_pat_[A-Za-z0-9_]+|x-access-token:[^@\s]+)",
        "***",
        text,
    )
    # Redact userinfo (user or user:secret) in URLs but keep host/path
    # for diagnostics.  `[^/@\s]+` excluding `/` prevents swallowing the
    # host when the secret itself contains `@` (e.g. `user:p@ss@host`).
    redacted = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", redacted)
    # Repeat once for `user:secret@` where the secret held an `@`.
    redacted = re.sub(r"(https?://\*\*\*@)[^/@\s]+@", r"\1", redacted)
    if len(redacted.encode("utf-8", "ignore")) > GIT_MAX_STDERR_BYTES:
        raw = redacted.encode("utf-8", "ignore")[:GIT_MAX_STDERR_BYTES]
        redacted = raw.decode("utf-8", "ignore") + "…[truncated]"
    return redacted


def git_error_payload(exc: BaseException) -> dict[str, Any]:
    """Convert a git failure into a JSON-serialisable error dict."""
    if isinstance(exc, GitError):
        return {
            "ok": False,
            "code": exc.code,
            "message": exc.message,
            "stderr": _redact(exc.stderr),
            "exit_code": exc.exit_code,
        }
    if isinstance(exc, (ValueError, FileNotFoundError)):
        code = "not_a_repo" if "epository" in str(exc) or "epo" in str(exc) else (
            "invalid_argument" if isinstance(exc, ValueError) else "not_a_repo"
        )
        return {
            "ok": False,
            "code": code,
            "message": str(exc),
            "stderr": "",
            "exit_code": None,
        }
    return {
        "ok": False,
        "code": "git_failed",
        "message": f"{type(exc).__name__}: {exc}",
        "stderr": "",
        "exit_code": None,
    }


def git_timeout_for(operation: str) -> float:
    """Return the timeout budget for *operation*."""
    if operation in _NET_OPS:
        return GIT_NETWORK_TIMEOUT_S
    return GIT_READ_TIMEOUT_S


def is_network_operation(operation: str) -> bool:
    """Return True when *operation* may touch the network."""
    return operation in _NET_OPS


# --- path security ------------------------------------------------------------


def normalize_repo_arg(repo_path: str | None) -> str:
    """Validate that *repo_path* is an absolute path under ``/workspace``.

    Rejects NUL bytes, newlines and relative paths fail-closed.  Returns the
    normalised path.
    """
    raw = repo_path or "/workspace"
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ValueError(f"Invalid repo path: {repo_path!r}")
    candidate = raw.strip() or "/workspace"
    normalized = os.path.normpath(candidate)
    if normalized != "/workspace" and not normalized.startswith("/workspace/"):
        raise ValueError(f"Repo path must be under /workspace: {repo_path!r}")
    return normalized


def parse_single_path_output(output: str) -> str | None:
    """Validate single-line path output from rev-parse/realpath (fail-closed).

    Returns the stripped single path, or None when empty, multiline or
    containing NUL/CR/LF. Used for ``--show-toplevel``/``--absolute-git-dir``/
    ``--git-common-dir`` and ``realpath -m`` outputs.
    """
    if not isinstance(output, str):
        return None
    if "\x00" in output:
        return None
    stripped = output.strip()
    if not stripped:
        return None
    lines = stripped.splitlines()
    if len(lines) != 1:
        return None
    candidate = lines[0].strip()
    if not candidate:
        return None
    if "\x00" in candidate or "\n" in candidate or "\r" in candidate:
        return None
    return candidate


def is_workspace_path(path: str) -> bool:
    """Return True when *path* is /workspace or under it.

    Fail-closed: non-strings, values with NUL/CR/LF, and lexically
    escaping values (``/workspace/../etc``) return False.  The input is
    normalised before the prefix check so ``..`` segments can never
    smuggle an escape past a pure prefix test.
    """
    if not isinstance(path, str):
        return False
    if "\x00" in path or "\n" in path or "\r" in path:
        return False
    normalized = os.path.normpath(path)
    return normalized == "/workspace" or normalized.startswith("/workspace/")


def normalize_relative_path(path: str) -> str:
    """Validate a repo-relative file path (no absolute, no ``..`` escape)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    if "\x00" in path or "\n" in path or "\r" in path:
        raise ValueError(f"Invalid path: {path!r}")
    cleaned = path.strip()
    if os.path.isabs(cleaned):
        raise ValueError(f"path must be repo-relative: {path!r}")
    normalized = os.path.normpath(cleaned)
    if normalized in {".", ".."} or normalized.startswith("../") or normalized.startswith("..\\"):
        raise ValueError(f"path escapes repository: {path!r}")
    if normalized.startswith("/"):
        raise ValueError(f"path escapes repository: {path!r}")
    return normalized


def validate_branch_name(name: str, *, field: str = "branch") -> str:
    """Validate a branch name fail-closed (no option injection)."""
    if not isinstance(name, str):
        raise ValueError(f"Invalid {field}: {name!r}")
    cleaned = name.strip()
    if not cleaned or cleaned in {".", ".."} or len(cleaned) > 255:
        raise ValueError(f"Invalid {field}: {name!r}")
    if cleaned.startswith("-") or cleaned.startswith("/") or cleaned.endswith("/"):
        raise ValueError(f"Invalid {field}: {name!r}")
    if "//" in cleaned or ".." in cleaned or "@{" in cleaned:
        raise ValueError(f"Invalid {field}: {name!r}")
    if _BRANCH_FORBIDDEN_RE.search(cleaned):
        raise ValueError(f"Invalid {field}: {name!r}")
    if any(ch in _BRANCH_BAD_CHARS for ch in cleaned):
        raise ValueError(f"Invalid {field}: {name!r}")
    if cleaned.endswith(".lock") or cleaned.endswith(".") or "/." in cleaned:
        raise ValueError(f"Invalid {field}: {name!r}")
    if not _GIT_REF_RE.match(cleaned):
        raise ValueError(f"Invalid {field}: {name!r}")
    return cleaned


def validate_commit_hash(value: str, *, field: str = "commit") -> str:
    """Validate a hex commit hash (full or abbreviated, min 4 chars)."""
    cleaned = (value or "").strip().lower()
    if not _HASH_RE.match(cleaned):
        raise ValueError(f"Invalid {field}: {value!r}")
    return cleaned


# --- git environment / auth ---------------------------------------------------

GIT_BASE_ENV: dict[str, str] = {
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EDITOR": "true",
    "VISUAL": "true",
    "EDITOR": "true",
    # BatchMode + no prompts: network ops fail fast instead of hanging.
    # GIT_ASKPASS is injected per-operation via the throwaway askpass
    # script only when a persistent GITHUB_TOKEN exists in the
    # workspace credential file — never baked in here and never via
    # caller-provided env (the Git RPC accepts no free env).  SSH uses
    # key auth with BatchMode (no passphrases/interactive prompts), so
    # SSH_ASKPASS is deliberately never set: the PAT must never be
    # offered to an SSH prompt.
    "GIT_SSH_COMMAND": (
        "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        "-o ConnectTimeout=15"
    ),
}

#: Persistent workspace credential file sourced inside the workspace before
#: git runs (must stay in sync with service.WORKSPACE_CREDENTIAL_ENV_FILE).
GIT_PERSISTENT_ENV_FILE = "/root/.opencuria-env.sh"

#: Fixed wrapper script: sources the persistent credential file (if present)
#: so GITHUB_TOKEN is inherited by git and its askpass child.  No user input
#: is ever interpolated here; git argv stays separate argv elements behind
#: ``exec "$@"``.  The conditional ``[ -f ... ]`` guard is required:
#: POSIX ``sh`` (dash) aborts a non-interactive ``. <missing>`` with exit 2,
#: which would break every git call in workspaces without credentials.
_GIT_EXEC_WRAPPER_SCRIPT = (
    'if [ -f /root/.opencuria-env.sh ]; then . /root/.opencuria-env.sh; fi; exec "$@"'
)
_GIT_EXEC_WRAPPER_NAME = "opencuria-git-exec"


def git_exec_wrapper_argv(argv: list[str]) -> list[str]:
    """Wrap a git argv so the persistent credential file is sourced.

    Returns ``["sh", "-c", <fixed script>, "opencuria-git-exec", *argv]``.
    The fixed script contains no caller input; user-controlled git arguments
    (branches, paths, messages) stay separate argv elements behind
    ``exec "$@"`` and are never shell-interpreted.  Works for both runtimes:
    Docker exec argv and QEMU single-quote escaping both preserve the argv
    boundary.  The git process inherits ``GITHUB_TOKEN`` from the sourced
    file (when present) without the token ever appearing in argv/URLs/logs.
    """
    if not argv or argv[0] != "git":
        raise ValueError("Only git argv can be wrapped")
    return ["sh", "-c", _GIT_EXEC_WRAPPER_SCRIPT, _GIT_EXEC_WRAPPER_NAME, *list(argv)]


def unwrap_git_exec_argv(argv: list[str]) -> list[str] | None:
    """Return the inner git argv when *argv* is a wrapper call, else None."""
    if (
        len(argv) >= 5
        and argv[0] == "sh"
        and argv[1] == "-c"
        and argv[2] == _GIT_EXEC_WRAPPER_SCRIPT
        and argv[3] == _GIT_EXEC_WRAPPER_NAME
        and argv[4] == "git"
    ):
        return list(argv[4:])
    return None


def build_git_env(
    *,
    askpass_script: str | None = None,
) -> dict[str, str]:
    """Build the deterministic non-interactive git environment.

    Only the secure base plus optional ``GIT_ASKPASS`` pointer.  No
    caller-provided env is accepted (the Git RPC must not allow
    ``GIT_CONFIG_*``/``PATH``/token injection).  ``GITHUB_TOKEN`` is never
    placed in this mapping for Docker/QEMU; it reaches the git process via
    the sourcing wrapper (:func:`git_exec_wrapper_argv`) from the
    persistent workspace credential file.  ``SSH_ASKPASS`` is never set:
    SSH runs BatchMode key auth only, and the PAT must never answer an
    SSH/host-key/passphrase prompt.  ``gh`` CLI tokens are out of scope.
    """
    env: dict[str, str] = dict(GIT_BASE_ENV)
    if askpass_script:
        env["GIT_ASKPASS"] = askpass_script
    return env


#: Fixed body of the throwaway ``GIT_ASKPASS`` script (testable unit).
#: Answers only GitHub HTTPS credential prompts: ``Username`` → the
#: non-interactive account name, ``Password``-like prompts → the PAT from
#: ``GITHUB_TOKEN``.  Any other prompt (host-key verification, SSH
#: passphrases, unexpected text) yields empty output with a nonzero exit
#: so no token is ever disclosed to the wrong consumer.
GIT_ASKPASS_SCRIPT_BODY = (
    'case "$1" in '
    "*Username*|*username*) echo x-access-token;; "
    "*Password*|*password*) printf '%s' \"${GITHUB_TOKEN:-}\";; "
    "*) exit 1;; "
    "esac"
)


@contextlib.asynccontextmanager
async def github_askpass_context(runtime, instance_id: str):
    """Provide a throwaway ``GIT_ASKPASS`` script for the persistent PAT.

    The script lives in ``/tmp`` *inside the workspace* (outside
    ``/workspace`` so it never leaks into repos), answers GitHub
    username/password prompts, and is removed in ``finally``.  Yields the
    script path or ``None`` when no persistent token is available.

    Token availability is probed *inside the workspace only*: the persistent
    credential file ``/root/.opencuria-env.sh`` (written at create/resume
    time).  Runner-process env and per-request env are deliberately ignored
    so the Git RPC exposes no free-env injection surface.  The askpass
    script reads ``GITHUB_TOKEN`` from its own process environment at
    git-prompt time — inheritance works because every git call runs behind
    the sourcing wrapper (:func:`git_exec_wrapper_argv`), which sources the
    same persistent file before ``exec``.  The token value is never
    embedded in argv, URLs, or logs.  Only ``GIT_ASKPASS`` is set (never
    ``SSH_ASKPASS``): SSH runs BatchMode key auth only.
    """
    exit_code, _ = await runtime.exec_command_wait(
        instance_id,
        command=["sh", "-c", "test -s /root/.opencuria-env.sh"],
        workdir="/workspace",
    )
    token_present = False
    if exit_code == 0:
        exit_code2, _ = await runtime.exec_command_wait(
            instance_id,
            command=[
                "sh",
                "-c",
                "if [ -f /root/.opencuria-env.sh ]; then "
                ". /root/.opencuria-env.sh; fi; "
                'test -n "${GITHUB_TOKEN:-}"',
                "opencuria-git-token-probe",
            ],
            workdir="/workspace",
        )
        token_present = exit_code2 == 0
    if not token_present:
        yield None
        return
    # The script reads GITHUB_TOKEN from its own environment (inherited from
    # the sourced credential file at exec time).  GitHub HTTPS prompts ask
    # for "Username" then "Password"; answer the non-interactive account
    # name for username and the PAT for password-like prompts only.
    # Unknown prompts exit nonzero with no output (never the token).
    script_body = GIT_ASKPASS_SCRIPT_BODY
    create_cmd = [
        "sh",
        "-c",
        "p=$(mktemp /tmp/opencuria-git-askpass-XXXXXX.sh) && "
        "printf '%s\\n' '#!/bin/sh' "
        + _shell_quote(script_body)
        + " > \"$p\" && chmod 700 \"$p\" && echo \"$p\"",
    ]
    exit_code, output = await runtime.exec_command_wait(
        instance_id, command=create_cmd, workdir="/workspace"
    )
    created = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if exit_code != 0 or not created.startswith("/tmp/opencuria-git-askpass-"):
        # Auth degrades to plain non-interactive (fail with a clear error
        # instead of hanging on a prompt).
        yield None
        return
    try:
        yield created
    finally:
        with contextlib.suppress(Exception):
            await runtime.exec_command_wait(
                instance_id,
                command=["rm", "-f", created],
                workdir="/workspace",
            )


def _shell_quote(text: str) -> str:
    """Single-quote *text* for embedding in a ``sh -c`` script."""
    return "'" + text.replace("'", "'\\''") + "'"


# --- porcelain v2 -z status parsing -------------------------------------------


def _split_nul(output: str) -> list[str]:
    """Split ``-z`` git output on NUL, tolerating str transport."""
    if not output:
        return []
    # Runtime exec returns str; embedded NULs survive str fine.
    return [entry for entry in output.split("\x00") if entry != ""]


def parse_status_v2(output: str) -> dict[str, Any]:
    """Parse ``git status --porcelain=v2 --branch -z`` output.

    Returns ``{"branch": {...}, "entries": [...]}`` where each entry has
    ``path``, ``old_path`` (renames/copies), ``x``/``y`` status codes,
    staged/unstaged change kinds and conflict flags.  Branch headers carry
    ``oid`` (``None`` when unborn), ``head`` (``None`` when detached),
    ``upstream`` and ``ahead``/``behind`` counts.
    """
    branch: dict[str, Any] = {
        "oid": None,
        "head": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
    }
    entries: list[dict[str, Any]] = []
    records = _split_nul(output)
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("# branch.oid "):
            value = record[len("# branch.oid "):].strip()
            branch["oid"] = None if value in {"(initial)", ""} else value
        elif record.startswith("# branch.head "):
            value = record[len("# branch.head "):].strip()
            branch["head"] = None if value in {"(detached)", ""} else value
        elif record.startswith("# branch.upstream "):
            value = record[len("# branch.upstream "):].strip()
            branch["upstream"] = value or None
        elif record.startswith("# branch.ab "):
            ahead = behind = 0
            for token in record[len("# branch.ab "):].split():
                if token.startswith("+"):
                    with contextlib.suppress(ValueError):
                        ahead = int(token[1:])
                elif token.startswith("-"):
                    with contextlib.suppress(ValueError):
                        behind = int(token[1:])
            branch["ahead"] = ahead
            branch["behind"] = behind
        elif record.startswith("1 "):
            # 1 <xy> <subm> <mH> <mI> <mW> <hH> <hI> <path>
            parts = record.split(" ", 8)
            if len(parts) < 9:
                continue
            _, xy, _subm, _mh, _mi, _mw, _hh, _hi, path = parts
            x = xy[0] if len(xy) > 0 else "."
            y = xy[1] if len(xy) > 1 else "."
            entries.append(
                {
                    "path": path,
                    "old_path": None,
                    "x": x,
                    "y": y,
                    "kind": "ordinary",
                }
            )
        elif record.startswith("2 "):
            # 2 <xy> <subm> <mH> <mI> <mW> <hH> <hI> <X><score> <path>
            # followed by NUL + <origPath>.  Paths may contain spaces,
            # so split only the fixed leading fields.
            fields = record[2:].split(" ", 8)
            if len(fields) < 9:
                continue
            xy, _subm, _mh, _mi, _mw, _hh, _hi, score, path = fields
            old_path = records[index] if index < len(records) else path
            index += 1
            x = xy[0] if len(xy) > 0 else "."
            y = xy[1] if len(xy) > 1 else "."
            entries.append(
                {
                    "path": path,
                    "old_path": old_path,
                    "x": x,
                    "y": y,
                    "kind": "renamed" if x in {"R", "C"} or y in {"R", "C"} else "ordinary",
                }
            )
        elif record.startswith("u "):
            # u <xy> <subm> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
            parts = record.split(" ", 10)
            if len(parts) < 11:
                continue
            _, xy = parts[0], parts[1]
            path = parts[-1]
            x = xy[0] if len(xy) > 0 else "U"
            y = xy[1] if len(xy) > 1 else "U"
            entries.append(
                {"path": path, "old_path": None, "x": x, "y": y, "kind": "unmerged"}
            )
        elif record.startswith("? "):
            entries.append(
                {
                    "path": record[2:],
                    "old_path": None,
                    "x": "?",
                    "y": "?",
                    "kind": "untracked",
                }
            )
        elif record.startswith("! "):
            # Ignored paths are intentionally dropped.
            continue
    return {"branch": branch, "entries": entries}


def _change_from_v2_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Map one parsed v2 entry to staged/unstaged file state dicts."""
    path: str = entry["path"]
    old_path: str | None = entry.get("old_path")
    x: str = entry.get("x", ".")
    y: str = entry.get("y", ".")
    kind: str = entry.get("kind", "ordinary")
    xy = f"{x}{y}"

    if kind == "untracked":
        return {
            "path": path,
            "old_path": None,
            "status": "A",
            "staged": None,
            "unstaged": "untracked",
            "conflict": None,
        }
    if kind == "unmerged" or xy in _CONFLICT_XY or (x == "U" or y == "U"):
        return {
            "path": path,
            "old_path": None,
            "status": "U",
            "staged": "U",
            "unstaged": "U",
            "conflict": xy if len(xy) == 2 else "UU",
        }
    staged: str | None = None
    unstaged: str | None = None
    # Staged side (index vs HEAD).
    if x not in {".", "?", "!"}:
        if x == "A":
            staged = "A"
        elif x == "D":
            staged = "D"
        elif x in {"R", "C"}:
            staged = "R" if x == "R" else "C"
        elif x == "M":
            staged = "M"
        elif x == "T":
            staged = "M"
        else:
            staged = "M"
    # Unstaged side (worktree vs index).
    if y not in {".", "?", "!"}:
        if y == "D":
            unstaged = "D"
        elif y in {"R", "C"}:
            unstaged = "R" if y == "R" else "C"
        elif y == "M":
            unstaged = "M"
        elif y == "T":
            unstaged = "M"
        else:
            unstaged = "M"
    if staged is None and unstaged is None:
        return {
            "path": path,
            "old_path": old_path,
            "status": "M",
            "staged": None,
            "unstaged": None,
            "conflict": None,
        }
    status = staged or unstaged or "M"
    return {
        "path": path,
        "old_path": old_path if (staged in {"R", "C"}) else None,
        "status": status,
        "staged": staged,
        "unstaged": unstaged,
        "conflict": None,
    }


def changes_from_status_v2(output: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return ``(changes, branch)`` from porcelain v2 ``-z`` output.

    ``changes`` holds one dict per path with ``path``, ``old_path``,
    ``status`` (``M``/``A``/``D``/``R``/``U``), ``staged`` (change kind or
    ``None``), ``unstaged`` (change kind/``untracked`` or ``None``) and
    ``conflict`` (``UU``/``AA``/… or ``None``).
    """
    parsed = parse_status_v2(output)
    changes: list[dict[str, Any]] = []
    for entry in parsed["entries"]:
        mapped = _change_from_v2_entry(entry)
        if mapped["staged"] is not None or mapped["unstaged"] is not None:
            changes.append(mapped)
    changes.sort(key=lambda item: item["path"])
    return changes, parsed["branch"]


# --- porcelain v1 -z fallback -------------------------------------------------


def parse_status_v1(output: str, branch_output: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse ``git status --porcelain=v1 -b -z`` output (fallback parser).

    Returns ``(changes, branch)`` with the same change shape as
    :func:`changes_from_status_v2`.  Rename/copy entries consume the
    following NUL record as the source path.
    """
    branch: dict[str, Any] = {
        "oid": None,
        "head": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
    }
    records = _split_nul(output)
    changes: list[dict[str, Any]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith("## "):
            header = record[3:]
            if header.startswith("No commits yet on "):
                branch["head"] = header[len("No commits yet on "):].split("...")[0].strip() or None
                branch["oid"] = None
            else:
                head_part = header.split("...")[0].strip()
                if head_part == "HEAD (no branch)":
                    branch["head"] = None
                else:
                    branch["head"] = head_part or None
                if "..." in header:
                    rest = header.split("...", 1)[1]
                    upstream = rest.split(" ")[0].strip()
                    branch["upstream"] = upstream.strip("[]") or None
                    ahead_match = re.search(r"ahead (\d+)", rest)
                    behind_match = re.search(r"behind (\d+)", rest)
                    if ahead_match:
                        branch["ahead"] = int(ahead_match.group(1))
                    if behind_match:
                        branch["behind"] = int(behind_match.group(1))
            continue
        if len(record) < 4:
            continue
        x, y = record[0], record[1]
        path = record[3:]
        old_path: str | None = None
        if x in {"R", "C"} or y in {"R", "C"}:
            # "R  new\x00old\x00"
            if index < len(records):
                old_path, path = path, records[index]
                index += 1
        xy = f"{x}{y}"
        if xy == "??":
            changes.append(
                {
                    "path": path,
                    "old_path": None,
                    "status": "A",
                    "staged": None,
                    "unstaged": "untracked",
                    "conflict": None,
                }
            )
        elif xy == "!!":
            continue
        elif xy in _CONFLICT_XY or x == "U" or y == "U":
            changes.append(
                {
                    "path": path,
                    "old_path": None,
                    "status": "U",
                    "staged": "U",
                    "unstaged": "U",
                    "conflict": xy,
                }
            )
        else:
            staged = None if x in {" ", "?"} else ("R" if x == "R" else ("C" if x == "C" else x))
            unstaged = None if y in {" ", "?"} else ("R" if y == "R" else ("C" if y == "C" else y))
            if staged is None and unstaged is None:
                continue
            changes.append(
                {
                    "path": path,
                    "old_path": old_path if staged in {"R", "C"} else None,
                    "status": staged or unstaged or "M",
                    "staged": staged,
                    "unstaged": unstaged,
                    "conflict": None,
                }
            )
    if branch_output:
        branch["oid"] = branch_output.strip() or None
    changes.sort(key=lambda item: item["path"])
    return changes, branch


# --- log / refs parsing -------------------------------------------------------


def parse_log_us_rs(output: str) -> list[dict[str, Any]]:
    """Parse ``git log --format=...%x1f...%x1e`` record output."""
    commits: list[dict[str, Any]] = []
    for record in output.split(_GIT_RS):
        record = record.strip("\n")
        if not record.strip():
            continue
        fields = record.split(_GIT_US)
        if len(fields) < 11:
            continue
        (
            full, short, parents, author, author_email, author_date,
            committer, committer_email, committer_date, subject, body,
        ) = fields[:11]
        full = full.strip()
        if not full:
            continue
        commits.append(
            {
                "hash": full,
                "short_hash": short.strip() or full[:7],
                "message": subject.strip(),
                "body": body.strip("\n"),
                "author": author.strip(),
                "author_email": author_email.strip(),
                "author_date": author_date.strip(),
                "committer": committer.strip(),
                "committer_email": committer_email.strip(),
                "committer_date": committer_date.strip(),
                "parents": [p for p in parents.split() if p],
            }
        )
    return commits


def parse_for_each_ref(output: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse ``git for-each-ref`` US-separated output into branches/remotes."""
    branches: list[dict[str, Any]] = []
    remotes: list[dict[str, Any]] = []
    for record in output.split(_GIT_RS):
        record = record.strip("\n")
        if not record.strip():
            continue
        fields = record.split(_GIT_US)
        if len(fields) < 7:
            continue
        refname, short, obj, objtype, upstream, track, head = fields[:7]
        refname = refname.strip()
        short = short.strip()
        obj = obj.strip()
        if not refname or not obj:
            continue
        if refname.startswith("refs/heads/"):
            branches.append(
                {
                    "name": short,
                    "tip_hash": obj,
                    "upstream": upstream.strip() or None,
                    "track": track.strip() or "",
                    "current": head.strip() == "*",
                }
            )
        elif refname.startswith("refs/remotes/"):
            remotes.append({"name": short, "tip_hash": obj})
    return branches, remotes


def parse_ahead_behind(track: str) -> tuple[int, int]:
    """Parse ``[ahead N][, behind M]`` for-each-ref track strings."""
    ahead = behind = 0
    if not track:
        return 0, 0
    ahead_match = re.search(r"ahead (\d+)", track)
    behind_match = re.search(r"behind (\d+)", track)
    if ahead_match:
        ahead = int(ahead_match.group(1))
    if behind_match:
        behind = int(behind_match.group(1))
    return ahead, behind


# --- numstat / raw -z parsing -------------------------------------------------


def _iter_nul_fields(output: str) -> list[str]:
    """Split ``-z`` output preserving empty fields as separators."""
    if not output:
        return []
    fields = output.split("\x00")
    # Drop the single trailing empty field git appends.
    if fields and fields[-1] == "":
        fields.pop()
    return fields


def parse_numstat_z(output: str) -> dict[str, dict[str, Any]]:
    """Parse ``git diff --numstat -z`` output keyed by new path.

    Exact ``-z`` record format (no guessing):

    * normal file: ``<added>\\t<deleted>\\t<path>\\0``
    * rename/copy: ``<added>\\t<deleted>\\t\\0<old>\\0<new>\\0``
      (empty third column anchors two extra NUL fields).

    Tabs inside filenames survive via ``split('\\t', 2)``.  A bare NUL
    field without tabs is never consumed as a rename target — it is
    skipped fail-safe on the next iteration.  Malformed records
    (missing tabs, non-numeric counts other than ``-``, missing
    rename fields, empty keys) are skipped; a malformed rename anchor
    also drops its two trailing fields to stay in sync.
    """
    stats: dict[str, dict[str, Any]] = {}
    fields = _iter_nul_fields(output)
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record or "\t" not in record:
            continue
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path_part = parts[0].strip(), parts[1].strip(), parts[2]
        if path_part == "":
            # Rename/copy anchor: exactly two more NUL fields follow.
            if index + 1 >= len(fields):
                continue
            old_path = fields[index]
            new_path = fields[index + 1]
            index += 2
            # Counts must still be valid; otherwise drop the whole record.
            if not _is_numstat_count(added_raw) or not _is_numstat_count(deleted_raw):
                continue
            if not old_path or not new_path:
                continue
            binary = added_raw == "-" or deleted_raw == "-"
            stats[new_path] = {
                "additions": 0 if binary else int(added_raw),
                "deletions": 0 if binary else int(deleted_raw),
                "binary": binary,
                "old_path": old_path,
            }
            continue
        # Normal record: path_part is the full path (may contain tabs).
        if not path_part:
            continue
        if not _is_numstat_count(added_raw) or not _is_numstat_count(deleted_raw):
            continue
        binary = added_raw == "-" or deleted_raw == "-"
        stats[path_part] = {
            "additions": 0 if binary else int(added_raw),
            "deletions": 0 if binary else int(deleted_raw),
            "binary": binary,
            "old_path": None,
        }
    return stats


def _is_numstat_count(token: str) -> bool:
    """Return True for valid numstat count tokens (digits or ``-``)."""
    if token == "-":
        return True
    return token.isdigit()


def parse_raw_z(output: str) -> dict[str, dict[str, Any]]:
    """Parse ``git diff --raw -z`` output keyed by new path."""
    infos: dict[str, dict[str, Any]] = {}
    fields = _iter_nul_fields(output)
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record.startswith(":"):
            continue
        meta = record[1:].split(" ")
        if len(meta) < 5:
            continue
        _old_mode, _new_mode, _old_hash, _new_hash, status_token = meta[:5]
        status = status_token[0] if status_token else "M"
        score = status_token[1:] if len(status_token) > 1 else ""
        if index >= len(fields):
            break
        first = fields[index]
        index += 1
        old_path: str | None = None
        new_path = first
        if status in {"R", "C"}:
            if index >= len(fields):
                break
            old_path, new_path = first, fields[index]
            index += 1
        infos[new_path] = {
            "status": status,
            "score": score,
            "old_path": old_path,
        }
    return infos


# --- unified patch parsing ----------------------------------------------------


#: C-style escapes git emits inside double-quoted paths (``core.quotepath``).
_C_QUOTE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    '"': '"',
    "\\": "\\",
}


def _c_style_unquote(token: str) -> str:
    """Unquote one C-style double-quoted path token, if quoted.

    Plain tokens pass through unchanged.  Quoted octal escapes
    (``\\303\\244`` for non-ASCII bytes) decode via UTF-8; unknown
    escapes keep their literal character.
    """
    if len(token) < 2 or not token.startswith('"') or not token.endswith('"'):
        return token
    body = token[1:-1]
    out: list[str] = []
    raw_bytes = bytearray()
    index = 0

    def _flush() -> None:
        if raw_bytes:
            out.append(bytes(raw_bytes).decode("utf-8", "surrogateescape"))
            raw_bytes.clear()

    while index < len(body):
        char = body[index]
        if char != "\\" or index + 1 >= len(body):
            _flush()
            out.append(char)
            index += 1
            continue
        nxt = body[index + 1]
        if nxt in _C_QUOTE_ESCAPES:
            _flush()
            out.append(_C_QUOTE_ESCAPES[nxt])
            index += 2
        elif nxt.isdigit():
            digits = body[index + 1:index + 4]
            if len(digits) == 3 and digits.isdigit():
                raw_bytes.append(int(digits, 8) & 0xFF)
                index += 4
            else:  # pragma: no cover - defensive, git always emits 3 digits
                _flush()
                out.append(nxt)
                index += 2
        else:
            _flush()
            out.append(nxt)
            index += 2
    _flush()
    return "".join(out)


def _truncate_text(text: str, limit: int = GIT_MAX_DIFF_BYTES) -> tuple[str, bool]:
    """Truncate *text* to *limit* bytes, returning ``(text, truncated)``."""
    raw = text.encode("utf-8", "ignore")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", "ignore") + "\n…[truncated]", True


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_patch_hunks(patch: str) -> tuple[list[dict[str, Any]], bool]:
    """Parse a unified patch into ``[{"header", "old_start", ...}]`` hunks.

    Returns ``(hunks, has_textual_diff)``.  Binary patches (``Binary files
    … differ``) yield ``([], False)``.
    """
    if not patch.strip():
        return [], False
    if "Binary files " in patch and " differ" in patch:
        return [], False
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in patch.splitlines():
        if raw_line.startswith("@@ "):
            match = _HUNK_RE.match(raw_line)
            if match:
                old_start = int(match.group(1))
                new_start = int(match.group(3))
            else:
                old_start = new_start = 0
            current = {
                "header": raw_line,
                "old_start": old_start,
                "new_start": new_start,
                "lines": [],
            }
            hunks.append(current)
            continue
        if current is None:
            continue
        if not raw_line:
            current["lines"].append({"type": "context", "content": ""})
        elif raw_line[0] == "+":
            current["lines"].append({"type": "add", "content": raw_line[1:]})
        elif raw_line[0] == "-":
            current["lines"].append({"type": "del", "content": raw_line[1:]})
        elif raw_line[0] == " ":
            current["lines"].append({"type": "context", "content": raw_line[1:]})
        elif raw_line[0] == "\\":
            continue
        else:
            continue
    if not hunks:
        return [], False
    return hunks, True


def split_patch_per_file(patch: str) -> dict[str, str]:
    """Split a multi-file unified patch into ``{new_path: patch}``.

    Each ``diff --git`` section resolves its ``new_path`` without
    guessing:

    1. ``+++``/``---`` markers (pre-hunk only).  ``+++ b/<path>`` wins;
       when ``+++`` is ``/dev/null`` (deletion) the ``--- a/<path>``
       side is used.  The marker remainder is the whole path: a leading
       ``"`` selects C-style unquoting of the raw quoted token
       (newline/quote/backslash/UTF-8 octal via :func:`_c_style_unquote`),
       otherwise the remainder up to the first ``TAB`` (timestamp
       separator, also covers the trailing-TAB git emits for unquoted
       paths with spaces) is taken verbatim — tabs survive because
       tabbed paths always arrive C-quoted.  The ``a/``/``b/`` prefix
       is stripped afterwards.
    2. Pure rename/copy/mode sections have no markers: ``rename to `` /
       ``copy to `` metadata is used (raw remainder, C-unquoted when
       quoted, no prefix to strip).
    3. Binary (or mode) sections without markers/rename fall back to a
       safely parseable ``diff --git`` header only: exactly two simple
       ``a/… b/…`` tokens or exactly two self-parsed C-quoted tokens.
       Unquoted paths with spaces (3+ tokens) are never split by
       midpoint heuristics and are dropped.

    Unparseable sections are dropped so ``raw``/``numstat`` still supply
    the file/status/binary entry without a misattributed patch.  The
    resolved key equals the ``--raw``/``--numstat -z`` new path (deleted
    files resolve via the ``---`` side to the same key).
    """
    if not patch:
        return {}
    lines = patch.splitlines(keepends=True)
    sections: list[tuple[str, list[str]]] = []
    header_rest: str | None = None
    current: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            if header_rest is not None:
                sections.append((header_rest, current))
            header_rest = line[len("diff --git "):]
            current = [line]
        elif header_rest is not None:
            current.append(line)
    if header_rest is not None:
        sections.append((header_rest, current))
    files: dict[str, str] = {}
    for hdr, sec in sections:
        new_path = _resolve_section_path(hdr, sec)
        if not new_path or new_path == "/dev/null":
            continue
        text = "".join(sec)
        if new_path in files:
            files[new_path] += text
        else:
            files[new_path] = text
    return files


def _extract_quoted_token(text: str) -> str | None:
    """Return the first C-quoted token in *text*, if well-formed.

    *text* must start with ``"``; the closing quote is located honouring
    ``\\`` escapes (``\\"`` does not close, ``\\\\`` skips both).  The
    raw token (including quotes, escapes preserved for
    :func:`_c_style_unquote`) is returned, else ``None``.
    """
    if len(text) < 2 or not text.startswith('"'):
        return None
    index = 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return text[: index + 1]
        index += 1
    return None


def _parse_marker_rest(rest: str) -> str | None:
    """Parse one ``---``/``+++`` remainder into a path or ``/dev/null``.

    C-quoted remainders unquote the raw first token directly;
    unquoted remainders take the whole path up to the first ``TAB``
    (timestamp separator).  The ``a/``/``b/`` prefix is stripped.
    Returns ``None`` when malformed.
    """
    line = rest.rstrip("\r\n")
    if not line:
        return None
    if line.startswith('"'):
        token = _extract_quoted_token(line)
        if token is None:
            return None
        path = _c_style_unquote(token)
    else:
        if '"' in line:
            return None
        path = line.split("\t", 1)[0]
        if not path:
            return None
    if path == "/dev/null":
        return "/dev/null"
    if path.startswith("a/") or path.startswith("b/"):
        stripped = path[2:]
        return stripped if stripped else None
    return path


def _parse_rename_rest(rest: str) -> str | None:
    """Parse a ``rename to``/``copy to`` remainder into a repo path."""
    line = rest.rstrip("\r\n")
    if not line:
        return None
    if line.startswith('"'):
        token = _extract_quoted_token(line)
        if token is None:
            return None
        trailing = line[len(token):]
        if trailing.strip(" \t") != "":
            return None
        path = _c_style_unquote(token)
        return path or None
    if '"' in line:
        return None
    return line or None


def _fallback_from_diff_git_header(rest: str) -> str | None:
    """Safely parse a ``diff --git`` remainder into the new path.

    Only exact cases: two simple ``a/… b/…`` tokens or two self-parsed
    C-quoted tokens.  Anything else (unquoted spaces, mixed quoting,
    trailing garbage) returns ``None`` — never a midpoint guess.
    """
    text = rest.strip()
    if not text:
        return None
    if text.startswith('"'):
        first = _extract_quoted_token(text)
        if first is None:
            return None
        remainder = text[len(first):].lstrip(" \t")
        if not remainder.startswith('"'):
            return None
        second = _extract_quoted_token(remainder)
        if second is None:
            return None
        if remainder[len(second):].strip() != "":
            return None
        old = _c_style_unquote(first)
        new = _c_style_unquote(second)
        if not old.startswith("a/") or not new.startswith("b/"):
            return None
        return new[2:] or None
    if '"' in text:
        return None
    parts = text.split(" ")
    if len(parts) != 2:
        return None
    old, new = parts
    if not old.startswith("a/") or not new.startswith("b/"):
        return None
    if len(old) <= 2 or len(new) <= 2:
        return None
    if "\t" in old or "\t" in new:
        return None
    return new[2:]


def _resolve_section_path(header_rest: str, section: list[str]) -> str | None:
    """Resolve one ``diff --git`` section to its new path, if possible."""
    minus_path: str | None = None
    plus_path: str | None = None
    plus_seen = False
    minus_seen = False
    marker_broken = False
    for line in section[1:]:
        if line.startswith("@@ "):
            break
        if line.startswith("--- "):
            minus_seen = True
            if minus_path is None and not marker_broken:
                parsed = _parse_marker_rest(line[len("--- "):])
                if parsed is not None:
                    minus_path = parsed
                else:
                    marker_broken = True
        elif line.startswith("+++ "):
            plus_seen = True
            if plus_path is None and not marker_broken:
                parsed = _parse_marker_rest(line[len("+++ "):])
                if parsed is not None:
                    plus_path = parsed
                else:
                    marker_broken = True
    if plus_seen or minus_seen:
        # Markers present: any malformed marker drops the section —
        # a half-valid pair must never reuse the surviving side.
        if marker_broken:
            return None
        if plus_path is not None and plus_path != "/dev/null":
            return plus_path
        if plus_path == "/dev/null":
            if minus_path is not None and minus_path != "/dev/null":
                return minus_path
            return None
        if plus_path is None and minus_path is None:
            pass  # malformed markers: try rename/header below
        else:
            return None
    for line in section[1:]:
        if line.startswith("@@ "):
            break
        if line.startswith("rename to "):
            parsed = _parse_rename_rest(line[len("rename to "):])
            if parsed is not None:
                return parsed
        elif line.startswith("copy to "):
            parsed = _parse_rename_rest(line[len("copy to "):])
            if parsed is not None:
                return parsed
    # Binary/mode sections without markers/rename: safe header fallback.
    return _fallback_from_diff_git_header(header_rest)


def parse_numstat_plain(output: str) -> dict[str, dict[str, Any]]:
    """Parse non-``-z`` ``--numstat`` output (fallback, no NUL support)."""
    stats: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path = parts
        # Rename arrows ``old => new`` cannot round-trip; key on display.
        try:
            added = int(added_raw)
        except ValueError:
            added = 0
        try:
            deleted = int(deleted_raw)
        except ValueError:
            deleted = 0
        stats[path] = {
            "additions": added,
            "deletions": deleted,
            "binary": added_raw.strip() == "-" or deleted_raw.strip() == "-",
            "old_path": None,
        }
    return stats


# --- file entry assembly ------------------------------------------------------


def build_file_changes(
    *,
    raw: dict[str, dict[str, Any]],
    numstat: dict[str, dict[str, Any]],
    patches: dict[str, str],
    changes: list[dict[str, Any]] | None = None,
    patch_budget: int = GIT_MAX_DIFF_FILES_WITH_PATCH,
) -> list[dict[str, Any]]:
    """Assemble frontend-facing file change dicts.

    ``raw`` comes from :func:`parse_raw_z`, ``numstat`` from
    :func:`parse_numstat_z`, ``patches`` from :func:`split_patch_per_file`.
    ``changes`` optionally restricts output to status-known paths.
    """
    entries: list[dict[str, Any]] = []
    paths = sorted(set(raw) | set(numstat) | set(patches))
    if changes is not None:
        known = {item["path"] for item in changes}
        paths = [p for p in paths if p in known]
    with_patch = 0
    for path in paths:
        info = raw.get(path, {})
        stat = numstat.get(path, {})
        status = info.get("status") or "M"
        old_path = info.get("old_path") or stat.get("old_path")
        patch_text = patches.get(path, "")
        hunks: list[dict[str, Any]] = []
        has_textual = False
        truncated = False
        if patch_text and with_patch < patch_budget:
            truncated_patch, was_truncated = _truncate_text(patch_text)
            hunks, has_textual = parse_patch_hunks(truncated_patch)
            truncated = was_truncated
            with_patch += 1
        elif patch_text:
            truncated = True
        binary = bool(stat.get("binary")) or (
            bool(patch_text) and not has_textual and "Binary files " in patch_text
        )
        entries.append(
            {
                "old_path": old_path or path,
                "new_path": path,
                "status": status,
                "additions": int(stat.get("additions", 0)),
                "deletions": int(stat.get("deletions", 0)),
                "binary": binary,
                "truncated": truncated,
                "has_textual_diff": has_textual and not binary,
                "diff": hunks,
            }
        )
    entries.sort(key=lambda item: item["new_path"])
    return entries


# --- discovery helpers (pure, testable) ---------------------------------------


def discovery_find_args(root: str = "/workspace", max_depth: int = 4) -> list[str]:
    """Return argv for ``.git`` dir discovery (prunes ``.git`` internals)."""
    return [
        "find",
        root,
        "-maxdepth",
        str(max_depth),
        "-name",
        ".git",
        "-prune",
        "-print0",
    ]


def discovery_repo_roots(find_output: str, *, limit: int = GIT_DISCOVERY_MAX_REPOS) -> list[str]:
    """Map ``find -print0`` output to repo root paths (pure helper)."""
    roots: list[str] = []
    for field in _iter_nul_fields(find_output):
        candidate = field.strip()
        if not candidate:
            continue
        # find prints the ".git" dir itself; parent is the worktree root.
        if candidate.endswith("/.git"):
            root = candidate[: -len("/.git")] or "/"
        elif candidate == ".git":
            root = "."
        else:
            continue
        if root != "/workspace" and not root.startswith("/workspace/"):
            continue
        if "/.git/" in candidate:
            continue
        if root not in roots:
            roots.append(root)
        if len(roots) >= limit:
            break
    roots.sort()
    return roots


__all__ = [
    "GIT_ALLOWED_ARGS",
    "GIT_ASKPASS_SCRIPT_BODY",
    "GIT_AUTHOR_EMAIL_MAX",
    "GIT_AUTHOR_NAME_MAX",
    "GIT_DISCOVERY_MAX_REPOS",
    "GIT_HISTORY_DEFAULT_LIMIT",
    "GIT_HISTORY_MAX_LIMIT",
    "GIT_DIFF_CONTEXT_LINES",
    "GIT_MAX_DIFF_BYTES",
    "GIT_MAX_DIFF_FILES_WITH_PATCH",
    "GIT_MAX_UNTRACKED_BYTES",
    "GIT_MAX_STDERR_BYTES",
    "GIT_READ_TIMEOUT_S",
    "GIT_NETWORK_TIMEOUT_S",
    "GIT_OPERATIONS",
    "GIT_PERSISTENT_ENV_FILE",
    "GitError",
    "build_file_changes",
    "build_git_env",
    "changes_from_status_v2",
    "check_git_args_allowed",
    "discovery_find_args",
    "discovery_repo_roots",
    "git_error_payload",
    "git_exec_wrapper_argv",
    "git_timeout_for",
    "github_askpass_context",
    "is_network_operation",
    "is_workspace_path",
    "normalize_relative_path",
    "normalize_repo_arg",
    "parse_single_path_output",
    "parse_ahead_behind",
    "parse_for_each_ref",
    "parse_log_us_rs",
    "parse_numstat_plain",
    "parse_numstat_z",
    "parse_patch_hunks",
    "parse_raw_z",
    "parse_status_v1",
    "parse_status_v2",
    "split_patch_per_file",
    "unwrap_git_exec_argv",
    "validate_author_email",
    "validate_author_identity",
    "validate_author_name",
    "validate_branch_name",
    "validate_commit_hash",
]
