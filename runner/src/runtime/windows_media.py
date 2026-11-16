"""Installation media and the cached base disk of Windows 11 guests.

Microsoft publishes no cloud image. The runner therefore installs Windows once
per edition, unattended, from an administrator-provided Windows 11 ISO and the
pinned guest components below, and caches the installed (not generalized)
disk next to the Ubuntu cloud images. Image builds overlay that cache exactly
like they overlay an Ubuntu cloud image.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from .diagnostics import WINDOWS_MEDIA_UNAVAILABLE, OperationRejectedError
from .storage import sha256_digest

if TYPE_CHECKING:
    from ..config import RunnerSettings

logger = structlog.get_logger(__name__)

ASSETS = Path(__file__).with_name("windows")


@dataclass(frozen=True)
class PinnedFile:
    """A downloadable guest component verified by size and SHA-256."""

    name: str
    url: str
    sha256: str
    size: int


#: Paravirtual drivers, QEMU guest agent and SPICE agent (clipboard).
VIRTIO_WIN = PinnedFile(
    name="virtio-win-0.1.302.iso",
    url=(
        "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/"
        "archive-virtio/virtio-win-0.1.302-1/virtio-win-0.1.302.iso"
    ),
    sha256="303f7ae40dad495d6ae474fdc571df58958a4dbc5c37a522d80f9a203867949d",
    size=877373440,
)
#: Win32-OpenSSH server (key authentication, default shell = MSYS2 bash).
OPENSSH = PinnedFile(
    name="OpenSSH-Win64-v10.0.0.0.msi",
    url=(
        "https://github.com/PowerShell/Win32-OpenSSH/releases/download/"
        "10.0.0.0p2-Preview/OpenSSH-Win64-v10.0.0.0.msi"
    ),
    sha256="ddec9c53864280759cf9f74791cefd387100e3946aa849a1c138a4ed1b96b7d9",
    size=6586368,
)
#: MSYS2 base system providing the POSIX userland of the guest.
MSYS2 = PinnedFile(
    name="msys2-base-x86_64-20260927.sfx.exe",
    url="https://repo.msys2.org/distrib/x86_64/msys2-base-x86_64-20260927.sfx.exe",
    sha256="ad336cccfda47758b5e15cda993fbba421115cb0b126697daef1ee4dfe37209f",
    size=43117824,
)
PINNED_FILES = (VIRTIO_WIN, OPENSSH, MSYS2)

#: MSYS2 packages every Windows guest provides (guest-side runner helpers use
#: python3, git, tar, coreutils and friends exactly like on Linux guests).
MSYS2_PACKAGES = (
    "git", "python", "openssh", "procps-ng", "util-linux", "tar", "zstd", "file",
    "which", "unzip", "zip", "curl", "wget", "vim", "nano", "less", "diffutils",
    "patch", "grep", "sed", "gawk", "findutils", "coreutils", "make", "rsync",
    "man-db",
)

#: base distro -> (install.wim image name, generic non-activating setup key).
EDITIONS: dict[str, tuple[str, str]] = {
    "pro": ("Windows 11 Pro", "VK7JG-NPHTM-C97JM-9MPGT-3V66T"),
    "enterprise": ("Windows 11 Enterprise", "XGVPP-NMH47-7TTHJ-W3FW7-8HV2C"),
    "education": ("Windows 11 Education", "YNMGQ-8RYV3-4PGQ3-C8XTP-7CFBY"),
    "pro-education": ("Windows 11 Pro Education", "8PTT6-RNW4C-6V7J2-C2D3X-MHBPB"),
    "pro-workstations": (
        "Windows 11 Pro for Workstations",
        "DXG7C-N36C4-C4HTG-X4T3X-2YV77",
    ),
}
_DISTRO = re.compile(r"windows:11(?:-(?P<edition>[a-z-]+))?")

#: Scripts that install the base system; they decide what a base contains.
INSTALL_SCRIPTS = ("SetupComplete.cmd", "setup.ps1")
#: Scripts every guest runs later. Builds refresh them from the runner, so a
#: change never requires a new base installation.
GUEST_SCRIPTS = (
    "bootstrap.ps1",
    "generalize.ps1",
    "generalize-unattend.xml",
    "display.ps1",
)
SETUP_SCRIPTS = INSTALL_SCRIPTS + GUEST_SCRIPTS
#: Virtual size of the installed system disk (Windows 11 requires 64 GB).
BASE_DISK_GB = 64


def windows_edition(base_distro: str) -> str:
    """Return the edition key of a ``windows:11[-edition]`` base distro."""
    match = _DISTRO.fullmatch((base_distro or "").strip().lower())
    edition = (match.group("edition") if match else None) or "pro"
    if not match or edition not in EDITIONS:
        raise OperationRejectedError(
            WINDOWS_MEDIA_UNAVAILABLE, f"Unsupported Windows base distro {base_distro!r}"
        )
    return edition


def render_autounattend(edition: str) -> str:
    """Return the installation answer file for one edition."""
    image_name, setup_key = EDITIONS[edition]
    template = (ASSETS / "autounattend.xml").read_text()
    return template.replace("{edition_name}", image_name).replace("{setup_key}", setup_key)


def assets_digest() -> str:
    """Hash every input that changes what a base installation contains."""
    digest = hashlib.sha256()
    for name in ("autounattend.xml", *INSTALL_SCRIPTS):
        digest.update(name.encode())
        digest.update((ASSETS / name).read_bytes())
    digest.update(json.dumps([p.__dict__ for p in PINNED_FILES]).encode())
    digest.update(" ".join(MSYS2_PACKAGES).encode())
    return digest.hexdigest()


class WindowsMedia:
    """Resolve, download and verify Windows installation inputs."""

    def __init__(self, settings: RunnerSettings) -> None:
        self._settings = settings
        self._cache = Path(settings.qemu_image_cache_dir) / "windows"
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def cache_dir(self) -> Path:
        """Directory holding pinned components, downloaded ISOs and bases."""
        return self._cache

    def _ensure_cache(self) -> None:
        self._cache.mkdir(parents=True, exist_ok=True)
        self._cache.chmod(0o755)

    def base_path(self, edition: str, iso_sha256: str) -> Path:
        """Return the cache path of an installed base disk."""
        key = hashlib.sha256(
            f"{edition}:{iso_sha256}:{assets_digest()}".encode()
        ).hexdigest()[:16]
        return self._cache / f"windows-11-{edition}-{key}.qcow2"

    @staticmethod
    def base_marker(base: Path) -> Path:
        """Return the completion marker of a cached base disk."""
        return base.with_suffix(".base.json")

    def base_ready(self, base: Path) -> bool:
        """Whether a completed, unmodified base disk is cached."""
        marker = self.base_marker(base)
        try:
            record = json.loads(marker.read_text())
            stat = base.stat()
        except (OSError, ValueError):
            return False
        return record.get("size_bytes") == stat.st_size and record.get("path") == str(base)

    def cached_base(self, edition: str) -> Path | None:
        """Return a ready base of ``edition`` built with the current assets.

        Lets builds continue after the administrator removed the ISO.
        """
        if not self._cache.is_dir():
            return None
        for marker in sorted(self._cache.glob(f"windows-11-{edition}-*.base.json")):
            try:
                record = json.loads(marker.read_text())
            except (OSError, ValueError):
                continue
            iso_sha256 = record.get("iso_sha256")
            if record.get("edition") != edition or not isinstance(iso_sha256, str):
                continue
            base = self.base_path(edition, iso_sha256)
            if base.with_suffix(".base.json") == marker and self.base_ready(base):
                return base
        return None

    def record_base(self, base: Path, details: dict[str, Any]) -> None:
        """Mark a base disk as completely installed."""
        marker = self.base_marker(base)
        temp = marker.with_name(marker.name + f".{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps({**details, "path": str(base), "size_bytes": base.stat().st_size})
        )
        os.replace(temp, marker)

    def _download(self, url: str, target: Path, *, size: int | None, sha256: str | None) -> None:
        temp = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        try:
            with urllib.request.urlopen(url, timeout=300) as response, temp.open("wb") as out:
                shutil.copyfileobj(response, out, length=4 * 1024 * 1024)
            self._verify(temp, size=size, sha256=sha256)
            temp.chmod(0o644)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _verify(path: Path, *, size: int | None, sha256: str | None) -> str:
        if size is not None and path.stat().st_size != size:
            raise RuntimeError(f"{path.name}: unexpected size")
        with path.open("rb") as stream:
            actual = sha256_digest(stream)
        if sha256 is not None and actual != sha256.lower():
            raise RuntimeError(f"{path.name}: checksum mismatch")
        return actual

    def ensure_pinned(self, pin: PinnedFile) -> Path:
        """Return a verified local copy of a pinned component (blocking)."""
        self._ensure_cache()
        target = self._cache / pin.name
        if target.exists():
            try:
                if target.stat().st_size == pin.size:
                    self._verify(target, size=pin.size, sha256=pin.sha256)
                    return target
            except RuntimeError:
                pass
            target.unlink()
        logger.info("windows_component_download", name=pin.name)
        try:
            self._download(pin.url, target, size=pin.size, sha256=pin.sha256)
        except Exception as exc:
            raise OperationRejectedError(
                WINDOWS_MEDIA_UNAVAILABLE, f"Could not download {pin.name}: {exc}"
            ) from exc
        return target

    def resolve_iso(self) -> tuple[Path, str]:
        """Return the configured Windows 11 ISO and its SHA-256 (blocking).

        ``RUNNER_WINDOWS_ISO`` is a local path or an http(s) URL; an optional
        ``RUNNER_WINDOWS_ISO_SHA256`` pins its content. Downloads are cached by
        content hash so a changed URL never reuses stale media.
        """
        source = (getattr(self._settings, "windows_iso", "") or "").strip()
        expected = (getattr(self._settings, "windows_iso_sha256", "") or "").strip().lower()
        if not source:
            raise OperationRejectedError(
                WINDOWS_MEDIA_UNAVAILABLE,
                "No Windows 11 installation ISO is configured (RUNNER_WINDOWS_ISO)",
            )
        self._ensure_cache()
        if re.match(r"https?://", source):
            name = "windows-11-" + hashlib.sha256(source.encode()).hexdigest()[:16] + ".iso"
            target = self._cache / name
            if not target.exists():
                logger.info("windows_iso_download")
                try:
                    self._download(source, target, size=None, sha256=expected or None)
                except Exception as exc:
                    raise OperationRejectedError(
                        WINDOWS_MEDIA_UNAVAILABLE, f"Could not download the Windows ISO: {exc}"
                    ) from exc
            path = target
        else:
            path = Path(source)
            if not path.is_file():
                raise OperationRejectedError(
                    WINDOWS_MEDIA_UNAVAILABLE, f"Windows ISO not found: {source}"
                )
        digest = self._iso_digest(path)
        if expected and digest != expected:
            raise OperationRejectedError(
                WINDOWS_MEDIA_UNAVAILABLE, "Windows ISO checksum does not match"
            )
        return path, digest

    def _iso_digest(self, path: Path) -> str:
        """Hash an ISO once per (inode, size, mtime); hashing 9 GB takes a while."""
        stat = path.stat()
        record = self._cache / (
            "iso-" + hashlib.sha256(str(path.absolute()).encode()).hexdigest()[:16] + ".sha256"
        )
        identity = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
        try:
            cached_identity, cached_digest = record.read_text().split()
            if cached_identity == identity:
                return cached_digest
        except (OSError, ValueError):
            pass
        digest = self._verify(path, size=None, sha256=None)
        record.write_text(f"{identity} {digest}\n")
        return digest

    def build_setup_iso(self, edition: str, target: Path) -> Path:
        """Write the setup media (answer file, scripts, installers) to ``target``."""
        stage = target.with_name(target.name + ".stage")
        shutil.rmtree(stage, ignore_errors=True)
        try:
            (stage / "opencuria").mkdir(parents=True)
            (stage / "installers").mkdir()
            (stage / "autounattend.xml").write_text(render_autounattend(edition))
            for name in SETUP_SCRIPTS:
                shutil.copyfile(ASSETS / name, stage / "opencuria" / name)
            (stage / "opencuria" / "media.json").write_text(
                json.dumps(
                    {
                        "openssh": OPENSSH.name,
                        "msys2": MSYS2.name,
                        "msys2_packages": list(MSYS2_PACKAGES),
                    }
                )
            )
            for pin in (OPENSSH, MSYS2):
                shutil.copyfile(self.ensure_pinned(pin), stage / "installers" / pin.name)
            tool = shutil.which("genisoimage") or shutil.which("mkisofs")
            if tool is None:
                raise RuntimeError("Neither genisoimage nor mkisofs found")
            subprocess.run(
                [tool, "-quiet", "-output", str(target), "-volid", "OCSETUP",
                 "-joliet", "-joliet-long", "-rock", str(stage)],
                check=True,
                capture_output=True,
                timeout=600,
            )
            target.chmod(0o644)
            return target
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def guest_scripts_archive() -> bytes:
    """Return a tar of the current guest scripts for ``C:\\ProgramData\\opencuria``."""
    import io
    import tarfile

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name in GUEST_SCRIPTS:
            data = (ASSETS / name).read_bytes()
            entry = tarfile.TarInfo(name=name)
            entry.mode = 0o644
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    return archive.getvalue()


def windows_configure_script() -> bytes:
    """Return the idempotent guest configuration every Windows build applies."""
    return (ASSETS / "configure.sh").read_bytes()
