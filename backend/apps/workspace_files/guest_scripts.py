"""Commands that stat, stream, archive, write and thumbnail workspace files.

Git blobs (a file as the index or a commit has it) stat and stream like files.

They run inside the guest (Linux, or MSYS2 bash on Windows) through the
runner's process streams. Paths are always argv values, never interpolated
into source. Every command resolves symlinks in the guest and refuses targets
outside ``/workspace``, the same sandbox the file explorer always had.

Absent optional arguments are passed as ``-`` (argv entries must not be
empty). Streaming commands answer with one header line before any payload::

    OK <size> <mtime> [<first> <last>]   payload follows
    NOTMOD <size> <mtime>                the caller's ETag still matches
    RANGE <size>                         the requested range is unsatisfiable
    ERR <code> [<size>]                  nothing follows
"""

from __future__ import annotations

#: Root every client-facing file operation is confined to.
WORKSPACE_ROOT = "/workspace"

_RESOLVE = r"""
root=$(realpath -e /workspace 2>/dev/null) || { echo 'ERR unavailable'; exit 0; }
real=$(realpath -e -- "$1" 2>/dev/null) || { echo 'ERR missing'; exit 0; }
case $real in "$root"|"$root"/*) ;; *) echo 'ERR outside'; exit 0 ;; esac
"""

# Byte range of a payload of $size bytes from the arguments $2 (first),
# $3 (last) and $4 (suffix length): sets $first and $last or answers RANGE.
_RANGE = r"""
first=0; last=$((size - 1))
if [ "$4" != - ]; then
    [ "$4" -gt 0 ] && [ "$size" -gt 0 ] || { echo "RANGE $size"; exit 0; }
    [ "$4" -lt "$size" ] && first=$((size - $4))
elif [ "$2" != - ]; then
    [ "$2" -lt "$size" ] || { echo "RANGE $size"; exit 0; }
    first=$2
    [ "$3" != - ] && [ "$3" -lt "$last" ] && last=$3
fi
"""

_READ = (
    _RESOLVE
    + r"""
[ -d "$real" ] && { echo 'ERR directory'; exit 0; }
[ -f "$real" ] || { echo 'ERR unsupported'; exit 0; }
[ -r "$real" ] || { echo 'ERR unreadable'; exit 0; }
info=$(stat -c '%s %Y' -- "$real") || { echo 'ERR unreadable'; exit 0; }
size=${info% *}; mtime=${info#* }
if [ "$size" -gt "$5" ]; then echo "ERR too_large $size"; exit 0; fi
if [ "$6" = "$size-$mtime" ]; then
    echo "NOTMOD $size $mtime"; exit 0
fi
"""
    + _RANGE
    + r"""
echo "OK $size $mtime $first $last"
[ "$size" -gt 0 ] || exit 0
if [ "$first" -eq 0 ] && [ "$last" -eq $((size - 1)) ]; then exec cat -- "$real"; fi
tail -c +"$((first + 1))" -- "$real" | head -c "$((last - first + 1))"
"""
)

# Git commands run in the repository named by $1, which must be inside
# /workspace. Blobs have no modification time (0); their object id is their
# version, so a matching ETag is the object id itself.
_GIT_REPOSITORY = r"""
root=$(realpath -e /workspace 2>/dev/null) || { echo 'ERR unavailable'; exit 0; }
repo=$(realpath -e -- "$1" 2>/dev/null) || { echo 'ERR missing'; exit 0; }
case $repo in "$root"|"$root"/*) ;; *) echo 'ERR outside'; exit 0 ;; esac
cd -- "$repo" 2>/dev/null || { echo 'ERR missing'; exit 0; }
command -v git >/dev/null 2>&1 || { echo 'ERR unavailable'; exit 0; }
"""

# $2 is a blob name such as ':0:path', 'HEAD:path' or '<commit>:path'. A path
# the revision lacks (or an unborn HEAD) is missing; trees and submodules are
# not files.
_GIT_STAT = (
    _GIT_REPOSITORY
    + r"""
oid=$(git rev-parse --verify --quiet "$2" 2>/dev/null) || {
    echo 'ERR missing'; exit 0; }
[ "$(git cat-file -t "$oid" 2>/dev/null)" = blob ] || {
    echo 'ERR unsupported'; exit 0; }
size=$(git cat-file -s "$oid" 2>/dev/null) || { echo 'ERR unreadable'; exit 0; }
echo "OK $oid $size"
"""
)

# Arguments as for _READ, plus the object id as $7.
_GIT_READ = (
    _GIT_REPOSITORY
    + r"""
[ "$(git cat-file -t "$7" 2>/dev/null)" = blob ] || { echo 'ERR missing'; exit 0; }
size=$(git cat-file -s "$7" 2>/dev/null) || { echo 'ERR unreadable'; exit 0; }
if [ "$size" -gt "$5" ]; then echo "ERR too_large $size"; exit 0; fi
if [ "$6" = "$7" ]; then echo "NOTMOD $size 0"; exit 0; fi
"""
    + _RANGE
    + r"""
echo "OK $size 0 $first $last"
[ "$size" -gt 0 ] || exit 0
if [ "$first" -eq 0 ] && [ "$last" -eq $((size - 1)) ]; then
    exec git cat-file blob "$7"
fi
git cat-file blob "$7" | tail -c +"$((first + 1))" | head -c "$((last - first + 1))"
"""
)

# The archive goes to an unlinked temporary file first, so its exact size is
# known (Content-Length, limit) before any byte leaves the guest. POSIX sh's
# file-size limit counts 512-byte blocks: allow at most maximum+512 bytes.
_ARCHIVE = (
    _RESOLVE
    + r"""
[ -d "$real" ] || { echo 'ERR unsupported'; exit 0; }
parent=${real%/*}; [ -n "$parent" ] || parent=/
archive=$(mktemp) || { echo 'ERR unavailable'; exit 0; }
exec 3< "$archive" 4> "$archive" || { echo 'ERR unavailable'; exit 0; }
rm -f -- "$archive"
blocks=$(($2 / 512 + 1))
current=$(ulimit -f)
if [ "$current" = unlimited ] || [ "$current" -gt "$blocks" ]; then
    ulimit -f "$blocks" 2>/dev/null
fi
status=0
{ tar czf - -C "$parent" -- "${real##*/}" >&4; } 2>/dev/null || status=$?
exec 4>&-
size=$(stat -Lc '%s' /proc/self/fd/3) || { echo 'ERR unavailable'; exit 0; }
if [ "$size" -gt "$2" ]; then echo "ERR too_large $size"; exit 0; fi
[ "$status" -eq 0 ] || { echo 'ERR archive_failed'; exit 0; }
echo "OK $size $(date +%s) 0 $((size - 1))"
exec cat <&3
"""
)

# Bytes arrive on stdin. They go to a hidden temporary file next to the target
# and are renamed into place only when exactly the announced size arrived, so
# an aborted upload never leaves a partial file behind.
_WRITE = r"""
root=$(realpath -e /workspace 2>/dev/null) || { echo 'ERR unavailable'; exit 0; }
dir=$(realpath -m -- "$1" 2>/dev/null) || { echo 'ERR invalid'; exit 0; }
case $dir in "$root"|"$root"/*) ;; *) echo 'ERR outside'; exit 0 ;; esac
mkdir -p -- "$dir" 2>/dev/null
[ -d "$dir" ] || { echo 'ERR not_directory'; exit 0; }
case $2 in ?*.*) base=${2%.*}; ext=.${2##*.} ;; *) base=$2; ext= ;; esac
target=$dir/$2; n=0
next() { n=$((n + 1)); target=$dir/${base}_$n$ext; }
if [ "$4" = rename ]; then
    while [ -e "$target" ] || [ -L "$target" ]; do next; done
elif [ -d "$target" ]; then
    echo 'ERR is_directory'; exit 0
fi
tmp=$(mktemp "$dir/.opencuria-upload.XXXXXXXX" 2>/dev/null) || {
    echo 'ERR unwritable'; exit 0; }
trap 'rm -f -- "$tmp"' EXIT
trap 'exit 1' HUP INT TERM PIPE
head -c "$(($3 + 1))" > "$tmp" || { echo 'ERR unwritable'; exit 0; }
size=$(stat -c '%s' -- "$tmp")
[ "$size" = "$3" ] || { echo "ERR incomplete $size"; exit 0; }
chmod 644 -- "$tmp"
if [ "$4" = rename ]; then
    while :; do
        mv -n -- "$tmp" "$target" 2>/dev/null
        [ -e "$tmp" ] || break
        next
    done
else
    mv -f -- "$tmp" "$target" || { echo 'ERR unwritable'; exit 0; }
fi
trap - EXIT
echo "OK $size"
printf '%s\n' "$target"
"""

# One small, already blurred JPEG of the first frame (videos) or the picture
# itself (images). ffmpeg ships with the workspace images; without it the
# caller falls back to decoding images itself.
_THUMBNAIL = (
    _RESOLVE
    + r"""
[ -f "$real" ] || { echo 'ERR unsupported'; exit 0; }
command -v ffmpeg >/dev/null 2>&1 || { echo 'ERR no_ffmpeg'; exit 0; }
out=$(mktemp) || { echo 'ERR unavailable'; exit 0; }
trap 'rm -f -- "$out"' EXIT
filter="scale=$2:$2:force_original_aspect_ratio=decrease:force_divisible_by=2"
filter="$filter,gblur=sigma=1.2"
render() {
    ffmpeg -nostdin -v error -y "$@" -i "$real" -frames:v 1 -vf "$filter" \
        -q:v 6 -update 1 -f image2 -c:v mjpeg "$out" 2>/dev/null && [ -s "$out" ]
}
# Stills have no timeline (a seek, even to 0, can skip JPEG frames); videos
# show a frame after the seek or, when shorter, their first one.
if [ "$3" = - ]; then render; else render -ss "$3" || render; fi || {
    echo 'ERR undecodable'; exit 0; }
size=$(stat -c '%s' -- "$out")
echo "OK $size 0 0 $((size - 1))"
exec cat -- "$out"
"""
)

_STAT = r"""
import json, os, stat, sys
root = os.path.realpath("/workspace")
out = []
for path in sys.argv[1:]:
    try:
        real = os.path.realpath(path)
        if real != root and not real.startswith(root + os.sep):
            out.append({"error": "outside"})
            continue
        info = os.stat(real)
        if stat.S_ISDIR(info.st_mode):
            kind = "directory"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            out.append({"error": "unsupported"})
            continue
        out.append({"type": kind, "size": info.st_size, "mtime": int(info.st_mtime)})
    except (FileNotFoundError, NotADirectoryError):
        out.append({"error": "missing"})
    except OSError:
        out.append({"error": "unreadable"})
print(json.dumps(out))
"""


def _optional(value: int | None) -> str:
    """Encode an absent number; stream argv entries must not be empty."""
    return "-" if value is None else str(value)


def stat_command(paths: list[str]) -> list[str]:
    """Return argv printing one JSON entry per path (type, size, mtime)."""
    return ["python3", "-c", _STAT, *paths]


def read_command(
    path: str,
    *,
    first: int | None,
    last: int | None,
    suffix: int | None,
    maximum: int,
    etag: str = "",
) -> list[str]:
    """Return argv streaming a regular file or one byte range of it."""
    return [
        "sh",
        "-c",
        _READ,
        "opencuria-file-read",
        path,
        _optional(first),
        _optional(last),
        _optional(suffix),
        str(maximum),
        etag or "-",
    ]


def git_stat_command(repo: str, name: str) -> list[str]:
    """Return argv printing ``OK <object id> <size>`` of the blob *name*."""
    return ["sh", "-c", _GIT_STAT, "opencuria-git-stat", repo, name]


def git_read_command(
    repo: str,
    object_id: str,
    *,
    first: int | None,
    last: int | None,
    suffix: int | None,
    maximum: int,
    etag: str = "",
) -> list[str]:
    """Return argv streaming one Git blob or one byte range of it."""
    return [
        "sh",
        "-c",
        _GIT_READ,
        "opencuria-git-read",
        repo,
        _optional(first),
        _optional(last),
        _optional(suffix),
        str(maximum),
        etag or "-",
        object_id,
    ]


def archive_command(path: str, maximum: int) -> list[str]:
    """Return argv streaming a directory as a gzip-compressed tar archive."""
    return ["sh", "-c", _ARCHIVE, "opencuria-file-archive", path, str(maximum)]


def write_command(directory: str, filename: str, size: int, conflict: str) -> list[str]:
    """Return argv writing exactly *size* stdin bytes as *directory*/*filename*.

    With ``conflict="rename"`` an existing name gets a ``_<n>`` suffix; with
    ``"replace"`` the file is replaced atomically.
    """
    return [
        "sh",
        "-c",
        _WRITE,
        "opencuria-file-write",
        directory,
        filename,
        str(size),
        conflict,
    ]


def thumbnail_command(
    path: str, edge: int, seek_seconds: float | None = None
) -> list[str]:
    """Return argv rendering a blurred JPEG of at most *edge* pixels per side.

    Videos pass *seek_seconds*; images have no timeline and pass ``None``.
    """
    return [
        "sh",
        "-c",
        _THUMBNAIL,
        "opencuria-file-thumbnail",
        path,
        str(int(edge)),
        "-" if seek_seconds is None else f"{seek_seconds:g}",
    ]
