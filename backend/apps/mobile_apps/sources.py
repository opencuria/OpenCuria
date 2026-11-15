"""Read an Expo project's build from a workspace through its runner.

The agent builds the app itself (``npx expo export``). Publishing resolves the
project's public Expo config with the project's own CLI, packs the build output
into one archive inside the workspace and transfers it in a single read.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import posixpath
import tarfile
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

import structlog

from apps.harness.access.base import sanitize_harness_path
from apps.harness.access.runner_accessor import (
    HARNESS_READ_MAX_BYTES,
    RunnerAccessorError,
    create_harness_accessor,
)
from apps.runners.services import get_runner_service

from .export import MAX_VERSION_BYTES, ExportError, ExportFiles, relative_path

log = structlog.get_logger(__name__)

CONFIG_TIMEOUT_SECONDS = 90.0
ARCHIVE_TIMEOUT_SECONDS = 90.0
#: Unpacked archive bound: the build plus package.json and the icon.
MAX_UNPACKED_BYTES = MAX_VERSION_BYTES + 8 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
#: Decompressed archive bound, with room for every member's header and padding.
MAX_DECOMPRESSED_BYTES = MAX_UNPACKED_BYTES + MAX_ARCHIVE_MEMBERS * 2048
#: Chunked runner transfers occasionally drop a chunk; the archive stays in
#: place until cleanup, so one more read usually succeeds.
READ_ATTEMPTS = 2

AccessorFactory = Callable[[str], Awaitable[Any]]


async def _runner_accessor(workspace_id: str) -> Any:
    return await create_harness_accessor(get_runner_service(), workspace_id)


def _public_config(stdout: str) -> dict[str, Any]:
    """Parse ``expo config --json`` output, tolerating log lines around it."""
    text = stdout.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ExportError("`npx expo config --type public --json` printed no JSON.")
    try:
        config = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ExportError("Could not parse the project's public Expo config.") from exc
    if not isinstance(config, dict):
        raise ExportError("The project's public Expo config is not an object.")
    return config


def _tail(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"…{text[-limit:]}"


def _too_large() -> ExportError:
    return ExportError(
        f"The build is larger than {MAX_VERSION_BYTES // (1024 * 1024)} MiB."
    )


class _BoundedStream(io.RawIOBase):
    """Reads *inner* up to *limit* bytes; more is a decompression bomb."""

    def __init__(self, inner: BinaryIO, limit: int) -> None:
        self._inner = inner
        self._left = limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self._inner.read(len(buffer))
        self._left -= len(data)
        if self._left < 0:
            raise _too_large()
        buffer[: len(data)] = data
        return len(data)


def unpack_archive(data: bytes) -> dict[str, bytes]:
    """Return regular files of a ``.tar.gz`` keyed by normalized path.

    Links, devices and paths escaping the root are skipped; the decompressed
    size, the unpacked size (skipped members included, since they are
    decompressed too) and the member count are bounded. The archive comes
    from the workspace, so none of its sizes are trusted.
    """
    files: dict[str, bytes] = {}
    total = 0
    stream = _BoundedStream(
        gzip.GzipFile(fileobj=io.BytesIO(data)), MAX_DECOMPRESSED_BYTES
    )
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_ARCHIVE_MEMBERS:
                    raise ExportError("The build contains too many files.")
                total += member.size
                if total > MAX_UNPACKED_BYTES:
                    raise _too_large()
                if not member.isreg():
                    continue
                try:
                    path = relative_path(member.name.removeprefix("./"))
                except ExportError:
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    files[path] = handle.read()
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ExportError("The build archive from the workspace is corrupt.") from exc
    return files


class WorkspaceExportReader:
    """Collects an Expo build from a workspace VM."""

    def __init__(self, accessor_factory: AccessorFactory | None = None) -> None:
        self._accessor_factory = accessor_factory or _runner_accessor

    @staticmethod
    async def _read_archive(accessor: Any, path: str) -> Any:
        """Transfer the archive, retrying once after an interrupted transfer."""
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                return await accessor.read_file(path, max_size=HARNESS_READ_MAX_BYTES)
            except RunnerAccessorError:
                if attempt == READ_ATTEMPTS:
                    raise
                log.warning("app_archive_read_retry", attempt=attempt)
        raise AssertionError("unreachable")

    async def read(
        self, workspace_id: uuid.UUID, project_path: str, output_dir: str = "dist"
    ) -> tuple[ExportFiles, bytes | None]:
        """Return the build and the raw package.json of *project_path*.

        Raises:
            ExportError: When the project, its config or its build is unusable.
            ServiceError: When the workspace or its runner is unavailable.
        """
        try:
            project = sanitize_harness_path(project_path)
        except ValueError as exc:
            raise ExportError(f"Invalid project path {project_path!r}.") from exc
        output = relative_path(output_dir or "dist")
        accessor = await self._accessor_factory(str(workspace_id))

        result = await accessor.exec_wait(
            ["npx", "--no-install", "expo", "config", "--type", "public", "--json"],
            workdir=project,
            timeout=CONFIG_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            raise ExportError(
                f"`npx expo config` failed in {project} (exit {result.exit_code}). "
                "Is this an Expo project with installed dependencies? "
                f"{_tail(result.stderr or result.stdout)}"
            )
        config = _public_config(result.stdout)

        members = [output, "package.json"]
        icon = config.get("icon")
        if isinstance(icon, str) and icon and "://" not in icon:
            try:
                members.append(relative_path(icon.removeprefix("./")))
            except ExportError:
                pass
        archive_path = f"/tmp/opencuria-app-{uuid.uuid4().hex}.tar.gz"
        try:
            packed = await accessor.exec_wait(
                ["tar", "-czhf", archive_path, "--ignore-failed-read", "-C", project]
                + ["--", *members],
                workdir=project,
                timeout=ARCHIVE_TIMEOUT_SECONDS,
            )
            if packed.exit_code != 0:
                raise ExportError(
                    f"Could not pack {posixpath.join(project, output)}: "
                    f"{_tail(packed.stderr)}"
                )
            too_large = ExportError(
                f"The build in {output} is larger than "
                f"{HARNESS_READ_MAX_BYTES // (1024 * 1024)} MiB compressed."
            )
            # The runner refuses bigger reads outright; check before transferring.
            size = await accessor.exec_wait(
                ["stat", "-c", "%s", "--", archive_path],
                timeout=ARCHIVE_TIMEOUT_SECONDS,
            )
            reported = size.stdout.strip()
            if size.exit_code == 0 and reported.isdigit():
                if int(reported) > HARNESS_READ_MAX_BYTES:
                    raise too_large
            archive = await self._read_archive(accessor, archive_path)
            if archive.truncated:
                raise too_large
        finally:
            try:
                await accessor.exec_wait(
                    ["rm", "-f", archive_path], timeout=ARCHIVE_TIMEOUT_SECONDS
                )
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                log.warning(
                    "app_archive_cleanup_failed", workspace_id=str(workspace_id)
                )
        # Decompressing up to ~100 MiB must not stall the event loop.
        files = await asyncio.to_thread(unpack_archive, archive.content)
        export = ExportFiles(config=config, output_dir=output, files=files)
        return export, files.get("package.json")
