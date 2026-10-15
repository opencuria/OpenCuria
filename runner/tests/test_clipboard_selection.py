import contextlib
import ctypes
import io
import unittest
from unittest.mock import patch

from src.services.sessions import clipboard_selection


class _FakeXFixes:
    def __init__(self, owner=17, copied_owner=None):
        self.current_owner = owner
        self.copied_owner = copied_owner
        self.calls = []

    def owner(self):
        return self.current_owner

    def wait_for_owner(self, previous_owner, deadline):
        self.calls.append(("temporary-owner", previous_owner))
        self.current_owner = 23
        return 23

    def _drain_events(self):
        self.calls.append(("drain", None))

    def wait_for_copy(self, deadline):
        self.calls.append(("copy-event", None))
        if self.copied_owner is None:
            raise clipboard_selection.ClipboardSelectionError
        self.current_owner = self.copied_owner
        return self.copied_owner

    def close(self):
        self.calls.append(("close", None))


class ClipboardSelectionHelperTests(unittest.TestCase):
    def test_wait_loop_accepts_real_xfixes_owner_notification(self):
        owner = 73
        event = clipboard_selection._XEvent()
        event.xfixes.type = 42
        event.xfixes.subtype = 0
        event.xfixes.owner = owner
        event.xfixes.selection = 11

        class _FakeX11:
            def XPending(self, display):
                return 1

            def XNextEvent(self, display, destination):
                ctypes.memmove(destination, ctypes.byref(event), ctypes.sizeof(event))

        xfixes = object.__new__(clipboard_selection._XFixes)
        xfixes.display = object()
        xfixes._x11 = _FakeX11()
        xfixes._event_base = 42
        xfixes._selection = 11

        self.assertEqual(
            xfixes._wait_for_selection_event(clipboard_selection.time.monotonic() + 1),
            owner,
        )

    def test_wait_loop_ignores_other_xfixes_subtypes(self):
        event = clipboard_selection._XEvent()
        event.xfixes.type = 42
        event.xfixes.subtype = 1
        event.xfixes.owner = 73
        event.xfixes.selection = 11

        class _FakeX11:
            def XPending(self, display):
                return 1

            def XNextEvent(self, display, destination):
                ctypes.memmove(destination, ctypes.byref(event), ctypes.sizeof(event))

        xfixes = object.__new__(clipboard_selection._XFixes)
        xfixes.display = object()
        xfixes._x11 = _FakeX11()
        xfixes._event_base = 42
        xfixes._selection = 11
        with self.assertRaises(clipboard_selection.ClipboardSelectionError):
            xfixes._wait_for_selection_event(clipboard_selection.time.monotonic())

    def test_guest_command_is_local_encoded_bounded_python_without_installs(self):
        command = clipboard_selection.build_guest_command(shift=True)
        self.assertIn("timeout --signal=TERM 2.5s python3 -c", command)
        self.assertTrue(command.endswith(" shift"))
        self.assertNotIn("apt-get", command)
        self.assertIn("base64.b64decode", command)

    def test_main_writes_unicode_without_extra_formatting(self):
        text = "same same — 日本語 🧪"
        output = io.StringIO()
        with (
            patch.object(clipboard_selection, "copy_selection", return_value=text),
            contextlib.redirect_stdout(output),
        ):
            result = clipboard_selection.main(["ctrl"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), text)

    def test_main_failure_is_generic_and_does_not_leak_old_clipboard(self):
        secret = "credential-that-must-not-appear"
        error = io.StringIO()
        with (
            patch.object(
                clipboard_selection,
                "copy_selection",
                side_effect=clipboard_selection.ClipboardSelectionError(secret),
            ),
            contextlib.redirect_stderr(error),
        ):
            result = clipboard_selection.main(["shift"])
        self.assertEqual(result, 1)
        self.assertNotIn(secret, error.getvalue())
        self.assertEqual(error.getvalue(), "clipboard selection failed\n")

    def test_rich_image_clipboard_is_not_replaced_before_fresh_copy(self):
        xfixes = _FakeXFixes(owner=17, copied_owner=17)
        with (
            patch.object(clipboard_selection, "_XFixes", return_value=xfixes),
            patch.object(
                clipboard_selection,
                "_clipboard_targets",
                return_value={"TARGETS", "image/png"},
            ),
            patch.object(clipboard_selection, "_start_temporary_owner") as owner,
            patch.object(
                clipboard_selection, "_read_clipboard", return_value="not bytes"
            ) as read,
            patch.object(clipboard_selection, "_run_copy_key") as key,
        ):
            read.return_value = b"fresh image selection"
            result = clipboard_selection.copy_selection(shift=False)
        self.assertEqual(result, "fresh image selection")
        owner.assert_not_called()
        read.assert_called_once()
        key.assert_called_once()
        self.assertEqual(xfixes.current_owner, 17)

    def test_rich_clipboard_noop_fails_without_temp_owner_or_plain_read(self):
        xfixes = _FakeXFixes(owner=17)
        with (
            patch.object(clipboard_selection, "_XFixes", return_value=xfixes),
            patch.object(
                clipboard_selection,
                "_clipboard_targets",
                return_value={"TARGETS", "image/png", "text/html"},
            ),
            patch.object(clipboard_selection, "_start_temporary_owner") as owner,
            patch.object(clipboard_selection, "_read_clipboard") as read,
            patch.object(clipboard_selection, "_run_copy_key") as key,
            self.assertRaises(clipboard_selection.ClipboardSelectionError),
        ):
            clipboard_selection.copy_selection(shift=False)
        owner.assert_not_called()
        read.assert_not_called()
        key.assert_called_once()
        self.assertEqual(xfixes.current_owner, 17)

    def test_noop_plain_text_uses_temp_owner_and_never_returns_snapshot(self):
        xfixes = _FakeXFixes(owner=17)
        with (
            patch.object(clipboard_selection, "_XFixes", return_value=xfixes),
            patch.object(
                clipboard_selection,
                "_clipboard_targets",
                return_value={"TARGETS", "UTF8_STRING", "STRING"},
            ),
            patch.object(
                clipboard_selection,
                "_read_previous_plain_text",
                return_value=b"old secret",
            ),
            patch.object(
                clipboard_selection, "_start_temporary_owner", return_value=object()
            ) as owner,
            patch.object(clipboard_selection, "_read_clipboard") as read,
            patch.object(clipboard_selection, "_run_copy_key") as key,
            self.assertRaises(clipboard_selection.ClipboardSelectionError),
        ):
            clipboard_selection.copy_selection(shift=False)
        owner.assert_called_once()
        read.assert_not_called()
        key.assert_called_once()
        self.assertEqual(xfixes.current_owner, 23)

    def test_repeated_same_text_is_returned_from_fresh_copy_operations(self):
        """Identical contents do not suppress the required ownership events."""
        xfixes = _FakeXFixes(owner=17, copied_owner=41)
        targets = {"TARGETS", "UTF8_STRING", "STRING"}
        with (
            patch.object(clipboard_selection, "_XFixes", return_value=xfixes),
            patch.object(
                clipboard_selection, "_clipboard_targets", return_value=targets
            ),
            patch.object(
                clipboard_selection, "_read_previous_plain_text", return_value=b"repeat"
            ),
            patch.object(
                clipboard_selection, "_start_temporary_owner", return_value=object()
            ) as owner,
            patch.object(
                clipboard_selection,
                "_read_clipboard",
                return_value=b"repeat",
            ) as read,
            patch.object(clipboard_selection, "_run_copy_key") as key,
        ):
            self.assertEqual(clipboard_selection.copy_selection(shift=False), "repeat")
            xfixes.current_owner = 41
            self.assertEqual(clipboard_selection.copy_selection(shift=False), "repeat")
        self.assertEqual(key.call_count, 2)
        self.assertEqual(owner.call_count, 2)
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(call.args[0] is False for call in key.call_args_list))

    def test_string_target_can_preserve_legacy_text_as_utf8(self):
        with patch.object(
            clipboard_selection,
            "_run_xclip_read",
            return_value=type("Result", (), {"returncode": 0, "stdout": b"caf\xe9"})(),
        ) as read:
            result = clipboard_selection._read_previous_plain_text({"STRING"}, 10.0)
        self.assertEqual(result, "café".encode())
        read.assert_has_calls(
            [
                unittest.mock.call("UTF8_STRING", 10.0),
                unittest.mock.call("STRING", 10.0),
            ]
        )
