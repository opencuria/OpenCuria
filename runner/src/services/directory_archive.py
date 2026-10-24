"""Bounded, single-pass archive creation inside a workspace guest."""

from __future__ import annotations

import os

DIRECTORY_ARCHIVE_TOO_LARGE = 65

# Separate read/write descriptors keep independent offsets. Unlink before tar
# starts so errors, signals and caller cancellation leave no named archive.
# POSIX sh's file-size limit uses 512-byte blocks: allow at most maximum+512
# bytes on disk, then check the exact size before emitting any base64.
_ARCHIVE_SCRIPT = f"""\
archive=$(mktemp) || exit 1
trap 'rm -f -- "$archive"' EXIT
trap 'exit 1' HUP INT TERM
exec 3< "$archive" 4> "$archive" || exit 1
rm -f -- "$archive" || exit 1
trap - EXIT
blocks=$(($3 / 512 + 1))
current=$(ulimit -f) || exit 1
if [ "$current" = unlimited ] || [ "$current" -gt "$blocks" ]; then
    ulimit -f "$blocks" || exit 1
fi
status=0
{{ tar czf - -C "$1" -- "$2" >&4; }} 2>/dev/null || status=$?
exec 4>&-
size=$(stat -Lc '%s' /proc/self/fd/3) || exit 1
if [ "$size" -gt "$3" ]; then exit {DIRECTORY_ARCHIVE_TOO_LARGE}; fi
if [ "$status" -ne 0 ]; then
    echo 'Archive creation failed' >&2
    exit 1
fi
printf '%s\\n' "$size"
base64 <&3
"""


def directory_archive_command(path: str, maximum: int) -> list[str]:
    """Return guest argv emitting a size line and base64 for a validated path."""
    return [
        "sh",
        "-c",
        _ARCHIVE_SCRIPT,
        "opencuria-directory-download",
        os.path.dirname(path),
        os.path.basename(path),
        str(maximum),
    ]
