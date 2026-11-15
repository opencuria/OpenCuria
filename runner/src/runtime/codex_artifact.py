"""Strict standalone Codex package layout and guest installer (stdlib only)."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

VERSION = "0.166.0"
MAX_ARCHIVE_SIZE = 150 * 1024 * 1024
MAX_PACKAGE_SIZE = 400 * 1024 * 1024
FILES = frozenset(
    {
        "bin/codex-app-server",
        "bin/codex-code-mode-host",
        "bin/codex-exec-server",
        "codex-package.json",
        "codex-path/rg",
        "codex-resources/bwrap",
    }
)
DIRECTORIES = frozenset({"bin", "codex-path", "codex-resources"})
PINS = {
    "x86_64-unknown-linux-musl": (
        "e69a1b62488288f702186b9c216a8e3930e45475f70124381d4e1c633f2e0c76",
        155332411,
    ),
    "aarch64-unknown-linux-musl": (
        "50bb0d5ce10faa13eb6e7b0b25e1fe9712d686c41b3c3fdff587512064630d5d",
        145150661,
    ),
}
RELEASE_URL = "https://github.com/openai/codex/releases/download/rust-v0.166.0/"


def target_for_platform(platform: str) -> str:
    """Use the official static musl package on both supported libc variants."""
    arch = "x86_64" if platform in {"linux-x64", "linux-x64-musl"} else "aarch64"
    if platform not in {
        "linux-x64",
        "linux-x64-musl",
        "linux-arm64",
        "linux-arm64-musl",
    }:
        raise ValueError("Unsupported Codex platform")
    return arch + "-unknown-linux-musl"


def manifest_entry() -> dict:
    """Return the pinned official standalone app-server package specification."""
    platforms = {}
    for platform in ("linux-x64", "linux-x64-musl", "linux-arm64", "linux-arm64-musl"):
        target = target_for_platform(platform)
        digest, size = PINS[target]
        platforms[platform] = {
            "url": RELEASE_URL + f"codex-app-server-package-{target}.tar.gz",
            "sha256": digest,
            "size": size,
        }
    return {"version": VERSION, "platforms": platforms}


def sha_file(path: Path) -> str:
    """Hash a file without buffering its contents."""
    with path.open("rb") as source:
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


class _BoundedPackageStream:
    """Bound all decompressed TAR bytes, including metadata and padding."""

    def __init__(self, stream: gzip.GzipFile) -> None:
        self.stream = stream
        self.received = 0

    def read(self, size: int = -1) -> bytes:
        # TAR payload reads are streamed; oversized PAX/header allocations fail.
        if size < 0 or size > 1024 * 1024:
            raise ValueError("Oversized Codex archive metadata")
        data = self.stream.read(size)
        self.received += len(data)
        if self.received > MAX_PACKAGE_SIZE + 1024 * 1024:
            raise ValueError("Codex archive exceeds decompression limit")
        return data


def inspect_package(path: Path, target: str) -> dict[str, str]:
    """Validate every archive entry and hash all payload bytes before execution."""
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > MAX_ARCHIVE_SIZE
    ):
        raise ValueError("Unsafe Codex archive")
    found = set()
    hashes = {}
    total = 0
    with (
        gzip.open(path, "rb") as compressed,
        tarfile.open(fileobj=_BoundedPackageStream(compressed), mode="r|") as archive,
    ):
        for member in archive:
            name = member.name
            if (
                name in found
                or set(member.pax_headers) - {"mtime"}
                or member.sparse is not None
            ):
                raise ValueError("Unexpected Codex archive entry")
            found.add(name)
            if name in DIRECTORIES and member.isdir() and member.size == 0:
                continue
            if name not in FILES or not member.isfile() or member.size <= 0:
                raise ValueError("Unexpected Codex archive entry")
            total += member.size
            if total > MAX_PACKAGE_SIZE:
                raise ValueError("Codex package exceeds size limit")
            if name == "codex-package.json" and member.size > 4096:
                raise ValueError("Invalid Codex package metadata")
            stream = archive.extractfile(member)
            digest = hashlib.sha256()
            metadata = bytearray()
            for block in iter(lambda stream=stream: stream.read(1024 * 1024), b""):
                digest.update(block)
                if name == "codex-package.json":
                    metadata.extend(block)
            hashes[name] = digest.hexdigest()
            if name == "codex-package.json" and json.loads(metadata) != {
                "layoutVersion": 1,
                "version": VERSION,
                "target": target,
                "variant": "codex-app-server",
                "entrypoint": "bin/codex-app-server",
                "execServer": "bin/codex-exec-server",
                "resourcesDir": "codex-resources",
                "pathDir": "codex-path",
            }:
                raise ValueError("Invalid Codex package metadata")
    if found != FILES | DIRECTORIES:
        raise ValueError("Incomplete Codex package")
    return hashes


def safe_directory(path: Path, *, create: bool = False) -> None:
    """Refuse links and non-directories in every absolute path component."""
    if not path.is_absolute() or any(p in {".", ".."} for p in path.parts):
        raise ValueError("Unsafe Codex directory")
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if create:
            try:
                current.mkdir(mode=0o755)
            except FileExistsError:
                pass
        mode = current.lstat().st_mode
        if not stat.S_ISDIR(mode):
            raise ValueError("Unsafe Codex directory")


def verify_tree(root: Path, hashes: dict[str, str]) -> None:
    """Reject missing, extra, linked, mutable or changed installed package bytes."""
    safe_directory(root)
    seen = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            seen.add(relative)
            if relative in DIRECTORIES:
                if not stat.S_ISDIR(mode) or mode & 0o222:
                    raise ValueError("Unsafe installed Codex directory")
            elif (
                relative not in hashes
                or not stat.S_ISREG(mode)
                or path.lstat().st_nlink != 1
                or mode & 0o222
                or sha_file(path) != hashes[relative]
                or stat.S_IMODE(mode)
                != (0o444 if relative.endswith(".json") else 0o555)
            ):
                raise ValueError("Changed installed Codex package")
    if seen != FILES | DIRECTORIES or root.stat().st_mode & 0o222:
        raise ValueError("Incomplete installed Codex package")


def publish_directory(stage: Path, final: Path) -> None:
    """Atomically publish without replacing even an empty concurrent directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(final), 1) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ValueError("Codex installation already exists")
        raise OSError(error, "Codex atomic publication failed")


def guest_main() -> None:
    """Install with no-replace publication under a guest lock, or reverify."""
    action, source_name, digest, root_name, target, encoded_hashes = sys.argv[1:]
    root = Path(root_name)
    source = Path(source_name)
    hashes = json.loads(encoded_hashes)
    if (
        action not in {"install", "verify"}
        or set(hashes) != FILES
        or target not in PINS
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not all(re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values())
    ):
        raise ValueError("Invalid Codex installation request")
    safe_directory(root, create=action == "install")
    parent = root / "codex-agent"
    safe_directory(parent, create=action == "install")
    final = parent / VERSION
    lock_fd = os.open(
        parent / ".install-lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    stage = None
    try:
        if (
            not stat.S_ISREG(os.fstat(lock_fd).st_mode)
            or os.fstat(lock_fd).st_nlink != 1
        ):
            raise ValueError("Unsafe Codex installation lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if action == "install" and not os.path.lexists(final):
            if (
                source.is_symlink()
                or not source.is_file()
                or source.stat().st_size > MAX_ARCHIVE_SIZE
                or sha_file(source) != digest
                or inspect_package(source, target) != hashes
            ):
                raise ValueError("Changed Codex archive")
            stage = Path(tempfile.mkdtemp(prefix=".codex-stage-", dir=parent))
            with tarfile.open(source, "r:gz") as archive:
                for directory in DIRECTORIES:
                    (stage / directory).mkdir(mode=0o755)
                for name in sorted(FILES):
                    with (
                        archive.extractfile(name) as src,
                        (stage / name).open("xb") as dst,
                    ):
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                    (stage / name).chmod(0o444 if name.endswith(".json") else 0o555)
            for directory in DIRECTORIES:
                (stage / directory).chmod(0o555)
            stage.chmod(0o555)
            verify_tree(stage, hashes)
            publish_directory(stage, final)
            stage = None
            fd = os.open(parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        verify_tree(final, hashes)
        with tempfile.TemporaryDirectory(prefix="opencuria-codex-version-") as home:
            output = subprocess.check_output(
                [str(final / "bin/codex-app-server"), "--version"],
                env={"HOME": home, "CODEX_HOME": home, "PATH": "/usr/bin:/bin"},
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        if output.strip() != f"codex-app-server {VERSION}".encode():
            raise ValueError("Invalid Codex executable version")
        sys.stdout.write(VERSION + "\n")
    finally:
        if stage is not None:
            for directory in [stage, *(stage / d for d in DIRECTORIES)]:
                if directory.exists():
                    directory.chmod(0o755)
            shutil.rmtree(stage)
        if action == "install":
            source.unlink(missing_ok=True)
        os.close(lock_fd)


if __name__ == "__main__":
    try:
        guest_main()
    except Exception:  # noqa: BLE001 - guest failure output must be sanitized
        sys.exit(4)  # Never expose guest output, paths or environment on failure.
