"""Guest operating system descriptor of a harness workspace.

Linux and Windows guests differ for the harness only through this descriptor.
Shell commands, runner file RPCs and guest-side helpers see POSIX paths on
both guests (MSYS2 on Windows). Native Windows programs such as Claude Code
and Codex see the workspace as ``C:\\workspace`` and report Windows paths in
their stdio payloads, which MSYS2 never converts. Paths cross that boundary
only through :meth:`GuestPlatform.to_native_path` and
:meth:`GuestPlatform.to_workspace_path`.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any

from common.guest_os import LINUX, WINDOWS, normalize_guest_os

#: Workspace root as seen by shells, runner RPCs and every harness tool.
HARNESS_WORKSPACE_ROOT = "/workspace"

_DRIVE_PATH = re.compile(r"(?P<drive>[A-Za-z]):(?P<rest>.*)", re.DOTALL)
_MSYS_DRIVE_PATH = re.compile(r"/(?P<drive>[A-Za-z])(?P<rest>/.*)?", re.DOTALL)
_SEPARATORS = re.compile(r"/+")


@dataclass(frozen=True)
class GuestPlatform:
    """What the harness must know about a workspace's guest OS.

    Attributes:
        name: Guest OS identifier (``common.guest_os``).
        display_name: OS name shown to models.
        prompt_context: Environment sentences of the product context.
        native_workspace_root: Workspace root as native guest programs see it.
        native_python: POSIX Python that native guest programs can execute.
        native_shell: POSIX ``sh`` that native guest programs can execute.
        executable_suffix: File name suffix of runtime artifact executables.
        case_sensitive_paths: Whether the guest file system is case sensitive.
        x11_desktop: Whether desktop consumers address the display through X11.
        process_stop: How stopping a background process ends it.
    """

    name: str
    display_name: str
    prompt_context: str
    native_workspace_root: str
    native_python: str
    native_shell: str
    executable_suffix: str
    case_sensitive_paths: bool
    x11_desktop: bool
    process_stop: str

    @property
    def _native_drive(self) -> tuple[str, str] | None:
        """Drive letter and root directory of a Windows workspace root."""
        match = _DRIVE_PATH.fullmatch(self.native_workspace_root)
        if match is None:
            return None
        return match["drive"].upper(), match["rest"].strip("\\/").replace("\\", "/")

    def to_workspace_path(self, raw: str) -> str | None:
        """Return the POSIX spelling of a path a guest program may report.

        On Windows guests ``C:\\workspace\\a``, ``c:/Workspace/a`` and
        ``/c/workspace/a`` become ``/workspace/a``; other drive, MSYS drive and
        UNC paths (and spellings escaping the workspace) return ``None``.
        Backslashes are separators there, so remaining input comes back with
        forward slashes. Linux guests return every input unchanged. Callers
        apply their POSIX rules (relative paths, ``normpath``) afterwards.
        """
        native = self._native_drive
        if native is None:
            return raw
        text = raw.replace("\\", "/")
        if text.startswith("//"):
            return None  # UNC and \\?\ device paths never name the workspace.
        drive_path = _DRIVE_PATH.fullmatch(text)
        if drive_path is not None:
            drive, rest = drive_path["drive"], drive_path["rest"]
            if not rest.startswith("/"):
                return None  # Drive-relative (``C:name``) depends on hidden state.
        else:
            msys_path = _MSYS_DRIVE_PATH.fullmatch(text)
            if msys_path is None:
                return text
            drive, rest = msys_path["drive"], msys_path["rest"] or "/"
        if drive.upper() != native[0]:
            return None
        normalized = posixpath.normpath(_SEPARATORS.sub("/", rest))
        root = f"/{native[1]}"
        # NTFS folds ASCII case only here; Unicode folding (e.g. the Kelvin
        # sign) would alias directories outside the workspace.
        prefix, remainder = normalized[: len(root)], normalized[len(root) :]
        if not prefix.isascii() or prefix.lower() != root.lower():
            return None
        if remainder and not remainder.startswith("/"):
            return None
        return HARNESS_WORKSPACE_ROOT + remainder

    def to_native_path(self, path: str) -> str:
        """Return how native guest programs spell a POSIX workspace path.

        Raises:
            ValueError: On Windows guests for paths outside the workspace.
        """
        native = self._native_drive
        if native is None:
            return path
        normalized = posixpath.normpath(path)
        if normalized == HARNESS_WORKSPACE_ROOT:
            return self.native_workspace_root
        if not normalized.startswith(f"{HARNESS_WORKSPACE_ROOT}/"):
            raise ValueError(f"Path is outside {HARNESS_WORKSPACE_ROOT}: {path}")
        relative = normalized.removeprefix(f"{HARNESS_WORKSPACE_ROOT}/")
        return self.native_workspace_root + "\\" + relative.replace("/", "\\")

    def artifact_executable(self, directory: str, name: str) -> str:
        """Return the POSIX path of a runtime artifact executable."""
        return f"{directory}/{name}{self.executable_suffix}"

    def desktop_environment(self, binding: dict[str, Any]) -> dict[str, str]:
        """Return the environment that addresses a runner desktop binding."""
        if not self.x11_desktop:
            return {}
        return {"DISPLAY": binding["display"], "XAUTHORITY": binding["xauthority"]}


LINUX_GUEST = GuestPlatform(
    name=LINUX,
    display_name="Linux",
    prompt_context=(
        "You are working inside an isolated OpenCuria workspace "
        f"(Linux container or VM) rooted at {HARNESS_WORKSPACE_ROOT}, "
        "not on the user's machine."
    ),
    native_workspace_root=HARNESS_WORKSPACE_ROOT,
    native_python="/usr/bin/python3",
    native_shell="/bin/sh",
    executable_suffix="",
    case_sensitive_paths=True,
    x11_desktop=True,
    process_stop="SIGTERM, then SIGKILL after a short grace period",
)

WINDOWS_GUEST = GuestPlatform(
    name=WINDOWS,
    display_name="Windows 11",
    prompt_context=(
        "You are working inside an isolated OpenCuria workspace (Windows 11 VM) "
        f"rooted at {HARNESS_WORKSPACE_ROOT} (C:\\workspace for native Windows "
        "programs), not on the user's machine. Shell commands run in bash "
        "(MSYS2) with GNU tools; PowerShell is available as powershell.exe. "
        "Write files as UTF-8. GUI programs started from the shell are not "
        "visible on the desktop."
    ),
    native_workspace_root="C:\\workspace",
    native_python="C:\\msys64\\usr\\bin\\python3.exe",
    native_shell="C:\\msys64\\usr\\bin\\sh.exe",
    executable_suffix=".exe",
    case_sensitive_paths=False,
    x11_desktop=False,
    process_stop="terminates the process tree",
)


def guest_for_os(guest_os: str | None) -> GuestPlatform:
    """Return the descriptor of a stored guest OS value (default Linux)."""
    return WINDOWS_GUEST if normalize_guest_os(guest_os) == WINDOWS else LINUX_GUEST


def guest_of(accessor: object) -> GuestPlatform:
    """Return an accessor's guest, treating accessors without one as Linux."""
    guest = getattr(accessor, "guest", None)
    return guest if isinstance(guest, GuestPlatform) else LINUX_GUEST


__all__ = [
    "HARNESS_WORKSPACE_ROOT",
    "LINUX_GUEST",
    "WINDOWS_GUEST",
    "GuestPlatform",
    "guest_for_os",
    "guest_of",
]
