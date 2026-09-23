"""File operations: stateful content-plane on top of the exec kernel.

Canonical home (Step 4) for file constants and stateful file
operations previously living on ``WorkspaceService`` in
:mod:`src.service`:

- constants ``FILE_READ_DEFAULT_MAX_SIZE``,
  ``FILE_READ_ABSOLUTE_MAX_SIZE``, ``FILE_UPLOAD_MAX_SIZE``,
  ``FILE_DOWNLOAD_MAX_SIZE`` (``src.service`` re-exports them),
- :class:`FileManager` owning ``_file_read_semaphores`` and
  ``_realpath_under_workspace``, ``list_files``, ``find_files``,
  ``read_file``, ``upload_file``, ``download_file``, ``stat_path``,
  ``write_file_content``.

Pure helpers (find/tar) were canonical here since Step 1 and are
reused directly by the manager. Path/filename sanitizers are imported
from :mod:`src.services.exec_kernel` (no ``src.service`` import, no
cycle). ``WorkspaceService`` keeps thin delegates (same
names/signatures/messages) plus a ``files`` property and a
``_file_read_semaphores`` alias onto the manager-owned dict.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import shlex
import tarfile
import uuid
from collections.abc import Callable

import structlog

from ..models import WorkspaceInfo
from ..runtime.base import RuntimeBackend
from .exec_kernel import sanitize_filename as _sanitize_filename
from .exec_kernel import sanitize_path as _sanitize_path

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
FILE_UPLOAD_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
#: Max raw bytes served by ``download_file``. Matches the absolute read cap
#: (100 MiB) so a single download can never buffer unbounded memory even
#: though payloads are chunked on the wire. Oversized downloads fail with a
#: small structured error instead of a runner disconnect.
FILE_DOWNLOAD_MAX_SIZE = 100 * 1024 * 1024  # 100 MB


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


def convert_archive_to_tar(content: bytes) -> bytes:
    """Convert an uploaded archive payload to a plain tar stream."""
    source = io.BytesIO(content)
    target = io.BytesIO()

    with tarfile.open(fileobj=source, mode="r:*") as src_tar:
        with tarfile.open(fileobj=target, mode="w") as dst_tar:
            for member in src_tar.getmembers():
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    raise ValueError("Archive contains unsafe paths")
                if member.issym() or member.islnk():
                    raise ValueError("Archive contains unsafe links")

                extracted = None
                if member.isfile():
                    extracted = src_tar.extractfile(member)
                dst_tar.addfile(member, extracted)

    return target.getvalue()


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

    Injected dependencies (all mirror ``WorkspaceService`` helpers):

    - ``runtimes``: runtime backends by type (kept for introspection;
      resolution itself goes through the callables below).
    - ``get_cached``: ``(workspace_id) -> WorkspaceInfo``; raises
      ``ValueError("... not found")`` for unknown ids.
    - ``get_runtime``: ``(workspace_id) -> RuntimeBackend``; raises
      ``RuntimeError`` for unknown runtimes.
    - ``upload_max_size``: optional override for ``FILE_UPLOAD_MAX_SIZE``
      (bytes). Defaults to the canonical constant. The facade passes its
      (patched-in-tests) value at call time so ``monkeypatch`` on either
      ``src.service`` or ``src.services.files`` stays effective; direct
      manager use falls back to the canonical constant.
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {}
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        # Limit concurrent file-read SSH channels per workspace to avoid
        # exhausting the SSH server's MaxSessions limit (default: 10).
        # Each read_file call opens at most 1 SSH channel, so a limit of 4
        # keeps peak channel usage well below 10.
        self._file_read_semaphores: dict[uuid.UUID, asyncio.Semaphore] = {}

    async def _realpath_under_workspace(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        path: str,
    ) -> str:
        """Resolve symlinks for *path* and ensure it stays in /workspace.

        Runs ``realpath -m`` inside the workspace, which resolves symlinks
        and ``..`` segments. Raises ``ValueError`` (fail-closed) when the
        resolved path escapes ``/workspace``. Falls back to *path* when
        ``realpath`` is unavailable in the image (coreutils ships it on
        Ubuntu, so this is only a safety net). Note: check-then-use is
        inherently TOCTOU-prone if the workspace mutates the link between
        the check and the file operation; accepted here as defense-in-depth
        on top of the ``/workspace`` sandbox.
        """
        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["realpath", "-m", path],
            workdir="/workspace",
        )
        if exit_code != 0:
            return path
        resolved = output.strip().splitlines()
        if not resolved or not resolved[0]:
            return path
        real = resolved[0].strip()
        if real != "/workspace" and not real.startswith("/workspace/"):
            raise ValueError(f"Path escapes /workspace: {path}")
        return real

    async def list_files(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> list[dict]:
        """List files and directories at *path* inside the workspace.

        Returns a list of dicts with ``name``, ``path``, ``type``, ``size``.
        """
        safe_path = _sanitize_path(path)
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
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

    async def read_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
        max_size: int | None = None,
    ) -> dict:
        """Read a file from the workspace container.

        Returns a dict with ``content`` (base64), ``size``, ``truncated``,
        and ``mime_type``.

        Concurrent reads are throttled via a per-workspace semaphore to
        avoid exceeding the SSH server's MaxSessions limit when many images
        are fetched simultaneously.
        """
        safe_path = _sanitize_path(path)
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
            safe_path = await self._realpath_under_workspace(
                runtime, info.instance_id, safe_path
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

        # Parse output: first line is size, second line MIME type, remainder base64.
        lines = output.splitlines()
        if len(lines) < 2:
            raise RuntimeError("Invalid file read response format")
        file_size = int(lines[0].strip())
        mime_type = lines[1].strip() or "application/octet-stream"
        content_output = "\n".join(lines[2:]) if len(lines) > 2 else ""
        truncated = file_size > read_limit

        return {
            "content": content_output.strip(),
            "size": file_size,
            "truncated": truncated,
            "mime_type": mime_type,
        }

    async def upload_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
        filename: str,
        content_b64: str,
        is_directory: bool = False,
        *,
        upload_max_size: int | None = None,
    ) -> None:
        """Upload a file into the workspace container.

        Args:
            workspace_id: Target workspace.
            path: Directory path to upload into.
            filename: Name of the file to create.
            content_b64: Base64-encoded file content.
            is_directory: If True, content is a tar.gz archive to extract.
        """
        safe_path = _sanitize_path(path)
        safe_filename = _sanitize_filename(filename)
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )

        cap = FILE_UPLOAD_MAX_SIZE if upload_max_size is None else upload_max_size
        # Exact cap on decoded bytes: decode + validate first, before any
        # mkdir side effect. A cheap approximate precheck may reject
        # obvious oversize early, but it must never reject a valid payload
        # at/below the cap (padding-aware bound, not a lossy estimate).
        clean = "".join((content_b64 or "").split())
        if len(clean) > (cap + 2) // 3 * 4 + 4:
            raise ValueError(f"Upload exceeds maximum size of {cap} bytes")
        try:
            decoded_content = base64.b64decode(clean, validate=True)
        except Exception as exc:
            raise ValueError("Invalid base64 upload payload") from exc
        if len(decoded_content) > cap:
            raise ValueError(f"Upload exceeds maximum size of {cap} bytes")

        # Ensure target directory exists
        await runtime.exec_command_wait(
            info.instance_id,
            command=["mkdir", "-p", safe_path],
            workdir="/workspace",
        )

        if is_directory:
            archive_data = convert_archive_to_tar(decoded_content)
        else:
            archive_data = build_single_file_tar(safe_filename, decoded_content)

        await runtime.put_archive(
            info.instance_id,
            safe_path,
            archive_data,
        )

        logger.info(
            "file_uploaded",
            workspace_id=str(workspace_id),
            path=safe_path,
            filename=safe_filename,
        )

    async def download_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> dict:
        """Download a file or directory from the workspace container.

        Returns a dict with ``content`` (base64), ``filename``, ``is_archive``
        and ``size`` (raw byte count). For directories, the content is a
        tar.gz archive. Payloads larger than ``FILE_DOWNLOAD_MAX_SIZE``
        raise ``ValueError`` so callers can return a small structured error
        instead of buffering unbounded memory.
        """
        safe_path = _sanitize_path(path)
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )

        # Check if it's a directory
        exit_code, _ = await runtime.exec_command_wait(
            info.instance_id,
            command=["test", "-d", safe_path],
            workdir="/workspace",
        )
        is_dir = exit_code == 0

        if is_dir:
            qp_dir = shlex.quote(os.path.dirname(safe_path))
            qp_base = shlex.quote(os.path.basename(safe_path))
            # Report the archive size first so huge directories fail with a
            # small error instead of streaming unbounded base64 into memory.
            size_cmd = f"tar czf - -C {qp_dir} {qp_base} 2>/dev/null | wc -c"
            exit_code, size_output = await runtime.exec_command_wait(
                info.instance_id,
                command=["sh", "-c", size_cmd],
                workdir="/workspace",
            )
            if exit_code != 0:
                raise RuntimeError(f"Failed to download: {size_output}")
            try:
                archive_size = int(size_output.strip().split()[0])
            except (ValueError, IndexError) as exc:
                raise RuntimeError(
                    f"Failed to download: invalid size {size_output!r}"
                ) from exc
            if archive_size > FILE_DOWNLOAD_MAX_SIZE:
                raise ValueError(
                    "Download exceeds maximum size of "
                    f"{FILE_DOWNLOAD_MAX_SIZE} bytes"
                )
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=[
                    "sh",
                    "-c",
                    f"tar czf - -C {qp_dir} {qp_base} | base64",
                ],
                workdir="/workspace",
            )
            filename = os.path.basename(safe_path) + ".tar.gz"
            raw_size = archive_size
        else:
            qpath = shlex.quote(safe_path)
            shell_cmd = f"test -f {qpath} || exit 1; " f"stat -c '%s' {qpath}"
            exit_code, size_output = await runtime.exec_command_wait(
                info.instance_id,
                command=["sh", "-c", shell_cmd],
                workdir="/workspace",
            )
            if exit_code != 0:
                raise RuntimeError(f"Failed to download: {size_output}")
            try:
                raw_size = int(size_output.strip().split()[-1])
            except (ValueError, IndexError) as exc:
                raise RuntimeError(
                    f"Failed to download: invalid size {size_output!r}"
                ) from exc
            if raw_size > FILE_DOWNLOAD_MAX_SIZE:
                raise ValueError(
                    "Download exceeds maximum size of "
                    f"{FILE_DOWNLOAD_MAX_SIZE} bytes"
                )
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=["base64", safe_path],
                workdir="/workspace",
            )
            filename = os.path.basename(safe_path)

        if exit_code != 0:
            raise RuntimeError(f"Failed to download: {output}")

        return {
            "content": output.strip(),
            "filename": filename,
            "is_archive": is_dir,
            "size": raw_size,
        }

    async def stat_path(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> dict:
        """Stat a path inside the workspace container.

        Returns a dict with ``path``, ``is_dir``, ``size``, ``mime_type``.
        """
        safe_path = _sanitize_path(path)
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )

        qpath = shlex.quote(safe_path)
        shell_cmd = (
            f"if [ -e {qpath} ]; then "
            f"if [ -d {qpath} ]; then echo 'dir'; "
            f"du -sb {qpath} | cut -f1; "
            f"echo 'inode/directory'; "
            f"else stat -c '%s' {qpath}; "
            f"file --mime-type -b {qpath} 2>/dev/null "
            "|| echo 'application/octet-stream'; "
            f"fi; else echo 'missing'; fi"
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=["sh", "-c", shell_cmd],
            workdir="/workspace",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to stat path: {output}")
        lines = output.strip().splitlines()
        if not lines or lines[0].strip() == "missing":
            raise FileNotFoundError(f"No such file or directory: {path}")
        is_dir = lines[0].strip() == "dir"
        size = int(lines[1].strip()) if len(lines) > 1 else 0
        mime_type = lines[2].strip() if len(lines) > 2 else "application/octet-stream"
        return {
            "path": safe_path,
            "is_dir": is_dir,
            "size": size,
            "mime_type": mime_type,
        }

    async def write_file_content(
        self,
        workspace_id: uuid.UUID,
        path: str,
        content_b64: str,
        mode: int = 0o644,
        *,
        upload_max_size: int | None = None,
    ) -> None:
        """Write file content atomically inside the workspace container.

        Args:
            workspace_id: Target workspace.
            path: Absolute path under ``/workspace``.
            content_b64: Base64-encoded file content.
            mode: File permission bits applied after the write.
        """
        safe_path = _sanitize_path(path)
        assert self._get_cached is not None and self._get_runtime is not None
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )
        cap = FILE_UPLOAD_MAX_SIZE if upload_max_size is None else upload_max_size
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
    "FILE_DOWNLOAD_MAX_SIZE",
    "FILE_READ_ABSOLUTE_MAX_SIZE",
    "FILE_READ_DEFAULT_MAX_SIZE",
    "FILE_UPLOAD_MAX_SIZE",
    "FileManager",
    "_FIND_FILES_QUERY_RE",
    "_FIND_FILES_SUCCESS_EXIT_CODES",
    "build_find_files_command",
    "build_single_file_tar",
    "build_tar_entries",
    "convert_archive_to_tar",
    "sanitize_find_query",
]
