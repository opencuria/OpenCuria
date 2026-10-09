"""Provision pinned, workspace-local runtime artifacts without agent logic."""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import structlog

from ..services.capture_fence import CaptureFence, live_interaction
from .base import RuntimeBackend

logger = structlog.get_logger(__name__)

ARTIFACT_MANIFEST_PATH = Path(__file__).with_name("artifact_manifest.json")
ARTIFACT_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=600, connect=20, sock_read=60
)
ARTIFACT_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
ARTIFACT_MAX_DOWNLOAD_SIZE = 300 * 1024 * 1024
ARTIFACT_GUEST_ROOT = "/opt/opencuria/runtimes"
CLAUDE_RELEASE_MANIFEST_SHA256 = "64f5abe05a43151810acf8c25169872e41293c96d3ae73d6508733a8b336e2b1"
CLAUDE_ARTIFACT_SPEC_SHA256 = "be1a957aeab980c02359264df859bcd78a7bbb45f70c2fd857b4a27a803eb0ed"
CLAUDE_PLATFORM_PINS = {
    "linux-x64": (
        "a967e7b1d8b4e47ee421d5433027880347952b0c0857abf880e2c942a4ec93b3",
        251456696,
    ),
    "linux-arm64": (
        "24caa9e6ff13bf227049a2626f1c816fc895023050f0ec3b12dbf14d897367e0",
        250798072,
    ),
    "linux-x64-musl": (
        "d23f28ef84f5459a25d2d4cf999a7debd41df83507160446983d4ca65554e50f",
        245203032,
    ),
    "linux-arm64-musl": (
        "a17c919b13df206371a55de716ab81e3dcd7c039f66b79003ed87b7665fdc15f",
        243152704,
    ),
}

# This is executed inside the guest; its only output is the installed version.
_GUEST_PLATFORM_PROBE = """\
set -eu
[ "$(uname -s)" = Linux ] || exit 1
case "$(uname -m)" in
    x86_64|amd64) arch=x64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) exit 1 ;;
esac
if [ -e /lib/libc.musl-x86_64.so.1 ] || [ -e /lib/libc.musl-aarch64.so.1 ] || (command -v ldd >/dev/null 2>&1 && ldd /bin/ls 2>&1 | grep -qi musl); then
    libc=-musl
else
    libc=
fi
printf 'linux-%s%s\\n' "$arch" "$libc"
"""

# Fixed shell installer; paths and expected values are manager-generated.
# It hashes a staged file, validates the real version and publishes through a
# same-directory hard-link so a concurrent/active binary is never replaced.
_GUEST_VERIFY = r"""\
set -eu
runtime_root=$1 digest=$2 version=$3
case "$runtime_root" in /*) ;; *) exit 4 ;; esac
case "$runtime_root" in *[!A-Za-z0-9/_-]*|*/../*|*/..) exit 4 ;; esac
path=$runtime_root
while [ "$path" != / ]; do
    [ ! -L "$path" ] || exit 4
    [ -d "$path" ] || exit 4
    path=${path%/*}
    [ -n "$path" ] || path=/
done
root=$runtime_root/claude-agent
version_dir=$root/$version
final=$version_dir/claude
[ -d "$root" ] && [ ! -L "$root" ] || exit 4
[ -d "$version_dir" ] && [ ! -L "$version_dir" ] || exit 4
[ -f "$final" ] && [ ! -L "$final" ] || exit 4
[ "$(sha256sum "$final" | cut -d ' ' -f 1)" = "$digest" ] || exit 4
output=$("$final" --version 2>/dev/null) || exit 4
[ "${output%% *}" = "$version" ] || exit 4
printf '%s\n' "$version"
"""

_GUEST_INSTALLER = r"""\
set -eu
source=$1 digest=$2 version=$3 stage_name=$4 runtime_root=$5
case "$runtime_root" in /*) ;; *) exit 2 ;; esac
case "$runtime_root" in *[!A-Za-z0-9/_-]*|*/../*|*/..) exit 2 ;; esac
case "$version" in 2.1.292) ;; *) exit 2 ;; esac
case "$digest" in *[!0-9a-f]*|'') exit 2 ;; esac
[ "${#digest}" -eq 64 ] || exit 2
case "$stage_name" in .claude-stage-[0-9a-f]*) ;; *) exit 2 ;; esac
[ "${#stage_name}" -eq 46 ] || exit 2
stage=
cleanup() { [ -z "$stage" ] || rm -f "$stage"; rm -f "$source"; }
trap cleanup EXIT HUP INT TERM
mkdir -p "$runtime_root"
path=$runtime_root
while [ "$path" != / ]; do
    [ ! -L "$path" ] && [ -d "$path" ] || exit 3
    path=${path%/*}
    [ -n "$path" ] || path=/
done
root=$runtime_root/claude-agent
version_dir=$root/$version
for directory in "$root" "$version_dir"; do
    if [ -L "$directory" ]; then exit 3; fi
    if [ ! -e "$directory" ]; then mkdir "$directory"; chmod 755 "$directory"; fi
    [ -d "$directory" ] && [ ! -L "$directory" ] || exit 3
done
final=$version_dir/claude
sha() { sha256sum "$1" | cut -d ' ' -f 1; }
valid() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(sha "$1")" = "$digest" ] || return 1
    output=$("$1" --version 2>/dev/null) || return 1
    [ "${output%% *}" = "$version" ]
}
if [ -e "$final" ] || [ -L "$final" ]; then
    valid "$final" || exit 4
    printf '%s\n' "$version"
    exit 0
fi
[ -f "$source" ] && [ ! -L "$source" ] && [ "$(sha "$source")" = "$digest" ] || exit 5
stage=$version_dir/$stage_name
( set -C; : > "$stage" ) || exit 6
cat "$source" > "$stage"
chmod 555 "$stage"
valid "$stage" || exit 7
if ! ln "$stage" "$final" 2>/dev/null; then
    valid "$final" || exit 8
fi
printf '%s\n' "$version"
"""



class ArtifactProvisionError(RuntimeError):
    """Raised when a pinned runtime artifact cannot be provisioned safely."""


class ArtifactRequestError(ValueError):
    """Raised for safe caller/platform validation errors."""


class RuntimeArtifactManager:
    """Ensure manifest-whitelisted native tools inside a guest workspace."""

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend],
        get_cached: Callable[[uuid.UUID], Any],
        get_runtime: Callable[[uuid.UUID], RuntimeBackend],
        state_dir: str | Path,
        *,
        manifest_path: str | Path | None = None,
        guest_runtime_root: str = ARTIFACT_GUEST_ROOT,
    ) -> None:
        self._runtimes = runtimes
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._state_dir = Path(state_dir).expanduser()
        self._cache_root = self._state_dir / "artifacts"
        if (
            not isinstance(guest_runtime_root, str)
            or not guest_runtime_root.startswith("/")
            or any(part in {"", ".", ".."} for part in guest_runtime_root.split("/")[1:])
            or not all(char.isalnum() or char in "/_-" for char in guest_runtime_root)
        ):
            raise ValueError("Invalid guest runtime root")
        self._guest_runtime_root = guest_runtime_root
        self._manifest_path = Path(manifest_path or ARTIFACT_MANIFEST_PATH)
        self.capture_fence: CaptureFence | None = None
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._background_guest_tasks: set[asyncio.Task] = set()
        try:
            raw_manifest = self._manifest_path.read_bytes()
            self._manifest = json.loads(raw_manifest.decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("Runtime artifact manifest unavailable") from exc
        if self._manifest.get("schema_version") != 1:
            raise RuntimeError("Unsupported runtime artifact manifest")
        manifest_entry = self._manifest.get("artifacts", {}).get("claude-agent", {})
        canonical_manifest = copy.deepcopy(self._manifest)
        canonical_manifest["artifacts"]["claude-agent"]["manifest_sha256"] = "x"
        canonical_bytes = (json.dumps(canonical_manifest, indent=2) + "\n").encode()
        if (
            hashlib.sha256(canonical_bytes).hexdigest()
            != CLAUDE_ARTIFACT_SPEC_SHA256
            or manifest_entry.get("manifest_sha256") != CLAUDE_ARTIFACT_SPEC_SHA256
            or manifest_entry.get("release_manifest_sha256")
            != CLAUDE_RELEASE_MANIFEST_SHA256
            or manifest_entry.get("signing_fingerprint")
            != "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
            or manifest_entry.get("version") != "2.1.292"
            or manifest_entry.get("manifest_url")
            != "https://downloads.claude.ai/claude-code-releases/2.1.292/manifest.json"
            or manifest_entry.get("signature_url")
            != "https://downloads.claude.ai/claude-code-releases/2.1.292/manifest.json.sig"
            or manifest_entry.get("signature_sha256")
            != "537e1a8bcca1646fd5e52d3dc8d16509395a88f6f18b13765b2c085d057567ea"
            or set(manifest_entry.get("platforms", {})) != set(CLAUDE_PLATFORM_PINS)
        ):
            raise RuntimeError("Runtime artifact manifest integrity check failed")

    @staticmethod
    def _platform_key(output: str) -> str:
        """Validate the guest's fixed platform-probe result."""
        value = output.strip()
        if value not in {
            "linux-x64",
            "linux-arm64",
            "linux-x64-musl",
            "linux-arm64-musl",
        }:
            raise ArtifactRequestError("Unsupported workspace platform")
        return value

    async def _guest_platform(self, runtime: RuntimeBackend, instance_id: str) -> str:
        try:
            code, output = await asyncio.wait_for(
                runtime.exec_command_wait(
                    instance_id, ["sh", "-c", _GUEST_PLATFORM_PROBE]
                ),
                timeout=30,
            )
        except Exception as exc:
            raise ArtifactProvisionError("Workspace platform detection failed") from exc
        if code != 0:
            raise ArtifactRequestError("Unsupported workspace platform")
        return self._platform_key(output)

    def _safe_cache_dirs(self, platform_key: str, digest: str) -> Path:
        """Create private cache directories, refusing symlinked components."""
        # The configured state directory is operator-controlled; the artifact
        # subtree is runner-owned and must not redirect writes through symlinks.
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._state_dir.is_symlink() or not self._state_dir.is_dir():
            raise ArtifactProvisionError("Artifact cache path is unsafe")
        current = self._state_dir
        if current.is_symlink() or not current.is_dir():
            raise ArtifactProvisionError("Artifact cache path is unsafe")
        for piece in ("artifacts", "claude-agent", "2.1.292", platform_key, digest):
            current = current / piece
            try:
                mode = current.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise ArtifactProvisionError("Artifact cache path is unsafe")
            except FileNotFoundError:
                try:
                    current.mkdir(mode=0o700)
                    current.chmod(0o700)
                except FileExistsError:
                    mode = current.lstat().st_mode
                    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                        raise ArtifactProvisionError("Artifact cache path is unsafe")
        return current

    async def _download(
        self, url: str, expected_size: int, expected_digest: str, destination: Path
    ) -> None:
        """Stream one fixed URL to an atomic, checksum-verified cache file."""
        if not url.startswith("https://downloads.claude.ai/claude-code-releases/"):
            raise ArtifactProvisionError("Artifact source is not approved")
        if expected_size <= 0 or expected_size > ARTIFACT_MAX_DOWNLOAD_SIZE:
            raise ArtifactProvisionError("Artifact size is outside the allowed limit")
        fd, temp_name = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        received = 0
        try:
            with os.fdopen(fd, "wb") as output:
                async with (
                    aiohttp.ClientSession(timeout=ARTIFACT_DOWNLOAD_TIMEOUT) as session,
                    session.get(url, allow_redirects=False) as response,
                ):
                    if response.status != 200:
                        raise ArtifactProvisionError("Artifact download failed")
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) != expected_size:
                        raise ArtifactProvisionError("Artifact download size mismatch")
                    async for chunk in response.content.iter_chunked(
                        ARTIFACT_DOWNLOAD_CHUNK_SIZE
                    ):
                        received += len(chunk)
                        if received > expected_size:
                            raise ArtifactProvisionError("Artifact download exceeded its size limit")
                        digest.update(chunk)
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != expected_size:
                raise ArtifactProvisionError("Artifact download was incomplete")
            if digest.hexdigest() != expected_digest:
                raise ArtifactProvisionError("Artifact checksum mismatch")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, destination)
            dir_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except ArtifactProvisionError:
            raise
        except Exception as exc:
            raise ArtifactProvisionError("Artifact download failed") from exc
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        """Hash a regular cache file without following links."""
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                return None
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            return digest.hexdigest()
        except OSError:
            return None

    @staticmethod
    def _make_archive(path: Path, archive_name: str) -> bytes:
        """Build a TAR archive without separately loading the source binary."""
        archive = io.BytesIO()
        with path.open("rb") as source, tarfile.open(fileobj=archive, mode="w") as tar:
            info = tarfile.TarInfo(archive_name)
            info.mode = 0o600
            info.size = path.stat().st_size
            tar.addfile(info, source)
        return archive.getvalue()

    @staticmethod
    async def _complete_guest_operation(awaitable, timeout: float):
        """Settle a guest-side-effect command before cancellation can orphan it."""
        operation = asyncio.create_task(awaitable)

        async def settle() -> None:
            try:
                await asyncio.shield(asyncio.wait_for(operation, timeout=timeout))
            except Exception as exc:
                logger.warning(
                    "runtime_artifact_guest_operation_failed",
                    error_type=type(exc).__name__,
                )

        try:
            return await asyncio.wait_for(asyncio.shield(operation), timeout=timeout)
        except asyncio.CancelledError:
            settlement = asyncio.create_task(settle())
            self._retain_background_task(settlement)
            raise
        except asyncio.TimeoutError:
            settlement = asyncio.create_task(settle())
            self._retain_background_task(settlement)
            raise

    async def _settle_cancelled_upload(
        self,
        upload: asyncio.Task,
        runtime: RuntimeBackend,
        instance_id: str,
        source: str,
    ) -> None:
        """Wait for an upload to settle, then remove its unique guest archive."""
        try:
            await asyncio.shield(upload)
        except Exception as exc:
            logger.warning("runtime_artifact_upload_failed", error_type=type(exc).__name__)
        await self._cleanup_guest_archive(runtime, instance_id, source)

    def _retain_background_task(self, task: asyncio.Task) -> None:
        """Keep cancellation cleanup alive and report failures without secrets."""
        self._background_guest_tasks.add(task)

        def completed(done: asyncio.Task) -> None:
            self._background_guest_tasks.discard(done)
            try:
                done.result()
            except Exception as exc:
                logger.warning("runtime_artifact_background_cleanup_failed", error_type=type(exc).__name__)

        task.add_done_callback(completed)

    async def _cleanup_guest_archive(
        self, runtime: RuntimeBackend, instance_id: str, source: str
    ) -> None:
        """Remove an uploaded-but-uninstalled archive without path interpolation."""
        try:
            await asyncio.wait_for(
                runtime.exec_command_wait(
                    instance_id,
                    ["sh", "-c", 'rm -f -- "$1"', "opencuria-artifact-cleanup", source],
                ),
                timeout=10,
            )
        except (RuntimeError, OSError, asyncio.TimeoutError, ValueError):
            logger.warning("runtime_artifact_temp_cleanup_unverified")

    @asynccontextmanager
    async def _locked(self, key: tuple[str, str, str]):
        """Serialize cache publication across tasks and runner processes."""
        async with self._locks_guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cache_dir = self._safe_cache_dirs(
                key[2], self._manifest["artifacts"][key[0]]["platforms"][key[2]]["sha256"]
            )
            lock_path = cache_dir.parent / (cache_dir.name + ".lock")

            def try_acquire() -> int | None:
                fd = os.open(
                    lock_path,
                    os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    os.fchmod(fd, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    os.close(fd)
                    return None
                except BaseException:
                    os.close(fd)
                    raise

            fd = None
            while fd is None:
                fd = await asyncio.to_thread(try_acquire)
                if fd is None:
                    await asyncio.sleep(0.05)
            try:
                yield
            finally:
                unlock = asyncio.create_task(
                    asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_UN)
                )
                try:
                    await asyncio.shield(unlock)
                except asyncio.CancelledError:
                    await asyncio.shield(unlock)
                    raise
                finally:
                    os.close(fd)

    @live_interaction
    async def ensure_runtime_artifact(
        self,
        workspace_id: uuid.UUID,
        artifact_id: str,
        version: str,
    ) -> dict[str, Any]:
        """Provision one approved native artifact into a workspace atomically."""
        if not isinstance(workspace_id, uuid.UUID):
            raise ArtifactRequestError("workspace_id must be a UUID")
        if not isinstance(artifact_id, str) or not isinstance(version, str):
            raise ArtifactRequestError("Invalid artifact identity")
        entry = self._manifest.get("artifacts", {}).get(artifact_id)
        if not entry or entry.get("version") != version:
            raise ArtifactRequestError("Unapproved runtime artifact or version")
        if artifact_id != "claude-agent" or version != "2.1.292":
            raise ArtifactRequestError("Unapproved runtime artifact or version")
        platforms = entry.get("platforms")
        if not isinstance(platforms, dict):
            raise TypeError("Runtime artifact manifest is invalid")

        info = self._get_cached(workspace_id)
        if not getattr(info, "instance_id", None):
            raise ArtifactRequestError("Workspace has no active instance")
        runtime = self._get_runtime(workspace_id)
        platform_key = await self._guest_platform(runtime, info.instance_id)
        spec = platforms.get(platform_key)
        if not isinstance(spec, dict):
            raise ArtifactRequestError("Unsupported workspace platform")
        digest = spec.get("sha256")
        url = spec.get("url")
        size = spec.get("size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not isinstance(url, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or size > ARTIFACT_MAX_DOWNLOAD_SIZE
            or CLAUDE_PLATFORM_PINS.get(platform_key) != (digest, size)
            or url
            != f"https://downloads.claude.ai/claude-code-releases/2.1.292/{platform_key}/claude"
        ):
            raise RuntimeError("Runtime artifact manifest is invalid")
        key = (artifact_id, version, platform_key)
        async with self._locked(key):
            try:
                cache_dir = self._safe_cache_dirs(platform_key, digest)
                binary = cache_dir / "claude"
                if self._hash_file(binary) != digest:
                    try:
                        binary.unlink()
                    except FileNotFoundError:
                        pass
                    await self._download(url, size, digest, binary)
                if binary.stat().st_size != size:
                    raise ArtifactProvisionError("Artifact cache size mismatch")

                archive_name = f"opencuria-claude-agent-{digest}-{uuid.uuid4().hex}"
                archive = await asyncio.to_thread(
                    self._make_archive, binary, archive_name
                )
                source = f"/tmp/{archive_name}"
                upload = asyncio.create_task(
                    asyncio.wait_for(
                        runtime.put_archive(info.instance_id, "/tmp", archive),
                        timeout=600,
                    )
                )
                try:
                    await asyncio.shield(upload)
                except asyncio.CancelledError:
                    cleanup = asyncio.create_task(
                        self._settle_cancelled_upload(
                            upload, runtime, info.instance_id, source
                        )
                    )
                    self._retain_background_task(cleanup)
                    raise
                stage_name = ".claude-stage-" + uuid.uuid4().hex
                guest_runtime_root = self._guest_runtime_root
                try:
                    install_command = [
                        "sh",
                        "-c",
                        _GUEST_INSTALLER,
                        "opencuria-artifact-installer",
                        source,
                        digest,
                        version,
                        stage_name,
                        guest_runtime_root,
                    ]
                    code, output = await self._complete_guest_operation(
                        runtime.exec_command_wait(info.instance_id, install_command),
                        timeout=60,
                    )
                    if code != 0 or output.strip() != version:
                        raise ArtifactProvisionError("Runtime artifact provisioning failed")
                except ArtifactProvisionError:
                    raise
                except Exception as exc:
                    raise ArtifactProvisionError(
                        "Runtime artifact provisioning failed"
                    ) from exc
                current = self._get_cached(workspace_id)
                if current.instance_id != info.instance_id:
                    raise ArtifactProvisionError("Workspace changed during artifact provisioning")
                if self.capture_fence is not None:
                    self.capture_fence.check_current(workspace_id)
                verify_command = [
                    "sh",
                    "-c",
                    _GUEST_VERIFY,
                    "opencuria-artifact-verify",
                    guest_runtime_root,
                    digest,
                    version,
                ]
                try:
                    code, output = await asyncio.wait_for(
                        runtime.exec_command_wait(info.instance_id, verify_command),
                        timeout=30,
                    )
                except Exception as exc:
                    # Verification is deliberately rerun after the install
                    # command and workspace-identity fence.
                    raise ArtifactProvisionError(
                        "Runtime artifact provisioning failed"
                    ) from exc
                if code != 0 or output.strip() != version:
                    raise ArtifactProvisionError("Runtime artifact provisioning failed")
                guest_path = f"{guest_runtime_root}/{artifact_id}/{version}/claude"
                return {
                    "ok": True,
                    "path": guest_path,
                    "version": version,
                    "platform": platform_key,
                }
            except (ArtifactRequestError, ArtifactProvisionError):
                raise
            except Exception as exc:
                logger.warning(
                    "runtime_artifact_ensure_failed",
                    workspace_id=str(workspace_id),
                    artifact_id=artifact_id,
                    version=version,
                    platform=platform_key,
                )
                raise ArtifactProvisionError(
                    "Runtime artifact provisioning failed"
                ) from exc

__all__ = ["ArtifactProvisionError", "RuntimeArtifactManager"]
