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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "0.166.0"
RELEASE_URL = "https://github.com/openai/codex/releases/download/rust-v0.166.0/"
DIRECTORIES = frozenset({"bin", "codex-path", "codex-resources"})
RENAME_NOREPLACE = 1
_MIB = 1024 * 1024


@dataclass(frozen=True)
class CodexTarget:
    """One official app-server package: pinned bytes, exact layout and limits."""

    sha256: str
    size: int
    max_archive_size: int
    max_package_size: int
    resources: frozenset[str]
    executable_suffix: str = ""

    @property
    def entrypoint(self) -> str:
        """Package-relative app-server executable."""
        return "bin/codex-app-server" + self.executable_suffix

    @property
    def exec_server(self) -> str:
        """Package-relative exec-server executable."""
        return "bin/codex-exec-server" + self.executable_suffix

    @property
    def files(self) -> frozenset[str]:
        """Every regular file of the package; nothing else is accepted."""
        suffix = self.executable_suffix
        return frozenset(
            {
                self.entrypoint,
                self.exec_server,
                "bin/codex-code-mode-host" + suffix,
                "codex-package.json",
                "codex-path/rg" + suffix,
                *(f"codex-resources/{name}" for name in self.resources),
            }
        )


# Official release assets. Limits sit just above the measured sizes (Windows:
# 165,654,104 B archive, 479,695,627 B of files; its sandbox helpers replace bwrap).
TARGETS = {
    "x86_64-unknown-linux-musl": CodexTarget(
        sha256="e69a1b62488288f702186b9c216a8e3930e45475f70124381d4e1c633f2e0c76",
        size=155332411,
        max_archive_size=150 * _MIB,
        max_package_size=400 * _MIB,
        resources=frozenset({"bwrap"}),
    ),
    "aarch64-unknown-linux-musl": CodexTarget(
        sha256="50bb0d5ce10faa13eb6e7b0b25e1fe9712d686c41b3c3fdff587512064630d5d",
        size=145150661,
        max_archive_size=150 * _MIB,
        max_package_size=400 * _MIB,
        resources=frozenset({"bwrap"}),
    ),
    "x86_64-pc-windows-msvc": CodexTarget(
        sha256="2476ec968c64e9ae47f449446320e47a54d16f5436ce6eb95eec758584f12eb6",
        size=165654104,
        max_archive_size=168 * _MIB,
        max_package_size=480 * _MIB,
        resources=frozenset(
            {"codex-command-runner.exe", "codex-windows-sandbox-setup.exe"}
        ),
        executable_suffix=".exe",
    ),
}
# Both Linux libc variants use the official static musl package.
PLATFORM_TARGETS = {
    "linux-x64": "x86_64-unknown-linux-musl",
    "linux-x64-musl": "x86_64-unknown-linux-musl",
    "linux-arm64": "aarch64-unknown-linux-musl",
    "linux-arm64-musl": "aarch64-unknown-linux-musl",
    "win32-x64": "x86_64-pc-windows-msvc",
}


def target_spec(target: str) -> CodexTarget:
    """Return the pinned package description of an approved target."""
    try:
        return TARGETS[target]
    except (KeyError, TypeError):
        raise ValueError("Unsupported Codex target") from None


def target_for_platform(platform: str) -> str:
    """Map a guest platform key to its official package target."""
    try:
        return PLATFORM_TARGETS[platform]
    except (KeyError, TypeError):
        raise ValueError("Unsupported Codex platform") from None


def package_url(target: str) -> str:
    """Fixed release asset URL of a target package."""
    target_spec(target)
    return RELEASE_URL + f"codex-app-server-package-{target}.tar.gz"


def archive_size_limits() -> dict[str, int]:
    """Approved package URLs and their compressed size limits."""
    return {package_url(t): spec.max_archive_size for t, spec in TARGETS.items()}


def package_metadata(target: str) -> dict:
    """The exact ``codex-package.json`` an official target package carries."""
    spec = target_spec(target)
    return {
        "layoutVersion": 1,
        "version": VERSION,
        "target": target,
        "variant": "codex-app-server",
        "entrypoint": spec.entrypoint,
        "execServer": spec.exec_server,
        "resourcesDir": "codex-resources",
        "pathDir": "codex-path",
    }


def manifest_entry() -> dict:
    """Return the pinned official standalone app-server package specification."""
    platforms = {}
    for platform, target in PLATFORM_TARGETS.items():
        spec = TARGETS[target]
        platforms[platform] = {
            "url": package_url(target),
            "sha256": spec.sha256,
            "size": spec.size,
        }
    return {"version": VERSION, "platforms": platforms}


def cygwin_runtime() -> bool:
    """Windows guests run this module in MSYS2 (Cygwin) Python on NTFS."""
    return sys.platform in {"cygwin", "msys"}


def version_environment(target: str, home: str) -> dict[str, str]:
    """Minimal ``--version`` environment; profile and config stay in ``home``."""
    env = {"HOME": home, "CODEX_HOME": home, "PATH": "/usr/bin:/bin"}
    if target_spec(target).executable_suffix == ".exe":
        # Native Windows programs resolve profile and temp directories from
        # these. The Cygwin runtime converts the paths and adds SYSTEMROOT and
        # WINDIR itself.
        for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
            env[name] = home
    return env


def sha_file(path: Path) -> str:
    """Hash a file without buffering its contents."""
    with path.open("rb") as source:
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


class _BoundedPackageStream:
    """Bound all decompressed TAR bytes, including metadata and padding."""

    def __init__(self, stream: gzip.GzipFile, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.received = 0

    def read(self, size: int = -1) -> bytes:
        # TAR payload reads are streamed; oversized PAX/header allocations fail.
        if size < 0 or size > _MIB:
            raise ValueError("Oversized Codex archive metadata")
        data = self.stream.read(size)
        self.received += len(data)
        if self.received > self.limit + _MIB:
            raise ValueError("Codex archive exceeds decompression limit")
        return data


def inspect_package(path: Path, target: str) -> dict[str, str]:
    """Validate every archive entry and hash all payload bytes before execution."""
    spec = target_spec(target)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > spec.max_archive_size
    ):
        raise ValueError("Unsafe Codex archive")
    found = set()
    hashes = {}
    total = 0
    with (
        gzip.open(path, "rb") as compressed,
        tarfile.open(
            fileobj=_BoundedPackageStream(compressed, spec.max_package_size), mode="r|"
        ) as archive,
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
            if name not in spec.files or not member.isfile() or member.size <= 0:
                raise ValueError("Unexpected Codex archive entry")
            total += member.size
            if total > spec.max_package_size:
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
            if name == "codex-package.json" and json.loads(
                metadata
            ) != package_metadata(target):
                raise ValueError("Invalid Codex package metadata")
    if found != spec.files | DIRECTORIES:
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
    """Reject missing, extra, linked, mutable or changed installed package bytes.

    ``hashes`` holds every package file. On NTFS the Cygwin runtime synthesizes
    modes: directories always report owner-write and execute bits follow file
    names, so there only the read-only state of files is checked. Entry set,
    types, link counts and bytes stay exact everywhere.
    """
    if not any(set(hashes) == spec.files for spec in TARGETS.values()):
        raise ValueError("Incomplete Codex package hashes")
    posix_modes = not cygwin_runtime()
    safe_directory(root)
    seen = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            seen.add(relative)
            if relative in DIRECTORIES:
                if not stat.S_ISDIR(mode) or (posix_modes and mode & 0o222):
                    raise ValueError("Unsafe installed Codex directory")
            elif (
                relative not in hashes
                or not stat.S_ISREG(mode)
                or path.lstat().st_nlink != 1
                or mode & 0o222
                or sha_file(path) != hashes[relative]
                or (
                    posix_modes
                    and stat.S_IMODE(mode)
                    != (0o444 if relative.endswith(".json") else 0o555)
                )
            ):
                raise ValueError("Changed installed Codex package")
    if seen != set(hashes) | DIRECTORIES or (
        posix_modes and root.stat().st_mode & 0o222
    ):
        raise ValueError("Incomplete installed Codex package")


def _renameat2() -> tuple[Any, int]:
    """Return the C runtime's renameat2 and that runtime's AT_FDCWD."""
    if not cygwin_runtime():
        return ctypes.CDLL(None, use_errno=True).renameat2, -100
    # dlopen(NULL) only exposes the Python executable on Cygwin; the runtime
    # DLL itself exports renameat2.
    for name in ("msys-2.0.dll", "cygwin1.dll"):
        try:
            return ctypes.CDLL(name, use_errno=True).renameat2, -2
        except (OSError, AttributeError):
            continue
    raise OSError(errno.ENOSYS, "renameat2 is unavailable")


def publish_directory(stage: Path, final: Path) -> None:
    """Atomically publish without replacing even an empty concurrent directory.

    Invariant: ``final`` is absent or one complete verified tree, moved by a
    single rename that fails if anything holds the name. A directory cannot be
    created first and filled atomically, and plain rename() replaces an empty
    directory, so both runtimes use renameat2(RENAME_NOREPLACE): Cygwin
    implements it as an NTFS rename without ReplaceIfExists (EEXIST on
    collision). The guest lock only serializes cooperating installers.
    """
    rename, at_fdcwd = _renameat2()
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    source, destination = os.fsencode(stage), os.fsencode(final)
    if rename(at_fdcwd, source, at_fdcwd, destination, RENAME_NOREPLACE) != 0:
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
    spec = target_spec(target)
    if (
        action not in {"install", "verify"}
        or set(hashes) != spec.files
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
                or source.stat().st_size > spec.max_archive_size
                or sha_file(source) != digest
                or inspect_package(source, target) != hashes
            ):
                raise ValueError("Changed Codex archive")
            stage = Path(tempfile.mkdtemp(prefix=".codex-stage-", dir=parent))
            with tarfile.open(source, "r:gz") as archive:
                for directory in DIRECTORIES:
                    (stage / directory).mkdir(mode=0o755)
                for name in sorted(spec.files):
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
                [str(final / spec.entrypoint), "--version"],
                env=version_environment(target, home),
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
