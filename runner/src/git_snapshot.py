"""Framed metadata reads for one Git snapshot, without guest temporary files."""

from __future__ import annotations

from . import git as git_ops


def snapshot_read_argv(marker: str) -> list[str]:
    """Batch fixed read commands; keep the framing marker a literal argument.

    Output is streamed unchanged, including porcelain's NUL-delimited paths.
    Only the primary status failure stops the batch; optional reads retain
    their own exit codes. The nonce prevents repository text from impersonating
    a frame; ambiguous or incomplete framing is rejected by the parser.
    """
    ref_format = (
        f"%(refname){git_ops._GIT_US}%(refname:short){git_ops._GIT_US}"
        f"%(objectname){git_ops._GIT_US}%(objecttype){git_ops._GIT_US}"
        f"%(upstream:short){git_ops._GIT_US}%(upstream:track){git_ops._GIT_US}"
        f"%(HEAD){git_ops._GIT_RS}"
    )
    script = (
        git_ops.GIT_SOURCE_ENV_SCRIPT
        + r"""
oc_git_marker=$1
oc_git_read() {
    if "$@" 2>&1; then oc_git_code=0; else oc_git_code=$?; fi
    printf '\000%s:%s\000' "$oc_git_marker" "$oc_git_code"
    return "$oc_git_code"
}
oc_git_read command git status --porcelain=v2 --branch \
    --untracked-files=all -z || exit 0
oc_git_read command git for-each-ref --format="$2" \
    refs/heads refs/remotes refs/tags || :
oc_git_read command git remote || :
oc_git_read command git symbolic-ref refs/remotes/origin/HEAD || :
"""
    )
    return ["sh", "-c", script, "opencuria-git-snapshot", marker, ref_format]


def parse_snapshot_reads(output: str, marker: str) -> list[tuple[int, str]]:
    """Return exact command outputs/statuses, rejecting partial/ambiguous frames."""
    boundary = f"\0{marker}:"
    results: list[tuple[int, str]] = []
    cursor = 0
    while len(results) < 4:
        end = output.find(boundary, cursor)
        status_end = output.find("\0", end + len(boundary)) if end >= 0 else -1
        if end < 0 or status_end < 0:
            break
        status = output[end + len(boundary) : status_end]
        if (
            not 1 <= len(status) <= 3
            or not status.isascii()
            or not status.isdecimal()
            or int(status) > 255
        ):
            break
        results.append((int(status), output[cursor:end]))
        cursor = status_end + 1
        if len(results) == 1 and results[0][0] != 0:
            break
    expected = 1 if results and results[0][0] != 0 else 4
    if len(results) != expected or cursor != len(output):
        raise git_ops.GitError("git_failed", "Invalid Git snapshot read response")
    return results
