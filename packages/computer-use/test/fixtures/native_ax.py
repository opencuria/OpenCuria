"""Dependency-free regression tests for native AT-SPI discovery and traversal."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch


def load_driver():
    """Load the driver without requiring a display or optional native packages."""
    path = Path(__file__).parents[2] / "native/linux.py"
    spec = importlib.util.spec_from_file_location("native_ax_driver", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    dependencies = {
        name: MagicMock()
        for name in ("structlog", "PIL", "Xlib")
    }
    with patch.dict(sys.modules, dependencies):
        spec.loader.exec_module(module)
    return module


native = load_driver()


def accessible(name="Window", children=(), size=(10, 20, 800, 600)):
    """Return an AT-SPI-shaped window or widget with independent interfaces."""
    node = Mock()
    node.get_name.return_value = name
    node.get_role_name.return_value = "frame"
    node.get_description.return_value = ""
    node.get_text_iface.return_value = None
    node.get_action_iface.return_value = None
    node.get_child_count.return_value = len(children)

    def child(index):
        result = children[index]
        if isinstance(result, Exception):
            raise result
        return result

    node.get_child_at_index.side_effect = child
    node.get_state_set.return_value.contains.side_effect = lambda state: state == "ENABLED"
    component = node.get_component_iface.return_value
    component.get_extents.return_value = SimpleNamespace(
        **dict(zip(("x", "y", "width", "height"), size))
    )
    return node


def application(*children, pid=42):
    app = accessible(children=children)
    app.get_process_id.return_value = pid
    return app


class NativeAccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.driver = native.LinuxDriver.__new__(native.LinuxDriver)
        flags = [
            "DEFUNCT", "ENABLED", "FOCUSED", "SELECTED", "EXPANDED", "INDETERMINATE",
            "CHECKED", "CHECKABLE", "EDITABLE", "FOCUSABLE",
        ]
        self.desktop = accessible()
        self.driver.atspi = SimpleNamespace(
            get_desktop=lambda _: self.desktop,
            CoordType=SimpleNamespace(SCREEN=0),
            StateType=SimpleNamespace(**{flag: flag for flag in flags}),
        )
        self.driver._refresh_accessibility = Mock()
        self.metadata = {
            "id": 123, "pid": 42, "title": "Window", "x": 10, "y": 20,
            "width": 800, "height": 600,
        }

    def apps(self, *apps):
        self.desktop.get_child_count.return_value = len(apps)
        self.desktop.get_child_at_index.side_effect = lambda index: apps[index]

    def test_stale_app_and_window_do_not_hide_a_healthy_app(self):
        stale = application()
        stale.get_child_count.side_effect = RuntimeError("closed bridge")
        target = accessible()
        self.apps(stale, application(RuntimeError("closed window"), target))
        self.assertIs(self.driver._ax_root(self.metadata), target)

    def test_unknown_pid_can_match_by_title_and_full_geometry(self):
        target = accessible()
        app = application(target)
        app.get_process_id.side_effect = RuntimeError("PID unavailable")
        self.apps(app)
        self.assertIs(self.driver._ax_root(self.metadata), target)

    def test_known_other_process_is_never_bound(self):
        other_app = application(accessible(), pid=99)
        self.apps(other_app)
        self.assertIsNone(self.driver._ax_root(self.metadata))
        other_app.get_child_count.assert_not_called()

    def test_full_geometry_disambiguates_same_title_and_origin(self):
        wrong_size = accessible(size=(10, 20, 400, 300))
        target = accessible()
        self.apps(application(wrong_size, target))
        self.assertIs(self.driver._ax_root(self.metadata), target)

    def test_ambiguous_windows_do_not_expose_actions_on_arbitrary_target(self):
        self.apps(application(accessible(), accessible()))
        self.assertIsNone(self.driver._ax_root(self.metadata))

    def tree(self, root):
        self.driver._metadata = lambda _: self.metadata
        self.driver._ax_root = lambda _: root
        self.driver.element_ids = {}
        self.driver.next_element = 0
        bound = native.BoundWindow(SimpleNamespace(id=123))
        return self.driver._tree(bound), bound

    def test_failed_child_and_optional_interfaces_preserve_siblings(self):
        button = accessible("Save")
        button.get_role_name.return_value = "push button"
        root = accessible(children=(RuntimeError("stale child"), button))
        root.get_description.side_effect = RuntimeError("unsupported description")
        root.get_text_iface.side_effect = RuntimeError("stale text interface")
        root.get_action_iface.side_effect = RuntimeError("stale action interface")
        (source, state), bound = self.tree(root)
        self.assertEqual(source, "at_spi")
        self.assertIn("push button Save", state)
        self.assertEqual(len(bound.elements), 2)
        root.get_attributes.assert_called_once_with()
        root.clear_cache.assert_called_once_with()

    def test_failed_root_reports_x11_fallback_instead_of_empty_atspi_tree(self):
        root = accessible()
        root.get_state_set.side_effect = RuntimeError("defunct root")
        (source, state), bound = self.tree(root)
        self.assertEqual(source, "x11")
        self.assertIn("has not exposed an AT-SPI accessibility tree", state)
        self.assertEqual(bound.elements, {})

    def test_failed_root_cache_refresh_retains_x11_observation(self):
        root = accessible()
        root.clear_cache.side_effect = RuntimeError("closed bridge")
        (source, _), bound = self.tree(root)
        self.assertEqual(source, "x11")
        self.assertEqual(bound.elements, {})

    def test_extended_properties_activate_a_lazy_chromium_tree(self):
        button = accessible("Save")
        button.get_role_name.return_value = "push button"
        root = accessible()

        def activate():
            root.get_child_count.return_value = 1
            root.get_child_at_index.side_effect = lambda _: button

        root.get_attributes.side_effect = activate
        (source, state), _ = self.tree(root)
        self.assertEqual(source, "at_spi")
        self.assertIn("push button Save", state)

    def test_bridges_without_extended_properties_retain_their_controls(self):
        button = accessible("Save")
        root = accessible(children=[button])
        root.get_attributes.side_effect = RuntimeError("attributes unsupported")
        (source, state), _ = self.tree(root)
        self.assertEqual(source, "at_spi")
        self.assertIn("Save", state)

    def test_bridge_is_enabled_for_existing_sessions_with_bounded_calls(self):
        gio = MagicMock()
        variant = lambda signature, value: (signature, value)
        repository = SimpleNamespace(Gio=gio, GLib=SimpleNamespace(Variant=variant))
        with patch.dict(sys.modules, {"gi.repository": repository}):
            self.driver._enable_accessibility()
        calls = gio.bus_get_sync.return_value.call_sync.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [call.args[4][1][1] for call in calls],
            ["IsEnabled", "ScreenReaderEnabled"],
        )
        for call in calls:
            self.assertEqual(call.args[4][1][2], ("b", True))
            self.assertEqual(call.args[7], 1000)

    def test_missing_accessibility_service_retains_x11_runtime(self):
        gio = MagicMock()
        gio.bus_get_sync.side_effect = RuntimeError("session bus unavailable")
        repository = SimpleNamespace(Gio=gio, GLib=MagicMock())
        with patch.dict(sys.modules, {"gi.repository": repository}):
            self.driver._enable_accessibility()

    def test_glib_pending_signals_are_drained_and_cache_invalidated(self):
        context = Mock()
        context.pending.side_effect = [True, True, False]
        glib = Mock()
        glib.MainContext.default.return_value = context
        with patch.dict(sys.modules, {"gi.repository": SimpleNamespace(GLib=glib)}):
            native.LinuxDriver._refresh_accessibility(self.desktop)
        self.assertEqual(context.iteration.call_args_list, [unittest.mock.call(False)] * 2)
        self.desktop.clear_cache.assert_called_once_with()

    def test_events_are_refreshed_for_on_demand_observations(self):
        self.apps(application(accessible()))
        self.driver._ax_root(self.metadata)
        self.driver._refresh_accessibility.assert_called_once_with(self.desktop)

    def test_qt_bridge_uses_utf16_units_without_appending_garbage(self):
        node = Mock()
        node.get_application.return_value.get_toolkit_name.return_value = "Qt"
        self.assertEqual(self.driver._text_lengths(node, "Qt äöü ✅😀"), (10, 10))

    def test_standard_bridge_inserts_utf8_bytes_with_character_offsets(self):
        node = Mock()
        node.get_application.return_value.get_toolkit_name.return_value = "gtk"
        text = "Gtk äöü ✅😀"
        self.assertEqual(
            self.driver._text_lengths(node, text), (len(text.encode()), len(text))
        )

    def test_unknown_toolkit_uses_standard_length_contract(self):
        node = Mock()
        node.get_application.side_effect = RuntimeError("no toolkit metadata")
        self.assertEqual(self.driver._text_lengths(node, "ä😀"), (6, 2))

    def test_disappearing_inventory_window_does_not_hide_healthy_windows(self):
        stale = Mock()
        stale.get_attributes.side_effect = RuntimeError("BadWindow")
        healthy = Mock()
        healthy.id = 123
        healthy.get_attributes.return_value.map_state = native.X.IsViewable
        self.driver.root = Mock()
        self.driver.root.query_tree.return_value.children = [stale, healthy]
        self.driver._property = lambda *_: []
        self.driver._metadata = lambda _: {**self.metadata, "app": "fixture"}
        with patch.object(native.error, "XError", RuntimeError):
            self.assertEqual(self.driver._list_windows()[0]["id"], 123)


class FirstAccessibilityObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_frame_observation_retries_until_controls_are_present(self):
        driver = native.LinuxDriver.__new__(native.LinuxDriver)
        driver.execution_id = "test"
        driver.settle_at = 0
        bound = native.BoundWindow(Mock())
        driver._bound = lambda _: bound

        def observe(_):
            if not bound.elements:
                bound.elements[0] = Mock()
                return "at_spi", "frame only"
            bound.elements[1] = Mock()
            return "at_spi", "frame with controls"

        driver._tree = Mock(side_effect=observe)
        with patch.object(native.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await driver.dispatch("state", {"text": True})
        self.assertEqual(result["state"], "frame with controls")
        self.assertTrue(bound.ax_observed)
        self.assertEqual(driver._tree.call_count, 2)
        self.assertEqual(sleep.await_count, 2)  # normal settle, then one AX retry

    async def test_missing_accessibility_has_a_bounded_first_observation(self):
        driver = native.LinuxDriver.__new__(native.LinuxDriver)
        driver.execution_id = "test"
        driver.settle_at = 0
        bound = native.BoundWindow(Mock())
        driver._bound = lambda _: bound
        driver._tree = Mock(return_value=("x11", "fallback"))
        with patch.object(native.asyncio, "sleep", new_callable=AsyncMock):
            result = await driver.dispatch("state", {"text": True})
        self.assertEqual(result["source"], "x11")
        self.assertEqual(driver._tree.call_count, 6)


class AccessibilityTextInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.driver = native.LinuxDriver.__new__(native.LinuxDriver)
        self.driver.execution_id = "test"
        self.driver.settle_at = 0
        self.bound = native.BoundWindow(Mock())
        self.driver._bound = lambda _: self.bound
        self.node = accessible("Verification text")
        self.node.get_editable_text_iface.return_value = None
        self.node.get_text_iface.return_value = None
        self.node.get_state_set.return_value.contains.side_effect = (
            lambda state: state in {"EDITABLE", "FOCUSED"}
        )
        self.driver.atspi = SimpleNamespace(
            StateType=SimpleNamespace(EDITABLE="EDITABLE", FOCUSED="FOCUSED")
        )
        self.driver._element = Mock(return_value=self.node)
        self.driver._paste_text = AsyncMock()
        self.args = {"elementId": 7, "text": "Chromium äöü ✅😀"}

    async def test_chromium_editable_field_is_focused_before_bound_paste(self):
        await self.driver.dispatch("type_text", self.args)
        self.node.get_component_iface.return_value.grab_focus.assert_called_once_with()
        self.driver._paste_text.assert_awaited_once_with(self.bound, self.args["text"])

    async def test_read_only_element_cannot_receive_fallback_input(self):
        self.node.get_state_set.return_value.contains.side_effect = lambda _: False
        with self.assertRaises(native.DriverError) as error:
            await self.driver.dispatch("type_text", self.args)
        self.assertEqual(error.exception.code, "UNSUPPORTED_ACTION")
        self.driver._paste_text.assert_not_awaited()

    async def test_failed_semantic_focus_does_not_paste_into_another_field(self):
        self.node.get_component_iface.return_value.grab_focus.return_value = False
        with self.assertRaises(native.DriverError) as error:
            await self.driver.dispatch("type_text", self.args)
        self.assertEqual(error.exception.code, "ACTION_FAILED")
        self.driver._paste_text.assert_not_awaited()

    async def test_stale_target_cannot_receive_fallback_input(self):
        self.driver._element.side_effect = native.DriverError("STALE_ELEMENT", "stale")
        with self.assertRaises(native.DriverError) as error:
            await self.driver.dispatch("type_text", self.args)
        self.assertEqual(error.exception.code, "STALE_ELEMENT")
        self.driver._paste_text.assert_not_awaited()

    async def test_asynchronous_focus_is_acknowledged_before_paste(self):
        self.node.get_state_set.return_value.contains.side_effect = [True, False, True]
        with patch.object(native.asyncio, "sleep", new_callable=AsyncMock):
            await self.driver.dispatch("type_text", self.args)
        self.assertEqual(self.node.clear_cache_single.call_count, 2)
        self.driver._paste_text.assert_awaited_once()

    async def test_unacknowledged_focus_never_pastes_into_a_different_control(self):
        self.node.get_state_set.return_value.contains.side_effect = lambda _: False
        with (
            patch.object(
                native, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 2]))
            ),
            self.assertRaises(native.DriverError) as error,
        ):
            await self.driver._focus_text_target(self.node)
        self.assertEqual(error.exception.code, "ACTION_FAILED")
        self.driver._paste_text.assert_not_awaited()


class ClipboardReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.driver = native.LinuxDriver.__new__(native.LinuxDriver)
        self.driver.connection = Mock()
        self.driver._press = AsyncMock()
        self.bound = native.BoundWindow(Mock())
        self.clipboard = Mock()
        self.clipboard.returncode = None
        self.clipboard.stdin.drain = AsyncMock()
        self.clipboard.wait = AsyncMock(return_value=0)

    async def test_paste_waits_until_new_clipboard_owner_is_ready(self):
        owners = [SimpleNamespace(id=1), SimpleNamespace(id=1), SimpleNamespace(id=2)]
        self.driver.connection.get_selection_owner.side_effect = owners
        with (
            patch.object(
                native.asyncio, "create_subprocess_exec",
                new_callable=AsyncMock, return_value=self.clipboard,
            ),
            patch.object(native.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            await self.driver._paste_text(self.bound, "Unicode ä😀")
        self.assertEqual(self.driver.connection.get_selection_owner.call_count, 3)
        self.driver._press.assert_awaited_once_with(self.bound, "Control_L+v")
        self.assertEqual(sleep.call_args_list[0], unittest.mock.call(0.01))
        self.clipboard.stdin.write.assert_called_once_with("Unicode ä😀".encode())
        self.clipboard.terminate.assert_called_once_with()

    async def test_unacquired_clipboard_does_not_send_a_paste_shortcut(self):
        self.driver.connection.get_selection_owner.return_value = SimpleNamespace(id=1)
        with (
            patch.object(
                native.asyncio, "create_subprocess_exec",
                new_callable=AsyncMock, return_value=self.clipboard,
            ),
            patch.object(
                native, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 2]))
            ),
            self.assertRaises(native.DriverError) as error,
        ):
            await self.driver._paste_text(self.bound, "text")
        self.assertEqual(error.exception.code, "CLIPBOARD_UNAVAILABLE")
        self.driver._press.assert_not_awaited()
        self.clipboard.terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
