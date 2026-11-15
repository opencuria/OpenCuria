"""Guest-side atomic copy-selection clipboard helper.

This module is intentionally self-contained: the runner ships its source to the
workspace and executes it with the guest's system Python. It must not grow
runtime package dependencies.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

_SCRIPT_TIMEOUT_S = 2.5
_HELPER_DEADLINE_S = 1.95
_SUBPROCESS_TIMEOUT_S = 0.4
_COPY_WAIT_S = 0.65
_XFIXES_SET_SELECTION_OWNER_NOTIFY = 1
_XFIXES_SELECTION_NOTIFY_EVENT = 0
_PRESERVABLE_TEXT_TARGETS = {
    "TARGETS",
    "TIMESTAMP",
    "MULTIPLE",
    "SAVE_TARGETS",
    # Tk advertises application/window identifiers as metadata, not formats.
    "TK_APPLICATION",
    "TK_WINDOW",
    "UTF8_STRING",
    "STRING",
    "TEXT",
    "COMPOUND_TEXT",
}


class ClipboardSelectionError(RuntimeError):
    """A generic failure that never includes clipboard content or process output."""


class _XFixesSelectionNotifyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("subtype", ctypes.c_int),
        ("owner", ctypes.c_ulong),
        ("selection", ctypes.c_ulong),
        ("timestamp", ctypes.c_ulong),
        ("selection_timestamp", ctypes.c_ulong),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int),
        ("xfixes", _XFixesSelectionNotifyEvent),
        ("pad", ctypes.c_long * 24),
    ]


class _XFixes:
    """Small ctypes wrapper for X11 selection-owner notifications."""

    def __init__(self) -> None:
        x11_name = ctypes.util.find_library("X11") or "libX11.so.6"
        fixes_name = ctypes.util.find_library("Xfixes") or "libXfixes.so.3"
        self._x11 = ctypes.CDLL(x11_name)
        self._fixes = ctypes.CDLL(fixes_name)
        self._configure_signatures()
        # Use only the trusted desktop environment passed by the runner.
        self.display = self._x11.XOpenDisplay(None)
        if not self.display:
            raise ClipboardSelectionError
        event_base = ctypes.c_int()
        error_base = ctypes.c_int()
        if not self._fixes.XFixesQueryExtension(
            self.display, ctypes.byref(event_base), ctypes.byref(error_base)
        ):
            self.close()
            raise ClipboardSelectionError
        self._event_base = event_base.value
        self._root = self._x11.XDefaultRootWindow(self.display)
        self._selection = self._x11.XInternAtom(self.display, b"CLIPBOARD", 0)
        self._fixes.XFixesSelectSelectionInput(
            self.display,
            self._root,
            self._selection,
            _XFIXES_SET_SELECTION_OWNER_NOTIFY,
        )
        self._x11.XSync(self.display, 0)
        self._drain_events()

    def _configure_signatures(self) -> None:
        self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._x11.XInternAtom.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        self._x11.XInternAtom.restype = ctypes.c_ulong
        self._x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x11.XGetSelectionOwner.restype = ctypes.c_ulong
        self._x11.XPending.argtypes = [ctypes.c_void_p]
        self._x11.XPending.restype = ctypes.c_int
        self._x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
        self._x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._fixes.XFixesQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        self._fixes.XFixesQueryExtension.restype = ctypes.c_int
        self._fixes.XFixesSelectSelectionInput.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        self._fixes.XFixesSelectSelectionInput.restype = None

    def owner(self) -> int:
        return int(self._x11.XGetSelectionOwner(self.display, self._selection))

    def _drain_events(self) -> None:
        event = _XEvent()
        while self._x11.XPending(self.display):
            self._x11.XNextEvent(self.display, ctypes.byref(event))

    def _wait_for_selection_event(
        self, deadline: float, *, previous_owner: int | None = None
    ) -> int:
        """Return an owner from a new XFixes notification, never a snapshot."""
        event = _XEvent()
        while time.monotonic() < deadline:
            while self._x11.XPending(self.display):
                self._x11.XNextEvent(self.display, ctypes.byref(event))
                notification = event.xfixes
                if (
                    event.type == self._event_base + _XFIXES_SELECTION_NOTIFY_EVENT
                    and notification.subtype == 0
                    and notification.selection == self._selection
                    and notification.owner
                    and (
                        previous_owner is None
                        or notification.owner != previous_owner
                    )
                ):
                    return int(notification.owner)
            threading.Event().wait(
                min(0.01, max(0.0, deadline - time.monotonic()))
            )
        raise ClipboardSelectionError

    def wait_for_owner(self, previous_owner: int, deadline: float) -> int:
        """Wait until the temporary text owner claims the selection."""
        return self._wait_for_selection_event(deadline, previous_owner=previous_owner)

    def wait_for_copy(self, deadline: float) -> int:
        """Wait for a post-shortcut owner event, including same-owner events."""
        return self._wait_for_selection_event(deadline)

    def close(self) -> None:
        display = getattr(self, "display", None)
        if display:
            self._x11.XCloseDisplay(display)
            self.display = None


def _remaining(deadline: float, cap: float = _SUBPROCESS_TIMEOUT_S) -> float:
    remaining = min(cap, deadline - time.monotonic())
    if remaining <= 0:
        raise ClipboardSelectionError
    return remaining


def _run_xclip_read(target: str, deadline: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["xclip", "-selection", "clipboard", "-out", "-target", target],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_remaining(deadline),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClipboardSelectionError from exc


def _clipboard_targets(deadline: float) -> set[str] | None:
    """Query advertised targets; unknown target lists are treated as rich."""
    try:
        result = _run_xclip_read("TARGETS", deadline)
        if result.returncode != 0:
            return None
        return {
            item.decode("ascii")
            for item in result.stdout.split()
        }
    except (ClipboardSelectionError, UnicodeDecodeError):
        return None


def _is_plain_text_only(targets: set[str] | None) -> bool:
    if not targets:
        return False
    return all(
        target in _PRESERVABLE_TEXT_TARGETS
        or target.lower() == "text/plain"
        or target.lower().startswith("text/plain;")
        for target in targets
    )


def _read_clipboard(deadline: float, *, owner_exists: bool) -> bytes:
    """Read fresh UTF-8 clipboard bytes after a selection-owner event."""
    result = _run_xclip_read("UTF8_STRING", deadline)
    if result.returncode != 0:
        if not owner_exists:
            return b""
        raise ClipboardSelectionError
    try:
        result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClipboardSelectionError from exc
    return result.stdout


def _read_previous_plain_text(
    targets: set[str], deadline: float
) -> bytes | None:
    """Snapshot a supported text target in UTF-8 for the temporary owner."""
    # UTF8_STRING conversion is the safest path even when the owner advertises
    # only TEXT/COMPOUND_TEXT or a text/plain target.
    try:
        result = _run_xclip_read("UTF8_STRING", deadline)
        if result.returncode == 0:
            result.stdout.decode("utf-8")
            return result.stdout
    except (ClipboardSelectionError, UnicodeDecodeError):
        pass

    candidates = [
        target
        for target in targets
        if target.lower() == "text/plain"
        or target.lower().startswith("text/plain;")
        or target in {"TEXT", "COMPOUND_TEXT"}
    ]
    for target in candidates:
        try:
            result = _run_xclip_read(target, deadline)
            if result.returncode == 0:
                text = result.stdout.decode("utf-8")
                return text.encode("utf-8")
        except (ClipboardSelectionError, UnicodeDecodeError):
            continue

    if "STRING" in targets:
        try:
            result = _run_xclip_read("STRING", deadline)
            if result.returncode == 0:
                return result.stdout.decode("latin-1").encode("utf-8")
        except ClipboardSelectionError:
            pass
    return None


def _stop_temporary_owner_if_unowned(
    process: subprocess.Popen[bytes], xfixes: _XFixes, previous_owner: int
) -> None:
    """Clean setup failures only while xclip has not become the clipboard owner."""
    try:
        if xfixes.owner() != previous_owner:
            return
        process.terminate()
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _start_temporary_owner(
    text: bytes,
    deadline: float,
    xfixes: _XFixes,
    previous_owner: int,
) -> subprocess.Popen[bytes]:
    """Start xclip holding previous plain text; preserve it on a no-op copy."""
    try:
        process = subprocess.Popen(
            ["xclip", "-quiet", "-selection", "clipboard", "-in", "-loops", "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise ClipboardSelectionError from exc
    if process.stdin is None:
        _stop_temporary_owner_if_unowned(process, xfixes, previous_owner)
        raise ClipboardSelectionError

    write_errors: list[OSError] = []

    def feed_owner() -> None:
        try:
            process.stdin.write(text)
            process.stdin.close()
        except OSError as exc:
            write_errors.append(exc)

    try:
        writer = threading.Thread(
            target=feed_owner, name="clipboard-owner-feed", daemon=True
        )
        writer.start()
        writer.join(timeout=_remaining(deadline, 0.5))
        if writer.is_alive() or write_errors or process.poll() is not None:
            raise ClipboardSelectionError
    except (ClipboardSelectionError, OSError, RuntimeError):
        _stop_temporary_owner_if_unowned(process, xfixes, previous_owner)
        raise ClipboardSelectionError from None
    return process


def _run_copy_key(shift: bool, deadline: float) -> None:
    combo = "ctrl+shift+c" if shift else "ctrl+c"
    try:
        result = subprocess.run(
            ["xdotool", "key", "--clearmodifiers", combo],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_remaining(deadline),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClipboardSelectionError from exc
    if result.returncode != 0:
        raise ClipboardSelectionError


def copy_selection(shift: bool) -> str:
    """Perform one copy shortcut and return the fresh UTF-8 clipboard text."""
    deadline = time.monotonic() + _HELPER_DEADLINE_S
    xfixes = _XFixes()
    try:
        previous_owner = xfixes.owner()
        if previous_owner:
            targets = _clipboard_targets(deadline)
            # Clipboard contents may change independently of runner keyboard
            # calls; never restore a stale snapshot over a newer owner.
            if (
                xfixes.owner() == previous_owner
                and _is_plain_text_only(targets)
            ):
                previous_text = _read_previous_plain_text(targets, deadline)
                if (
                    previous_text is not None
                    and xfixes.owner() == previous_owner
                ):
                    process = _start_temporary_owner(
                        previous_text, deadline, xfixes, previous_owner
                    )
                    try:
                        temporary_owner = xfixes.wait_for_owner(
                            previous_owner, deadline
                        )
                    except ClipboardSelectionError:
                        _stop_temporary_owner_if_unowned(
                            process, xfixes, previous_owner
                        )
                        raise
                    if xfixes.owner() != temporary_owner:
                        raise ClipboardSelectionError

        # Discard setup notifications; only events after the single chord count.
        xfixes._drain_events()
        _run_copy_key(shift, deadline)
        new_owner = xfixes.wait_for_copy(
            min(deadline, time.monotonic() + _COPY_WAIT_S)
        )
        fresh_text = _read_clipboard(deadline, owner_exists=True)
        if xfixes.owner() != new_owner:
            raise ClipboardSelectionError
        try:
            return fresh_text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ClipboardSelectionError from exc
    finally:
        xfixes.close()


def guest_source() -> str:
    """Read this standalone helper's local source for guest-side execution."""
    return Path(__file__).read_text(encoding="utf-8")


def build_guest_command(*, shift: bool) -> str:
    """Build a bounded, dependency-free shell command carrying this helper."""
    source = base64.b64encode(guest_source().encode("utf-8")).decode("ascii")
    bootstrap = (
        "import base64; exec(compile(base64.b64decode('"
        + source
        + "'), '<clipboard-selection>', 'exec'))"
    )
    argument = "shift" if shift else "ctrl"
    return (
        f"timeout --signal=TERM {_SCRIPT_TIMEOUT_S}s python3 -c "
        f"{shlex.quote(bootstrap)} {argument}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; stdout is exclusively the copied clipboard text."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in {"ctrl", "shift"}:
        sys.stderr.write("clipboard selection failed\n")
        return 1
    try:
        result = copy_selection(shift=args[0] == "shift")
    except Exception:  # noqa: BLE001 - never expose clipboard data or guest details.
        sys.stderr.write("clipboard selection failed\n")
        return 1
    sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
