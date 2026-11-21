"""Workspace file operations for the harness, the editor and the explorer.

:class:`FileManager` owns bounded harness reads/writes, editor documents,
listings and path checks. Clients stream file contents over HTTP through the
backend's process streams instead. Pure find and tar helpers live here too.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shlex
import tarfile
import uuid
from collections.abc import Callable

import structlog

from ..editor_protocol import EDITOR_FILE_SCRIPT, EDITOR_MAX_BYTES, editor_command
from ..models import WorkspaceInfo
from ..runtime.base import RuntimeBackend
from .capture_fence import CaptureFence, live_interaction
from .workspace_helpers import sanitize_path as _sanitize_path

logger = structlog.get_logger(__name__)

FIND_FILES_DEFAULT_LIMIT = 50
FIND_FILES_PRUNE_NAMES = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    ".next",
)
_FIND_FILES_QUERY_RE = re.compile(r"^[A-Za-z0-9_/:.+-]*$")
_FIND_FILES_SUCCESS_EXIT_CODES = {0, 1, 141}

FILE_READ_DEFAULT_MAX_SIZE = 5 * 1024 * 1024  # 5 MB
FILE_READ_ABSOLUTE_MAX_SIZE = 100 * 1024 * 1024  # 100 MB
FILE_UPLOAD_MAX_SIZE = 10 * 1024 * 1024  # 10 MB, chunked harness writes


def build_tar_entries(
    files: list[tuple[str, bytes, int]],
) -> bytes:
    """Build a tar archive containing multiple files."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for filename, content, mode in files:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            info.mode = mode
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def build_single_file_tar(filename: str, content: bytes) -> bytes:
    """Build a tar archive containing exactly one file."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(content)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def sanitize_find_query(query: str) -> str:
    """Return a safe ``find -ipath`` query fragment.

    Only characters that the chat ``@`` mention regex allows are accepted.
    ``..`` is rejected even though ``.`` is otherwise valid.
    """
    cleaned = (query or "").strip()
    if ".." in cleaned or not _FIND_FILES_QUERY_RE.fullmatch(cleaned):
        raise ValueError("Invalid find query")
    return cleaned


def build_find_files_command(query: str, limit: int) -> list[str]:
    """Build ``bash -lc`` argv that finds workspace files up to *limit*.

    Prunes common junk directories. An empty *query* lists shallower paths
    first; a non-empty query uses case-insensitive ``-ipath``.
    """
    capped = max(1, min(int(limit), FIND_FILES_DEFAULT_LIMIT))
    prune = " -o ".join(
        f"-name {shlex.quote(name)}" for name in FIND_FILES_PRUNE_NAMES
    )
    match = ""
    if query:
        match = f"-ipath {shlex.quote(f'*{query}*')} "
    pipeline = (
        f"find {shlex.quote('/workspace')} \\( {prune} \\) -prune "
        f"-o -type f {match}-printf '%d\\t%p\\n' "
        f"| sort -n | head -n {capped + 1}"
    )
    return ["bash", "-lc", pipeline]


class FileManager:
    """Owns stateful workspace file operations.

    Runtime backends and workspace lookup functions are injected by the
    runner composition root. Upload limits use
    :data:`FILE_UPLOAD_MAX_SIZE`.
    """

    def __init__(
        self,
        runtime: RuntimeBackend | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
    ) -> None:
        self.capture_fence: CaptureFence | None = None
        self._runtime = runtime
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        # Limit concurrent file-read SSH channels per workspace to avoid
        # exhausting the SSH server's MaxSessions limit (default: 10).
        # Each read_file call opens at most 1 SSH channel, so a limit of 4
        # keeps peak channel usage well below 10.
        self._file_read_semaphores: dict[uuid.UUID, asyncio.Semaphore] = {}
        self._editor_write_lock = asyncio.Lock()

    @live_interaction
    async def read_editor_file(self, workspace_id: uuid.UUID, path: str) -> dict:
        """Read complete UTF-8 text and its revision for an editor document."""
        safe_path = self._file_path(path, allow_external=False)
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )
        code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=editor_command(EDITOR_FILE_SCRIPT, {
                "operation": "read", "path": safe_path,
            }),
            workdir="/workspace",
        )
        if code:
            raise RuntimeError("Could not read editor file")
        return json.loads(output)

    @live_interaction
    async def save_editor_file(
        self, workspace_id: uuid.UUID, path: str, content: str,
        expected_revision: str,
    ) -> dict:
        """Atomically save complete text after checking the on-disk revision."""
        safe_path = self._file_path(path, allow_external=False)
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("Expected UTF-8 text")
        decoded = content.encode("utf-8")
        if len(decoded) > EDITOR_MAX_BYTES:
            raise ValueError("The editor supports files up to 5 MiB")
        if not isinstance(expected_revision, str) or not re.fullmatch(
            r"[0-9a-f]{64}|missing", expected_revision
        ):
            raise ValueError("Invalid expected_revision")
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        async with self._editor_write_lock:
            safe_path = await self._realpath_under_workspace(
                runtime, info.instance_id, safe_path
            )
            name = f".opencuria-editor-{uuid.uuid4().hex}"
            directory = os.path.dirname(safe_path)
            temporary = os.path.join(directory, name)
            try:
                await runtime.put_archive(
                    info.instance_id, directory, build_single_file_tar(name, decoded)
                )
                code, output = await runtime.exec_command_wait(
                    info.instance_id,
                    command=editor_command(EDITOR_FILE_SCRIPT, {
                        "operation": "write", "path": safe_path,
                        "temporary": temporary,
                        "expected_revision": expected_revision,
                    }),
                    workdir="/workspace",
                )
                if code:
                    raise RuntimeError("Could not save editor file")
                return json.loads(output)
            finally:
                await runtime.exec_command_wait(
                    info.instance_id, command=["rm", "-f", "--", temporary],
                    workdir="/workspace",
                )

    @staticmethod
    def _file_path(path: str, *, allow_external: bool) -> str:
        """Normalize a guest path; only harness RPCs may leave /workspace."""
        if not allow_external:
            return _sanitize_path(path)
        if (
            not isinstance(path, str)
            or not path.strip()
            or "\x00" in path
            or "\n" in path
        ):
            raise ValueError(f"Invalid path: {path}")
        candidate = path if os.path.isabs(path) else f"/workspace/{path}"
        return os.path.normpath(candidate)

    async def _realpath_under_workspace(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        path: str,
        *,
        allow_external: bool = False,
    ) -> str:
        """Resolve symlinks; keep non-harness paths within /workspace.

        Runs ``realpath -m`` inside the workspace, which resolves symlinks
        and ``..`` segments. For non-harness calls, raises ``ValueError``
        when the resolved path escapes ``/workspace``. Falls back to *path*
        when ``realpath`` is unavailable in the image (coreutils ships it on
        Ubuntu, so this is only a safety net). Note: check-then-use is
        inherently TOCTOU-prone if the workspace mutates the link between
        the check and the file operation; accepted here as defense-in-depth
        on top of the non-harness ``/workspace`` sandbox.
        """
        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["realpath", "-m", path],
            workdir="/workspace",
        )
        if exit_code != 0:
            return path
        real = output.removesuffix("\n")
        if not real:
            return path
        if "\n" in real or "\x00" in real:
            raise ValueError("Invalid resolved file path")
        if not allow_external and real != "/workspace" and not real.startswith(
            "/workspace/"
        ):
            raise ValueError(f"Path escapes /workspace: {path}")
        return real

    @live_interaction
    async def list_files(
        self,
        workspace_id: uuid.UUID,
        path: str,
        *,
        allow_external: bool = False,
    ) -> list[dict]:
        """List files and directories at *path* inside the workspace.

        Returns a list of dicts with ``name``, ``path``, ``type``, ``size``.
        """
        safe_path = self._file_path(path, allow_external=allow_external)
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path, allow_external=allow_external
        )

        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=[
                "find",
                safe_path,
                "-maxdepth",
                "1",
                "-mindepth",
                "1",
                "-printf",
                r"%y\t%s\t%p\n",
            ],
            workdir="/workspace",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to list files: {output}")

        entries: list[dict] = []
        for line in output.strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            file_type_char, size_str, file_path = parts
            entries.append(
                {
                    "name": os.path.basename(file_path),
                    "path": file_path,
                    "type": "directory" if file_type_char == "d" else "file",
                    "size": int(size_str) if size_str.isdigit() else 0,
                }
            )

        # Sort: directories first, then alphabetically
        entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
        return entries

    @live_interaction
    async def find_files(
        self,
        workspace_id: uuid.UUID,
        query: str = "",
        limit: int = FIND_FILES_DEFAULT_LIMIT,
    ) -> dict:
        """Search workspace files for mention autocomplete.

        Returns ``{"paths": [{"path", "name"}], "truncated": bool}``. Results
        are capped at ``FIND_FILES_DEFAULT_LIMIT``.
        """
        safe_query = sanitize_find_query(query)
        capped = max(1, min(int(limit), FIND_FILES_DEFAULT_LIMIT))
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        command = build_find_files_command(safe_query, capped)
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=command,
            workdir="/workspace",
        )
        if exit_code not in _FIND_FILES_SUCCESS_EXIT_CODES:
            raise RuntimeError(f"Failed to find files: {output}")

        paths: list[dict] = []
        for line in output.strip().splitlines():
            parts = line.split("\t", 1)
            file_path = parts[-1].strip()
            if not file_path:
                continue
            if file_path != "/workspace" and not file_path.startswith(
                "/workspace/"
            ):
                continue
            paths.append(
                {
                    "name": os.path.basename(file_path),
                    "path": file_path,
                }
            )

        truncated = len(paths) > capped
        return {"paths": paths[:capped], "truncated": truncated}

    @live_interaction
    async def read_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
        max_size: int | None = None,
        *,
        allow_external: bool = False,
    ) -> dict:
        """Read a file from the workspace VM.

        Returns a dict with ``content`` (base64), ``size``, ``truncated``,
        and ``mime_type``.

        Concurrent reads are throttled via a per-workspace semaphore to
        avoid exceeding the SSH server's MaxSessions limit when many images
        are fetched simultaneously.
        """
        safe_path = self._file_path(path, allow_external=allow_external)
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        # One semaphore per workspace; created lazily.
        sem = self._file_read_semaphores.get(workspace_id)
        if sem is None:
            sem = asyncio.Semaphore(4)
            self._file_read_semaphores[workspace_id] = sem

        if max_size is None:
            read_limit = FILE_READ_DEFAULT_MAX_SIZE
        else:
            read_limit = int(max_size)
            if read_limit <= 0:
                raise ValueError("max_size must be a positive integer")
            if read_limit > FILE_READ_ABSOLUTE_MAX_SIZE:
                raise ValueError(
                    f"max_size exceeds allowed maximum ({FILE_READ_ABSOLUTE_MAX_SIZE} bytes)"
                )

        async with sem:
            if self.capture_fence is not None:
                self.capture_fence.check_current(workspace_id)
            safe_path = await self._realpath_under_workspace(
                runtime, info.instance_id, safe_path, allow_external=allow_external
            )
            # Combine stat + read into a single SSH exec to halve the number
            # of SSH channels opened compared to two sequential commands.
            # Output format:
            #   line 1 = file size (bytes)
            #   line 2 = MIME type
            #   rest   = base64 content
            # Paths are embedded via shlex.quote so a quote in the path
            # cannot break out of the shell quoting.
            qpath = shlex.quote(safe_path)
            shell_cmd = (
                # Guard: exit 1 immediately if the file does not exist.
                # Without this, the else-branch's `head | base64` pipeline
                # exits 0 even on a missing file, causing a ValueError when
                # we try to parse the empty first line as an integer.
                f"test -f {qpath} || exit 1; "
                f"SZ=$(stat -c '%s' {qpath}); "
                f"MT=$(file --mime-type -b {qpath} 2>/dev/null "
                "|| echo 'application/octet-stream'); "
                f'echo "$SZ"; '
                f'echo "$MT"; '
                f'if [ "$SZ" -le {read_limit} ]; then '
                f"  base64 {qpath}; "
                f"else "
                f"  head -c {read_limit} {qpath} | base64; "
                f"fi"
            )
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=["sh", "-c", shell_cmd],
                workdir="/workspace",
            )

        if exit_code != 0:
            raise RuntimeError(f"Failed to read file: {output}")

        # The guest emits LF-delimited size/MIME headers and wrapped base64.
        # Split only the headers: splitting/joining every base64 line allocates
        # hundreds of thousands of strings for large reads on the event loop.
        lines = output.split("\n", 2)
        if len(lines) < 2 or (len(lines) == 2 and not lines[1]):
            raise RuntimeError("Invalid file read response format")
        file_size = int(lines[0].strip())
        mime_type = lines[1].strip() or "application/octet-stream"
        content_output = lines[2] if len(lines) > 2 else ""
        truncated = file_size > read_limit

        return {
            "content": content_output.strip(),
            "size": file_size,
            "truncated": truncated,
            "mime_type": mime_type,
        }

    @live_interaction
    async def write_file_content(
        self,
        workspace_id: uuid.UUID,
        path: str,
        content_b64: str,
        mode: int = 0o644,
        *,
        allow_external: bool = False,
    ) -> None:
        """Write file content atomically inside the workspace VM.

        Args:
            workspace_id: Target workspace.
            path: Absolute path under ``/workspace``.
            content_b64: Base64-encoded file content.
            mode: File permission bits applied after the write.
        """
        safe_path = self._file_path(path, allow_external=allow_external)
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path, allow_external=allow_external
        )
        cap = FILE_UPLOAD_MAX_SIZE
        # Exact cap on decoded bytes: decode + validate before writing.
        # Whitespace is normalized first (base64 output may wrap lines).
        try:
            decoded = base64.b64decode("".join(content_b64.split()), validate=True)
        except Exception as exc:
            raise ValueError("Invalid base64 file payload") from exc
        if len(decoded) > cap:
            raise ValueError(f"Write exceeds maximum size of {cap} bytes")
        if mode < 0 or mode > 0o777:
            raise ValueError(f"Invalid file mode: {mode!r}")

        archive = build_single_file_tar(os.path.basename(safe_path), decoded)
        if allow_external:
            # Archive extraction needs the destination directory to exist;
            # keep ordinary file-explorer writes unchanged.
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=["mkdir", "-p", os.path.dirname(safe_path)],
                workdir="/workspace",
            )
            if exit_code != 0:
                raise RuntimeError(f"Failed to create parent directory: {output}")
        await runtime.put_archive(
            info.instance_id,
            os.path.dirname(safe_path) or "/workspace",
            archive,
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=["chmod", format(mode, "o"), safe_path],
            workdir="/workspace",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to set file mode: {output}")
        logger.info(
            "file_written",
            workspace_id=str(workspace_id),
            path=safe_path,
        )


__all__ = [
    "FIND_FILES_DEFAULT_LIMIT",
    "FIND_FILES_PRUNE_NAMES",
    "FILE_READ_ABSOLUTE_MAX_SIZE",
    "FILE_READ_DEFAULT_MAX_SIZE",
    "FILE_UPLOAD_MAX_SIZE",
    "FileManager",
    "_FIND_FILES_QUERY_RE",
    "_FIND_FILES_SUCCESS_EXIT_CODES",
    "build_find_files_command",
    "build_single_file_tar",
    "build_tar_entries",
    "sanitize_find_query",
]
