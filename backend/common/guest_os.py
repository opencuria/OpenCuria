"""Guest operating systems of workspaces: the shared vocabulary.

Pure values without Django, so every layer (the runners app, the harness
access contracts and their transports) shares one definition. Missing or
unknown values mean Linux.
"""

from __future__ import annotations

LINUX = "linux"
WINDOWS = "windows"
GUEST_OS_CHOICES = ((LINUX, "Linux"), (WINDOWS, "Windows"))


def normalize_guest_os(value: str | None) -> str:
    """Map stored or reported guest OS values to a known one (default Linux)."""
    return WINDOWS if value == WINDOWS else LINUX
