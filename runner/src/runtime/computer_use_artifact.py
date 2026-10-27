"""Pinned Computer Use package specification and standalone guest installer."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

VERSION = "0.1.0"
NODE_VERSION = "22.23.2"
NODE_PINS = {
    "linux-x64": (
        "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307",
        31058332,
    ),
    "linux-arm64": (
        "fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8",
        30246708,
    ),
}
FILES = {
    "server.js",
    "worker.js",
    "linux.py",
    "manifest.json",
    "NOTICES.txt",
    "node",
    "opencuria-cua",
}
ASSET = (
    Path(__file__).with_name("assets") / "computer-use-0.1.0.tar.gz"
    if "__file__" in globals()
    else Path("/nonexistent")
)


def manifest_entry() -> dict:
    """Return the official, immutable Node archives used by this package."""
    return {
        "version": VERSION,
        "platforms": {
            platform: {
                "sha256": digest,
                "size": size,
                "url": f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-{platform}.tar.xz",
            }
            for platform, (digest, size) in NODE_PINS.items()
        },
    }


def sha_file(path: Path) -> str:
    """Hash regular files without following symbolic links."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Unsafe runtime file")
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def safe_directory(path: Path, create: bool = False) -> None:
    """Reject symbolic links in every directory component."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe runtime path")
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if create:
            try:
                current.mkdir(mode=0o755)
            except FileExistsError:
                pass
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError("Unsafe runtime directory")


def verify(root: Path, hashes: dict[str, str]) -> None:
    """Verify the complete immutable installed runtime before starting it."""
    safe_directory(root)
    if {p.name for p in root.iterdir()} != FILES or root.stat().st_mode & 0o222:
        raise ValueError("Changed runtime installation")
    for name, digest in hashes.items():
        path = root / name
        mode = path.lstat().st_mode
        if (
            not stat.S_ISREG(mode)
            or mode & 0o222
            or path.lstat().st_nlink != 1
            or sha_file(path) != digest
        ):
            raise ValueError("Changed runtime bytes")


def inspect_bundle(path: Path) -> dict[str, str]:
    """Validate fixed bundle files, rejecting extra or linked entries."""
    expected = FILES - {"node", "opencuria-cua"}
    hashes = {}
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            total += member.size
            if (
                member.name not in expected
                or member.name in hashes
                or not member.isfile()
                or member.pax_headers
                or member.size < 1
                or total > 4 * 1024 * 1024
            ):
                raise ValueError("Invalid Computer Use bundle")
            hashes[member.name] = hashlib.sha256(
                archive.extractfile(member).read()
            ).hexdigest()
    if set(hashes) != expected:
        raise ValueError("Incomplete Computer Use bundle")
    return hashes


def node_member(archive: tarfile.TarFile, platform: str) -> tarfile.TarInfo:
    """Select the official interpreter, never extracting archive paths."""
    name = f"node-v{NODE_VERSION}-{platform}/bin/node"
    matches = []
    for index, member in enumerate(archive):
        if index > 10000:
            raise ValueError("Oversized Node archive")
        if member.name == name:
            matches.append(member)
    if (
        len(matches) != 1
        or not matches[0].isfile()
        or not 1 <= matches[0].size <= 150 * 1024 * 1024
    ):
        raise ValueError("Invalid Node interpreter")
    return matches[0]


def launcher(root: Path) -> bytes:
    """Use absolute interpreter/script paths independent of PATH and cwd."""
    return f'#!/bin/sh\nexec "{root}/node" --max-old-space-size=256 "{root}/server.js" "$@"\n'.encode()


def guest_main() -> None:
    """Install under a guest lock, atomically publish, and reverify."""
    (
        action,
        root_name,
        node_source,
        bundle_source,
        platform,
        bundle_digest,
        encoded_hashes,
    ) = sys.argv[1:]
    if action not in {"install", "verify"} or platform not in NODE_PINS:
        raise ValueError("Unapproved Computer Use runtime")
    root = Path(root_name)
    hashes = json.loads(encoded_hashes)
    if set(hashes) != FILES or any(
        len(v) != 64 or set(v) - set("0123456789abcdef") for v in hashes.values()
    ):
        raise ValueError("Invalid runtime hashes")
    safe_directory(root, create=action == "install")
    parent = root / "computer-use"
    safe_directory(parent, create=action == "install")
    final = parent / VERSION
    fd = os.open(
        parent / ".install-lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    stage = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise ValueError("Unsafe runtime lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if action == "install" and not os.path.lexists(final):
            node_path, bundle_path = Path(node_source), Path(bundle_source)
            if (
                sha_file(node_path) != NODE_PINS[platform][0]
                or sha_file(bundle_path) != bundle_digest
                or inspect_bundle(bundle_path)
                != {k: hashes[k] for k in FILES - {"node", "opencuria-cua"}}
            ):
                raise ValueError("Changed runtime package")
            stage = Path(tempfile.mkdtemp(prefix=".cua-stage-", dir=parent))
            with (
                tarfile.open(node_path, "r:xz") as archive,
                archive.extractfile(node_member(archive, platform)) as src,
                (stage / "node").open("xb") as dst,
            ):
                shutil.copyfileobj(src, dst)
            with tarfile.open(bundle_path, "r:gz") as archive:
                for name in FILES - {"node", "opencuria-cua"}:
                    with (
                        archive.extractfile(name) as src,
                        (stage / name).open("xb") as dst,
                    ):
                        shutil.copyfileobj(src, dst)
            (stage / "opencuria-cua").write_bytes(launcher(final))
            for path in stage.iterdir():
                path.chmod(0o555 if path.name in {"node", "opencuria-cua"} else 0o444)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            stage.chmod(0o555)
            verify(stage, hashes)
            # Publication is serialized by the guest lock; do not replace an existing tree.
            if os.path.lexists(final):
                raise ValueError("Runtime installation already exists")
            rename = ctypes.CDLL(None, use_errno=True).renameat2
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
                    raise ValueError("Runtime installation already exists")
                raise OSError(error, "Runtime publication failed")
            stage = None
            parent_fd = os.open(parent, os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        verify(final, hashes)
        subprocess.run(
            [str(final / "node"), "--version"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        probe = "import Xlib, PIL, structlog, gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi"
        check = subprocess.run(
            ["/usr/bin/python3", "-c", probe],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if action == "install" and check.returncode:
            # Upgrade older workspace images through their distribution packages.
            env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
            for command in (
                ["apt-get", "update", "-qq"],
                [
                    "apt-get",
                    "install",
                    "-y",
                    "--no-install-recommends",
                    "python3-xlib",
                    "python3-pil",
                    "python3-structlog",
                    "python3-gi",
                    "gir1.2-atspi-2.0",
                    "at-spi2-core",
                    "xclip",
                ],
            ):
                subprocess.run(
                    command,
                    env=env,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=300,
                )
            check = subprocess.run(
                ["/usr/bin/python3", "-c", probe],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        check.check_returncode()
        if not shutil.which("xclip"):
            raise ValueError("Missing clipboard helper")
        sys.stdout.write(VERSION + "\n")
    finally:
        if stage is not None:
            stage.chmod(0o755)
            shutil.rmtree(stage)
        if action == "install":
            Path(node_source).unlink(missing_ok=True)
            Path(bundle_source).unlink(missing_ok=True)
        os.close(fd)


if __name__ == "__main__":
    try:
        guest_main()
    except Exception:  # noqa: BLE001 - guest failures must not expose environment data
        sys.exit(4)
