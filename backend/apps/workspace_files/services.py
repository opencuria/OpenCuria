"""Stream workspace files between clients and the guest over HTTP.

Clients ask for signed links (:meth:`WorkspaceFileService.links`) or an upload
link (:meth:`WorkspaceFileService.create_upload`) over the authenticated REST
API, then transfer bytes through :mod:`.http`. Bytes travel as one runner
process stream per request (:mod:`.guest_scripts`), so neither the backend nor
the client ever holds a whole file: transfers are bounded by
:data:`FILE_TRANSFER_MAX_BYTES`, not by memory. Files as a Git index or commit
has them (:meth:`WorkspaceFileService.git_link`) stream the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import posixpath
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import structlog
from asgiref.sync import sync_to_async
from django.core.cache import cache

from apps.harness.access.base import StreamClosedError, WorkspaceByteStream
from apps.runners.git_validation import validate_git_args, validate_repo_path

from . import guest_scripts, tokens
from .media_types import ARCHIVE_TYPE, MediaType, media_type

log = structlog.get_logger(__name__)

MEBIBYTE = 1024 * 1024
#: Largest file a client may open, download or upload.
FILE_TRANSFER_MAX_BYTES = 500 * MEBIBYTE
#: Paths per link request (one guest call for all of them).
MAX_LINK_PATHS = 100
#: Concurrent transfers per workspace; more wait for a free slot.
STREAMS_PER_WORKSPACE = 4
STREAM_WAIT_SECONDS = 30.0
STREAM_OPEN_TIMEOUT = 20.0
STAT_TIMEOUT = 20.0
#: Long transfers keep the workspace from being stopped as idle.
ACTIVITY_TOUCH_SECONDS = 60.0
#: Blurred previews fit in a square of this many pixels.
THUMBNAIL_EDGE = 48
THUMBNAIL_MAX_BYTES = 64 * 1024
#: Images decoded by the backend when the guest has no ffmpeg.
THUMBNAIL_IMAGE_SOURCE_MAX_BYTES = 32 * MEBIBYTE
THUMBNAIL_IMAGE_MAX_PIXELS = 50_000_000
THUMBNAIL_CACHE_SECONDS = 7 * 24 * 3600
#: Video previews show the frame at this offset (or the first one).
THUMBNAIL_VIDEO_SEEK_SECONDS = 1.0
FILENAME_MAX_BYTES = 255
_HEADER_MAX_BYTES = 512

AccessorFactory = Callable[[str], Awaitable[Any]]


class FileAccessError(Exception):
    """A refused file request with its HTTP status and machine code."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
        size: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.size = size


_GUEST_ERRORS = {
    "missing": (404, "file_not_found", "The file does not exist."),
    "outside": (404, "file_not_found", "The file does not exist."),
    "unsupported": (415, "unsupported_file", "This file type cannot be transferred."),
    "unreadable": (403, "file_unreadable", "The file cannot be read."),
    "directory": (409, "is_directory", "The path is a directory."),
    "is_directory": (409, "is_directory", "A directory with this name exists."),
    "not_directory": (409, "not_directory", "The target is not a directory."),
    "unwritable": (403, "file_unwritable", "The file cannot be written."),
    "invalid": (400, "invalid_path", "The path is invalid."),
    "incomplete": (400, "upload_incomplete", "The upload ended early."),
    "archive_failed": (500, "archive_failed", "The folder could not be packed."),
    "undecodable": (404, "no_thumbnail", "No preview is available."),
    "no_ffmpeg": (404, "no_thumbnail", "No preview is available."),
    "unavailable": (503, "workspace_unavailable", "The workspace is unavailable."),
}


def _too_large(size: int | None = None) -> FileAccessError:
    return FileAccessError(
        413,
        "file_too_large",
        f"Files up to {FILE_TRANSFER_MAX_BYTES // MEBIBYTE} MB can be transferred.",
        size=size,
    )


def clean_workspace_path(path: object) -> str:
    """Return *path* normalized, or raise when it leaves ``/workspace``."""
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 4096
        or any(char in path for char in "\x00\r\n\\")
    ):
        raise FileAccessError(400, "invalid_path", "The path is invalid.")
    normalized = posixpath.normpath(path)
    if normalized != guest_scripts.WORKSPACE_ROOT and not normalized.startswith(
        guest_scripts.WORKSPACE_ROOT + "/"
    ):
        raise FileAccessError(400, "invalid_path", "Paths must be inside /workspace.")
    return normalized


def clean_filename(name: object) -> str:
    """Return *name* when it is one safe path segment."""
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or len(name.encode("utf-8")) > FILENAME_MAX_BYTES
    ):
        raise FileAccessError(400, "invalid_filename", "The file name is invalid.")
    return name


@dataclass(frozen=True)
class FileLink:
    """Metadata and signed URLs of one requested path."""

    path: str
    name: str
    type: str = ""
    size: int = 0
    modified_at: int = 0
    kind: str = ""
    mime_type: str = ""
    content_url: str = ""
    download_url: str = ""
    thumbnail_url: str = ""
    #: Git object the link streams (Git index and commit sides only).
    object_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class FileLinks:
    """Links for a batch of paths and their common expiry."""

    files: list[FileLink]
    expires_at: int


@dataclass(frozen=True)
class UploadLink:
    """Where a client sends the bytes of one file."""

    upload_url: str
    expires_at: int
    max_bytes: int


@dataclass(frozen=True)
class UploadResult:
    """The file an upload created."""

    path: str
    size: int


@dataclass
class FileTransfer:
    """A streaming response: status, payload metadata and its body."""

    status: int
    media: MediaType
    filename: str
    size: int = 0
    first: int = 0
    last: int = -1
    etag: str = ""
    modified_at: int = 0
    ranged: bool = True
    body: AsyncIterator[bytes] | None = None
    close: Callable[[], Awaitable[None]] = field(default=lambda: _noop())

    @property
    def length(self) -> int:
        """Bytes in this response body."""
        return max(0, self.last - self.first + 1)


async def _noop() -> None:
    return None


def _quote_path(value: str) -> str:
    return quote(value, safe="")


def content_url(token: str, filename: str, version: str) -> str:
    """Return the API-relative URL streaming the linked file."""
    return (
        f"/workspace-files/content/{token}/{_quote_path(filename or 'file')}"
        f"?v={version}"
    )


def thumbnail_url(token: str, version: str) -> str:
    """Return the API-relative URL of the linked file's blurred preview."""
    return f"/workspace-files/thumbnail/{token}?v={version}"


def upload_url(token: str) -> str:
    """Return the API-relative URL accepting one upload."""
    return f"/workspace-files/upload/{token}"


async def _default_accessor(workspace_id: str) -> Any:
    from apps.harness.access.runner_accessor import create_harness_accessor
    from apps.runners.services import get_runner_service

    return await create_harness_accessor(get_runner_service(), workspace_id)


def _runner_service() -> Any:
    from apps.runners.services import get_runner_service

    return get_runner_service()


def _map_access_errors(exc: BaseException) -> FileAccessError:
    """Translate workspace lookups and runner state into HTTP refusals."""
    from apps.runners.exceptions import RunnerOfflineError, WorkspaceNotFoundError
    from common.exceptions import ConflictError

    if isinstance(exc, FileAccessError):
        return exc
    if isinstance(exc, ConflictError):
        # Lifecycle fences (stopping, capturing an image) refuse interactions.
        return FileAccessError(409, exc.code or "conflict", exc.message, retry_after=5)
    if isinstance(exc, WorkspaceNotFoundError):
        return FileAccessError(404, "workspace_not_found", "Workspace not found.")
    if isinstance(exc, RunnerOfflineError):
        return FileAccessError(
            503, "runner_offline", "The workspace's runner is offline.", retry_after=5
        )
    return FileAccessError(
        503,
        "workspace_unavailable",
        "The workspace is not running or does not answer.",
        retry_after=5,
    )


class WorkspaceFileService:
    """Links, downloads, uploads and previews of workspace files."""

    def __init__(
        self,
        *,
        accessor_factory: AccessorFactory | None = None,
        runner_service: Callable[[], Any] | None = None,
    ) -> None:
        self._accessor_factory = accessor_factory or _default_accessor
        self._runner_service = runner_service or _runner_service
        self._budgets: dict[str, asyncio.Semaphore] = {}
        self._touched: dict[str, float] = {}

    # -- Authorization ------------------------------------------------------

    async def _authorize_user(
        self, workspace_id: uuid.UUID, *, user: Any, organization_id: uuid.UUID
    ) -> None:
        """Require that *user* owns the workspace in the active organization."""
        access = self._runner_service().workspace_access
        try:
            await sync_to_async(access.get_workspace_for_user)(
                workspace_id, user=user, organization_id=organization_id
            )
        except Exception as exc:
            raise _map_access_errors(exc) from exc

    async def authorize(self, claims: tokens.FileLinkClaims) -> None:
        """Re-check a link's owner on every request (revocation is immediate)."""
        try:
            workspace_id = uuid.UUID(claims.workspace_id)
            organization_id = uuid.UUID(claims.organization_id)
        except ValueError as exc:
            raise FileAccessError(404, "link_invalid", "This link is invalid.") from exc
        access = self._runner_service().workspace_access
        allowed = await sync_to_async(access.is_active_workspace_owner)(
            claims.user_id, workspace_id, organization_id=organization_id
        )
        if not allowed:
            raise FileAccessError(404, "file_not_found", "The file does not exist.")

    # -- Links --------------------------------------------------------------

    async def links(
        self,
        workspace_id: uuid.UUID,
        paths: list[str],
        *,
        user: Any,
        organization_id: uuid.UUID,
    ) -> FileLinks:
        """Stat *paths* in one guest call and sign a read link for each."""
        if not paths or len(paths) > MAX_LINK_PATHS:
            raise FileAccessError(
                400, "invalid_paths", f"Request 1 to {MAX_LINK_PATHS} paths."
            )
        await self._authorize_user(
            workspace_id, user=user, organization_id=organization_id
        )
        valid: list[str] = []
        for path in paths:
            with contextlib.suppress(FileAccessError):
                valid.append(clean_workspace_path(path))
        valid = list(dict.fromkeys(valid))
        entries: dict[str, Any] = {}
        if valid:
            entries = await self._stat(workspace_id, valid)
        expires_at = tokens.read_link_expiry()
        files: list[FileLink] = []
        for path in paths:
            try:
                clean = clean_workspace_path(path)
            except FileAccessError:
                files.append(FileLink(path=str(path), name="", error="invalid_path"))
                continue
            files.append(
                self._link(
                    entries.get(clean),
                    clean,
                    user.id,
                    organization_id,
                    workspace_id,
                    expires_at,
                    requested=str(path),
                )
            )
        return FileLinks(files=files, expires_at=expires_at)

    async def git_link(
        self,
        workspace_id: uuid.UUID,
        *,
        repo_path: str,
        path: str,
        revision: str,
        user: Any,
        organization_id: uuid.UUID,
    ) -> FileLinks:
        """Sign a link to one side of a file in a Git repository.

        ``worktree`` is the file on disk (see :meth:`links`). ``index``,
        ``head`` and commits resolve to a Git blob whose link names that
        immutable object. A side without the file links ``file_not_found``.
        """
        try:
            repo = clean_workspace_path(validate_repo_path(repo_path))
            args = validate_git_args(
                "file_content", {"path": path, "revision": revision}
            )
        except ValueError as exc:
            raise FileAccessError(400, "invalid_path", str(exc)) from exc
        file_path = clean_workspace_path(posixpath.join(repo, args["path"]))
        if args["revision"] == "worktree":
            return await self.links(
                workspace_id, [file_path], user=user, organization_id=organization_id
            )
        await self._authorize_user(
            workspace_id, user=user, organization_id=organization_id
        )
        # Stage 0 keeps a file named like "1:x" from naming a merge stage.
        prefix = {"index": ":0:", "head": "HEAD:"}.get(
            args["revision"], f"{args['revision']}:"
        )
        entry = await self._stat_blob(workspace_id, repo, prefix + args["path"])
        expires_at = tokens.read_link_expiry()
        link = self._link(
            entry,
            file_path,
            user.id,
            organization_id,
            workspace_id,
            expires_at,
            requested=file_path,
        )
        return FileLinks(files=[link], expires_at=expires_at)

    async def _stat_blob(
        self, workspace_id: uuid.UUID, repo: str, name: str
    ) -> dict[str, Any]:
        """Return a stat entry for the Git blob *name* of *repo*."""
        try:
            accessor = await self._accessor_factory(str(workspace_id))
            result = await accessor.exec_wait(
                guest_scripts.git_stat_command(repo, name), timeout=STAT_TIMEOUT
            )
        except Exception as exc:
            raise _map_access_errors(exc) from exc
        words = result.stdout.split()
        if (
            len(words) == 3
            and words[0] == "OK"
            and tokens.is_object_id(words[1])
            and words[2].isdigit()
        ):
            return {
                "type": "file",
                "size": int(words[2]),
                "mtime": 0,
                "repo": repo,
                "object_id": words[1],
            }
        if len(words) == 2 and words[0] == "ERR" and words[1] != "unavailable":
            return {"error": words[1]}
        log.warning(
            "workspace_files_git_stat_failed",
            workspace_id=str(workspace_id),
            exit_code=result.exit_code,
        )
        raise FileAccessError(
            503, "workspace_unavailable", "The workspace did not answer."
        )

    async def _stat(self, workspace_id: uuid.UUID, paths: list[str]) -> dict[str, Any]:
        """Return guest metadata per path in one guest call."""
        try:
            accessor = await self._accessor_factory(str(workspace_id))
            result = await accessor.exec_wait(
                guest_scripts.stat_command(paths), timeout=STAT_TIMEOUT
            )
        except Exception as exc:
            raise _map_access_errors(exc) from exc
        try:
            entries = json.loads(result.stdout)
            if not isinstance(entries, list) or len(entries) != len(paths):
                raise ValueError("entry count")
        except (ValueError, TypeError) as exc:
            log.warning(
                "workspace_files_stat_failed",
                workspace_id=str(workspace_id),
                exit_code=result.exit_code,
            )
            raise FileAccessError(
                503, "workspace_unavailable", "The workspace did not answer."
            ) from exc
        return dict(zip(paths, entries))

    @staticmethod
    def _link(
        entry: Any,
        path: str,
        user_id: int,
        organization_id: uuid.UUID,
        workspace_id: uuid.UUID,
        expires_at: int,
        *,
        requested: str,
    ) -> FileLink:
        name = posixpath.basename(path) or "workspace"
        if not isinstance(entry, dict) or entry.get("error"):
            error = entry.get("error") if isinstance(entry, dict) else "unreadable"
            code = _GUEST_ERRORS.get(str(error), _GUEST_ERRORS["unreadable"])[1]
            return FileLink(path=requested, name=name, error=code)
        kind_of_entry = "directory" if entry.get("type") == "directory" else "file"
        size = int(entry.get("size") or 0) if kind_of_entry == "file" else 0
        modified_at = int(entry.get("mtime") or 0)
        object_id = str(entry.get("object_id") or "")
        if object_id:
            token = tokens.issue_blob_token(
                user_id=user_id,
                organization_id=str(organization_id),
                workspace_id=str(workspace_id),
                path=path,
                repo=str(entry["repo"]),
                object_id=object_id,
                expires_at=expires_at,
            )
            version = object_id
        else:
            token = tokens.issue_read_token(
                user_id=user_id,
                organization_id=str(organization_id),
                workspace_id=str(workspace_id),
                path=path,
                expires_at=expires_at,
            )
            version = f"{size}-{modified_at}"
        if kind_of_entry == "directory":
            archive = f"{name}.tar.gz"
            return FileLink(
                path=requested,
                name=name,
                type="directory",
                modified_at=modified_at,
                kind="binary",
                mime_type=ARCHIVE_TYPE.content_type,
                download_url=content_url(token, archive, version) + "&download=1",
            )
        media = media_type(name)
        url = content_url(token, name, version)
        return FileLink(
            path=requested,
            name=name,
            type="file",
            size=size,
            modified_at=modified_at,
            kind=media.kind,
            mime_type=media.content_type.split(";")[0],
            content_url=url,
            download_url=url + "&download=1",
            thumbnail_url=(
                thumbnail_url(token, version)
                if media.kind in ("image", "video") and size > 0 and not object_id
                else ""
            ),
            object_id=object_id,
        )

    # -- Uploads ------------------------------------------------------------

    async def create_upload(
        self,
        workspace_id: uuid.UUID,
        *,
        directory: str,
        filename: str,
        size: int,
        conflict: tokens.UploadConflict,
        user: Any,
        organization_id: uuid.UUID,
    ) -> UploadLink:
        """Sign an upload of exactly *size* bytes as *directory*/*filename*."""
        target = clean_workspace_path(directory)
        name = clean_filename(filename)
        if not isinstance(size, int) or size < 0:
            raise FileAccessError(400, "invalid_size", "The size is invalid.")
        if size > FILE_TRANSFER_MAX_BYTES:
            raise _too_large(size)
        if conflict not in ("rename", "replace"):
            raise FileAccessError(400, "invalid_conflict", "Unknown conflict policy.")
        await self._authorize_user(
            workspace_id, user=user, organization_id=organization_id
        )
        token, expires_at = tokens.issue_upload_token(
            user_id=user.id,
            organization_id=str(organization_id),
            workspace_id=str(workspace_id),
            directory=target,
            filename=name,
            size=size,
            conflict=conflict,
        )
        return UploadLink(
            upload_url=upload_url(token),
            expires_at=expires_at,
            max_bytes=FILE_TRANSFER_MAX_BYTES,
        )

    async def upload(
        self,
        claims: tokens.FileLinkClaims,
        body: AsyncIterator[bytes],
        *,
        content_length: int | None,
    ) -> UploadResult:
        """Write the request body into the workspace (all or nothing)."""
        if content_length is not None and content_length != claims.size:
            raise FileAccessError(
                400, "size_mismatch", "The body size differs from the announced size."
            )
        await self.authorize(claims)
        command = guest_scripts.write_command(
            claims.path, claims.filename, claims.size, claims.conflict
        )
        async with self._transfer_slot(claims.workspace_id):
            stream = await self._open(claims.workspace_id, command)
            try:
                received = 0
                closed = False
                pending = bytearray()

                async def flush(final: bool) -> None:
                    # Each send is one runner round trip: send full chunks.
                    nonlocal closed
                    while pending and (final or len(pending) >= _chunk_size()):
                        piece = bytes(pending[: _chunk_size()])
                        del pending[: _chunk_size()]
                        if closed:
                            continue
                        try:
                            await stream.send(piece)
                        except StreamClosedError:
                            closed = True  # The guest refused; its answer follows.

                async for chunk in body:
                    received += len(chunk)
                    if received > claims.size:
                        raise FileAccessError(
                            400, "size_mismatch", "The body is larger than announced."
                        )
                    pending += chunk
                    await flush(final=False)
                if received != claims.size:
                    raise FileAccessError(
                        400, "upload_incomplete", "The upload ended early."
                    )
                await flush(final=True)
                if not closed:
                    with contextlib.suppress(StreamClosedError):
                        await stream.send_eof()
                output = await _read_all(stream, limit=8192)
            except BaseException:
                # End stdin instead of killing the guest: it sees a short
                # upload and removes its temporary file itself.
                with contextlib.suppress(Exception):
                    await stream.send_eof()
                    await asyncio.wait_for(stream.wait_closed(), 5)
                raise
            finally:
                await stream.aclose()
        status, _, rest = output.decode("utf-8", "replace").partition("\n")
        words = status.split()
        if len(words) == 2 and words[0] == "OK" and rest.strip():
            path = rest.rstrip("\n")
            log.info(
                "workspace_file_uploaded",
                workspace_id=claims.workspace_id,
                size=claims.size,
            )
            return UploadResult(path=path, size=int(words[1]))
        raise _guest_error(words)

    # -- Downloads ----------------------------------------------------------

    async def open_content(
        self,
        claims: tokens.FileLinkClaims,
        *,
        byte_range: tuple[int | None, int | None, int | None] | None,
        if_none_match: str = "",
        head: bool = False,
    ) -> FileTransfer:
        """Open the linked file (or one range of it) as a streaming response.

        Directories stream as a ``.tar.gz`` archive, blob links the Git object
        they name. The caller must call :attr:`FileTransfer.close` after the
        body, also on errors.
        """
        await self.authorize(claims)
        name = posixpath.basename(claims.path) or "workspace"
        first, last, suffix = byte_range or (None, None, None)
        if claims.operation == "blob":
            command = guest_scripts.git_read_command(
                claims.repo,
                claims.object_id,
                first=first,
                last=last,
                suffix=suffix,
                maximum=FILE_TRANSFER_MAX_BYTES,
                etag=if_none_match,
            )
        else:
            command = guest_scripts.read_command(
                claims.path,
                first=first,
                last=last,
                suffix=suffix,
                maximum=FILE_TRANSFER_MAX_BYTES,
                etag=if_none_match,
            )
        slot = self._transfer_slot(claims.workspace_id)
        await slot.__aenter__()
        try:
            stream, words, leftover = await self._open_with_header(
                claims.workspace_id, command
            )
            if (
                words[:2] == ["ERR", "directory"]
                and byte_range is None
                and claims.operation == "read"
            ):
                await stream.aclose()
                stream, words, leftover = await self._open_with_header(
                    claims.workspace_id,
                    guest_scripts.archive_command(claims.path, FILE_TRANSFER_MAX_BYTES),
                )
                transfer = await self._transfer(
                    words, leftover, stream, ARCHIVE_TYPE, f"{name}.tar.gz", head
                )
                transfer.ranged = False
            else:
                transfer = await self._transfer(
                    words,
                    leftover,
                    stream,
                    media_type(name),
                    name,
                    head,
                    etag=claims.object_id,
                )
        except BaseException:
            await slot.__aexit__(None, None, None)
            raise
        inner_close = transfer.close

        async def close() -> None:
            try:
                await inner_close()
            finally:
                await slot.__aexit__(None, None, None)

        transfer.close = close
        return transfer

    async def _transfer(
        self,
        words: list[str],
        leftover: bytes,
        stream: WorkspaceByteStream,
        media: MediaType,
        filename: str,
        head: bool,
        *,
        etag: str = "",
    ) -> FileTransfer:
        """Turn a guest header into a response and its body iterator.

        The ETag is ``<size>-<mtime>`` unless the caller knows a better one
        (a blob's object id).
        """
        tag = words[0] if words else ""
        if tag == "NOTMOD" and len(words) == 3:
            size, modified_at = int(words[1]), int(words[2])
            await stream.aclose()
            return FileTransfer(
                304,
                media,
                filename,
                size=size,
                etag=etag or f"{size}-{modified_at}",
                modified_at=modified_at,
            )
        if tag == "RANGE" and len(words) == 2:
            await stream.aclose()
            raise FileAccessError(
                416,
                "range_not_satisfiable",
                "Range not satisfiable.",
                size=int(words[1]),
            )
        if tag != "OK" or len(words) != 5:
            await stream.aclose()
            raise _guest_error(words)
        size, modified_at, first, last = (int(word) for word in words[1:])
        transfer = FileTransfer(
            200,
            media,
            filename,
            size=size,
            first=first,
            last=last,
            etag=etag or f"{size}-{modified_at}",
            modified_at=modified_at,
            close=stream.aclose,
        )
        if not head:
            transfer.body = _bounded_body(stream, leftover, transfer.length)
        return transfer

    # -- Thumbnails ---------------------------------------------------------

    async def thumbnail(self, claims: tokens.FileLinkClaims, version: str) -> bytes:
        """Return a small blurred JPEG of an image or video (cached)."""
        await self.authorize(claims)
        media = media_type(claims.path)
        if media.kind not in ("image", "video"):
            raise FileAccessError(404, "no_thumbnail", "No preview is available.")
        key = _thumbnail_cache_key(claims, version)
        cached = await cache.aget(key) if version else None
        if isinstance(cached, bytes):
            return cached
        seek = THUMBNAIL_VIDEO_SEEK_SECONDS if media.kind == "video" else None
        async with self._transfer_slot(claims.workspace_id):
            stream, words, leftover = await self._open_with_header(
                claims.workspace_id,
                guest_scripts.thumbnail_command(claims.path, THUMBNAIL_EDGE, seek),
            )
            try:
                if words[:2] == ["ERR", "no_ffmpeg"] and media.kind == "image":
                    data = None
                elif words and words[0] == "OK" and len(words) == 5:
                    data = await _collect(
                        _bounded_body(stream, leftover, int(words[1])),
                        THUMBNAIL_MAX_BYTES,
                    )
                    if not data.startswith(b"\xff\xd8"):
                        raise FileAccessError(
                            404, "no_thumbnail", "No preview is available."
                        )
                else:
                    raise _guest_error(words)
            finally:
                await stream.aclose()
            if data is None:
                data = await self._decode_thumbnail(claims)
        if version:
            await cache.aset(key, data, THUMBNAIL_CACHE_SECONDS)
        return data

    async def _decode_thumbnail(self, claims: tokens.FileLinkClaims) -> bytes:
        """Render an image preview in the backend (guests without ffmpeg)."""
        stream, words, leftover = await self._open_with_header(
            claims.workspace_id,
            guest_scripts.read_command(
                claims.path,
                first=None,
                last=None,
                suffix=None,
                maximum=THUMBNAIL_IMAGE_SOURCE_MAX_BYTES,
            ),
        )
        try:
            if not words or words[0] != "OK" or len(words) != 5:
                raise FileAccessError(404, "no_thumbnail", "No preview is available.")
            source = await _collect(
                _bounded_body(stream, leftover, int(words[1])),
                THUMBNAIL_IMAGE_SOURCE_MAX_BYTES,
            )
        finally:
            await stream.aclose()
        try:
            return await asyncio.to_thread(render_thumbnail, source)
        except Exception as exc:
            raise FileAccessError(
                404, "no_thumbnail", "No preview is available."
            ) from exc

    # -- Guest streams --------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _transfer_slot(self, workspace_id: str) -> AsyncIterator[None]:
        """Hold one of the workspace's transfer slots and count as activity."""
        budget = self._budgets.setdefault(
            workspace_id, asyncio.Semaphore(STREAMS_PER_WORKSPACE)
        )
        try:
            await asyncio.wait_for(budget.acquire(), STREAM_WAIT_SECONDS)
        except TimeoutError as exc:
            raise FileAccessError(
                503,
                "workspace_busy",
                "Too many transfers for this workspace; try again shortly.",
                retry_after=2,
            ) from exc
        toucher = asyncio.create_task(self._keep_active(workspace_id))
        try:
            yield
        finally:
            toucher.cancel()
            await asyncio.gather(toucher, return_exceptions=True)
            budget.release()

    async def _keep_active(self, workspace_id: str) -> None:
        """Touch workspace activity at most once per interval, however busy."""
        lifecycle = self._runner_service().workspace_lifecycle
        loop = asyncio.get_running_loop()
        while True:
            last = self._touched.get(workspace_id, float("-inf"))
            wait = last + ACTIVITY_TOUCH_SECONDS - loop.time()
            if wait <= 0:
                self._touched[workspace_id] = loop.time()
                with contextlib.suppress(Exception):
                    await lifecycle.touch_workspace_activity(uuid.UUID(workspace_id))
                wait = ACTIVITY_TOUCH_SECONDS
            await asyncio.sleep(wait)

    async def _open(self, workspace_id: str, command: list[str]) -> WorkspaceByteStream:
        try:
            accessor = await self._accessor_factory(workspace_id)
            return await accessor.open_process(command, timeout=STREAM_OPEN_TIMEOUT)
        except Exception as exc:
            log.info(
                "workspace_file_stream_refused",
                workspace_id=workspace_id,
                error=type(exc).__name__,
            )
            raise _map_access_errors(exc) from exc

    async def _open_with_header(
        self, workspace_id: str, command: list[str]
    ) -> tuple[WorkspaceByteStream, list[str], bytes]:
        """Open a guest stream and read its header line."""
        stream = await self._open(workspace_id, command)
        buffer = b""
        try:
            while b"\n" not in buffer:
                chunk = await stream.receive()
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > _HEADER_MAX_BYTES and b"\n" not in buffer:
                    raise ValueError("header too long")
        except (StreamClosedError, ValueError) as exc:
            await stream.aclose()
            raise FileAccessError(
                503, "workspace_unavailable", "The workspace did not answer."
            ) from exc
        except BaseException:
            await stream.aclose()
            raise
        header, _, leftover = buffer.partition(b"\n")
        return stream, header.decode("ascii", "replace").split(), leftover


def _chunk_size() -> int:
    return WorkspaceByteStream.MAX_CHUNK_SIZE


def _guest_error(words: list[str]) -> FileAccessError:
    if words[:2] == ["ERR", "too_large"]:
        size = int(words[2]) if len(words) > 2 and words[2].isdigit() else None
        return _too_large(size)
    code = words[1] if len(words) > 1 and words[0] == "ERR" else "unavailable"
    status, machine, message = _GUEST_ERRORS.get(code, _GUEST_ERRORS["unavailable"])
    return FileAccessError(status, machine, message)


async def _bounded_body(
    stream: WorkspaceByteStream, leftover: bytes, length: int
) -> AsyncIterator[bytes]:
    """Yield exactly *length* payload bytes; fail when the guest sends fewer."""
    remaining = length
    if leftover:
        piece = leftover[:remaining]
        remaining -= len(piece)
        if piece:
            yield piece
    while remaining > 0:
        chunk = await stream.receive()
        if not chunk:
            raise StreamClosedError("file ended before its announced size")
        piece = chunk[:remaining]
        remaining -= len(piece)
        yield piece


async def _collect(body: AsyncIterator[bytes], limit: int) -> bytes:
    parts: list[bytes] = []
    total = 0
    async for chunk in body:
        total += len(chunk)
        if total > limit:
            raise FileAccessError(404, "no_thumbnail", "No preview is available.")
        parts.append(chunk)
    return b"".join(parts)


async def _read_all(stream: WorkspaceByteStream, *, limit: int) -> bytes:
    output = b""
    with contextlib.suppress(StreamClosedError):
        while len(output) <= limit:
            chunk = await stream.receive()
            if not chunk:
                break
            output += chunk
    return output[:limit]


def _thumbnail_cache_key(claims: tokens.FileLinkClaims, version: str) -> str:
    import hashlib

    digest = hashlib.sha256(
        f"{claims.workspace_id}\0{claims.path}\0{version}".encode()
    ).hexdigest()
    return f"workspace-file-thumbnail:{digest}"


def render_thumbnail(source: bytes) -> bytes:
    """Return a blurred JPEG of at most :data:`THUMBNAIL_EDGE` pixels per side."""
    from PIL import Image, ImageFilter, ImageOps

    with Image.open(
        io.BytesIO(source), formats=("PNG", "JPEG", "GIF", "WEBP", "BMP")
    ) as image:
        if image.width * image.height > THUMBNAIL_IMAGE_MAX_PIXELS:
            raise ValueError("Image exceeds the preview pixel limit")
        image.draft("RGB", (THUMBNAIL_EDGE * 4, THUMBNAIL_EDGE * 4))
        picture = ImageOps.exif_transpose(image)
        picture.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE), Image.Resampling.LANCZOS)
        picture = picture.convert("RGB").filter(ImageFilter.GaussianBlur(1.2))
    output = io.BytesIO()
    picture.save(output, format="JPEG", quality=60)
    return output.getvalue()


_service: WorkspaceFileService | None = None


def get_workspace_file_service() -> WorkspaceFileService:
    """Return the process-wide service (it owns the transfer budgets)."""
    global _service
    if _service is None:
        _service = WorkspaceFileService()
    return _service
