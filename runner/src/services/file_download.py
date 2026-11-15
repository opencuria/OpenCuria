"""Guest-side type probe and bounded regular-file download in one exec."""

from __future__ import annotations

FILE_DOWNLOAD_TOO_LARGE = 65

_DOWNLOAD_SCRIPT = f"""\
if [ -d "$1" ]; then
    printf 'directory\\n'
    exit 0
fi
test -f "$1" || exit 1
size=$(stat -c '%s' -- "$1") || exit 1
if [ "$size" -gt "$2" ]; then exit {FILE_DOWNLOAD_TOO_LARGE}; fi
printf '%s\\n' "$size"
exec base64 -- "$1"
"""


def file_download_command(path: str, maximum: int) -> list[str]:
    """Emit a directory marker or a size line and base64 for a validated path.

    Check the size before emitting any file content. Paths remain opaque argv
    values, including quotes and shell metacharacters.
    """
    return ["sh", "-c", _DOWNLOAD_SCRIPT, "opencuria-file-download", path, str(maximum)]
