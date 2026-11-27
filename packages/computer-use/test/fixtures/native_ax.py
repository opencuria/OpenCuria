"""Dependency-free regression tests for the native AT-SPI driver."""

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
    dependencies = {name: MagicMock() for name in ("structlog", "PIL", "Xlib")}
    with patch.dict(sys.modules, dependencies):
        spec.loader.exec_module(module)
    return module


native = load_driver()
BUS = ":1.5"
ROOT = native.ROOT


def bits(*names: str) -> list[int]:
    value = 0
    for name in names:
        value |= 1 << native.STATES[name]
    return [value & 0xFFFFFFFF, value >> 32]


class FakeBus:
    """An in-memory AT-SPI application tree with the driver's call surface."""

    def __init__(
        self, objects: dict[str, dict], frames: list[str], pid: int = 42
    ) -> None:
        self.objects = objects
        self.frames = frames
        self.process = pid
        self.listening = True
        self.activity: dict[str, float] = {}
        self.changes: dict[str, dict] = {}
        self.actions: list[tuple[str, int]] = []
        self.calls = 0

    def pump(self) -> None:
        pass

    def take(self, bus: str) -> dict:
        return self.changes.pop(bus, None) or {
            "structure": set(),
            "props": set(),
            "documents": set(),
            "overflow": False,
        }

    def change(self, path: str, kind: str = "structure") -> None:
        entry = self.changes.setdefault(
            BUS,
            {"structure": set(), "props": set(), "documents": set(), "overflow": False},
        )
        entry[kind].add(path)

    def pid(self, bus: str) -> int:
        return self.process

    def get(self, bus, path, interface, name):
        return self.call(bus, path, native.PROPERTIES, "Get", (interface, name))[0]

    def call(
        self, bus, path, interface, method, args=None, signature=None, timeout=3000
    ):
        self.calls += 1
        if path == ROOT and bus == native.REGISTRY:
            return ([(BUS, ROOT)],)
        if path == ROOT:
            if method == "GetChildren":
                return ([(BUS, frame) for frame in self.frames],)
            if method == "Get" and args[1] == "ToolkitName":
                return ("GTK",)
        node = self.objects.get(path)
        if node is None:
            raise RuntimeError(
                "GDBus.Error:org.freedesktop.DBus.Error.UnknownObject: gone"
            )
        if method == "GetChildren":
            return ([(BUS, child) for child in node.get("children", [])],)
        if method == "GetRoleName":
            return (node.get("role", "panel"),)
        if method == "GetState":
            return (bits(*node.get("states", ["enabled", "visible", "showing"])),)
        if method == "GetAttributes":
            return ({},)
        if method == "GetInterfaces":
            derived = (
                ["Accessible"]
                + (["Action"] if "actions" in node else [])
                + (["Text"] if "text" in node else [])
            )
            return (
                [f"org.a11y.atspi.{name}" for name in node.get("interfaces", derived)],
            )
        if method == "GetRelationSet":
            return (
                [
                    (
                        native.LABELLED_BY,
                        [(BUS, label) for label in node.get("labels", [])],
                    )
                ],
            )
        if method == "GetExtents":
            return (node.get("extents", (0, 0, 0, 0)),)
        if method == "GetName":
            actions = node.get("actions", [])
            if args[0] >= len(actions):
                raise RuntimeError("no such action")
            return (actions[args[0]],)
        if method == "DoAction":
            self.actions.append((path, args[0]))
            return (True,)
        if method == "GetText":
            return (node.get("text", "")[args[0] : args[1]],)
        if method == "SetTextContents":
            node["text"] = args[0]
            return (True,)
        if method == "Get":
            name = args[1]
            if name == "Name":
                return (node.get("name", ""),)
            if name == "Description":
                return (node.get("description", ""),)
            if name == "NActions":
                if "actions" not in node:
                    raise RuntimeError("no action interface")
                return (len(node["actions"]),)
            if name == "CharacterCount":
                if "text" not in node:
                    raise RuntimeError("no text interface")
                return (len(node["text"]),)
            if name in node.get("numbers", {}):
                return (node["numbers"][name],)
        raise RuntimeError(f"unsupported {method} {args}")

    def many(self, requests, timeout=3000):
        results = []
        for bus, path, interface, method, args, signature in requests:
            try:
                results.append(self.call(bus, path, interface, method, args, signature))
            except Exception as exc:  # noqa: BLE001 - mirrors the real batch API
                results.append(exc)
        return results


def driver_for(objects: dict[str, dict], frames: list[str] | None = None) -> tuple:
    driver = native.LinuxDriver.__new__(native.LinuxDriver)
    driver.atspi = FakeBus(objects, frames or ["/f"])
    driver.windows = {}
    driver.next_element = 1
    driver.touched = set()
    driver.toolkits = {}
    driver.dirty = False
    driver.action_at = 0.0
    driver.execution_id = "test"
    metadata = {
        "id": 7,
        "pid": 42,
        "title": "Form",
        "x": 0,
        "y": 0,
        "width": 800,
        "height": 600,
    }
    driver._metadata = lambda _window: metadata
    driver._geometry = lambda _window: (0, 0, 800, 600)
    bound = native.BoundWindow(SimpleNamespace(id=7))
    bound.root = (BUS, "/f")
    bound.ax_observed = True
    driver._root_for = lambda _bound: bound.root
    return driver, bound


def form() -> dict[str, dict]:
    return {
        "/f": {
            "role": "frame",
            "name": "Form",
            "children": ["/doc"],
            "extents": (0, 0, 800, 600),
        },
        "/doc": {
            "role": "document web",
            "name": "Page",
            "children": ["/wrap", "/combo", "/table", "/hidden"],
        },
        "/wrap": {"role": "section", "children": ["/label", "/entry", "/save"]},
        "/label": {"role": "static", "name": "Keywords"},
        "/entry": {
            "role": "entry",
            "name": "Keywords",
            "text": "old￼",
            "states": ["enabled", "visible", "showing", "editable", "focusable"],
            "actions": ["activate", "showContextMenu"],
        },
        "/save": {
            "role": "push button",
            "name": "Save",
            "actions": ["press", "showContextMenu"],
            "children": ["/save-text"],
        },
        "/save-text": {"role": "static", "name": "Save"},
        "/combo": {
            "role": "combo box",
            "name": "Language",
            "actions": ["open"],
            "states": ["enabled", "visible", "showing", "expandable", "focusable"],
            "children": ["/menu"],
        },
        "/menu": {"role": "menu", "states": ["enabled"], "children": ["/en", "/de"]},
        "/en": {
            "role": "menu item",
            "name": "English",
            "states": ["enabled", "selected"],
            "actions": ["select"],
        },
        "/de": {
            "role": "menu item",
            "name": "Deutsch",
            "states": ["enabled"],
            "actions": ["select"],
        },
        "/table": {"role": "table", "children": ["/hrow", "/row"]},
        "/hrow": {"role": "table row", "children": ["/h1", "/h2"]},
        "/h1": {"role": "column header", "name": "Question"},
        "/h2": {"role": "column header", "name": "None"},
        "/row": {"role": "table row", "children": ["/c1", "/c2"]},
        "/c1": {"role": "table cell", "name": "Violence"},
        "/c2": {"role": "table cell", "children": ["/radio"]},
        "/radio": {
            "role": "radio button",
            "states": ["enabled", "visible", "showing", "focusable"],
            "actions": ["select"],
        },
        "/hidden": {
            "role": "panel",
            "states": ["enabled"],
            "children": ["/ghost", "/kept"],
        },
        "/ghost": {"role": "push button", "name": "Unmount", "states": ["enabled"]},
        "/kept": {
            "role": "push button",
            "name": "List View",
            "states": ["enabled", "visible"],
        },
    }


def lines(result: dict) -> list[str]:
    return [
        "\t" * node["depth"] + f"{node['id']} {node['line']}"
        for node in result["nodes"]
    ]


class TreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_tree_contexts_and_hidden_widgets(self):
        driver, bound = driver_for(form())
        result = await driver._tree(bound, {})
        text = "\n".join(lines(result))
        self.assertEqual(result["source"], "at_spi")
        self.assertIn(
            "entry (settable) Keywords, Value: old", text
        )  # placeholder removed
        self.assertNotIn("￼", text)
        self.assertNotIn(
            "Secondary Actions: activate", text
        )  # default verbs are implied
        self.assertIn("push button Save\n", text + "\n")  # redundant text child dropped
        self.assertNotIn("container", text)  # unnamed wrappers flattened
        self.assertIn("combo box (collapsed) Language, Value: English", text)
        self.assertIn(
            "menu item (selected) English", text
        )  # options of a closed list stay
        self.assertIn("radio button (unchecked) Context: Violence › None", text)
        self.assertNotIn("Unmount", text)  # invisible
        self.assertIn(
            "push button List View", text
        )  # visible control in hidden container

    async def test_element_ids_are_stable_and_queries_see_states(self):
        driver, bound = driver_for(form())
        first = {
            node["name"]: node["id"]
            for node in (await driver._tree(bound, {"fresh": True}))["nodes"]
        }
        second = {
            node["name"]: node["id"]
            for node in (await driver._tree(bound, {"fresh": True}))["nodes"]
        }
        self.assertEqual(first, second)
        entry = next(
            n for n in (await driver._tree(bound, {}))["nodes"] if n["role"] == "entry"
        )
        self.assertIn("editable", entry["states"])
        self.assertIn("focusable", entry["states"])

    async def test_incremental_updates_reread_only_changed_subtrees(self):
        objects = form()
        driver, bound = driver_for(objects)
        await driver._tree(bound, {})
        objects["/wrap"]["children"].append("/new")
        objects["/new"] = {"role": "static", "name": "3 remaining"}
        objects["/entry"]["text"] = "fresh"
        driver.atspi.change("/wrap")
        driver.atspi.change("/entry", "props")
        driver.atspi.calls = 0
        result = await driver._tree(bound, {})
        incremental = driver.atspi.calls
        text = "\n".join(lines(result))
        self.assertIn("text 3 remaining", text)
        self.assertIn("Value: fresh", text)
        driver.atspi.calls = 0
        await driver._tree(bound, {"fresh": True})
        self.assertLess(
            incremental, driver.atspi.calls / 2
        )  # not a full walk of the window

    async def test_unknown_document_load_forces_a_full_read(self):
        objects = form()
        driver, bound = driver_for(objects)
        await driver._tree(bound, {})
        objects["/doc"]["children"].append("/late")
        objects["/late"] = {"role": "heading", "name": "Late content"}
        driver.atspi.changes[BUS] = {
            "structure": {"/late-doc"},
            "props": set(),
            "documents": {"/late-doc"},
            "overflow": False,
        }
        self.assertIn("Late content", "\n".join(lines(await driver._tree(bound, {}))))

    async def test_untrusted_toolkits_are_read_completely(self):
        objects = form()
        driver, bound = driver_for(objects)
        driver.toolkits[BUS] = "qt"
        await driver._tree(bound, {})
        objects["/doc"]["children"].append("/silent")
        objects["/silent"] = {"role": "heading", "name": "No event"}
        self.assertIn("No event", "\n".join(lines(await driver._tree(bound, {}))))

    async def test_truncation_is_reported(self):
        objects = {
            "/f": {
                "role": "frame",
                "name": "Big",
                "children": [f"/n{i}" for i in range(50)],
            }
        }
        objects.update(
            {f"/n{i}": {"role": "static", "name": f"Row {i}"} for i in range(50)}
        )
        driver, bound = driver_for(objects)
        result = await driver._tree(bound, {"maxNodes": 20})
        self.assertTrue(result["truncated"])
        self.assertIn("Partial tree", result["notice"])

    async def test_labels_name_controls_and_layout_gives_context(self):
        objects = {
            "/f": {
                "role": "frame",
                "name": "GTK",
                "children": ["/grid"],
                "extents": (0, 0, 600, 400),
            },
            "/grid": {
                "role": "panel",
                "children": [
                    "/yes",
                    "/no",
                    "/ads",
                    "/hno",
                    "/hyes",
                    "/name",
                    "/name-label",
                ],
            },
            "/hno": {"role": "label", "name": "No", "extents": (100, 10, 40, 20)},
            "/hyes": {"role": "label", "name": "Yes", "extents": (200, 10, 40, 20)},
            "/ads": {
                "role": "label",
                "name": "Contains ads",
                "extents": (0, 50, 90, 20),
            },
            "/no": {
                "role": "radio button",
                "extents": (110, 50, 20, 20),
                "actions": ["click"],
                "states": ["enabled", "visible", "showing", "focusable", "checked"],
            },
            "/yes": {
                "role": "radio button",
                "extents": (210, 50, 20, 20),
                "actions": ["click"],
                "states": ["enabled", "visible", "showing", "focusable"],
            },
            "/name-label": {
                "role": "label",
                "name": "App name",
                "extents": (0, 100, 80, 20),
            },
            "/name": {
                "role": "text",
                "labels": ["/name-label"],
                "text": "1.0",
                "extents": (100, 100, 200, 20),
                "states": ["enabled", "visible", "showing", "editable", "focusable"],
            },
        }
        driver, bound = driver_for(objects)
        text = "\n".join(lines(await driver._tree(bound, {})))
        self.assertIn("radio button (unchecked) Context: Contains ads › Yes", text)
        self.assertIn("radio button (checked) Context: Contains ads › No", text)
        self.assertIn("text (settable) App name, Value: 1.0", text)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.driver, _ = driver_for({})
        self.metadata = {
            "id": 1,
            "pid": 42,
            "title": "Dialog",
            "x": 10,
            "y": 20,
            "width": 400,
            "height": 300,
        }

    def frames(self, **objects):
        self.driver.atspi.objects = objects
        self.driver.atspi.frames = list(objects)

    def test_title_and_geometry_match_the_window(self):
        self.frames(**{
            "/a": {"role": "frame", "name": "Other", "extents": (500, 500, 100, 100)},
            "/b": {"role": "dialog", "name": "Dialog", "extents": (10, 20, 400, 300)},
        })  # fmt: skip
        self.assertEqual(LinuxDriverRoot(self.driver, self.metadata), (BUS, "/b"))

    def test_another_process_is_never_bound(self):
        self.frames(
            **{
                "/b": {
                    "role": "dialog",
                    "name": "Dialog",
                    "extents": (10, 20, 400, 300),
                }
            }
        )
        self.driver.atspi.process = 99
        self.assertIsNone(LinuxDriverRoot(self.driver, self.metadata))

    def test_a_leaked_identical_frame_loses_to_the_newest(self):
        self.frames(**{
            "/org/a11y/atspi/accessible/10": {"role": "dialog", "name": "Dialog", "extents": (10, 20, 400, 300)},
            "/org/a11y/atspi/accessible/99": {"role": "dialog", "name": "Dialog", "extents": (10, 20, 400, 300)},
        })  # fmt: skip
        self.assertEqual(
            LinuxDriverRoot(self.driver, self.metadata),
            (BUS, "/org/a11y/atspi/accessible/99"),
        )

    def test_unrelated_frames_are_ambiguous(self):
        self.frames(**{
            "/x": {"role": "dialog", "name": "Dialog", "extents": (10, 20, 400, 300)},
            "/y": {"role": "dialog", "name": "Dialog", "extents": (10, 20, 400, 300)},
        })  # fmt: skip
        self.assertIsNone(LinuxDriverRoot(self.driver, self.metadata))


def LinuxDriverRoot(driver, metadata):  # noqa: N802 - reads like the method under test
    return native.LinuxDriver._ax_root(driver, metadata)


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.objects = form()
        self.driver, self.bound = driver_for(self.objects)
        self.nodes = {
            n["name"] or n["role"]: n
            for n in (await self.driver._tree(self.bound, {}))["nodes"]
        }

    async def test_click_prefers_the_semantic_action(self):
        await self.driver._click_element(
            self.bound, self.driver._ref(self.bound, self.nodes["Save"]["id"]), {}
        )
        self.assertEqual(self.driver.atspi.actions, [("/save", 0)])

    async def test_stale_elements_are_reported(self):
        identifier = self.nodes["Save"]["id"]
        del self.objects["/save"]
        with self.assertRaises(native.DriverError) as caught:
            self.driver._ref(self.bound, identifier)
        self.assertEqual(caught.exception.code, "STALE_ELEMENT")

    async def test_set_value_uses_editable_text_and_verifies(self):
        self.objects["/entry"]["interfaces"] = ["Accessible", "Text", "EditableText"]
        result = await self.driver._set_value(
            self.bound,
            self.driver._ref(
                self.bound,
                self.nodes["Keywords"]["id"]
                if self.nodes["Keywords"]["role"] == "entry"
                else 0,
            ),
            "1.0.0",
        )
        self.assertEqual(result, {"value": "1.0.0"})
        self.assertEqual(self.objects["/entry"]["text"], "1.0.0")

    async def test_set_value_without_editable_text_replaces_by_keyboard(self):
        entry = next(n for n in self.nodes.values() if n["role"] == "entry")
        ref = self.driver._ref(self.bound, entry["id"])
        self.objects["/entry"]["interfaces"] = ["Accessible", "Text"]
        self.driver._focus = AsyncMock()
        self.driver._press = AsyncMock()

        async def paste(_bound, text):
            self.objects["/entry"]["text"] = text

        self.driver._paste_text = paste
        result = await self.driver._set_value(self.bound, ref, "neu")
        self.assertEqual(result, {"value": "neu"})
        self.driver._press.assert_any_await(self.bound, "BackSpace")

    async def test_mismatching_text_is_an_error_with_the_actual_value(self):
        entry = next(n for n in self.nodes.values() if n["role"] == "entry")
        ref = self.driver._ref(self.bound, entry["id"])
        self.objects["/entry"]["interfaces"] = ["Accessible", "Text"]
        self.driver._focus = AsyncMock()
        self.driver._press = AsyncMock()
        self.driver._paste_text = AsyncMock()
        self.driver._wait_text = AsyncMock(return_value="trunc")
        with self.assertRaises(native.DriverError) as caught:
            await self.driver._set_value(self.bound, ref, "truncated text")
        self.assertIn("'trunc'", str(caught.exception))

    async def test_boolean_values_toggle_only_when_needed(self):
        radio = next(n for n in self.nodes.values() if n["role"] == "radio button")
        ref = self.driver._ref(self.bound, radio["id"])

        async def click(_bound, _ref, _args):
            self.objects["/radio"]["states"] = [
                "enabled",
                "visible",
                "showing",
                "checked",
            ]

        self.driver._click_element = click
        self.assertEqual(
            await self.driver._set_value(self.bound, ref, True), {"checked": True}
        )
        self.driver._click_element = AsyncMock()
        self.assertEqual(
            await self.driver._set_value(self.bound, ref, True), {"checked": True}
        )
        self.driver._click_element.assert_not_awaited()

    async def test_acted_on_elements_are_reread_without_events(self):
        entry = next(n for n in self.nodes.values() if n["role"] == "entry")
        self.driver._ref(self.bound, entry["id"])
        self.objects["/entry"]["text"] = "changed silently"
        text = "\n".join(lines(await self.driver._tree(self.bound, {})))
        self.assertIn("Value: changed silently", text)


class KeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_type_characters_handles_xlib_map_objects_and_unmapped_symbols(self):
        driver, bound = driver_for({})
        mapping = {ord("a"): [(38, 0)], ord("A"): [(38, 1)]}
        connection = Mock()
        connection.keysym_to_keycodes.side_effect = lambda keysym: map(
            lambda item: item, mapping.get(keysym, [])
        )
        connection.display.info = SimpleNamespace(min_keycode=8, max_keycode=12)
        connection.get_keyboard_mapping.return_value = [(1,), (1,), (0, 0), (1,), (0,)]
        driver.connection = connection
        sent = []
        driver._send_key = lambda _bound, code, pressed, state: sent.append(
            (code, pressed, state)
        )
        with patch.object(native.asyncio, "sleep", new_callable=AsyncMock):
            count = await driver._type_characters(bound, "aA€")
        self.assertEqual(count, 3)
        self.assertEqual(sent[0], (38, True, 0))
        self.assertEqual(sent[2], (38, True, native.X.ShiftMask))
        remapped = connection.change_keyboard_mapping.call_args_list
        self.assertEqual(remapped[0].args, (12, [(0x01000000 + ord("€"),) * 2]))
        self.assertEqual(remapped[-1].args, (12, [(0, 0)]))


class ProtocolTests(unittest.TestCase):
    def test_raw_dbus_errors_become_actionable(self):
        reply = native.error_reply(
            RuntimeError(
                "g-dbus-error-quark: GDBus.Error:org.freedesktop.DBus.Error.UnknownObject: No such object path"
            )
        )
        self.assertEqual(reply["code"], "STALE_ELEMENT")
        reply = native.error_reply(native.DriverError("NOT_FOUND", "missing"))
        self.assertEqual((reply["code"], reply["error"]), ("NOT_FOUND", "missing"))

    def test_paths_are_validated_and_staged_with_their_names(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder, "a.png"), Path(folder, "b c.txt")
            first.write_text("1")
            second.write_text("2")
            with self.assertRaises(native.DriverError):
                native.LinuxDriver._paths([str(first), f"{folder}/missing.png"])
            staged = native.LinuxDriver._stage(
                native.LinuxDriver._paths([str(first), str(second)])
            )
            self.assertEqual(
                sorted(p.name for p in Path(staged).iterdir()), ["a.png", "b c.txt"]
            )
            self.assertEqual(Path(staged, "b c.txt").read_text(), "2")


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
                native.asyncio,
                "create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=self.clipboard,
            ),
            patch.object(native.asyncio, "sleep", new_callable=AsyncMock),
        ):
            await self.driver._paste_text(self.bound, "Unicode ä😀")
        self.driver._press.assert_awaited_once_with(self.bound, "Control_L+v")
        self.clipboard.stdin.write.assert_called_once_with("Unicode ä😀".encode())

    async def test_unacquired_clipboard_does_not_send_a_paste_shortcut(self):
        self.driver.connection.get_selection_owner.return_value = SimpleNamespace(id=1)
        with (
            patch.object(
                native.asyncio,
                "create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=self.clipboard,
            ),
            patch.object(
                native, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 2]))
            ),
            self.assertRaises(native.DriverError) as error,
        ):
            await self.driver._paste_text(self.bound, "text")
        self.assertEqual(error.exception.code, "CLIPBOARD_UNAVAILABLE")
        self.driver._press.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
