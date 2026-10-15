"""Live clipboard-selection integration tests using an isolated local Xvnc.

These tests deliberately exercise the guest helper as a separate Python process,
with a real Tk Text widget, XFixes selection-owner events, xclip, and xdotool.
They never connect to the runner's desktop display or monkeypatch clipboard code.
"""

from __future__ import annotations

import base64
import ctypes.util
import importlib.util
import json
import os
import random
import select
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

_HELPER = Path(__file__).parents[1] / "src/services/sessions/clipboard_selection.py"
# A valid 1x1 PNG, used to make xclip advertise an actual image-only clipboard.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/f4sAAAAASUVORK5CYII="
)
_X11_OWNER = r"""
import ctypes
import ctypes.util

x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
x11.XInternAtom.restype = ctypes.c_ulong
x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XGetSelectionOwner.restype = ctypes.c_ulong
x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
display = x11.XOpenDisplay(None)
if not display:
    raise SystemExit("could not open isolated X display")
selection = x11.XInternAtom(display, b"CLIPBOARD", 0)
print(x11.XGetSelectionOwner(display, selection))
x11.XCloseDisplay(display)
"""

_TK_SERVER = r"""
import json
import select
import sys
import tkinter as tk

root = tk.Tk()
root.title("OpenCuria live clipboard integration")
text = tk.Text(root)
text.pack()
text.focus_force()

def read_commands():
    if select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        if line:
            command = json.loads(line)
            text.delete("1.0", "end")
            text.insert("1.0", command["text"])
            text.tag_remove("sel", "1.0", "end")
            if command["selected"]:
                text.tag_add("sel", "1.0", "end-1c")
            root.deiconify()
            root.lift()
            root.focus_force()
            text.focus_force()
            root.update_idletasks()
            print("ACK", flush=True)
    root.after(10, read_commands)

root.deiconify()
root.update()
root.after(10, read_commands)
print("READY", flush=True)
root.mainloop()
"""


class LiveClipboardSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = ("Xvnc", "xclip", "xdotool", "xdpyinfo")
        missing = [name for name in required if shutil.which(name) is None]
        if missing:
            raise unittest.SkipTest(
                "missing live X11 integration tools: " + ", ".join(missing)
            )
        if importlib.util.find_spec("tkinter") is None:
            raise unittest.SkipTest("Python tkinter is unavailable")
        if not ctypes.util.find_library("Xfixes"):
            raise unittest.SkipTest("libXfixes is unavailable")

        # Xvnc's -displayfd mode chooses display :0 when it is open, while it
        # daemonizes by default. Pick a random high display and pass both ports
        # explicitly, so its long-running process stays owned by this fixture.
        display_number = str(random.randrange(100, 20_000))
        cls._xserver = subprocess.Popen(
            [
                shutil.which("Xvnc") or "Xvnc",
                ":" + display_number,
                "-geometry",
                "800x600",
                "-depth",
                "24",
                "-SecurityTypes",
                "None",
                "-localhost",
                "-interface",
                "127.0.0.1",
                "-rfbport",
                str(20_000 + int(display_number)),
                "-noWebsocket",
                "-nolisten",
                "tcp",
                "-quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls._stop_process, cls._xserver)

        cls._env = os.environ.copy()
        cls._env["DISPLAY"] = ":" + display_number
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if cls._xserver.poll() is not None:
                raise RuntimeError("isolated Xvnc exited during startup")
            if (
                subprocess.run(
                    ["xdpyinfo"],
                    env=cls._env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=1,
                ).returncode
                == 0
            ):
                break
            threading.Event().wait(0.05)
        else:
            raise RuntimeError("isolated Xvnc did not accept X11 clients")

        cls._tk = subprocess.Popen(
            [sys.executable, "-u", "-c", _TK_SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cls._env,
            bufsize=1,
        )
        cls.addClassCleanup(cls._stop_process, cls._tk)
        assert cls._tk.stdout is not None
        ready, _, _ = select.select([cls._tk.stdout], [], [], 8)
        if not ready or cls._tk.stdout.readline().strip() != "READY":
            raise RuntimeError("Tk clipboard test window did not start")

    @staticmethod
    def _stop_process(process: subprocess.Popen[object] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def _set_tk_text(self, value: str, *, selected: bool) -> None:
        assert self._tk.stdin is not None and self._tk.stdout is not None
        self._tk.stdin.write(
            json.dumps({"text": value, "selected": selected}, ensure_ascii=False) + "\n"
        )
        self._tk.stdin.flush()
        ready, _, _ = select.select([self._tk.stdout], [], [], 3)
        self.assertTrue(ready, "Tk did not acknowledge the text/selection update")
        self.assertEqual(self._tk.stdout.readline().strip(), "ACK")

    def _run_helper(self, key: str = "ctrl") -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(_HELPER), key],
            env=self._env,
            capture_output=True,
            check=False,
            timeout=4,
        )

    def _clipboard_owner(self) -> int:
        result = subprocess.run(
            [sys.executable, "-c", _X11_OWNER],
            env=self._env,
            capture_output=True,
            check=True,
            timeout=2,
        )
        return int(result.stdout)

    def _set_clipboard_text(self, value: str) -> None:
        process = subprocess.Popen(
            ["xclip", "-quiet", "-selection", "clipboard", "-in", "-loops", "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._env,
        )
        self.addCleanup(self._stop_process, process)
        assert process.stdin is not None
        process.stdin.write(value.encode("utf-8"))
        process.stdin.close()
        # Wait until xclip has become the actual CLIPBOARD owner.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-out", "-target", "UTF8_STRING"],
                env=self._env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=1,
            )
            if result.returncode == 0 and result.stdout == value.encode("utf-8"):
                return
            threading.Event().wait(0.02)
        self._stop_process(process)
        self.fail("xclip did not publish the test clipboard text")

    def _read_clipboard(self, target: str = "UTF8_STRING") -> bytes:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-out", "-target", target],
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
        self.assertEqual(result.returncode, 0, "could not read the X11 clipboard")
        return result.stdout

    def test_repeated_and_changed_selection_returns_fresh_tk_copy(self) -> None:
        same_text = "identical selection — 日本語 🧪"
        self._set_tk_text(same_text, selected=True)

        first = self._run_helper()
        self.assertEqual(first.returncode, 0, first.stderr.decode("utf-8", "replace"))
        self.assertEqual(first.stdout, same_text.encode("utf-8"))

        # A second copy must be observed even though bytes have not changed.
        second = self._run_helper()
        self.assertEqual(second.returncode, 0, second.stderr.decode("utf-8", "replace"))
        self.assertEqual(second.stdout, same_text.encode("utf-8"))

        new_text = "fresh selection changed between copies — 🛰"
        self._set_tk_text(new_text, selected=True)
        third = self._run_helper()
        self.assertEqual(third.returncode, 0, third.stderr.decode("utf-8", "replace"))
        self.assertEqual(third.stdout, new_text.encode("utf-8"))

    def test_no_selection_fails_without_returning_or_replacing_existing_text(
        self,
    ) -> None:
        old_text = "preserve existing plain clipboard text"
        self._set_clipboard_text(old_text)
        self._set_tk_text("there is no selected range", selected=False)

        result = self._run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"clipboard selection failed\n")
        self.assertEqual(self._read_clipboard(), old_text.encode("utf-8"))

    def _set_image_clipboard(self) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [
                "xclip",
                "-quiet",
                "-selection",
                "clipboard",
                "-in",
                "-target",
                "image/png",
                "-loops",
                "0",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._env,
        )
        assert process.stdin is not None
        process.stdin.write(_PNG)
        process.stdin.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            targets = subprocess.run(
                ["xclip", "-selection", "clipboard", "-out", "-target", "TARGETS"],
                env=self._env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=1,
            )
            if targets.returncode == 0 and b"image/png" in targets.stdout:
                return process
            threading.Event().wait(0.02)
        self._stop_process(process)
        self.fail("xclip did not publish the image-only test clipboard")

    def test_image_only_clipboard_is_replaced_only_after_fresh_tk_selection(
        self,
    ) -> None:
        image_owner = self._set_image_clipboard()
        try:
            fresh_text = "selected after an image clipboard — 🌈"
            self._set_tk_text(fresh_text, selected=True)
            result = self._run_helper()
            self.assertEqual(
                result.returncode, 0, result.stderr.decode("utf-8", "replace")
            )
            self.assertEqual(result.stdout, fresh_text.encode("utf-8"))
            self.assertEqual(self._read_clipboard(), fresh_text.encode("utf-8"))
        finally:
            self._stop_process(image_owner)

    def test_no_selection_preserves_image_only_clipboard_and_owner(self) -> None:
        image_owner = self._set_image_clipboard()
        try:
            original_owner = self._clipboard_owner()
            self._set_tk_text("there is no selected range", selected=False)

            result = self._run_helper()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr, b"clipboard selection failed\n")
            self.assertEqual(self._clipboard_owner(), original_owner)
            self.assertEqual(self._read_clipboard("image/png"), _PNG)
        finally:
            self._stop_process(image_owner)

    def test_large_multiline_unicode_selection_is_copied_without_loss(self) -> None:
        line = "Selection — 日本語 Ελληνικά café\n"
        selected = line * 1_000
        self.assertGreater(len(selected.encode("utf-8")), 20_000)
        self._set_tk_text(selected, selected=True)

        result = self._run_helper()
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stdout, selected.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
