"""Bounded Linux desktop I/O for the workspace Computer Use runtime.

This helper has no model, prompts, credentials or agent loop. Each request is
an operation on an explicitly bound X11 window. AT-SPI is optional at runtime;
its absence is reported as a limited X11 tree rather than invented elements.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import io
import json
import math
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from PIL import Image
from Xlib import XK, X, display, error, protocol

MAX_LINE = 128 * 1024
MAX_TEXT = 64 * 1024
MAX_NODES = 1500
MAX_DEPTH = 32

structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
log = structlog.get_logger(__name__)


class DriverError(Exception):
    """A bounded, actionable desktop error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class BoundWindow:
    """A window identity and its most recent AX/image observations."""

    window: Any
    identity: str = field(default_factory=lambda: uuid.uuid4().hex)
    snapshot_id: str = ""
    elements: dict[int, tuple[Any, str, str]] = field(default_factory=dict)
    screenshot_id: str = ""
    geometry: tuple[int, int, int, int] = (0, 0, 0, 0)
    image_size: tuple[int, int] = (0, 0)


class LinuxDriver:
    """Execute desktop operations while retaining window and snapshot identity."""

    def __init__(self) -> None:
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            self._discover_session_bus()
        self.connection = display.Display()
        self.root = self.connection.screen().root
        self.windows: dict[int, BoundWindow] = {}
        self.held_keys: set[int] = set()
        self.held_buttons: set[int] = set()
        self.lock_fd: int | None = None
        self.execution_id = ""
        self.settle_at = 0.0
        self.next_element = 0
        self.element_ids: dict[tuple[int, int], int] = {}
        self.generation = int(os.environ.get("OPENCURIA_CUA_GENERATION", "0"))
        self.max_dimension = min(
            7680, max(320, int(os.environ.get("OPENCURIA_CUA_MAX_DIMENSION", "2400")))
        )
        self.atspi: Any = None
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi

            Atspi.set_timeout(500, 1000)
            self.atspi = Atspi
        except (ImportError, ValueError, AttributeError):
            pass

    @staticmethod
    def _discover_session_bus() -> None:
        """Join this display's existing XFCE bus in older workspace images."""
        from pathlib import Path

        current_display = os.environ.get("DISPLAY", "")
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                if b"xfce4-session" not in (process / "cmdline").read_bytes():
                    continue
                environment = dict(
                    item.split(b"=", 1)
                    for item in (process / "environ").read_bytes().split(b"\0")
                    if b"=" in item
                )
                if environment.get(b"DISPLAY", b"").decode() != current_display:
                    continue
                address = environment.get(b"DBUS_SESSION_BUS_ADDRESS", b"").decode()
                if address:
                    os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
                    return
            except (OSError, ValueError, UnicodeDecodeError):
                continue

    def _property(self, window: Any, name: str) -> Any:
        value = window.get_full_property(
            self.connection.intern_atom(name), X.AnyPropertyType
        )
        return value.value if value else None

    def _geometry(self, window: Any) -> tuple[int, int, int, int]:
        geometry = window.get_geometry()
        origin = self.root.translate_coords(window, 0, 0)
        return origin.x, origin.y, geometry.width, geometry.height

    def _metadata(self, window: Any) -> dict[str, Any]:
        x, y, width, height = self._geometry(window)
        title = self._property(window, "_NET_WM_NAME")
        if isinstance(title, bytes):
            title = title.decode("utf-8", errors="replace")
        if not isinstance(title, str):
            title = window.get_wm_name() or ""
        pid = self._property(window, "_NET_WM_PID")
        return {
            "id": window.id,
            "title": str(title)[:1024],
            "app": (window.get_wm_class() or (f"x11:{window.id}",))[-1],
            "pid": int(pid[0]) if pid is not None and len(pid) else None,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "focused": self._focused(window),
        }

    def _focused(self, window: Any) -> bool:
        active = self._property(self.root, "_NET_ACTIVE_WINDOW")
        if active is not None and len(active) and int(active[0]) != 0:
            return int(active[0]) == window.id
        focus = self.connection.get_input_focus().focus
        for _ in range(30):
            if not hasattr(focus, "id") or focus.id == self.root.id:
                return False
            if focus.id == window.id:
                return True
            try:
                focus = focus.query_tree().parent
            except error.XError:
                return False
        return False

    def _list_windows(self) -> list[dict[str, Any]]:
        listed = self._property(self.root, "_NET_CLIENT_LIST")
        candidates = (
            [self.connection.create_resource_object("window", int(i)) for i in listed]
            if listed is not None
            else []
        )
        for window in self.root.query_tree().children:
            attributes = window.get_attributes()
            if window.get_wm_class() or attributes.override_redirect:
                candidates.append(window)
        result = []
        seen = set()
        for window in candidates:
            try:
                if window.id in seen:
                    continue
                seen.add(window.id)
                if window.get_attributes().map_state != X.IsViewable:
                    continue
                metadata = self._metadata(window)
                if metadata["width"] > 0 and metadata["height"] > 0:
                    result.append(metadata)
            except error.XError:
                continue
        return sorted(result, key=lambda item: (item["app"], item["id"]))[:200]

    def _public_window(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Project driver identity fields out of the agent-visible inventory."""
        window = self.connection.create_resource_object("window", metadata["id"])
        states = self._property(window, "_NET_WM_STATE")
        modal = self.connection.intern_atom("_NET_WM_STATE_MODAL")
        types = self._property(window, "_NET_WM_WINDOW_TYPE")
        window_type = (
            self.connection.get_atom_name(int(types[0]))
            .removeprefix("_NET_WM_WINDOW_TYPE_")
            .lower()
            if types is not None and len(types)
            else "normal"
        )
        result = {key: value for key, value in metadata.items() if key != "pid"}
        if not result["title"]:
            result.pop("title")
        result.update(
            modal=bool(states is not None and modal in states), window_type=window_type
        )
        return result

    def _apps(self, windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Include installed launchable apps, with their matching open windows."""
        from gi.repository import Gio

        apps = []
        for desktop in Gio.AppInfo.get_all():
            if (
                desktop.get_nodisplay()
                or desktop.get_is_hidden()
                or desktop.get_boolean("Terminal")
            ):
                continue
            name = desktop.get_name()
            app_id = desktop.get_id()
            classes = {
                name.casefold(),
                app_id.removesuffix(".desktop").casefold(),
                (desktop.get_startup_wm_class() or "").casefold(),
            }
            running = [w for w in windows if w["app"].casefold() in classes]
            apps.append(
                {
                    "id": app_id,
                    "displayName": name,
                    "isRunning": bool(running),
                    "windows": running,
                }
            )
        return sorted(apps, key=lambda app: (app["displayName"].casefold(), app["id"]))

    def _bound(self, args: dict[str, Any]) -> BoundWindow:
        while self.connection.pending_events():
            event = self.connection.next_event()
            if event.type == X.DestroyNotify:
                self.windows.pop(event.window.id, None)
        identifier = args.get("windowId")
        bound = self.windows.get(identifier)
        if bound is None or args.get("identity") != bound.identity:
            raise DriverError(
                "WINDOW_CLOSED", "Bind this window again from a fresh inventory"
            )
        try:
            if bound.window.get_attributes().map_state != X.IsViewable:
                raise DriverError("WINDOW_CLOSED", "The selected window is not visible")
        except error.XError as exc:
            self.windows.pop(identifier, None)
            raise DriverError(
                "WINDOW_CLOSED", "The selected window was closed"
            ) from exc
        return bound

    def _require_focus(self, bound: BoundWindow) -> None:
        if not self._focused(bound.window):
            raise DriverError(
                "WINDOW_NOT_FOCUSED",
                "Call app.activate() before coordinate or keyboard input",
            )

    def _element(self, bound: BoundWindow, args: dict[str, Any]) -> Any:
        identifier = args.get("elementId")
        if (
            args.get("snapshotId") != bound.snapshot_id
            or identifier not in bound.elements
        ):
            raise DriverError(
                "STALE_ELEMENT", "Read a fresh AX state before using this element"
            )
        node, role, name = bound.elements[identifier]
        if node.get_role_name() != role or str(node.get_name() or "") != name:
            raise DriverError(
                "STALE_ELEMENT", "The element changed; read a fresh AX state"
            )
        states = node.get_state_set()
        if states.contains(self.atspi.StateType.DEFUNCT):
            raise DriverError(
                "STALE_ELEMENT", "The accessibility element no longer exists"
            )
        return node

    def _ax_root(self, metadata: dict[str, Any]) -> Any:
        if self.atspi is None:
            return None
        desktop = self.atspi.get_desktop(0)
        if desktop is None:
            return None
        best = None
        best_score = 0
        for index in range(min(desktop.get_child_count(), 200)):
            app = desktop.get_child_at_index(index)
            if metadata["pid"] and app.get_process_id() != metadata["pid"]:
                continue
            for child_index in range(min(app.get_child_count(), 100)):
                node = app.get_child_at_index(child_index)
                if not node:
                    continue
                name = node.get_name() or ""
                score = 5 if name and name == metadata["title"] else 0
                component = node.get_component_iface()
                if component:
                    extents = component.get_extents(self.atspi.CoordType.SCREEN)
                    if (
                        abs(extents.x - metadata["x"]) < 8
                        and abs(extents.y - metadata["y"]) < 40
                    ):
                        score += 3
                if score > best_score:
                    best, best_score = node, score
        return best

    def _tree(self, bound: BoundWindow) -> tuple[str, str]:
        metadata = self._metadata(bound.window)
        bound.elements.clear()
        bound.snapshot_id = uuid.uuid4().hex
        try:
            root = self._ax_root(metadata)
        except Exception:  # noqa: BLE001 - unavailable AX must retain X11 observations
            root = None

        def identifier(key: int) -> int:
            native_key = (bound.window.id, key)
            if native_key not in self.element_ids:
                self.element_ids[native_key] = self.next_element
                self.next_element += 1
            return self.element_ids[native_key]

        if root is None:
            index = identifier(0)
            name = metadata.get("title", "")
            return "x11", f"Accessibility source: x11\n{index} window {name}".rstrip()

        count = 0

        def collect(node: Any, depth: int) -> dict[str, Any] | None:
            nonlocal count
            if depth > MAX_DEPTH or count >= MAX_NODES:
                return None
            try:
                states = node.get_state_set()
                if states.contains(self.atspi.StateType.DEFUNCT):
                    return None
                count += 1
                index = identifier(hash(node))
                role = node.get_role_name() or "unknown"
                name = str(node.get_name() or "")
                bound.elements[index] = (node, role, name)
                flags = [
                    flag.lower()
                    for flag in (
                        "FOCUSED",
                        "SELECTED",
                        "EXPANDED",
                        "INDETERMINATE",
                        "CHECKED",
                        "CHECKABLE",
                        "EDITABLE",
                        "FOCUSABLE",
                    )
                    if states.contains(getattr(self.atspi.StateType, flag))
                ]
                info = []
                for title, value in [
                    (None, name),
                    ("Description", node.get_description()),
                ]:
                    if (
                        value
                        and str(value).strip()
                        and value not in [v for _, v in info]
                    ):
                        info.append((title, str(value)))
                if role != "password text" and node.get_text_iface():
                    value = self.atspi.Text.get_text(
                        node, 0, min(16000, self.atspi.Text.get_character_count(node))
                    )
                    if value and value not in [v for _, v in info]:
                        info.append(("Value", value))
                action = node.get_action_iface()
                actions = (
                    [
                        action.get_action_name(i)
                        for i in range(min(action.get_n_actions(), 10))
                    ]
                    if action
                    else []
                )
                children = []
                for i in range(min(node.get_child_count(), MAX_NODES)):
                    child = node.get_child_at_index(i)
                    if child:
                        item = collect(child, depth + 1)
                        if item:
                            children.append(item)
                return {
                    "id": index,
                    "role": role,
                    "info": info,
                    "flags": flags,
                    "enabled": states.contains(self.atspi.StateType.ENABLED),
                    "actions": actions,
                    "children": children,
                }
            except Exception:  # noqa: BLE001 - one stale node must not hide the window
                return None

        containers = {"group", "panel", "filler", "section", "generic"}
        texts = {"text", "static", "label"}
        interactive_roles = {
            "push button",
            "button",
            "toggle button",
            "check box",
            "radio button",
            "entry",
            "password text",
            "link",
            "menu item",
            "combo box",
            "slider",
            "spin button",
            "scroll bar",
            "scroll pane",
            "viewport",
        }

        def interactive(node: dict[str, Any]) -> bool:
            return bool(
                node["flags"]
                or node["role"] in interactive_roles
                or any(
                    a.lower()
                    not in {
                        "dodefault",
                        "clickancestor",
                        "showcontextmenu",
                        "scrolltovisible",
                    }
                    for a in node["actions"]
                )
            )

        def compact(node: dict[str, Any]) -> dict[str, Any] | None:
            children = [compact(child) for child in node["children"]]
            node["children"] = [child for child in children if child is not None]
            named = bool(node["info"] or node["role"] in {"image", "icon"})
            active = interactive(node)
            if not (named or node["children"] or active):
                return None
            if not active and not named and node["role"] in containers:
                if len(node["children"]) == 1:
                    return node["children"][0]
                node["children"] = [
                    sub
                    for child in node["children"]
                    for sub in (
                        child["children"]
                        if child["role"] == node["role"]
                        and not interactive(child)
                        and not child["info"]
                        else [child]
                    )
                ]
            return node

        tree = collect(root, 0)
        tree = compact(tree) if tree else None
        lines = ["Accessibility source: at_spi"]

        def render(node: dict[str, Any], depth: int) -> None:
            role = node["role"]
            role = (
                "container" if role in containers else "text" if role in texts else role
            )
            if role == "menu item":
                role = ""
            flags = [
                f
                for f in ("focused", "selected", "expanded", "indeterminate")
                if f in node["flags"]
            ]
            if "checked" in node["flags"]:
                flags.append("checked")
            elif "checkable" in node["flags"] and "indeterminate" not in flags:
                flags.append("unchecked")
            if "editable" in node["flags"]:
                flags.append("settable")
            if not node["enabled"] and role not in {"container", "text"}:
                flags.append("disabled")
            info = list(node["info"])
            secondary = [
                action
                for i, action in enumerate(node["actions"])
                if not (
                    i == 0
                    and action.lower()
                    in {"press", "click", "dodefault", "clickancestor"}
                )
                and action.lower() != "showcontextmenu"
            ]
            if secondary:
                info.append(("Secondary Actions", ", ".join(secondary)))
            info.sort(key=lambda item: len(item[1]) > 100 or "\n" in item[1])
            text = ", ".join(
                value
                if title is None or len(info) == 1 and title != "Secondary Actions"
                else f"{title}: {value}"
                for title, value in info
            )
            line = (
                " ".join(
                    p
                    for p in [
                        role,
                        f"({', '.join(flags)})" if flags else "",
                        text,
                    ]
                    if p
                )
                .replace("\r", "\\r")
                .replace("\n", "\\n")
                .replace("\t", "\\t")
            )
            lines.append("\t" * depth + f"{node['id']} {line}")
            for child in node["children"]:
                render(child, depth + 1)

        if tree:
            render(tree, 0)
        if count >= MAX_NODES:
            lines.append("[Accessibility tree truncated; inspect a smaller target]")
        return "at_spi", "\n".join(lines)[:MAX_TEXT]

    def _capture(self, bound: BoundWindow) -> dict[str, Any]:
        x, y, width, height = self._geometry(bound.window)
        screen = self.root.get_geometry()
        if x < 0 or y < 0 or x + width > screen.width or y + height > screen.height:
            raise DriverError(
                "WINDOW_OFF_SCREEN",
                "Move the entire window onto the desktop before capturing it",
            )
        raw = bound.window.get_image(0, 0, width, height, X.ZPixmap, 0xFFFFFFFF)
        image = Image.frombytes("RGB", (width, height), raw.data, "raw", "BGRX")
        image.thumbnail(
            (self.max_dimension, self.max_dimension), Image.Resampling.LANCZOS
        )
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
        if output.tell() > 2 * 1024 * 1024:
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=55)
        if output.tell() > 2 * 1024 * 1024:
            raise DriverError(
                "OUTPUT_LIMIT",
                "Screenshot exceeds 2 MiB; reduce screenshot_max_dimension",
            )
        bound.screenshot_id = uuid.uuid4().hex
        bound.geometry = x, y, width, height
        bound.image_size = image.size
        return {
            "image": base64.b64encode(output.getvalue()).decode("ascii"),
            "screenshotId": bound.screenshot_id,
            "width": image.width,
            "height": image.height,
            "generation": self.generation,
        }

    def _point(
        self, bound: BoundWindow, args: dict[str, Any], key: str = "coordinates"
    ) -> tuple[int, int]:
        geometry = self._geometry(bound.window)
        point = args.get(key)
        if not isinstance(point, list) or len(point) != 2:
            raise DriverError("INVALID_ARGUMENT", "Coordinates require [x,y]")
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in point
        ):
            raise DriverError("INVALID_ARGUMENT", "Coordinates must be finite numbers")
        width, height = geometry[2:]
        if not 0 <= point[0] < width or not 0 <= point[1] < height:
            raise DriverError(
                "INVALID_ARGUMENT", "Coordinates are outside the observed screenshot"
            )
        return (
            geometry[0] + round(point[0]),
            geometry[1] + round(point[1]),
        )

    def _keycodes(self, chord: str) -> list[int]:
        aliases = {
            "Ctrl": "Control_L",
            "Control": "Control_L",
            "Alt": "Alt_L",
            "Shift": "Shift_L",
            "Meta": "Super_L",
            "Enter": "Return",
            "Esc": "Escape",
            "ctrl": "Control_L",
            "control": "Control_L",
            "alt": "Alt_L",
            "shift": "Shift_L",
            "super": "Super_L",
            "meta": "Super_L",
        }
        if not isinstance(chord, str) or not chord or len(chord) > 100:
            raise DriverError("INVALID_ARGUMENT", "A bounded X11 key chord is required")
        keys = [aliases.get(key.strip(), key.strip()) for key in chord.split("+")]
        result = []
        for key in keys:
            keysym = XK.string_to_keysym(key)
            code = self.connection.keysym_to_keycode(keysym)
            if not keysym or not code:
                raise DriverError("INVALID_ARGUMENT", f"Unknown X11 key {key[:40]}")
            result.append(code)
        return result

    def _send_key(
        self, bound: BoundWindow, code: int, pressed: bool, state: int
    ) -> None:
        """Route a key directly to the bound window without stealing focus."""
        event = protocol.event.KeyPress if pressed else protocol.event.KeyRelease
        bound.window.send_event(
            event(
                time=int(time.monotonic() * 1000) & 0xFFFFFFFF,
                root=self.root,
                window=bound.window,
                child=X.NONE,
                root_x=0,
                root_y=0,
                event_x=0,
                event_y=0,
                state=state,
                detail=code,
                same_screen=1,
            ),
            propagate=False,
            event_mask=0,
        )
        self.connection.sync()

    def _modifier(self, code: int) -> int:
        """Resolve modifier masks from the server's active keyboard map."""
        for index, codes in enumerate(self.connection.get_modifier_mapping()):
            if code in codes:
                return 1 << index
        return 0

    def _send_pointer(
        self,
        bound: BoundWindow,
        x: int,
        y: int,
        kind: str,
        button: int = 0,
        state: int = 0,
    ) -> None:
        """Deliver window-relative pointer events without moving the desktop mouse."""
        target = bound.window
        origin_x, origin_y, _, _ = self._geometry(target)
        event = {
            "press": protocol.event.ButtonPress,
            "release": protocol.event.ButtonRelease,
            "motion": protocol.event.MotionNotify,
        }[kind]
        if kind == "motion":
            target.send_event(
                protocol.event.EnterNotify(
                    time=int(time.monotonic() * 1000) & 0xFFFFFFFF,
                    root=self.root,
                    window=target,
                    child=X.NONE,
                    root_x=x,
                    root_y=y,
                    event_x=x - origin_x,
                    event_y=y - origin_y,
                    state=state,
                    mode=X.NotifyNormal,
                    detail=X.NotifyNonlinear,
                    flags=1,
                ),
                propagate=False,
                event_mask=0,
            )
        target.send_event(
            event(
                time=int(time.monotonic() * 1000) & 0xFFFFFFFF,
                root=self.root,
                window=target,
                child=X.NONE,
                root_x=x,
                root_y=y,
                event_x=x - origin_x,
                event_y=y - origin_y,
                state=state,
                detail=button,
                same_screen=1,
            ),
            propagate=False,
            event_mask=0,
        )
        self.connection.sync()

    async def _click(
        self,
        bound: BoundWindow,
        x: int,
        y: int,
        button: int,
        count: int = 1,
        duration_ms: int = 0,
        key: str | None = None,
    ) -> None:
        """Release modifier chords and held buttons even when a click is cancelled."""
        codes = self._keycodes(key) if key else []
        for _ in range(count):
            state = 0
            try:
                for code in codes:
                    self._send_key(bound, code, True, state)
                    state |= self._modifier(code)
                self._send_pointer(bound, x, y, "motion", state=state)
                self._send_pointer(bound, x, y, "press", button, state)
                await asyncio.sleep(duration_ms / 1000)
            finally:
                self._send_pointer(
                    bound, x, y, "release", button, state | (1 << (7 + button))
                )
                for code in reversed(codes):
                    self._send_key(bound, code, False, state)
                    state &= ~self._modifier(code)

    async def _press(
        self, bound: BoundWindow, chord: str, duration_ms: int = 25
    ) -> None:
        codes = self._keycodes(chord)
        state = 0
        try:
            for code in codes:
                self.held_keys.add(code)
                self._send_key(bound, code, True, state)
                state |= self._modifier(code)
                await asyncio.sleep(0.033)
            await asyncio.sleep(duration_ms / 1000)
        finally:
            for code in reversed(codes):
                self._send_key(bound, code, False, state)
                state &= ~self._modifier(code)
                self.held_keys.discard(code)
                await asyncio.sleep(0.033)

    def cleanup_keys(self) -> None:
        """Release only input pressed by this helper, including interrupted drags."""
        # XSendEvent does not change the server's global key/button state.
        self.held_keys.clear()
        self.held_buttons.clear()
        self.connection.sync()

    def end(self) -> None:
        """Release one execution's native input ownership."""
        self.cleanup_keys()
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None
        self.execution_id = ""

    async def dispatch(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute one validated operation and return plain JSON data."""
        if op == "begin":
            if self.execution_id:
                raise DriverError("BUSY", "A native execution is already active")
            name = os.environ.get("DISPLAY", "none").replace("/", "_").replace(":", "_")
            descriptor = os.open(
                f"/tmp/opencuria-cua-{os.getuid()}-{name}.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise DriverError(
                    "DESKTOP_BUSY", "Another automation owns this desktop"
                ) from exc
            self.lock_fd = descriptor
            self.execution_id = str(args.get("executionId") or "")
            return {}
        if op == "end":
            self.end()
            return {}
        if not self.execution_id:
            raise DriverError(
                "EXECUTION_ENDED", "Native operation requires an active execution"
            )
        await asyncio.sleep(max(0, self.settle_at - time.monotonic()))
        if op == "launch_app":
            from gi.repository import Gio

            name = args.get("app")
            if not isinstance(name, str) or not name:
                raise DriverError("INVALID_ARGUMENT", "An app inventory ID is required")
            app = (
                Gio.DesktopAppInfo.new_from_filename(name)
                if name.startswith("/")
                else Gio.DesktopAppInfo.new(name)
            )
            if app is None:
                raise DriverError(
                    "APP_UNAVAILABLE", f"Application {name} is unavailable"
                )
            app.launch([], None)
            self.settle_at = time.monotonic() + 0.1
            return {}
        if op in {"windows", "apps", "inventory"}:
            windows = [self._public_window(window) for window in self._list_windows()]
            if op == "windows":
                return {"items": windows}
            apps = self._apps(windows)
            return (
                {"items": apps}
                if op == "apps"
                else {
                    "apps": apps,
                    "browsers": [],
                }
            )
        if op == "bind":
            identifier = args.get("windowId")
            if identifier not in {w["id"] for w in self._list_windows()}:
                raise DriverError(
                    "WINDOW_CLOSED", "Window is not present in the current inventory"
                )
            window = self.connection.create_resource_object("window", identifier)
            window.change_attributes(event_mask=X.StructureNotifyMask)
            bound = self.windows.get(identifier) or BoundWindow(window=window)
            self.windows[identifier] = bound
            return {"identity": bound.identity}
        bound = self._bound(args)
        if op == "state":
            result: dict[str, Any] = {}
            if args.get("text") or args.get("image"):
                source, text = self._tree(bound)
                result.update(state=text, source=source, snapshotId=bound.snapshot_id)
            if args.get("image"):
                result.update(self._capture(bound))
            return result
        if op == "activate":
            bound.window.configure(stack_mode=X.Above)
            self.root.send_event(
                protocol.event.ClientMessage(
                    window=bound.window,
                    client_type=self.connection.intern_atom("_NET_ACTIVE_WINDOW"),
                    data=(32, [2, X.CurrentTime, 0, 0, 0]),
                ),
                event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
            )
            bound.window.set_input_focus(X.RevertToParent, X.CurrentTime)
            self.connection.sync()
            await asyncio.sleep(0.1)
            self._require_focus(bound)
            return {}
        if op == "press_key":
            await self._press(bound, args.get("key", ""), args.get("durationMs", 25))
        elif op in {"click", "secondary"} and args.get("elementId") is not None:
            if op == "click" and (
                args.get("mouseButton", "left") not in {"left", "right", "l", "r"}
                or args.get("clickCount", 1) != 1
            ):
                raise DriverError(
                    "UNSUPPORTED_ACTION",
                    "Use observed coordinates for secondary or double clicks",
                )
            node = self._element(bound, args)
            action = node.get_action_iface()
            name = args.get("action") if op == "secondary" else "click"
            if op == "click" and args.get("mouseButton") in {"right", "r"}:
                component = node.get_component_iface()
                rectangle = component.get_extents(self.atspi.CoordType.SCREEN)
                await self._click(
                    bound,
                    rectangle.x + rectangle.width // 2,
                    rectangle.y + rectangle.height // 2,
                    3,
                )
            elif action:
                for i in range(action.get_n_actions()):
                    action_name = action.get_action_name(i)
                    if action_name == name or (
                        op == "click" and action_name == "press"
                    ):
                        if not action.do_action(i):
                            raise DriverError(
                                "ACTION_FAILED", "Accessibility action was rejected"
                            )
                        break
                else:
                    if op == "click":
                        component = node.get_component_iface()
                        rectangle = component.get_extents(self.atspi.CoordType.SCREEN)
                        await self._click(
                            bound,
                            rectangle.x + rectangle.width // 2,
                            rectangle.y + rectangle.height // 2,
                            1,
                        )
                    else:
                        raise DriverError(
                            "UNSUPPORTED_ACTION",
                            "Requested action is not in the observed element's action list",
                        )
            else:
                raise DriverError(
                    "UNSUPPORTED_ACTION",
                    "This element has no semantic click; use observed coordinates",
                )
        elif op == "type_text":
            text = args.get("text")
            if not isinstance(text, str) or len(text.encode("utf-8")) > 16000:
                raise DriverError(
                    "INVALID_ARGUMENT", "Text must be a string of at most 16000 bytes"
                )
            if args.get("elementId") is not None:
                node = self._element(bound, args)
                editable = node.get_editable_text_iface()
                text_iface = node.get_text_iface()
                if not editable or not text_iface:
                    raise DriverError(
                        "UNSUPPORTED_ACTION", "Selected element is not editable"
                    )
                position = max(0, self.atspi.Text.get_caret_offset(node))
                for index in reversed(range(self.atspi.Text.get_n_selections(node))):
                    selected = self.atspi.Text.get_selection(node, index)
                    position = selected.start_offset
                    if not self.atspi.EditableText.delete_text(
                        node, selected.start_offset, selected.end_offset
                    ):
                        raise DriverError(
                            "ACTION_FAILED", "Selected text could not be replaced"
                        )
                if not self.atspi.EditableText.insert_text(
                    node, position, text, len(text.encode("utf-8"))
                ):
                    raise DriverError("ACTION_FAILED", "Text insertion was rejected")
                self.atspi.Text.set_caret_offset(node, position + len(text))
            else:
                clipboard = await asyncio.create_subprocess_exec(
                    "xclip",
                    "-selection",
                    "clipboard",
                    "-quiet",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    clipboard.stdin.write(text.encode("utf-8"))
                    await clipboard.stdin.drain()
                    clipboard.stdin.close()
                    await asyncio.sleep(0.05)
                    if clipboard.returncode is not None:
                        raise DriverError(
                            "CLIPBOARD_UNAVAILABLE",
                            "Clipboard helper exited before paste",
                        )
                    await self._press(bound, "Control_L+v")
                    # GTK requests TARGETS and UTF8_STRING separately. A one-request
                    # clipboard owner exits too early and silently pastes nothing.
                    await asyncio.sleep(0.2)
                finally:
                    if clipboard.returncode is None:
                        clipboard.terminate()
                    try:
                        await asyncio.wait_for(clipboard.wait(), 1)
                    except TimeoutError:
                        clipboard.kill()
                        await clipboard.wait()
        elif op == "type_characters":
            text = args.get("text")
            if not isinstance(text, str) or len(text.encode()) > 16000:
                raise DriverError("INVALID_ARGUMENT", "Printable text is required")
            for character in text:
                keysym = ord(character)
                mapped = self.connection.keysym_to_keycodes(keysym)
                if not mapped:
                    raise DriverError("UNSUPPORTED_KEY", f"No key for {character}")
                code, index = mapped[0]
                state = X.ShiftMask if index % 2 else 0
                self._send_key(bound, code, True, state)
                self._send_key(bound, code, False, state)
            self.settle_at = time.monotonic() + 0.1
            return {"charactersSent": len(text)}
        elif op == "click":
            x, y = self._point(bound, args)
            button = {"left": 1, "middle": 2, "right": 3, "l": 1, "m": 2, "r": 3}.get(
                args.get("mouseButton", "left")
            )
            count = args.get("clickCount", 1)
            if button is None or not isinstance(count, int) or not 1 <= count <= 100:
                raise DriverError(
                    "INVALID_ARGUMENT", "Unsupported mouse button or click count"
                )
            await self._click(
                bound, x, y, button, count, args.get("durationMs", 0), args.get("key")
            )
        elif op == "scroll":
            if args.get("elementId") is not None:
                node = self._element(bound, args)
                component = node.get_component_iface()
                if not component:
                    raise DriverError(
                        "UNSUPPORTED_ACTION", "Element has no visible scroll target"
                    )
                rectangle = component.get_extents(self.atspi.CoordType.SCREEN)
                x, y = (
                    rectangle.x + rectangle.width // 2,
                    rectangle.y + rectangle.height // 2,
                )
            else:
                x, y = self._point(bound, args)
            button = {
                "up": 4,
                "down": 5,
                "left": 6,
                "right": 7,
                "u": 4,
                "d": 5,
                "l": 6,
                "r": 7,
            }.get(args.get("direction"))
            pixels = args.get("pixels", 300)
            if (
                button is None
                or not isinstance(pixels, (int, float))
                or isinstance(pixels, bool)
                or not math.isfinite(pixels)
                or not 1 <= pixels <= 2**53 - 1
            ):
                raise DriverError(
                    "INVALID_ARGUMENT", "Scroll requires a direction and 1..5000 pixels"
                )
            for _ in range(min(1000, max(1, int(pixels // 40)))):
                await self._click(bound, x, y, button)
        elif op == "drag":
            start = self._point(bound, args, "from")
            end = self._point(bound, args, "to")
            try:
                self._send_pointer(bound, *start, "motion")
                self._send_pointer(bound, *start, "press", 1)
                for step in range(1, 21):
                    self._send_pointer(
                        bound,
                        round(start[0] + (end[0] - start[0]) * step / 20),
                        round(start[1] + (end[1] - start[1]) * step / 20),
                        "motion",
                        state=X.Button1Mask,
                    )
                    await asyncio.sleep(0.01)
            finally:
                self._send_pointer(bound, *end, "release", 1, X.Button1Mask)
        else:
            raise DriverError(
                "UNKNOWN_OPERATION", "Unsupported native desktop operation"
            )
        self.settle_at = max(self.settle_at, time.monotonic() + 0.1)
        return {}


async def main() -> None:
    """Serve JSON lines until EOF, releasing native input on every exit path."""
    # Third-party X11/GI libraries can write directly to stdout. Keep the
    # protocol on a private non-inherited descriptor and redirect all ambient
    # Python/C stdout to stderr before initializing native libraries.
    protocol_output = os.fdopen(
        os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1
    )
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    driver: LinuxDriver | None = None
    try:
        driver = LinuxDriver()
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_LINE + 1)
            if not line:
                break
            identifier: Any = None
            try:
                if len(line) > MAX_LINE:
                    raise DriverError(
                        "INPUT_LIMIT", "Native request exceeds its byte limit"
                    )
                request = json.loads(line)
                identifier = request["id"]
                result = await driver.dispatch(request["op"], request.get("args") or {})
                reply = {"id": identifier, "ok": True, "output": result}
            except Exception as exc:  # noqa: BLE001 - bound native protocol failures
                reply = {
                    "id": identifier,
                    "ok": False,
                    "code": getattr(exc, "code", "NATIVE_ERROR"),
                    "error": str(exc)[:1500],
                    "effect_status": "unknown",
                }
            protocol_output.write(json.dumps(reply, ensure_ascii=False) + "\n")
            protocol_output.flush()
    except Exception as exc:  # noqa: BLE001 - keep native diagnostics on stderr
        log.error("computer_use_driver_failed", error_type=type(exc).__name__)
    finally:
        if driver is not None:
            driver.end()
            driver.connection.close()
        protocol_output.close()


if __name__ == "__main__":
    asyncio.run(main())
