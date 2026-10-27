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
import os
import signal
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from PIL import Image
from Xlib import XK, X, display, error, protocol
from Xlib.ext import xtest

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
            "app": "/".join(window.get_wm_class() or ()),
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
            else self.root.query_tree().children
        )
        result = []
        for window in candidates:
            try:
                if window.get_attributes().map_state != X.IsViewable:
                    continue
                metadata = self._metadata(window)
                if metadata["width"] > 1 and metadata["height"] > 1:
                    result.append(metadata)
            except error.XError:
                continue
        return result[:200]

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
        except Exception:
            root = None
        if root is None:
            return "x11", (
                f"Accessibility source: x11 "
                f"(limited window metadata; use a screenshot)\n"
                f"Window {metadata['id']}: {metadata['title']} ({metadata['app']})"
            )
        lines = ["Accessibility source: at_spi"]
        size = len(lines[0])

        def visit(node: Any, depth: int) -> None:
            nonlocal size
            if (
                depth > MAX_DEPTH
                or len(bound.elements) >= MAX_NODES
                or size >= MAX_TEXT - 2048
            ):
                return
            try:
                identifier = len(bound.elements) + 1
                states = node.get_state_set()
                if states.contains(self.atspi.StateType.DEFUNCT):
                    return
                role = node.get_role_name() or "unknown"
                original_name = str(node.get_name() or "")
                bound.elements[identifier] = (node, role, original_name)
                name = original_name.replace("\n", " ")[:512]
                line = f"{'  ' * depth}{identifier} {role} {name}"
                if states.contains(self.atspi.StateType.FOCUSED):
                    line += " [focused]"
                if states.contains(self.atspi.StateType.EDITABLE):
                    line += " [editable]"
                if not states.contains(self.atspi.StateType.SENSITIVE):
                    line += " [disabled]"
                text_iface = node.get_text_iface()
                if text_iface and role != "password text":
                    value = self.atspi.Text.get_text(
                        node, 0, min(512, self.atspi.Text.get_character_count(node))
                    )
                    if value:
                        line += f" Value: {json.dumps(value, ensure_ascii=False)}"
                action = node.get_action_iface()
                if action:
                    actions = [
                        action.get_action_name(i)
                        for i in range(min(action.get_n_actions(), 10))
                    ]
                    line += f" Actions: {actions}"
                lines.append(line)
                size += len(line.encode("utf-8"))
                for i in range(min(node.get_child_count(), MAX_NODES)):
                    child = node.get_child_at_index(i)
                    if child:
                        visit(child, depth + 1)
            except Exception:
                return

        visit(root, 0)
        if len(bound.elements) >= MAX_NODES or size >= MAX_TEXT - 2048:
            lines.append("[Accessibility tree truncated; inspect a smaller target]")
        return "at_spi", "\n".join(lines)

    def _capture(self, bound: BoundWindow) -> dict[str, Any]:
        self._require_focus(bound)
        x, y, width, height = self._geometry(bound.window)
        screen = self.root.get_geometry()
        if x < 0 or y < 0 or x + width > screen.width or y + height > screen.height:
            raise DriverError(
                "WINDOW_OFF_SCREEN",
                "Move the entire window onto the desktop before capturing it",
            )
        raw = self.root.get_image(x, y, width, height, X.ZPixmap, 0xFFFFFFFF)
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
        self._require_focus(bound)
        if args.get("screenshotId") != bound.screenshot_id or not bound.screenshot_id:
            raise DriverError(
                "STALE_SCREENSHOT",
                "Capture a fresh window screenshot before using coordinates",
            )
        geometry = self._geometry(bound.window)
        if geometry != bound.geometry:
            raise DriverError(
                "STALE_SCREENSHOT",
                "The window moved or resized; capture a fresh screenshot",
            )
        point = args.get(key)
        if not isinstance(point, list) or len(point) != 2:
            raise DriverError("INVALID_ARGUMENT", "Coordinates require [x,y]")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in point):
            raise DriverError("INVALID_ARGUMENT", "Coordinates must be finite numbers")
        width, height = bound.image_size
        if not 0 <= point[0] < width or not 0 <= point[1] < height:
            raise DriverError(
                "INVALID_ARGUMENT", "Coordinates are outside the observed screenshot"
            )
        return (
            geometry[0] + round(point[0] * geometry[2] / width),
            geometry[1] + round(point[1] * geometry[3] / height),
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

    async def _press(self, chord: str) -> None:
        try:
            for code in self._keycodes(chord):
                self.held_keys.add(code)
                xtest.fake_input(self.connection, X.KeyPress, code)
            self.connection.sync()
            await asyncio.sleep(0.025)
        finally:
            self.cleanup_keys()

    def cleanup_keys(self) -> None:
        """Release only input pressed by this helper, including interrupted drags."""
        for code in reversed(tuple(self.held_keys)):
            xtest.fake_input(self.connection, X.KeyRelease, code)
        for button in tuple(self.held_buttons):
            xtest.fake_input(self.connection, X.ButtonRelease, button)
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
        if op in {"windows", "apps", "inventory"}:
            windows = self._list_windows()
            if op == "windows":
                return {"items": windows}
            apps: dict[str, dict[str, Any]] = {}
            for window in windows:
                name = window["app"] or window["title"]
                apps.setdefault(name, {"id": name, "name": name, "windows": []})[
                    "windows"
                ].append(window)
            return (
                {"items": list(apps.values())}
                if op == "apps"
                else {
                    "apps": list(apps.values()),
                    "browsers": [],
                    "generation": self.generation,
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
            bound = BoundWindow(window=window)
            self.windows[identifier] = bound
            return {"identity": bound.identity}
        bound = self._bound(args)
        if op == "state":
            result: dict[str, Any] = {}
            if args.get("text"):
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
            self._require_focus(bound)
            await self._press(args.get("key", ""))
        elif op in {"click", "secondary"} and args.get("elementId") is not None:
            if op == "click" and (
                args.get("mouseButton", "left") != "left"
                or args.get("clickCount", 1) != 1
            ):
                raise DriverError(
                    "UNSUPPORTED_ACTION",
                    "Use observed coordinates for secondary or double clicks",
                )
            node = self._element(bound, args)
            action = node.get_action_iface()
            name = args.get("action") if op == "secondary" else "click"
            if action:
                for i in range(action.get_n_actions()):
                    action_name = action.get_action_name(i)
                    if action_name == name or (
                        op == "click" and action_name in {"press", "activate"}
                    ):
                        if not action.do_action(i):
                            raise DriverError(
                                "ACTION_FAILED", "Accessibility action was rejected"
                            )
                        break
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
                self._require_focus(bound)
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
                    await self._press("Control_L+v")
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
        elif op == "click":
            x, y = self._point(bound, args)
            button = {"left": 1, "middle": 2, "right": 3}.get(
                args.get("mouseButton", "left")
            )
            count = args.get("clickCount", 1)
            if button is None or count not in {1, 2}:
                raise DriverError(
                    "INVALID_ARGUMENT", "Unsupported mouse button or click count"
                )
            xtest.fake_input(self.connection, X.MotionNotify, x=x, y=y)
            try:
                for _ in range(count):
                    self.held_buttons.add(button)
                    xtest.fake_input(self.connection, X.ButtonPress, button)
                    xtest.fake_input(self.connection, X.ButtonRelease, button)
                    self.held_buttons.discard(button)
                    self.connection.sync()
                    await asyncio.sleep(0.03)
            finally:
                self.cleanup_keys()
        elif op == "scroll":
            if args.get("elementId") is not None:
                node = self._element(bound, args)
                component = node.get_component_iface()
                if not component:
                    raise DriverError(
                        "UNSUPPORTED_ACTION", "Element has no visible scroll target"
                    )
                self._require_focus(bound)
                rectangle = component.get_extents(self.atspi.CoordType.SCREEN)
                x, y = (
                    rectangle.x + rectangle.width // 2,
                    rectangle.y + rectangle.height // 2,
                )
            else:
                x, y = self._point(bound, args)
            button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(
                args.get("direction")
            )
            pixels = args.get("pixels", 300)
            if (
                button is None
                or not isinstance(pixels, int)
                or isinstance(pixels, bool)
                or not 1 <= pixels <= 5000
            ):
                raise DriverError(
                    "INVALID_ARGUMENT", "Scroll requires a direction and 1..5000 pixels"
                )
            xtest.fake_input(self.connection, X.MotionNotify, x=x, y=y)
            for _ in range(min(100, max(1, pixels // 40))):
                xtest.fake_input(self.connection, X.ButtonPress, button)
                xtest.fake_input(self.connection, X.ButtonRelease, button)
            self.connection.sync()
        elif op == "drag":
            start = self._point(bound, args, "from")
            end = self._point(bound, args, "to")
            try:
                xtest.fake_input(
                    self.connection, X.MotionNotify, x=start[0], y=start[1]
                )
                self.held_buttons.add(1)
                xtest.fake_input(self.connection, X.ButtonPress, 1)
                for step in range(1, 21):
                    xtest.fake_input(
                        self.connection,
                        X.MotionNotify,
                        x=round(start[0] + (end[0] - start[0]) * step / 20),
                        y=round(start[1] + (end[1] - start[1]) * step / 20),
                    )
                    self.connection.sync()
                    await asyncio.sleep(0.01)
            finally:
                self.cleanup_keys()
        else:
            raise DriverError(
                "UNKNOWN_OPERATION", "Unsupported native desktop operation"
            )
        bound.screenshot_id = ""
        await asyncio.sleep(0.1)
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
            except Exception as exc:
                reply = {
                    "id": identifier,
                    "ok": False,
                    "code": getattr(exc, "code", "NATIVE_ERROR"),
                    "error": str(exc)[:1500],
                    "effect_status": "unknown",
                }
            protocol_output.write(json.dumps(reply, ensure_ascii=False) + "\n")
            protocol_output.flush()
    except Exception as exc:
        log.error("computer_use_driver_failed", error_type=type(exc).__name__)
    finally:
        if driver is not None:
            driver.end()
            driver.connection.close()
        protocol_output.close()


if __name__ == "__main__":
    asyncio.run(main())
