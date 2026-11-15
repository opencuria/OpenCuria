"""Guest operating systems of workspace images.

The guest OS is a fact of an image, never user input: ``ubuntu:<version>``
recipes build Linux guests, ``windows:11[-<edition>]`` recipes build Windows 11
guests. Image versions record it once (``ImageInstance.guest_os``) and every
workspace inherits it from its image. Missing values mean Linux.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LINUX = "linux"
WINDOWS = "windows"
GUEST_OS_CHOICES = ((LINUX, "Linux"), (WINDOWS, "Windows"))

_UBUNTU = re.compile(r"ubuntu:(?P<version>\d{2}\.\d{2})")
#: Supported Windows 11 recipe values and the edition the runner installs.
WINDOWS_EDITIONS: dict[str, str] = {
    "windows:11": "pro",
    "windows:11-pro": "pro",
    "windows:11-enterprise": "enterprise",
    "windows:11-education": "education",
    "windows:11-pro-education": "pro-education",
    "windows:11-pro-workstations": "pro-workstations",
}


@dataclass(frozen=True)
class GuestMinimums:
    """Smallest workspace resources a guest OS supports."""

    vcpus: int
    memory_mb: int
    disk_size_gb: int


#: Windows 11 system requirements (2 cores, 4 GB RAM, 64 GB storage).
GUEST_MINIMUMS: dict[str, GuestMinimums] = {
    LINUX: GuestMinimums(vcpus=1, memory_mb=1, disk_size_gb=1),
    WINDOWS: GuestMinimums(vcpus=2, memory_mb=4096, disk_size_gb=64),
}


def normalize_base_distro(value: str | None) -> str:
    """Return the canonical spelling of a recipe base distro."""
    return (value or "").strip().lower()


def guest_os_for_distro(base_distro: str | None) -> str:
    """Return the guest OS a recipe builds; raise ``ValueError`` if unsupported."""
    distro = normalize_base_distro(base_distro)
    if _UBUNTU.fullmatch(distro):
        return LINUX
    if distro in WINDOWS_EDITIONS:
        return WINDOWS
    raise ValueError(f"Unsupported base distro: {base_distro!r}")


def is_supported_base_distro(base_distro: str | None) -> bool:
    """Whether a recipe base distro can be built."""
    try:
        guest_os_for_distro(base_distro)
    except ValueError:
        return False
    return True


def normalize_guest_os(value: str | None) -> str:
    """Map stored or reported guest OS values to a known one (default Linux)."""
    return WINDOWS if value == WINDOWS else LINUX


def guest_minimums(guest_os: str | None) -> GuestMinimums:
    """Return the resource floor of a guest OS."""
    return GUEST_MINIMUMS[normalize_guest_os(guest_os)]
