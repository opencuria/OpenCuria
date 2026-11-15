#!/usr/bin/env python3
"""Install/reverify the pinned optional Codex test runtime using only stdlib."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from email.message import Message
from pathlib import Path
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / ".opencuria/codex-test-runtime"


class ReleaseRedirects(urllib.request.HTTPRedirectHandler):
    """Permit HTTPS redirects only to official GitHub release asset hosts."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Validate the redirect before urllib makes the next request."""
        parsed = urllib.parse.urlsplit(newurl)
        if (
            parsed.scheme != "https"
            or parsed.hostname
            not in {
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
            }
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise ValueError("Unexpected Codex download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def copy_bounded(source: BinaryIO, destination: BinaryIO, limit: int) -> None:
    """Stream no more than the pinned archive size."""
    total = 0
    while block := source.read(1024 * 1024):
        total += len(block)
        if total > limit:
            raise ValueError("Codex archive exceeds its pinned size")
        destination.write(block)


def install(archive_source: Path | None) -> Path:
    """Verify the approved archive and every installed file before execution."""
    machine = platform.machine().lower()
    if sys.platform != "linux" or machine not in {
        "x86_64",
        "amd64",
        "aarch64",
        "arm64",
    }:
        raise ValueError("Codex test runtime requires Linux x86_64 or aarch64")
    # This module is standalone stdlib: no runner service or Django imports.
    codex = runpy.run_path(str(ROOT / "runner/src/runtime/codex_artifact.py"))
    manifest = json.loads(
        (ROOT / "runner/src/runtime/artifact_manifest.json").read_text()
    )["artifacts"]["codex-agent"]
    models = json.loads(
        (ROOT / "backend/apps/harness/engines/codex/models.json").read_text()
    )
    if (
        manifest != codex["manifest_entry"]()
        or models["runtime_version"] != codex["VERSION"]
    ):
        raise ValueError("Codex manifest/catalog version pins disagree")
    target = codex["target_for_platform"](
        "linux-x64" if machine in {"x86_64", "amd64"} else "linux-arm64"
    )
    digest, size = codex["PINS"][target]
    codex["safe_directory"](DESTINATION, create=True)
    lock = os.open(
        DESTINATION / ".install-lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Unsafe test runtime lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        archive = DESTINATION / "package.tar.gz"
        if not os.path.lexists(archive):
            fd, temporary = tempfile.mkstemp(prefix=".download-", dir=DESTINATION)
            try:
                with os.fdopen(fd, "wb") as output:
                    if archive_source is not None:
                        if archive_source.is_symlink() or not archive_source.is_file():
                            raise ValueError("Unsafe source archive")
                        with archive_source.open("rb") as source:
                            copy_bounded(source, output, size)
                    else:
                        url = (
                            codex["RELEASE_URL"]
                            + f"codex-app-server-package-{target}.tar.gz"
                        )
                        opener = urllib.request.build_opener(ReleaseRedirects())
                        request = urllib.request.Request(
                            url, headers={"User-Agent": "OpenCuria-test-setup"}
                        )
                        with opener.open(request, timeout=60) as source:
                            copy_bounded(source, output, size)
                    output.flush()
                    os.fsync(output.fileno())
                pending = Path(temporary)
                if (
                    pending.stat().st_size != size
                    or codex["sha_file"](pending) != digest
                ):
                    raise ValueError(
                        "Codex archive does not match the approved size/SHA-256"
                    )
                codex["inspect_package"](pending, target)
                os.rename(pending, archive)
            finally:
                Path(temporary).unlink(missing_ok=True)
        if (
            archive.is_symlink()
            or not archive.is_file()
            or archive.stat().st_nlink != 1
            or archive.stat().st_size != size
            or codex["sha_file"](archive) != digest
        ):
            raise ValueError(
                "Cached Codex archive is unsafe or changed; refusing replacement"
            )
        hashes = codex["inspect_package"](archive, target)
        final = DESTINATION / "package"
        if not os.path.lexists(final):
            stage = Path(tempfile.mkdtemp(prefix=".package-", dir=DESTINATION))
            try:
                # No extractall: inspection admits only fixed regular files.
                with tarfile.open(archive, "r:gz") as source:
                    for directory in codex["DIRECTORIES"]:
                        (stage / directory).mkdir()
                    for name in sorted(codex["FILES"]):
                        with (
                            source.extractfile(name) as payload,
                            (stage / name).open("xb") as output,
                        ):
                            shutil.copyfileobj(payload, output)
                        (stage / name).chmod(0o444 if name.endswith(".json") else 0o555)
                for directory in codex["DIRECTORIES"]:
                    (stage / directory).chmod(0o555)
                stage.chmod(0o555)
                codex["verify_tree"](stage, hashes)
                codex["publish_directory"](stage, final)
            finally:
                if stage.exists():
                    for directory in [
                        stage,
                        *(stage / d for d in codex["DIRECTORIES"]),
                    ]:
                        if directory.exists():
                            directory.chmod(0o755)
                    shutil.rmtree(stage)
        codex["verify_tree"](final, hashes)
        binary = final / "bin/codex-app-server"
        with tempfile.TemporaryDirectory(prefix="opencuria-codex-version-") as home:
            result = subprocess.check_output(
                [str(binary), "--version"],
                env={"HOME": home, "CODEX_HOME": home, "PATH": "/usr/bin:/bin"},
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        if result.strip() != f"codex-app-server {codex['VERSION']}".encode():
            raise ValueError("Codex executable version mismatch")
        return binary
    finally:
        os.close(lock)


def main() -> None:
    """Fetch once or verify an existing install; never discover a system CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        help="Seed from a local official archive (same size/SHA-256 checks)",
    )
    args = parser.parse_args()
    try:
        binary = install(args.archive)
    except Exception as exc:
        # Do not expose signed redirect URLs or response bodies in setup logs.
        sys.stderr.write(
            f"Codex test setup failed ({type(exc).__name__}); "
            "no unverified runtime executed.\n"
        )
        raise SystemExit(1) from None
    sys.stdout.write(f"Verified Codex test runtime: {binary}\n")
    sys.stdout.write(f"Verified package archive: {DESTINATION / 'package.tar.gz'}\n")


if __name__ == "__main__":
    main()
