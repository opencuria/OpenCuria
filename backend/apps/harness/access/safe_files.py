"""Symlink-safe, bounded file reads over the existing workspace byte stream."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
from typing import Any

from .file_types import FileContent, guess_mime_type

SAFE_MEDIA_READ_DEFAULT_MAX_SIZE = 10 * 1024 * 1024
SAFE_MEDIA_READ_TIMEOUT = 20.0
SAFE_MEDIA_READ_CLOSE_TIMEOUT = 2.0


class SafeFileReadError(RuntimeError):
    """A safe media read failed (messages intentionally omit paths/details)."""


# This script runs inside the workspace through open_process. It opens every
# component from / using dir_fd + O_NOFOLLOW, so neither directory nor leaf
# symlinks can redirect the read. Only bounded bytes are ever encoded/emitted.
_SAFE_READ_SCRIPT = r"""import base64, json, os, stat, sys


def emit(value):
    line = json.dumps(value, separators=(",", ":")) + "\n"
    sys.stdout.buffer.write(line.encode("ascii"))
    sys.stdout.buffer.flush()


def fail():
    emit({"ok": False, "error": "Unable to read file"})
    raise SystemExit(1)


fds = []
try:
    if len(sys.argv) != 4:
        fail()
    root, path, raw_limit = sys.argv[1:]
    limit = int(raw_limit)
    if (not root.startswith("/") or os.path.normpath(root) != root
            or not path.startswith("/") or os.path.normpath(path) != path
            or path == root or not path.startswith(root.rstrip("/") + "/")
            or limit < 0):
        fail()
    parts = path.lstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        fail()
    current = os.open("/", os.O_RDONLY | directory | nofollow)
    fds.append(current)
    for component in parts[:-1]:
        current = os.open(component, os.O_RDONLY | directory | nofollow,
                           dir_fd=current)
        fds.append(current)
    leaf = os.open(
        parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
        dir_fd=current)

    fds.append(leaf)
    if not stat.S_ISREG(os.fstat(leaf).st_mode):
        fail()
    data = bytearray()
    while len(data) < limit + 1:
        block = os.read(leaf, min(65536, limit + 1 - len(data)))
        if not block:
            break
        data.extend(block)
    size = max(os.fstat(leaf).st_size, len(data))
    truncated = len(data) > limit or size > limit
    payload = bytes(data[:limit])
    emit({"ok": True, "content": base64.b64encode(payload).decode("ascii"),
          "size": size, "truncated": truncated})
except SystemExit:
    raise
except BaseException:
    fail()
finally:
    for fd in reversed(fds):
        try:
            os.close(fd)
        except OSError:
            pass
"""


def _validated_path(path: str, root: str) -> str:
    """Return a normalized absolute path strictly below *root*."""
    if not isinstance(root, str) or not root or "\x00" in root:
        raise ValueError("invalid root")
    normalized_root = os.path.normpath(root)
    if not os.path.isabs(root) or normalized_root != root:
        raise ValueError("invalid root")
    if not isinstance(path, str) or not path or "\x00" in path or "\n" in path:
        raise ValueError("invalid path")
    if os.path.isabs(path):
        normalized_path = os.path.normpath(path)
        if normalized_path != path:
            raise ValueError("invalid path")
    else:
        if os.path.normpath(path) != path:
            raise ValueError("invalid path")
        normalized_path = os.path.join(root, path)
    root_prefix = root.rstrip("/") + "/"
    if normalized_path == root or not normalized_path.startswith(root_prefix):
        raise ValueError("invalid path")
    if any(part in ("", ".", "..") for part in normalized_path.lstrip("/").split("/")):
        raise ValueError("invalid path")
    return normalized_path


def _validated_limit(max_size: int | None) -> int:
    if max_size is None:
        return SAFE_MEDIA_READ_DEFAULT_MAX_SIZE
    if isinstance(max_size, bool) or not isinstance(max_size, int):
        raise ValueError("invalid size")
    if max_size < 0 or max_size > SAFE_MEDIA_READ_DEFAULT_MAX_SIZE:
        raise ValueError("invalid size")
    return max_size


async def _close_stream(stream: Any) -> None:
    """Attempt bounded remote cleanup, including when the caller is cancelled."""
    task = asyncio.create_task(stream.aclose())
    try:
        await asyncio.wait_for(asyncio.shield(task), SAFE_MEDIA_READ_CLOSE_TIMEOUT)
    except asyncio.CancelledError:
        # Keep cleanup alive across a caller cancellation, but never leave a
        # close task hanging indefinitely.
        try:
            await asyncio.wait_for(asyncio.shield(task), SAFE_MEDIA_READ_CLOSE_TIMEOUT)
        except BaseException:
            task.cancel()
        raise
    except BaseException:
        if not task.done():
            task.cancel()


async def _read_stream(
    accessor: Any, root: str, path: str, limit: int, timeout: float
) -> bytes:
    """Open the isolated reader process, drain its bounded JSON result, close."""
    stream = await accessor.open_process(
        ["python3", "-u", "-c", _SAFE_READ_SCRIPT, root, path, str(limit)],
        workdir=root,
        env={},
        timeout=timeout,
    )
    try:
        await stream.send_eof()
        maximum_output = (limit + 1) * 4 // 3 + 4096
        output = bytearray()
        while True:
            chunk = await stream.receive()
            if not chunk:
                break
            if len(output) + len(chunk) > maximum_output:
                raise SafeFileReadError("Safe file read failed")
            output.extend(chunk)
        exit_code = await stream.wait_closed()
        if exit_code != 0:
            raise SafeFileReadError("Safe file read failed")
        return bytes(output)
    finally:
        await _close_stream(stream)


async def read_file_no_follow(
    accessor: Any,
    path: str,
    max_size: int | None = None,
    *,
    root: str = "/workspace",
    timeout: float = SAFE_MEDIA_READ_TIMEOUT,
) -> FileContent:
    """Read a normalized regular file below root without following symlinks.

    ``root`` is injectable for tests; production callers use ``/workspace``.
    The data plane uses ``open_process`` (not the JSON file RPC), with a total
    bounded read deadline and raw binary payload encoded only on process stdout.
    """
    try:
        normalized_path = _validated_path(path, root)
        limit = _validated_limit(max_size)
    except (TypeError, ValueError):
        raise SafeFileReadError("Safe file read failed") from None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise SafeFileReadError("Safe file read failed")

    try:
        raw = await asyncio.wait_for(
            _read_stream(
                accessor, root, normalized_path, limit, float(timeout)
            ),
            timeout=float(timeout),
        )
        result = json.loads(raw.decode("ascii"))
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise SafeFileReadError("Safe file read failed")
        encoded = result.get("content")
        if not isinstance(encoded, str):
            raise SafeFileReadError("Safe file read failed")
        content = base64.b64decode(encoded, validate=True)
        size = result.get("size")
        truncated = result.get("truncated")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(truncated, bool)
            or len(content) > limit
            or size < len(content)
            or (not truncated and size != len(content))
            or (truncated and len(content) == 0 and size == 0)
        ):
            raise SafeFileReadError("Safe file read failed")
        return FileContent(
            content=content,
            size=size,
            truncated=truncated,
            mime=guess_mime_type(normalized_path),
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        raise SafeFileReadError("Safe file read timed out") from None
    except SafeFileReadError:
        raise
    except BaseException:
        # Never echo paths, command output, OS errors, or tracebacks to callers.
        raise SafeFileReadError("Safe file read failed") from None
