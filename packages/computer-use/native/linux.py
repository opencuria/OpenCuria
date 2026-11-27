"""Bounded Linux desktop I/O for the workspace Computer Use runtime.

This helper has no model, prompts, credentials or agent loop. Each request is
an operation on an explicitly bound X11 window. Accessibility is read directly
from the AT-SPI D-Bus interfaces with pipelined requests; its absence is
reported as a limited X11 tree rather than invented elements.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import io
import json
import math
import os
import re
import shutil
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import structlog
from PIL import Image
from Xlib import XK, X, Xatom, display, error, protocol

MAX_LINE = 1024 * 1024
MAX_INPUT_TEXT = 64 * 1024
DEFAULT_MAX_NODES = 6000
HARD_MAX_NODES = 20000
MAX_DEPTH = 64
MAX_VALUE = 16000
MIRROR_MAX_AGE = 90.0
BATCH = 1500
ACC = "org.a11y.atspi.Accessible"
ACTION = "org.a11y.atspi.Action"
COMPONENT = "org.a11y.atspi.Component"
TEXT = "org.a11y.atspi.Text"
EDITABLE = "org.a11y.atspi.EditableText"
VALUE = "org.a11y.atspi.Value"
SELECTION = "org.a11y.atspi.Selection"
PROPERTIES = "org.freedesktop.DBus.Properties"
REGISTRY = "org.a11y.atspi.Registry"
ROOT = "/org/a11y/atspi/accessible/root"
SCREEN = 0
SCROLL_ANYWHERE = 6
LABELLED_BY = 2
OBJECT = "\ufffc"
STATES = {
    "active": 1, "armed": 2, "busy": 3, "checked": 4, "collapsed": 5,
    "defunct": 6, "editable": 7, "enabled": 8, "expandable": 9,
    "expanded": 10, "focusable": 11, "focused": 12, "modal": 16,
    "multi_line": 17, "multiselectable": 18, "pressed": 20, "selectable": 22,
    "selected": 23, "sensitive": 24, "showing": 25, "visible": 30,
    "indeterminate": 32, "required": 33, "invalid": 36, "is_default": 39,
    "checkable": 41, "has_popup": 42, "read_only": 43,
}  # fmt: skip
STATE_NAMES = tuple((name.replace("_", "-"), bit) for name, bit in STATES.items())
STRUCTURE_EVENTS = {
    "ChildrenChanged", "LoadComplete", "Reload", "RowInserted", "RowDeleted",
    "RowReordered", "ModelChanged", "VisibleDataChanged",
}  # fmt: skip
CONTAINERS = {"panel", "filler", "section", "generic", "group", "unknown", "invalid"}
TEXT_ROLES = {"text", "static", "label", "caption"}
STATIC_ROLES = TEXT_ROLES | {"paragraph", "heading", "separator", "notification"}
FIELD_ROLES = {
    "entry", "text", "password text", "spin button", "spin box", "combo box", "terminal",
    "document text", "editbar", "search box",
}  # fmt: skip
INTERACTIVE_ROLES = {
    "push button", "button", "toggle button", "check box", "radio button",
    "entry", "password text", "link", "menu item", "check menu item",
    "radio menu item", "combo box", "slider", "spin button", "scroll bar",
    "page tab", "tree item", "list item", "menu", "switch", "spin box",
}  # fmt: skip
CONTEXT_ROLES = {
    "push button", "button", "toggle button", "check box", "radio button",
    "entry", "password text", "text", "spin button", "spin box", "combo box", "slider",
    "link", "switch", "menu item", "check menu item", "radio menu item",
    "image", "icon",
}  # fmt: skip
DEFAULT_VERBS = {
    "press", "click", "activate", "select", "jump", "open", "check", "uncheck",
    "toggle", "expand or contract", "switch",
}  # fmt: skip
OPTION_ROLES = {
    "menu item", "check menu item", "radio menu item", "list item", "option",
    "tree item", "table cell", "page tab",
}  # fmt: skip
CELL_ROLES = {"table cell", "cell", "grid cell"}
FOLDING_ROLES = {"menu", "combo box", "menu button"}
ROW_ROLES = {"table row", "row"}
TABLE_ROLES = {"table", "tree table", "grid"}
HEADER_ROLES = {"column header", "table column header"}
GENERIC_ACTIONS = {"", "dodefault", "clickancestor", "click ancestor"}
IGNORED_ACTIONS = GENERIC_ACTIONS | {"showcontextmenu", "scrolltovisible"}
CLICK_ACTIONS = (
    "click", "press", "jump", "open", "toggle", "check", "uncheck", "select",
    "activate", "expand or contract", "expand", "collapse", "switch",
)  # fmt: skip
ACCEPT_NAMES = re.compile(
    r"^(_?open|_?select|upload|choose|ok|_?öffnen|auswählen|hochladen|wählen|"
    r"ouvrir|abrir|apri|openen|insert|attach)$",
    re.IGNORECASE,
)
FILE_DIALOG_TITLE = re.compile(
    r"open|upload|file|select|choose|datei|öffnen|hochladen|auswählen|ouvrir|abrir",
    re.IGNORECASE,
)

structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
log = structlog.get_logger(__name__)


class DriverError(Exception):
    """A bounded, actionable desktop error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _clean(value: Any, *, multiline: bool = False) -> str:
    """Drop embedded-object placeholders and redundant whitespace."""
    if not value:
        return ""
    text = str(value).replace(OBJECT, " ")
    if multiline:
        return text.strip()  # all Unicode whitespace, e.g. no-break spaces
    return " ".join(text.split())


def _short(value: str, limit: int = 300) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}… ({len(value)} chars)"


def _number(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


@dataclass
class Ref:
    """The AT-SPI identity of an element that was shown to the agent."""

    bus: str
    path: str
    role: str
    name: str


@dataclass
class BoundWindow:
    """A window identity, its AT-SPI root and its coordinate mapping."""

    window: Any
    identity: str = field(default_factory=lambda: uuid.uuid4().hex)
    root: tuple[str, str] | None = None
    ids: dict[tuple[str, str], int] = field(default_factory=dict)
    refs: dict[int, Ref] = field(default_factory=dict)
    ax_observed: bool = False
    # Window pixel = origin + screenshot coordinate * scale.
    transform: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    mirror: dict[str, Any] | None = None
    cache: dict[str, Any] | None = None


class AtspiBus:
    """Pipelined AT-SPI requests and change events on the accessibility bus."""

    EVENTS = (
        "object:children-changed",
        "object:state-changed",
        "object:text-changed",
        "object:property-change",
        "object:selection-changed",
        "object:row-inserted",
        "object:row-deleted",
        "object:row-reordered",
        "object:model-changed",
        "object:visible-data-changed",
        "document:load-complete",
        "document:reload",
        "window:create",
        "window:destroy",
    )

    def __init__(self, addresses: list[str] | None = None) -> None:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self.Gio = Gio
        self.GLib = GLib
        self.connection = None
        candidates = list(addresses or [])
        candidates.append("")  # last resort: the session bus launcher
        failure: Exception | None = None
        for address in candidates:
            try:
                if not address:
                    session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                    address = session.call_sync(
                        "org.a11y.Bus",
                        "/org/a11y/bus",
                        "org.a11y.Bus",
                        "GetAddress",
                        None,
                        GLib.VariantType("(s)"),
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    ).unpack()[0]
                self.connection = Gio.DBusConnection.new_for_address_sync(
                    address,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )
                self.address = address
                break
            except Exception as exc:  # noqa: BLE001 - a stale address: try the next
                failure = exc
        if self.connection is None:
            raise failure or RuntimeError("No accessibility bus")
        self.context = GLib.MainContext.default()
        self.activity: dict[str, float] = {}
        self.changes: dict[str, dict[str, Any]] = {}
        self.last_event = 0.0
        self.subscriptions: list[int] = []
        self.listening = False
        self.pids: dict[str, int | None] = {}

    def _event(
        self,
        _connection: Any,
        sender: str,
        path: str,
        _interface: str,
        member: str,
        *_: Any,
    ) -> None:
        now = time.monotonic()
        self.activity[sender] = now
        self.last_event = now
        entry = self.changes.setdefault(
            sender,
            {"structure": set(), "props": set(), "documents": set(), "overflow": False},
        )
        if entry["overflow"]:
            return
        if member in {"LoadComplete", "Reload"}:
            entry["documents"].add(path)
        kind = "structure" if member in STRUCTURE_EVENTS else "props"
        entry[kind].add(path)
        if len(entry["structure"]) + len(entry["props"]) > 4000:
            entry["overflow"] = True

    def take(self, bus: str) -> dict[str, Any]:
        """Changed object paths of one application since the previous take."""
        return self.changes.pop(bus, None) or {
            "structure": set(),
            "props": set(),
            "documents": set(),
            "overflow": False,
        }

    def listen(self) -> None:
        """Ask applications for change events while an execution runs."""
        if self.listening:
            return
        self.listening = True
        for interface in ("Object", "Document", "Window"):
            self.subscriptions.append(
                self.connection.signal_subscribe(
                    None,
                    f"org.a11y.atspi.Event.{interface}",
                    None,
                    None,
                    None,
                    self.Gio.DBusSignalFlags.NONE,
                    self._event,
                )
            )
        self.many(
            [
                (
                    REGISTRY,
                    "/org/a11y/atspi/registry",
                    REGISTRY,
                    "RegisterEvent",
                    (event, [], ""),
                    "(sass)",
                )
                for event in self.EVENTS
            ]  # fmt: skip
        )

    def unlisten(self) -> None:
        """Stop event delivery so idle applications do not emit for us."""
        if not self.listening:
            return
        self.listening = False
        self.many(
            [
                (
                    REGISTRY,
                    "/org/a11y/atspi/registry",
                    REGISTRY,
                    "DeregisterEvent",
                    (event,),
                    "(s)",
                )
                for event in self.EVENTS
            ]  # fmt: skip
        )
        for subscription in self.subscriptions:
            self.connection.signal_unsubscribe(subscription)
        self.subscriptions.clear()
        self.pump()

    def pump(self) -> None:
        for _ in range(100000):
            if not self.context.pending():
                break
            self.context.iteration(False)

    def call(
        self,
        bus: str,
        path: str,
        interface: str,
        method: str,
        args: Any = None,
        signature: str | None = None,
        timeout: int = 3000,
    ) -> tuple:
        variant = self.GLib.Variant(signature, args) if signature else None
        return self.connection.call_sync(
            bus,
            path,
            interface,
            method,
            variant,
            None,
            self.Gio.DBusCallFlags.NONE,
            timeout,
            None,
        ).unpack()

    def get(self, bus: str, path: str, interface: str, name: str) -> Any:
        return self.call(bus, path, PROPERTIES, "Get", (interface, name), "(ss)")[0]

    def many(self, requests: list[tuple], timeout: int = 3000) -> list[Any]:
        """Issue requests concurrently; failures are returned, not raised."""
        results: list[Any] = [None] * len(requests)
        for start in range(0, len(requests), BATCH):
            pending = [0]

            def done(
                connection: Any, result: Any, index: int, pending: list[int] = pending
            ) -> None:
                try:
                    results[index] = connection.call_finish(result).unpack()
                except Exception as exc:  # noqa: BLE001 - returned to the caller
                    results[index] = exc
                pending[0] -= 1

            for index in range(start, min(len(requests), start + BATCH)):
                bus, path, interface, method, args, signature = requests[index]
                variant = self.GLib.Variant(signature, args) if signature else None
                pending[0] += 1
                self.connection.call(
                    bus,
                    path,
                    interface,
                    method,
                    variant,
                    None,
                    self.Gio.DBusCallFlags.NONE,
                    timeout,
                    None,
                    done,
                    index,
                )
            deadline = time.monotonic() + timeout / 1000 + 2
            while pending[0] > 0 and time.monotonic() < deadline:
                self.context.iteration(True)
        return results

    def pid(self, bus: str) -> int | None:
        if bus not in self.pids:
            try:
                self.pids[bus] = int(
                    self.call(
                        "org.freedesktop.DBus",
                        "/org/freedesktop/DBus",
                        "org.freedesktop.DBus",
                        "GetConnectionUnixProcessID",
                        (bus,),
                        "(s)",
                        1000,
                    )[0]
                )
            except Exception:  # noqa: BLE001 - some brokers hide credentials
                self.pids[bus] = None
        return self.pids[bus]


def _states(value: Any) -> int:
    if isinstance(value, Exception) or not value:
        return 0
    words = value[0]
    return int(words[0]) | (int(words[1]) << 32 if len(words) > 1 else 0)


def _has(states: int, name: str) -> bool:
    return bool(states & (1 << STATES[name]))


def _ok(value: Any) -> bool:
    return value is not None and not isinstance(value, Exception)


class LinuxDriver:
    """Execute desktop operations while retaining window and element identity."""

    def __init__(self) -> None:
        # Desktop apps use the session bus of the display's desktop session,
        # which can differ from the bus the runner started this process with.
        # Join it so launched apps and the accessibility bridge match.
        self._discover_session_bus()
        self.connection = display.Display()
        self.root = self.connection.screen().root
        self.windows: dict[int, BoundWindow] = {}
        self.lock_fd: int | None = None
        self.execution_id = ""
        self.action_at = 0.0
        self.dirty = False
        self.touched: set[tuple[str, str]] = set()
        self.toolkits: dict[str, str] = {}
        self.next_element = 1
        self.generation = int(os.environ.get("OPENCURIA_CUA_GENERATION", "0"))
        self.max_dimension = min(
            7680, max(320, int(os.environ.get("OPENCURIA_CUA_MAX_DIMENSION", "2400")))
        )
        self.atspi: AtspiBus | None = None
        try:
            self._enable_accessibility()
            self.atspi = AtspiBus(self._atspi_addresses())
        except Exception as exc:  # noqa: BLE001 - X11 remains usable without AT-SPI
            log.warning(
                "computer_use_accessibility_unavailable", error_type=type(exc).__name__
            )

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _enable_accessibility() -> None:
        """Activate the display's bridge, including already-running sessions."""
        from gi.repository import Gio, GLib

        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            for name in ("IsEnabled", "ScreenReaderEnabled"):
                bus.call_sync(
                    "org.a11y.Bus",
                    "/org/a11y/bus",
                    "org.freedesktop.DBus.Properties",
                    "Set",
                    GLib.Variant(
                        "(ssv)", ("org.a11y.Status", name, GLib.Variant("b", True))
                    ),
                    None,
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
        except Exception as exc:  # noqa: BLE001 - X11 remains usable without a bridge
            log.warning(
                "computer_use_accessibility_activation_failed",
                error_type=type(exc).__name__,
            )

    def _atspi_addresses(self) -> list[str]:
        """Where applications look for the accessibility bus, in libatspi order:
        AT_SPI_BUS_ADDRESS, the display's AT_SPI_BUS root property, the session."""
        addresses = []
        if os.environ.get("AT_SPI_BUS_ADDRESS"):
            addresses.append(os.environ["AT_SPI_BUS_ADDRESS"])
        with contextlib.suppress(Exception):
            value = self._property(self.root, "AT_SPI_BUS")
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if isinstance(value, str) and value.strip("\0 "):
                addresses.append(value.strip("\0 "))
        return addresses

    @staticmethod
    def _discover_session_bus() -> None:
        """Join the session bus of this display's desktop session, if any."""
        from pathlib import Path

        current_display = os.environ.get("DISPLAY", "")
        sessions = (
            b"xfce4-session", b"gnome-session", b"startplasma", b"lxsession",
            b"mate-session", b"cinnamon-session", b"lxqt-session",
        )  # fmt: skip
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                command = (process / "cmdline").read_bytes().split(b"\0")[0]
                if not any(name in command for name in sessions):
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

    # ---------------------------------------------------------------- windows

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
        transient = window.get_wm_transient_for()
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
            "transient_for": getattr(transient, "id", None) or None,
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
            try:
                attributes = window.get_attributes()
                if window.get_wm_class() or attributes.override_redirect:
                    candidates.append(window)
            except error.XError:
                continue
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
        if not result["transient_for"]:
            result.pop("transient_for")
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
            # Windows listed under an app name that app by its inventory ID.
            running = [
                {**w, "app": app_id} for w in windows if w["app"].casefold() in classes
            ]
            apps.append(
                {
                    "id": app_id,
                    "displayName": name,
                    "isRunning": bool(running),
                    "windows": running,
                }
            )
        return sorted(apps, key=lambda app: (app["displayName"].casefold(), app["id"]))

    def _bind(self, identifier: int) -> BoundWindow:
        window = self.connection.create_resource_object("window", identifier)
        window.change_attributes(event_mask=X.StructureNotifyMask)
        bound = self.windows.get(identifier) or BoundWindow(window=window)
        self.windows[identifier] = bound
        return bound

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
                "The window manager did not activate the selected window",
            )

    # ------------------------------------------------------------ AT-SPI tree

    def _ax_root(self, metadata: dict[str, Any]) -> tuple[str, str] | None:
        """Match an X11 window to its AT-SPI frame by process, title and geometry."""
        bus = self.atspi
        if bus is None:
            return None
        bus.pump()
        try:
            apps = bus.call(REGISTRY, ROOT, ACC, "GetChildren")[0]
        except Exception:  # noqa: BLE001 - the registry may be restarting
            return None
        candidates = []
        for app_bus, app_path in apps[:200]:
            pid = bus.pid(app_bus)
            if metadata.get("pid") and pid and pid != metadata["pid"]:
                continue
            candidates.append((app_bus, app_path))
        children = bus.many(
            [
                (app_bus, path, ACC, "GetChildren", None, None)
                for app_bus, path in candidates
            ]
        )
        dormant = [
            index
            for index, result in enumerate(children)
            if _ok(result)
            and all(path.endswith("/null") for _, path in result[0])
            and metadata.get("pid")
        ]
        if dormant:
            # Chromium/Electron expose their windows only after an assistive
            # client reads extended properties of the application, as Orca does.
            bus.many(
                [(*candidates[i], ACC, "GetAttributes", None, None) for i in dormant]
            )
            time.sleep(0.3)
            for index, result in zip(
                dormant,
                bus.many(
                    [(*candidates[i], ACC, "GetChildren", None, None) for i in dormant]
                ),
            ):
                children[index] = result
        frames = [
            (app_bus, path)
            for (app_bus, _), result in zip(candidates, children)
            if _ok(result)
            for child_bus, path in result[0][:100]
            if child_bus == app_bus
        ]
        details = bus.many(
            [
                request
                for frame_bus, path in frames
                for request in (
                    (frame_bus, path, PROPERTIES, "Get", (ACC, "Name"), "(ss)"),
                    (frame_bus, path, COMPONENT, "GetExtents", (SCREEN,), "(u)"),
                    (frame_bus, path, ACC, "GetState", None, None),
                )
            ]
        )
        best = None
        best_score = 0
        ambiguous = False
        for index, frame in enumerate(frames):
            name, extents, state = details[index * 3 : index * 3 + 3]
            if _ok(state) and _has(_states(state), "defunct"):
                continue
            # Closed dialogs can linger as hidden frames with the same title.
            score = 1 if _ok(state) and _has(_states(state), "showing") else 0
            if _ok(name) and name[0] and name[0] == metadata["title"]:
                score += 5
            if _ok(extents):
                x, y, width, height = extents[0]
                if (
                    abs(x - metadata["x"]) < 8
                    and abs(y - metadata["y"]) < 40
                    and abs(width - metadata["width"]) < 16
                    and abs(height - metadata["height"]) < 48
                ):
                    score += 3
            if score > best_score:
                best, best_score, ambiguous = frame, score, False
            elif score and score == best_score:
                if (
                    best is not None
                    and frame[0] == best[0]
                    and self._newer(frame[1], best[1]) is not None
                ):
                    # Toolkits such as Qt can leak the frame of a closed dialog;
                    # object paths grow, so the newest identical frame is live.
                    best = frame if self._newer(frame[1], best[1]) else best
                else:
                    ambiguous = True
        return None if ambiguous or best_score < 3 else best

    @staticmethod
    def _newer(path: str, other: str) -> bool | None:
        left, right = path.rsplit("/", 1)[-1], other.rsplit("/", 1)[-1]
        if not (left.isdigit() and right.isdigit()):
            return None
        return int(left) > int(right)

    def _root_for(self, bound: BoundWindow) -> tuple[str, str] | None:
        if bound.root is not None and self.atspi is not None:
            try:
                self.atspi.call(*bound.root, ACC, "GetRoleName", timeout=1000)
                return bound.root
            except Exception:  # noqa: BLE001 - rediscover a replaced frame
                bound.root = None
        bound.root = self._ax_root(self._metadata(bound.window))
        return bound.root

    def _fetch(
        self,
        bus_name: str,
        entries: list[tuple[str, str | None, int]],
        limit: int,
        nodes: dict[str, dict[str, Any]],
    ) -> bool:
        """Read subtrees breadth-first into ``nodes`` with one batch per level."""
        bus = self.atspi
        assert bus is not None
        level = [(path, parent, depth, False) for path, parent, depth in entries]
        truncated = False
        fresh: list[dict[str, Any]] = []
        while level:
            requests = []
            for path, _, _, _ in level:
                requests += [
                    (bus_name, path, ACC, "GetChildren", None, None),
                    (bus_name, path, PROPERTIES, "Get", (ACC, "Name"), "(ss)"),
                    (bus_name, path, ACC, "GetRoleName", None, None),
                    (bus_name, path, ACC, "GetState", None, None),
                ]
            results = bus.many(requests)
            following: list[tuple[str, str | None, int, bool]] = []
            for index, (path, parent, depth, attach) in enumerate(level):
                children, name, role, states = results[index * 4 : index * 4 + 4]
                if not _ok(role) or not _ok(states):
                    continue
                state = _states(states)
                if _has(state, "defunct"):
                    continue
                node = {
                    "path": path, "parent": parent, "depth": depth,
                    "role": role[0] or "unknown",
                    "name": _clean(name[0]) if _ok(name) else "",
                    "states": state, "children": [], "child_count": 0,
                    "value": "", "description": "", "actions": [], "labels": [],
                    "context": "",
                }  # fmt: skip
                nodes[path] = node
                fresh.append(node)
                if attach and parent in nodes:
                    nodes[parent]["children"].append(path)
                if not _ok(children) or depth >= MAX_DEPTH:
                    continue
                node["child_count"] = len(children[0])
                for child_bus, child_path in children[0]:
                    if child_bus != bus_name or child_path in nodes:
                        continue
                    if len(nodes) + len(following) >= limit:
                        truncated = True
                        break
                    following.append((child_path, path, depth + 1, True))
            level = following
        self._details(bus_name, fresh)
        return truncated

    def _walk(
        self, bus_name: str, root_path: str, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """A fresh subtree as an index-linked list (root first)."""
        nodes: dict[str, dict[str, Any]] = {}
        with contextlib.suppress(Exception):
            # Chromium/Electron enable their full tree for assistive clients
            # that read extended properties, as Orca does.
            self.atspi.call(bus_name, root_path, ACC, "GetAttributes", timeout=1000)
        truncated = self._fetch(bus_name, [(root_path, None, 0)], limit, nodes)
        return self._flatten(nodes, root_path), truncated

    @staticmethod
    def _flatten(nodes: dict[str, dict[str, Any]], start: str) -> list[dict[str, Any]]:
        order: list[dict[str, Any]] = []
        positions: dict[str, int] = {}
        stack = [start]
        while stack:
            path = stack.pop()
            node = nodes.get(path)
            if node is None or path in positions:
                continue
            positions[path] = len(order)
            order.append(node)
            stack.extend(reversed(node["children"]))
        flat = []
        for node in order:
            copy = dict(node)
            copy["parent"] = (
                -1 if node["path"] == start else positions.get(node["parent"], -1)
            )
            copy["children"] = [
                positions[c] for c in node["children"] if c in positions
            ]
            copy["labels"] = list(node["labels"])
            flat.append(copy)
        return flat

    def _details(self, bus_name: str, nodes: list[dict[str, Any]]) -> None:
        """Fetch values, actions, descriptions and labels where they matter."""
        bus = self.atspi
        assert bus is not None
        first: list[tuple] = []
        targets: list[tuple[int, str]] = []
        for index, node in enumerate(nodes):
            role, states, path = node["role"], node["states"], node["path"]
            node.update(value="", description="", actions=[], labels=[])
            control = role in INTERACTIVE_ROLES or any(
                _has(states, flag) for flag in ("focusable", "editable", "checkable")
            )
            if control or role in {
                "image",
                "icon",
                "dialog",
                "alert",
                "file chooser",
                "document web",
                "document frame",
            }:
                first.append(
                    (bus_name, path, PROPERTIES, "Get", (ACC, "Description"), "(ss)")
                )
                targets.append((index, "description"))
            if role not in STATIC_ROLES:
                first.append((bus_name, path, ACTION, "GetName", (0,), "(i)"))
                targets.append((index, "action0"))
            if control:
                first.append(
                    (bus_name, path, PROPERTIES, "Get", (ACTION, "NActions"), "(ss)")
                )
                targets.append((index, "nactions"))
            if role != "password text" and (
                _has(states, "editable")
                or role in {"terminal", "document text"}
                or (
                    not node["name"]
                    and not node["child_count"]
                    and role not in CONTAINERS
                )
            ):
                first.append(
                    (
                        bus_name,
                        path,
                        PROPERTIES,
                        "Get",
                        (TEXT, "CharacterCount"),
                        "(ss)",
                    )
                )
                targets.append((index, "characters"))
            if (
                role
                in {
                    "slider",
                    "spin button",
                    "spin box",
                    "progress bar",
                    "dial",
                    "level bar",
                    "scroll bar",
                }
                and role != "scroll bar"
            ):
                # Individual properties only: some ATK bridges abort the whole
                # application while serializing Properties.GetAll(Value).
                for prop in ("CurrentValue", "MinimumValue", "MaximumValue"):
                    first.append(
                        (bus_name, path, PROPERTIES, "Get", (VALUE, prop), "(ss)")
                    )
                    targets.append((index, prop))
            if not node["name"] and control:
                first.append((bus_name, path, ACC, "GetRelationSet", None, None))
                targets.append((index, "relations"))
        results = bus.many(first)
        second: list[tuple] = []
        followups: list[tuple[int, str, Any]] = []
        for (index, kind), result in zip(targets, results):
            if not _ok(result):
                continue
            node = nodes[index]
            path = node["path"]
            if kind == "description":
                node["description"] = _clean(result[0])
            elif kind == "action0":
                node["actions"] = [str(result[0])]
            elif kind == "nactions":
                for action in range(1, min(int(result[0]), 10)):
                    second.append((bus_name, path, ACTION, "GetName", (action,), "(i)"))
                    followups.append((index, "action", action))
            elif kind == "characters":
                count = int(result[0])
                if count > 0:
                    second.append(
                        (
                            bus_name,
                            path,
                            TEXT,
                            "GetText",
                            (0, min(count, MAX_VALUE)),
                            "(ii)",
                        )
                    )
                    followups.append((index, "text", count))
            elif kind in {"CurrentValue", "MinimumValue", "MaximumValue"}:
                node.setdefault("range", {})[kind] = result[0]
            elif kind == "relations":
                for relation, related in result[0]:
                    if relation == LABELLED_BY:
                        for target_bus, target_path in related[:3]:
                            second.append(
                                (
                                    target_bus,
                                    target_path,
                                    PROPERTIES,
                                    "Get",
                                    (ACC, "Name"),
                                    "(ss)",
                                )
                            )
                            followups.append((index, "label", None))
        for node in nodes:
            numbers = node.pop("range", None)
            if numbers and "CurrentValue" in numbers:
                text = _number(numbers["CurrentValue"])
                low, high = numbers.get("MinimumValue"), numbers.get("MaximumValue")
                if low is not None and high is not None and high > low:
                    text += f" ({_number(low)}–{_number(high)})"
                node["value"] = text
        for (index, kind, extra), result in zip(followups, bus.many(second)):
            if not _ok(result):
                continue
            node = nodes[index]
            if kind == "action":
                node["actions"].append(str(result[0]))
            elif kind == "text":
                value = _clean(result[0], multiline=True)
                if extra > MAX_VALUE:
                    value += f"… ({extra} chars)"
                node["value"] = value
            elif kind == "label" and _clean(result[0]):
                node["labels"].append(_clean(result[0]))

    def _refresh(self, bus_name: str, nodes: list[dict[str, Any]]) -> None:
        """Re-read changed properties of known nodes in place."""
        if not nodes:
            return
        bus = self.atspi
        assert bus is not None
        results = bus.many(
            [
                request
                for node in nodes
                for request in (
                    (bus_name, node["path"], PROPERTIES, "Get", (ACC, "Name"), "(ss)"),
                    (bus_name, node["path"], ACC, "GetRoleName", None, None),
                    (bus_name, node["path"], ACC, "GetState", None, None),
                )
            ]
        )
        for index, node in enumerate(nodes):
            name, role, states = results[index * 3 : index * 3 + 3]
            if _ok(name):
                node["name"] = _clean(name[0])
            if _ok(role):
                node["role"] = role[0] or "unknown"
            if _ok(states):
                node["states"] = _states(states)
        self._details(bus_name, nodes)

    def _update(
        self, mirror: dict[str, Any], changes: dict[str, Any], limit: int
    ) -> bool:
        """Apply change events to the mirror; False when a full read is cheaper."""
        nodes = mirror["nodes"]
        bus_name, root_path = mirror["root"]
        if any(path not in nodes for path in changes["documents"]):
            # A document was attached without children-changed events (Chromium
            # does this while enabling web accessibility): its place is unknown.
            return False
        structure = {path for path in changes["structure"] if path in nodes}
        props = {path for path in changes["props"] if path in nodes} - structure
        if not structure and not props:
            return True

        def covered(path: str) -> bool:
            parent = nodes[path]["parent"]
            while parent is not None:
                if parent in structure:
                    return True
                parent = nodes[parent]["parent"] if parent in nodes else None
            return False

        tops = [path for path in structure if not covered(path)]
        if root_path in tops or len(tops) > 300:
            return False
        for path in tops:
            node = nodes[path]
            parent, depth = node["parent"], node["depth"]
            pending = [path]
            while pending:
                removed = nodes.pop(pending.pop(), None)
                if removed is not None:
                    pending.extend(removed["children"])
            fresh: dict[str, dict[str, Any]] = {}
            if self._fetch(
                bus_name, [(path, parent, depth)], max(1, limit - len(nodes)), fresh
            ):
                mirror["truncated"] = True
            nodes.update(fresh)
            if path not in fresh and parent in nodes:
                nodes[parent]["children"] = [
                    c for c in nodes[parent]["children"] if c != path
                ]
        self._refresh(bus_name, [nodes[path] for path in props if path in nodes])
        mirror["version"] += 1
        return True

    # ------------------------------------------------------- tree presentation

    @staticmethod
    def _interactive(node: dict[str, Any]) -> bool:
        states = node["states"]
        if node["role"] in INTERACTIVE_ROLES:
            return True
        if any(
            _has(states, flag)
            for flag in ("focusable", "editable", "checkable", "expandable")
        ):
            return True
        return any(action.lower() not in IGNORED_ACTIONS for action in node["actions"])

    @staticmethod
    def _text_of(nodes: list[dict[str, Any]], index: int, *, last: bool = False) -> str:
        """The first (or last) visible text inside a subtree."""
        stack = [index]
        found = ""
        visited = 0
        while stack and visited < 80:
            visited += 1
            current = stack.pop(0) if not last else stack.pop()
            node = nodes[current]
            text = node["name"] or (
                node["value"] if node["role"] in STATIC_ROLES else ""
            )
            if text:
                if not last:
                    return text[:80]
                found = text[:80]
            children = node["children"]
            if last:
                stack.extend(children)
            else:
                stack[0:0] = children
        return found

    def _contexts(self, bus_name: str, nodes: list[dict[str, Any]]) -> None:
        """Name label-related controls; describe the rest by table, layout or neighbor."""
        need: list[int] = []
        for index, node in enumerate(nodes):
            if (
                node["name"]
                or node["role"] not in CONTEXT_ROLES
                or not self._interactive(node)
            ):
                continue
            if node["labels"]:
                node["name"] = ", ".join(node["labels"])
                continue
            if _has(node["states"], "multi_line"):
                continue  # editors and documents: neighbors do not label them
            if any(self._text_of(nodes, child) for child in node["children"]):
                continue
            node["context"] = self._table_context(nodes, index)
            if not node["context"]:
                need.append(index)
        if need:
            self._spatial_contexts(bus_name, nodes, need)
        groups: dict[tuple[int, str, str], list[int]] = {}
        for index, node in enumerate(nodes):
            if node["context"]:
                groups.setdefault(
                    (node["parent"], node["role"], node["context"]), []
                ).append(index)
        for members in groups.values():
            if len(members) > 1:
                for position, index in enumerate(members, 1):
                    nodes[index]["context"] += f" #{position}"

    def _spatial_contexts(
        self, bus_name: str, nodes: list[dict[str, Any]], need: list[int]
    ) -> None:
        """Use the label left of a control (its row) and the text above it (its column)."""
        texts: set[int] = set()
        for index in need[:200]:
            scope = index
            for _ in range(4):
                if nodes[scope]["parent"] < 0:
                    break
                scope = nodes[scope]["parent"]
            pending = [scope]
            found = 0
            while pending and found < 400:
                current = pending.pop()
                node = nodes[current]
                if node["role"] in TEXT_ROLES | HEADER_ROLES | CELL_ROLES | {
                    "heading",
                    "row header",
                } and (node["name"] or node["value"]):
                    texts.add(current)
                    found += 1
                pending.extend(node["children"])
        wanted = sorted(set(need[:200]) | texts)
        extents: dict[int, tuple[int, int, int, int]] = {}
        if self.atspi is not None and wanted:
            results = self.atspi.many(
                [
                    (
                        bus_name,
                        nodes[i]["path"],
                        COMPONENT,
                        "GetExtents",
                        (SCREEN,),
                        "(u)",
                    )
                    for i in wanted
                ]
            )
            for index, result in zip(wanted, results):
                if _ok(result) and result[0][2] > 0 and result[0][3] > 0:
                    extents[index] = tuple(result[0])
        # Text inside another control's own container (a chosen value, a chip)
        # labels that control, not its neighbors.
        controls = [0] * len(nodes)
        for position in range(len(nodes) - 1, -1, -1):
            item = nodes[position]
            own = int(item["role"] in CONTEXT_ROLES and self._interactive(item))
            controls[position] = own + sum(controls[c] for c in item["children"])

        def foreign(text: int, target: int) -> bool:
            lineage = set()
            current = target
            while current >= 0:
                lineage.add(current)
                current = nodes[current]["parent"]
            current = nodes[text]["parent"]
            for _ in range(3):
                if current < 0 or current in lineage:
                    return False
                if controls[current]:
                    return True
                current = nodes[current]["parent"]
            return False

        for index in need:
            node = nodes[index]
            box = extents.get(index)
            context = ""
            if box is not None:
                x, y, width, height = box
                cx, cy = x + width / 2, y + height / 2
                row, column = None, None
                row_gap = column_gap = float("inf")
                for text in texts:
                    other = extents.get(text)
                    if other is None or text == index or foreign(text, index):
                        continue
                    ox, oy, ow, oh = other
                    if (
                        abs((oy + oh / 2) - cy) <= max(height, oh) / 2
                        and ox + ow <= cx
                        and cx - (ox + ow) < row_gap
                    ):
                        row, row_gap = text, cx - (ox + ow)
                    if (
                        abs((ox + ow / 2) - cx) <= max(width, ow) / 2 + 4
                        and oy + oh <= cy
                        and cy - (oy + oh) < column_gap
                    ):
                        column, column_gap = text, cy - (oy + oh)
                parts = []
                for choice in (row, column):
                    if choice is not None:
                        label = nodes[choice]["name"] or nodes[choice]["value"]
                        if label and label not in parts:
                            parts.append(label[:80])
                context = " › ".join(parts)
            if not context and not node["value"]:
                context = self._preceding_text(nodes, index)
            node["context"] = context

    def _table_context(self, nodes: list[dict[str, Any]], index: int) -> str:
        cell = index
        for _ in range(4):
            cell = nodes[cell]["parent"]
            if cell < 0:
                return ""
            if nodes[cell]["role"] in CELL_ROLES:
                break
        else:
            return ""
        row = nodes[cell]["parent"]
        if row < 0 or nodes[row]["role"] not in ROW_ROLES:
            return ""
        cells = [
            c
            for c in nodes[row]["children"]
            if nodes[c]["role"] in CELL_ROLES | HEADER_ROLES | {"row header"}
        ]
        if cell not in cells:
            return ""
        column = cells.index(cell)
        table = nodes[row]["parent"]
        for _ in range(3):
            if table < 0 or nodes[table]["role"] in TABLE_ROLES:
                break
            table = nodes[table]["parent"]
        parts = []
        if cells[0] != cell:
            parts.append(self._text_of(nodes, cells[0]))
        if table >= 0:
            rows = []
            pending = list(nodes[table]["children"])
            while pending and len(rows) < 3:
                current = pending.pop(0)
                if nodes[current]["role"] in ROW_ROLES:
                    rows.append(current)
                elif nodes[current]["role"] not in CELL_ROLES:
                    pending[0:0] = nodes[current]["children"]
            for candidate in rows:
                headers = [
                    c
                    for c in nodes[candidate]["children"]
                    if nodes[c]["role"] in HEADER_ROLES | CELL_ROLES
                ]
                if (
                    candidate != row
                    and column < len(headers)
                    and (
                        any(nodes[c]["role"] in HEADER_ROLES for c in headers)
                        or candidate == rows[0]
                    )
                ):
                    parts.append(self._text_of(nodes, headers[column]))
                    break
        return " › ".join(part for part in parts if part)

    def _preceding_text(self, nodes: list[dict[str, Any]], index: int) -> str:
        current = index
        for _ in range(4):
            parent = nodes[current]["parent"]
            if parent < 0:
                return ""
            siblings = nodes[parent]["children"]
            position = siblings.index(current) if current in siblings else 0
            for sibling in reversed(siblings[:position]):
                if self._interactive(nodes[sibling]) and nodes[sibling]["name"]:
                    continue
                text = self._text_of(nodes, sibling, last=True)
                if text:
                    return text
            current = parent
        return ""

    @staticmethod
    def _flags(node: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Displayed flags and the full state vocabulary for queries."""
        states = node["states"]
        role = node["role"]
        shown = [
            flag
            for flag in ("focused", "selected", "expanded", "indeterminate")
            if _has(states, flag)
        ]
        if _has(states, "expandable") and not _has(states, "expanded"):
            shown.append("collapsed")
        if _has(states, "checked"):
            shown.append("checked")
        elif (
            _has(states, "checkable")
            or role
            in {"check box", "radio button", "check menu item", "radio menu item"}
        ) and not _has(states, "indeterminate"):
            shown.append("unchecked")
        if _has(states, "pressed"):
            shown.append("pressed")
        if _has(states, "editable"):
            shown.append("settable")
            if _has(states, "read_only"):
                shown.append("read-only")
        for flag, label in (
            ("required", "required"),
            ("invalid", "invalid"),
            ("modal", "modal"),
            ("is_default", "default"),
            ("busy", "busy"),
        ):
            if _has(states, flag):
                shown.append(label)
        if not (
            _has(states, "enabled") or _has(states, "sensitive")
        ) and role not in CONTAINERS | TEXT_ROLES | {
            "paragraph",
            "heading",
        }:
            shown.append("disabled")
        every = set(shown)
        every.update(name for name, bit in STATE_NAMES if states >> bit & 1)
        if "settable" in every:
            every.add("editable")
        if "enabled" not in every and "sensitive" not in every:
            every.add("disabled")  # GTK 4 reports only "sensitive"
        return shown, sorted(every)

    def _present(
        self, bound: BoundWindow, bus_name: str, nodes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Compact the raw tree into the agent-visible element list."""
        if not nodes:
            return []
        self._contexts(bus_name, nodes)
        for node in nodes:
            if node["role"] == "combo box" and not node["value"]:
                node["value"] = self._selected_option(nodes, node)

        # Hidden widgets (closed popovers, unmapped buttons) are not operable.
        # Toolkits that never report visibility keep everything.
        hide = _has(nodes[0]["states"], "visible")

        def compact(index: int, top: bool, inside_choice: bool = False) -> list[int]:
            node = nodes[index]
            choice = inside_choice or node["role"] in {"combo box", "list box"}
            kids: list[int] = []
            for child in node["children"]:
                kids.extend(compact(child, False, choice))
            if (
                hide
                and not top
                and not inside_choice
                and not _has(node["states"], "visible")
            ):
                # A hidden container can still hold named controls that report
                # themselves visible and accept actions (hidden toolbar items);
                # unnamed buttons and fields of closed popovers are noise.
                return [
                    kid
                    for kid in kids
                    if nodes[kid]["name"] and not _has(nodes[kid]["states"], "editable")
                ]
            if (
                len(kids) == 1
                and node["name"]
                and nodes[kids[0]]["role"] in TEXT_ROLES
                and not nodes[kids[0]]["out"]
                and nodes[kids[0]]["name"] == node["name"]
            ):
                kids = []
            node["out"] = kids
            named = bool(
                node["name"]
                or node["value"]
                or node["description"]
                or node["role"] in {"image", "icon"}
            )
            active = self._interactive(node)
            if top:
                return [index]
            if not (named or kids or active):
                return []
            if (
                node["role"] in TEXT_ROLES
                and not kids
                and not active
                and len(node["name"]) == 1
                and not node["name"].isalnum()
            ):
                return []  # list bullets and separators
            if (
                not active
                and not named
                and node["role"] in CONTAINERS | TEXT_ROLES | {"paragraph"}
            ):
                return kids
            if (
                not named
                and not kids
                and not node["context"]
                and (
                    not (
                        _has(node["states"], "enabled")
                        or _has(node["states"], "sensitive")
                    )
                    or node["role"] in CELL_ROLES
                )
            ):
                return []  # disabled placeholders and empty renderer cells
            if len(kids) > 1:
                seen: set[tuple[str, str]] = set()
                unique = []
                for kid in kids:
                    item = nodes[kid]
                    signature = (item["role"], item["name"])
                    if not item["out"] and item["name"] and signature in seen:
                        continue
                    seen.add(signature)
                    unique.append(kid)
                node["out"] = kids = unique
            if node["role"] in CELL_ROLES and not named and len(kids) == 1:
                return kids
            return [index]

        for node in nodes:
            node["out"] = []
        ordered: list[dict[str, Any]] = []

        def emit(
            index: int, depth: int, parent_id: int | None, folded: bool = False
        ) -> None:
            node = nodes[index]
            key = (bus_name, node["path"])
            identifier = bound.ids.get(key)
            if identifier is None:
                identifier = self.next_element
                self.next_element += 1
                bound.ids[key] = identifier
            bound.refs[identifier] = Ref(
                bus_name, node["path"], node["role"], node["name"]
            )
            shown, every = self._flags(node)
            role = node["role"]
            secondary = [
                action
                for position, action in enumerate(node["actions"])
                if action.lower() not in IGNORED_ACTIONS | {"setfocus"}
                and not action.lower().startswith("scroll")
                and not (position == 0 and action.lower() in DEFAULT_VERBS)
                and not (
                    role in CELL_ROLES | {"tree item", "list item"}
                    and action.lower() in {"edit", "activate", "expand or contract"}
                )
            ]
            role = node["role"]
            display_role = (
                "container"
                if role in CONTAINERS
                else "text"
                if role in TEXT_ROLES and not _has(node["states"], "editable")
                else role
            )
            info: list[str] = []
            if node["name"]:
                info.append(_short(node["name"]))  # full text stays in "name"
            if node["value"] and node["value"] != node["name"]:
                info.append(f"Value: {_short(node['value'])}")
            if (
                node["description"]
                and not self._echoes(node["name"], node["description"])
                and node["description"] != node["value"]
            ):
                info.append(f"Description: {_short(node['description'], 200)}")
            if node["context"]:
                info.append(f"Context: {node['context']}")
            if secondary:
                info.append(f"Secondary Actions: {', '.join(secondary)}")
            line = " ".join(
                part
                for part in (
                    display_role,
                    f"({', '.join(shown)})" if shown else "",
                    ", ".join(info),
                )
                if part
            )
            # Closed menus and drop-down lists fold their items: queries still
            # find them, but the printed tree shows only how many there are.
            folding = role in FOLDING_ROLES
            closed = {
                child for child in node["out"]
                if folding and not _has(nodes[child]["states"], "showing")
            }  # fmt: skip
            hidden, pending = 0, list(closed)
            while pending:
                current = pending.pop()
                hidden += nodes[current]["role"] in OPTION_ROLES
                pending.extend(nodes[current]["out"])
            if hidden:
                line += f" [{hidden} items]"
            line = line.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
            ordered.append(
                {
                    "folded": folded,
                    "id": identifier,
                    "parent": parent_id,
                    "depth": depth,
                    "role": role,
                    "name": node["name"],
                    "value": node["value"],
                    "description": node["description"],
                    "context": node["context"],
                    "states": every,
                    "actions": node["actions"],
                    "line": line,
                }
            )
            for child in node["out"]:
                emit(child, depth + 1, identifier, folded or child in closed)

        for index in compact(0, True):
            emit(index, 0, None)
        return ordered

    @staticmethod
    def _echoes(name: str, description: str) -> bool:
        """A tooltip that only restates the name ("Save" / "Save the document")."""
        word = re.sub(r"[\W_]+", " ", name).strip().casefold()
        return bool(word) and (
            word == description.casefold()
            or re.search(
                rf"\b{re.escape(word)}\b",
                re.sub(r"[\W_]+", " ", description).casefold(),
            )
            is not None
        )

    @staticmethod
    def _selected_option(nodes: list[dict[str, Any]], combo: dict[str, Any]) -> str:
        pending = list(combo["children"])
        visited = 0
        while pending and visited < 400:
            visited += 1
            node = nodes[pending.pop(0)]
            if (
                node["role"] in OPTION_ROLES
                and _has(node["states"], "selected")
                and node["name"]
            ):
                return node["name"]
            pending.extend(node["children"])
        return ""

    def _fallback(self, bound: BoundWindow, metadata: dict[str, Any]) -> dict[str, Any]:
        key = ("x11", str(bound.window.id))
        identifier = bound.ids.setdefault(key, self.next_element)
        if identifier == self.next_element:
            self.next_element += 1
        return {
            "source": "x11",
            "nodes": [
                {
                    "id": identifier, "parent": None, "depth": 0, "role": "window",
                    "name": metadata.get("title", ""), "value": "", "description": "",
                    "context": "", "states": [], "actions": [],
                    "line": f"window {metadata.get('title', '')}".strip(),
                }
            ],
            "truncated": False,
            "notice": (
                "This window has not exposed an AT-SPI accessibility tree. Use a "
                "screenshot and coordinates. Apps started with accessibility disabled "
                "may need reopening; Chromium/Electron may need "
                "--force-renderer-accessibility."
            ),
        }  # fmt: skip

    async def _tree(self, bound: BoundWindow, args: dict[str, Any]) -> dict[str, Any]:
        """Present the window (or a subtree) from an event-maintained mirror."""
        metadata = self._metadata(bound.window)
        if self.atspi is None:
            return self._fallback(bound, metadata)
        limit = min(
            HARD_MAX_NODES, max(10, int(args.get("maxNodes") or DEFAULT_MAX_NODES))
        )
        root = self._root_for(bound)
        if root is None:
            return self._fallback(bound, metadata)
        bus_name, root_path = root
        self.atspi.pump()
        started = time.monotonic()
        mirror = bound.mirror
        changes = self.atspi.take(bus_name)
        # Elements this driver acted on are re-read even if the toolkit sent
        # no change event (Qt combo boxes, for example).
        changes["structure"] |= {path for bus, path in self.touched if bus == bus_name}
        self.touched = {item for item in self.touched if item[0] != bus_name}
        reusable = (
            mirror is not None
            and mirror["root"] == root
            and mirror["limit"] == limit
            and self.atspi.listening
            and self._incremental(bus_name)
            and not args.get("fresh")
            and not changes["overflow"]
            and time.monotonic() - mirror["time"] < MIRROR_MAX_AGE
        )
        if not reusable or not self._update(mirror, changes, limit):
            nodes: dict[str, dict[str, Any]] = {}
            with contextlib.suppress(Exception):
                # Chromium/Electron enable their full tree for assistive
                # clients that read extended properties, as Orca does.
                self.atspi.call(bus_name, root_path, ACC, "GetAttributes", timeout=1000)
            truncated = self._fetch(bus_name, [(root_path, None, 0)], limit, nodes)
            if not bound.ax_observed:
                for _ in range(5):
                    if len(nodes) > 1:
                        break
                    await asyncio.sleep(0.1)
                    nodes = {}
                    truncated = self._fetch(
                        bus_name, [(root_path, None, 0)], limit, nodes
                    )
                bound.ax_observed = True
            if root_path not in nodes:
                return self._fallback(bound, metadata)
            version = (mirror or {}).get("version", 0) + 1
            mirror = bound.mirror = {
                "root": root, "limit": limit, "nodes": nodes, "truncated": truncated,
                "version": version, "time": time.monotonic(),
            }  # fmt: skip
        start = root_path
        root_id = args.get("root")
        if root_id is not None:
            ref = self._lookup(bound, root_id)
            start = ref.path
            if start not in mirror["nodes"]:
                flat, truncated = self._walk(ref.bus, ref.path, limit)
                if not flat:
                    raise DriverError(
                        "STALE_ELEMENT", f"Element {root_id} no longer exists"
                    )
                return self._result(bound, ref.bus, flat, truncated, started)
        key = (start, mirror["version"])
        if bound.cache and bound.cache["key"] == key:
            return bound.cache["result"]
        flat = self._flatten(mirror["nodes"], start)
        result = self._result(bound, bus_name, flat, mirror["truncated"], started)
        result["scanned"] = len(mirror["nodes"])
        bound.cache = {"key": key, "result": result}
        return result

    def _result(
        self,
        bound: BoundWindow,
        bus_name: str,
        flat: list[dict[str, Any]],
        truncated: bool,
        started: float,
    ) -> dict[str, Any]:
        result = {
            "source": "at_spi",
            "nodes": self._present(bound, bus_name, flat),
            "truncated": truncated,
            "scanned": len(flat),
            "elapsedMs": round((time.monotonic() - started) * 1000),
        }
        if truncated:
            result["notice"] = (
                f"Partial tree: stopped after {len(flat)} elements. Use find(), "
                "getAXState({ root }) or a larger maxNodes for the rest."
            )
        return result

    # ------------------------------------------------------------- elements

    def _ref(self, bound: BoundWindow, identifier: Any) -> Ref:
        ref = self._lookup(bound, identifier)
        self.touched.add((ref.bus, ref.path))
        return ref

    def _lookup(self, bound: BoundWindow, identifier: Any) -> Ref:
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            raise DriverError("INVALID_ARGUMENT", "An element ID must be an integer")
        ref = bound.refs.get(identifier)
        if ref is None:
            raise DriverError(
                "STALE_ELEMENT",
                f"Element {identifier} is unknown for this window; read the state or use find()",
            )
        if self.atspi is None:
            raise DriverError("UNSUPPORTED_ACTION", "Accessibility is unavailable")
        try:
            role, states = self.atspi.many(
                [
                    (ref.bus, ref.path, ACC, "GetRoleName", None, None),
                    (ref.bus, ref.path, ACC, "GetState", None, None),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - reported as stale
            raise DriverError(
                "STALE_ELEMENT", f"Element {identifier} no longer exists"
            ) from exc
        if not _ok(role) or not _ok(states) or _has(_states(states), "defunct"):
            raise DriverError(
                "STALE_ELEMENT",
                f"Element {identifier} ({ref.role} {ref.name!r}) no longer exists; find it again",
            )
        if role[0] != ref.role:
            raise DriverError(
                "STALE_ELEMENT", f"Element {identifier} was replaced; find it again"
            )
        return ref

    def _info(self, ref: Ref) -> dict[str, Any]:
        bus = self.atspi
        assert bus is not None
        states, interfaces = bus.many(
            [
                (ref.bus, ref.path, ACC, "GetState", None, None),
                (ref.bus, ref.path, ACC, "GetInterfaces", None, None),
            ]
        )
        names = {
            item.rsplit(".", 1)[-1]
            for item in (interfaces[0] if _ok(interfaces) else [])
        }
        actions: list[str] = []
        if "Action" in names:
            with contextlib.suppress(Exception):
                count = int(bus.get(ref.bus, ref.path, ACTION, "NActions"))
                results = bus.many(
                    [
                        (ref.bus, ref.path, ACTION, "GetName", (i,), "(i)")
                        for i in range(min(count, 16))
                    ]
                )
                actions = [str(r[0]) if _ok(r) else "" for r in results]
        return {"states": _states(states), "interfaces": names, "actions": actions}

    def _text(self, ref: Ref) -> str:
        bus = self.atspi
        assert bus is not None
        count = int(bus.get(ref.bus, ref.path, TEXT, "CharacterCount"))
        if count <= 0:
            return ""
        return str(
            bus.call(
                ref.bus,
                ref.path,
                TEXT,
                "GetText",
                (0, min(count, MAX_VALUE * 4)),
                "(ii)",
            )[0]
        ).replace(OBJECT, "")

    def _extents(self, ref: Ref) -> tuple[int, int, int, int]:
        assert self.atspi is not None
        try:
            return tuple(
                self.atspi.call(
                    ref.bus, ref.path, COMPONENT, "GetExtents", (SCREEN,), "(u)"
                )[0]
            )
        except Exception as exc:  # noqa: BLE001 - reported as an action failure
            raise DriverError(
                "UNSUPPORTED_ACTION", "The element has no on-screen position"
            ) from exc

    def _center(self, ref: Ref) -> tuple[int, int]:
        x, y, width, height = self._extents(ref)
        return x + width // 2, y + height // 2

    async def _visible_center(self, bound: BoundWindow, ref: Ref) -> tuple[int, int]:
        """Scroll an element into the window if needed and return its screen center."""
        wx, wy, ww, wh = self._geometry(bound.window)
        for attempt in range(2):
            x, y, width, height = self._extents(ref)
            cx, cy = x + width // 2, y + height // 2
            if width > 0 and height > 0 and wx <= cx < wx + ww and wy <= cy < wy + wh:
                return cx, cy
            if attempt == 0:
                with contextlib.suppress(Exception):
                    self.atspi.call(
                        ref.bus,
                        ref.path,
                        COMPONENT,
                        "ScrollTo",
                        (SCROLL_ANYWHERE,),
                        "(u)",
                    )
                await asyncio.sleep(0.25)
        raise DriverError(
            "ACTION_FAILED",
            "The element is not visible inside the window; scroll it into view first",
        )

    async def _focus(self, bound: BoundWindow, ref: Ref) -> None:
        bus = self.atspi
        assert bus is not None
        with contextlib.suppress(Exception):
            bus.call(ref.bus, ref.path, COMPONENT, "GrabFocus")
        for attempt in range(2):
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    state = _states(bus.call(ref.bus, ref.path, ACC, "GetState"))
                except Exception as exc:  # noqa: BLE001 - transient item-view objects
                    raise DriverError(
                        "STALE_ELEMENT",
                        f"{ref.role} {ref.name!r} disappeared while focusing it",
                    ) from exc
                if _has(state, "focused"):
                    return
                await asyncio.sleep(0.025)
            if attempt == 0:
                x, y = await self._visible_center(bound, ref)
                await self._click(bound, x, y, 1)
        raise DriverError(
            "ACTION_FAILED",
            "The element did not receive focus; check for a blocking dialog",
        )

    async def _wait_text(self, ref: Ref, expected: str, timeout: float = 1.0) -> str:
        deadline = time.monotonic() + timeout
        actual = ""
        while True:
            with contextlib.suppress(Exception):
                actual = self._text(ref)
            if self._same(actual, expected) or time.monotonic() >= deadline:
                return actual
            await asyncio.sleep(0.05)

    @staticmethod
    def _same(actual: str, expected: str) -> bool:
        def norm(value: str) -> str:
            return value.replace("\r\n", "\n").replace(OBJECT, "").strip("\n")

        return norm(actual) == norm(expected)

    async def _click_element(
        self, bound: BoundWindow, ref: Ref, args: dict[str, Any]
    ) -> None:
        button_name = args.get("mouseButton", "left")
        button = {"left": 1, "middle": 2, "right": 3, "l": 1, "m": 2, "r": 3}.get(
            button_name
        )
        count = args.get("clickCount", 1)
        if button is None or not isinstance(count, int) or not 1 <= count <= 100:
            raise DriverError(
                "INVALID_ARGUMENT", "Unsupported mouse button or click count"
            )
        plain = count == 1 and not args.get("durationMs") and not args.get("key")
        info = self._info(ref)
        lowered = [action.lower() for action in info["actions"]]
        bus = self.atspi
        assert bus is not None
        if plain and button == 1:
            if _has(info["states"], "editable") and ref.role in FIELD_ROLES:
                await self._focus(bound, ref)
                return
            # A click selects a row or cell; "activate" on those opens them.
            verbs = (
                ("click", "press", "select")
                if ref.role in CELL_ROLES | {"list item", "tree item", "icon"}
                else CLICK_ACTIONS
            )
            for name in verbs:
                if name in lowered:
                    if bus.call(
                        ref.bus,
                        ref.path,
                        ACTION,
                        "DoAction",
                        (lowered.index(name),),
                        "(i)",
                    )[0]:
                        return
                    break
        if plain and button == 3 and "showcontextmenu" in lowered:
            if bus.call(
                ref.bus,
                ref.path,
                ACTION,
                "DoAction",
                (lowered.index("showcontextmenu"),),
                "(i)",
            )[0]:
                return
        try:
            x, y = await self._visible_center(bound, ref)
        except DriverError:
            generic = [
                i
                for i, action in enumerate(lowered)
                if action in GENERIC_ACTIONS - {""}
            ]
            if plain and button == 1 and generic:
                bus.call(ref.bus, ref.path, ACTION, "DoAction", (generic[0],), "(i)")
                return
            raise
        await self._click(
            bound, x, y, button, count, args.get("durationMs", 0), args.get("key")
        )

    async def _set_value(
        self, bound: BoundWindow, ref: Ref, value: Any
    ) -> dict[str, Any]:
        bus = self.atspi
        assert bus is not None
        info = self._info(ref)
        states, interfaces = info["states"], info["interfaces"]
        if isinstance(value, bool):
            state = (
                "pressed"
                if ref.role == "toggle button" and not _has(states, "checkable")
                else "checked"
            )
            if ref.role in OPTION_ROLES and not _has(states, "checkable"):
                state = "selected"
            if _has(states, state) != value:
                await self._click_element(bound, ref, {})
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                    if (
                        _has(
                            _states(bus.call(ref.bus, ref.path, ACC, "GetState")), state
                        )
                        == value
                    ):
                        break
            current = _has(_states(bus.call(ref.bus, ref.path, ACC, "GetState")), state)
            if current != value:
                raise DriverError(
                    "ACTION_FAILED",
                    f"The element is still {'checked' if current else 'unchecked'}",
                )
            return {"checked": current}
        if isinstance(value, (int, float)) and "Value" in interfaces:
            bus.call(
                ref.bus,
                ref.path,
                PROPERTIES,
                "Set",
                (VALUE, "CurrentValue", self.atspi.GLib.Variant("d", float(value))),
                "(ssv)",
            )
            await asyncio.sleep(0.1)
            current = float(bus.get(ref.bus, ref.path, VALUE, "CurrentValue"))
            if abs(current - float(value)) > 1e-6:
                raise DriverError(
                    "ACTION_FAILED",
                    f"The control accepted {_number(current)} instead of {_number(value)}",
                )
            return {"value": current}
        if not isinstance(value, (str, int, float)):
            raise DriverError(
                "INVALID_ARGUMENT", "setValue expects a string, number or boolean"
            )
        text = str(value)
        if len(text.encode()) > MAX_INPUT_TEXT:
            raise DriverError("INVALID_ARGUMENT", "Text must be at most 64 KiB")
        if not _has(states, "editable") and ref.role in {
            "combo box",
            "list box",
            "list",
            "menu button",
            "push button",
            "page tab list",
            "tree",
            "table",
        }:
            return await self._select_option(bound, ref, text, info)
        if not _has(states, "editable") and "Text" not in interfaces:
            raise DriverError(
                "UNSUPPORTED_ACTION", f"Element is a {ref.role}, not an editable field"
            )
        if "EditableText" in interfaces:
            # Focus first, like typing would: a following pressKey("Return")
            # must reach this field.
            with contextlib.suppress(Exception):
                await self._focus(bound, ref)
            with contextlib.suppress(Exception):
                if bus.call(
                    ref.bus, ref.path, EDITABLE, "SetTextContents", (text,), "(s)"
                )[0]:
                    actual = await self._wait_text(ref, text, 0.5)
                    if self._same(actual, text):
                        with contextlib.suppress(Exception):
                            bus.call(
                                ref.bus,
                                ref.path,
                                TEXT,
                                "SetCaretOffset",
                                (len(actual),),
                                "(i)",
                            )
                        return {"value": actual}
        for attempt in range(2):
            await self._focus(bound, ref)
            count = 0
            with contextlib.suppress(Exception):
                count = int(bus.get(ref.bus, ref.path, TEXT, "CharacterCount"))
            selected = False
            if count and attempt == 0 and "Text" in interfaces:
                with contextlib.suppress(Exception):
                    if int(bus.call(ref.bus, ref.path, TEXT, "GetNSelections")[0]):
                        selected = bool(
                            bus.call(
                                ref.bus,
                                ref.path,
                                TEXT,
                                "SetSelection",
                                (0, 0, count),
                                "(iii)",
                            )[0]
                        )
                    else:
                        selected = bool(
                            bus.call(
                                ref.bus,
                                ref.path,
                                TEXT,
                                "AddSelection",
                                (0, count),
                                "(ii)",
                            )[0]
                        )
                    await asyncio.sleep(0.03)
                    start, end = bus.call(
                        ref.bus, ref.path, TEXT, "GetSelection", (0,), "(i)"
                    )
                    selected = selected and start == 0 and end >= count
            if count and not selected:
                await self._press(bound, "Control_L+a")
            if count:
                await self._press(bound, "BackSpace")
            if text:
                await self._paste_text(bound, text)
            actual = await self._wait_text(ref, text, 1.5)
            if self._same(actual, text):
                return {"value": actual}
        raise DriverError(
            "ACTION_FAILED",
            f"The field contains {_short(actual, 200)!r} instead of the requested value",
        )

    async def _select_option(
        self, bound: BoundWindow, ref: Ref, text: str, info: dict[str, Any]
    ) -> dict[str, Any]:
        """Choose an option of a combo box or list by its visible text."""
        bus = self.atspi
        assert bus is not None
        wanted = text.casefold().strip()

        def candidates(showing_only: bool = False) -> list[dict[str, Any]]:
            nodes, _ = self._walk(ref.bus, ref.path, 3000)
            options = [
                n for n in nodes[1:] if n["role"] in OPTION_ROLES and n["name"]
                and (not showing_only or _has(n["states"], "showing"))
            ]  # fmt: skip
            for root in self._popup_roots(bound, ref.bus):
                more, _ = self._walk(ref.bus, root, 3000)
                options += [
                    n
                    for n in more
                    if n["role"] in OPTION_ROLES
                    and n["name"]
                    and _has(n["states"], "showing")
                ]
            return options

        def choose(options: list[dict[str, Any]]) -> dict[str, Any] | None:
            for test in (
                lambda n: n["name"].casefold() == wanted,
                lambda n: n["name"].casefold().startswith(wanted),
                lambda n: wanted in n["name"].casefold(),
            ):
                matches = [n for n in options if test(n)]
                if len({m["name"] for m in matches}) == 1:
                    return matches[0]
                if len(matches) > 1:
                    raise DriverError(
                        "AMBIGUOUS",
                        "Several options match: "
                        + ", ".join(sorted({m["name"] for m in matches})[:10]),
                    )
            return None

        def state() -> tuple[str, bool]:
            nodes, _ = self._walk(ref.bus, ref.path, 3000)
            if not nodes:
                raise DriverError("STALE_ELEMENT", "The list disappeared")
            names = {
                n["name"].casefold() for n in nodes[1:] if n["role"] in OPTION_ROLES
            }
            # GTK/Qt combo names show the current choice; web combo names are labels.
            return nodes[0]["name"], nodes[0]["name"].casefold() in names

        async def activate(option: dict[str, Any], pointer: bool = False) -> None:
            target = Ref(ref.bus, option["path"], option["role"], option["name"])
            lowered = [a.lower() for a in self._info(target)["actions"]]
            if pointer and _has(option["states"], "showing"):
                x, y = self._center(target)
                await self._click(bound, x, y, 1)
                self._mark()
                await self._settle(bound)
                return
            for name in ("select", "click", "press", "activate"):
                if name in lowered:
                    bus.call(
                        target.bus,
                        target.path,
                        ACTION,
                        "DoAction",
                        (lowered.index(name),),
                        "(i)",
                    )
                    break
            else:
                return  # no semantic choice: use the popup below
            self._mark()
            await self._settle(bound)

        async def expand() -> None:
            lowered = [a.lower() for a in info["actions"]]
            for name in ("open", "expand", "showmenu", "press", "click"):
                if name in lowered:
                    bus.call(
                        ref.bus,
                        ref.path,
                        ACTION,
                        "DoAction",
                        (lowered.index(name),),
                        "(i)",
                    )
                    break
            else:
                x, y = await self._visible_center(bound, ref)
                await self._click(bound, x, y, 1)
            self._mark()
            await self._settle(bound)

        async def confirmed(option: dict[str, Any], reflects: bool) -> bool:
            deadline = time.monotonic() + 1.2
            while True:
                nodes, _ = self._walk(ref.bus, ref.path, 3000)
                if nodes:
                    top = nodes[0]
                    if reflects:
                        if top["name"].casefold() == option["name"].casefold():
                            return True
                    else:
                        own = " ".join(
                            filter(
                                None,
                                [
                                    top["name"],
                                    top["value"],
                                    self._selected_option(nodes, top),
                                ],
                            )
                        )
                        if option["name"].casefold() in own.casefold() or any(
                            n["name"] == option["name"]
                            and _has(n["states"], "selected")
                            for n in nodes[1:]
                        ):
                            return True
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(0.1)

        before, reflects = state()
        if reflects and before.casefold() == wanted:
            return {"value": before}
        option = choose(candidates())
        if option is not None:
            await activate(option)
            if await confirmed(option, reflects):
                return {"value": option["name"]}
        # Toolkits such as Qt only commit choices made in the open popup.
        await expand()
        options = candidates(showing_only=True) or candidates()
        option = choose(options)
        if option is None:
            names = sorted({n["name"] for n in options})
            with contextlib.suppress(Exception):
                await self._press(bound, "Escape")
            raise DriverError(
                "NOT_FOUND",
                f"No option named {text!r}. Options: {', '.join(names[:20]) or 'none found'}",
            )
        await activate(option, pointer=True)
        if await confirmed(option, reflects):
            return {"value": option["name"]}
        raise DriverError(
            "ACTION_FAILED", f"Selecting {option['name']!r} could not be confirmed"
        )

    def _popup_roots(self, bound: BoundWindow, bus_name: str) -> list[str]:
        """Showing popup windows of the same application (GTK/Qt combo menus)."""
        bus = self.atspi
        assert bus is not None
        try:
            frames = bus.call(bus_name, ROOT, ACC, "GetChildren")[0]
        except Exception:  # noqa: BLE001 - popups are optional
            return []
        frames = [
            path
            for child_bus, path in frames
            if child_bus == bus_name and (bound.root is None or path != bound.root[1])
        ]
        states = bus.many(
            [(bus_name, path, ACC, "GetState", None, None) for path in frames]
        )
        return [
            path
            for path, state in zip(frames, states)
            if _has(_states(state), "showing")
        ][:5]

    async def _select_text(
        self, bound: BoundWindow, ref: Ref, args: dict[str, Any]
    ) -> None:
        bus = self.atspi
        assert bus is not None
        needle = args.get("text")
        if not isinstance(needle, str) or not needle:
            raise DriverError("INVALID_ARGUMENT", "selectText needs the text to select")
        content = self._text(ref)
        prefix, suffix = args.get("prefix") or "", args.get("suffix") or ""
        matches = [
            m.start() for m in re.finditer(re.escape(needle), content)
            if content[: m.start()].endswith(prefix) and content[m.start() + len(needle):].startswith(suffix)
        ]  # fmt: skip
        if not matches:
            raise DriverError(
                "NOT_FOUND", f"{needle!r} does not occur in the element's text"
            )
        if len(matches) > 1:
            raise DriverError(
                "AMBIGUOUS",
                f"{needle!r} occurs {len(matches)} times; pass prefix or suffix",
            )
        start, end = matches[0], matches[0] + len(needle)
        await self._focus(bound, ref)
        kind = args.get("selectionType", "text")
        if kind == "text":
            if int(bus.call(ref.bus, ref.path, TEXT, "GetNSelections")[0]):
                ok = bus.call(
                    ref.bus, ref.path, TEXT, "SetSelection", (0, start, end), "(iii)"
                )[0]
            else:
                ok = bus.call(
                    ref.bus, ref.path, TEXT, "AddSelection", (start, end), "(ii)"
                )[0]
        elif kind in {"cursor_before", "cursor_after"}:
            ok = bus.call(
                ref.bus,
                ref.path,
                TEXT,
                "SetCaretOffset",
                (start if kind == "cursor_before" else end,),
                "(i)",
            )[0]
        else:
            raise DriverError(
                "INVALID_ARGUMENT",
                "selectionType must be text, cursor_before or cursor_after",
            )
        if not ok:
            raise DriverError(
                "ACTION_FAILED", "The application rejected the text selection"
            )

    # ------------------------------------------------------------ observation

    def _mark(self) -> None:
        self.action_at = time.monotonic()
        self.dirty = True

    async def _settle(self, bound: BoundWindow | None, max_wait: float = 1.5) -> None:
        """After input, wait until the application stops reporting changes."""
        if not self.dirty:
            return
        started = self.action_at
        bus_name = bound.root[0] if bound is not None and bound.root else None
        while True:
            now = time.monotonic()
            if self.atspi is None or not self.atspi.listening:
                if now - started >= 0.15:
                    break
            else:
                self.atspi.pump()
                last = (
                    self.atspi.activity.get(bus_name, 0.0)
                    if bus_name
                    else self.atspi.last_event
                )
                if now - started >= 0.1 and now - max(last, started) >= 0.12:
                    break
            if now - started >= max_wait:
                break
            await asyncio.sleep(0.02)
        self.dirty = False

    def _capture(self, bound: BoundWindow, args: dict[str, Any]) -> dict[str, Any]:
        x, y, width, height = self._geometry(bound.window)
        screen = self.root.get_geometry()
        if x < 0 or y < 0 or x + width > screen.width or y + height > screen.height:
            raise DriverError(
                "WINDOW_OFF_SCREEN",
                "Move the entire window onto the desktop before capturing it",
            )
        left, top, right, bottom = 0, 0, width, height
        region = args.get("region")
        if region is not None:
            if isinstance(region, dict):
                rx, ry, rw, rh = (
                    region["x"],
                    region["y"],
                    region["width"],
                    region["height"],
                )
                left, top = max(0, int(rx) - 8), max(0, int(ry) - 8)
                right, bottom = (
                    min(width, int(rx + rw) + 8),
                    min(height, int(ry + rh) + 8),
                )
            else:
                ox, oy, sx, sy = bound.transform
                rx, ry, rw, rh = (float(v) for v in region)
                left, top = max(0, int(ox + rx * sx)), max(0, int(oy + ry * sy))
                right, bottom = (
                    min(width, int(ox + (rx + rw) * sx)),
                    min(height, int(oy + (ry + rh) * sy)),
                )
            if right - left < 4 or bottom - top < 4:
                raise DriverError(
                    "INVALID_ARGUMENT", "The screenshot region is outside the window"
                )
        raw = bound.window.get_image(
            left, top, right - left, bottom - top, X.ZPixmap, 0xFFFFFFFF
        )
        image = Image.frombytes(
            "RGB", (right - left, bottom - top), raw.data, "raw", "BGRX"
        )
        limit = self.max_dimension
        if args.get("maxDimension"):
            limit = min(limit, max(200, int(args["maxDimension"])))
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
        if output.tell() > 2 * 1024 * 1024:
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=55)
        if output.tell() > 2 * 1024 * 1024:
            raise DriverError(
                "OUTPUT_LIMIT", "Screenshot exceeds 2 MiB; pass a smaller maxDimension"
            )
        bound.transform = (
            float(left), float(top),
            (right - left) / image.width, (bottom - top) / image.height,
        )  # fmt: skip
        return {
            "image": base64.b64encode(output.getvalue()).decode("ascii"),
            "width": image.width,
            "height": image.height,
            "region": [left, top, right - left, bottom - top]
            if region is not None
            else None,
            "scale": round((right - left) / image.width, 4),
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
        ox, oy, sx, sy = bound.transform
        x, y = ox + point[0] * sx, oy + point[1] * sy
        width, height = geometry[2:]
        if not 0 <= x < width or not 0 <= y < height:
            raise DriverError(
                "INVALID_ARGUMENT", "Coordinates are outside the observed screenshot"
            )
        return geometry[0] + round(x), geometry[1] + round(y)

    def _to_screenshot(
        self, bound: BoundWindow, rect: tuple[int, int, int, int]
    ) -> dict[str, float]:
        wx, wy, _, _ = self._geometry(bound.window)
        ox, oy, sx, sy = bound.transform
        x, y, width, height = rect
        return {
            "x": round((x - wx - ox) / sx, 1), "y": round((y - wy - oy) / sy, 1),
            "width": round(width / sx, 1), "height": round(height / sy, 1),
        }  # fmt: skip

    # ------------------------------------------------------------------ input

    def _keycodes(self, chord: str) -> list[int]:
        aliases = {
            "ctrl": "Control_L", "control": "Control_L", "alt": "Alt_L",
            "shift": "Shift_L", "super": "Super_L", "meta": "Super_L",
            "cmd": "Super_L", "win": "Super_L", "enter": "Return", "esc": "Escape",
            "del": "Delete", "ins": "Insert", "pgup": "Prior", "pageup": "Prior",
            "pgdn": "Next", "pagedown": "Next", "space": "space", "backspace": "BackSpace",
            "tab": "Tab", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
            "home": "Home", "end": "End", "escape": "Escape", "return": "Return",
            "delete": "Delete", "plus": "plus", "minus": "minus",
        }  # fmt: skip
        if not isinstance(chord, str) or not chord or len(chord) > 100:
            raise DriverError("INVALID_ARGUMENT", "A bounded X11 key chord is required")
        parts = chord.split("+") if chord != "+" else ["plus"]
        keys = []
        for raw in parts:
            key = raw.strip()
            keys.append(aliases.get(key.casefold(), key) if len(key) > 1 else key)
        result = []
        for key in keys:
            keysym = XK.string_to_keysym(key)
            if not keysym and len(key) == 1:
                keysym = ord(key) if ord(key) < 0x100 else 0x01000000 + ord(key)
            code = self.connection.keysym_to_keycode(keysym) if keysym else 0
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

    def _pointer_target(self, bound: BoundWindow, x: int, y: int) -> Any:
        """The bound window, or a popup/menu window of any app stacked above the point."""
        try:
            child = self.root.translate_coords(self.root, x, y).child
            if child and child != X.NONE and child.id != bound.window.id:
                attributes = child.get_attributes()
                if (
                    attributes.override_redirect
                    and attributes.map_state == X.IsViewable
                ):
                    return child
        except error.XError:
            pass
        return bound.window

    def _send_pointer(
        self,
        bound: BoundWindow,
        x: int,
        y: int,
        kind: str,
        button: int = 0,
        state: int = 0,
        target: Any = None,
    ) -> None:
        """Deliver window-relative pointer events without moving the desktop mouse."""
        target = target or bound.window
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
                    root=self.root, window=target, child=X.NONE,
                    root_x=x, root_y=y, event_x=x - origin_x, event_y=y - origin_y,
                    state=state, mode=X.NotifyNormal, detail=X.NotifyNonlinear, flags=1,
                ),
                propagate=False,
                event_mask=0,
            )  # fmt: skip
        target.send_event(
            event(
                time=int(time.monotonic() * 1000) & 0xFFFFFFFF,
                root=self.root, window=target, child=X.NONE,
                root_x=x, root_y=y, event_x=x - origin_x, event_y=y - origin_y,
                state=state, detail=button, same_screen=1,
            ),
            propagate=False,
            event_mask=0,
        )  # fmt: skip
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
        target = self._pointer_target(bound, x, y)
        for index in range(count):
            state = 0
            try:
                for code in codes:
                    self._send_key(bound, code, True, state)
                    state |= self._modifier(code)
                self._send_pointer(bound, x, y, "motion", state=state, target=target)
                self._send_pointer(bound, x, y, "press", button, state, target=target)
                await asyncio.sleep(duration_ms / 1000)
            finally:
                self._send_pointer(
                    bound,
                    x,
                    y,
                    "release",
                    button,
                    state | (1 << (7 + button)),
                    target=target,
                )
                for code in reversed(codes):
                    self._send_key(bound, code, False, state)
                    state &= ~self._modifier(code)
            if index + 1 < count:
                await asyncio.sleep(0.03)

    async def _press(
        self, bound: BoundWindow, chord: str, duration_ms: int = 25
    ) -> None:
        codes = self._keycodes(chord)
        state = 0
        try:
            for code in codes:
                self._send_key(bound, code, True, state)
                state |= self._modifier(code)
                await asyncio.sleep(0.033)
            await asyncio.sleep(duration_ms / 1000)
        finally:
            for code in reversed(codes):
                self._send_key(bound, code, False, state)
                state &= ~self._modifier(code)
                await asyncio.sleep(0.033)

    async def _type_characters(self, bound: BoundWindow, text: str) -> int:
        """Send printable keys, temporarily mapping characters absent from the keymap."""
        info = self.connection.display.info
        spare: int | None = None
        sent = 0
        try:
            for character in text:
                special = {"\n": XK.XK_Return, "\r": XK.XK_Return, "\t": XK.XK_Tab}
                keysym = special.get(character) or (
                    ord(character)
                    if ord(character) < 0x100
                    else 0x01000000 + ord(character)
                )
                mapped = [
                    (code, index)
                    for code, index in self.connection.keysym_to_keycodes(keysym)
                    if index in (0, 1)
                ]
                if mapped:
                    code, index = mapped[0]
                    state = X.ShiftMask if index == 1 else 0
                else:
                    if spare is None:
                        mapping = self.connection.get_keyboard_mapping(
                            info.min_keycode, info.max_keycode - info.min_keycode + 1
                        )
                        free = [
                            info.min_keycode + offset
                            for offset, symbols in enumerate(mapping)
                            if not any(symbols) and info.min_keycode + offset > 8
                        ]
                        if not free:
                            raise DriverError(
                                "UNSUPPORTED_KEY", f"No key for {character!r}"
                            )
                        spare = free[-1]
                    self.connection.change_keyboard_mapping(spare, [(keysym, keysym)])
                    self.connection.sync()
                    await asyncio.sleep(0.04)
                    code, state = spare, 0
                self._send_key(bound, code, True, state)
                self._send_key(bound, code, False, state)
                sent += 1
                if not mapped:
                    await asyncio.sleep(0.02)
        finally:
            if spare is not None:
                self.connection.change_keyboard_mapping(spare, [(0, 0)])
                self.connection.sync()
        return sent

    def cleanup_keys(self) -> None:
        """XSendEvent does not change the server's global key/button state."""
        self.connection.sync()

    def end(self) -> None:
        """Release one execution's native input ownership."""
        self.cleanup_keys()
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None
        self.execution_id = ""

    async def _paste_text(self, bound: BoundWindow, text: str) -> None:
        """Paste Unicode text through the selected window's native input route."""
        selection = self.connection.intern_atom("CLIPBOARD")
        previous_owner = self.connection.get_selection_owner(selection)
        previous_id = getattr(previous_owner, "id", 0)
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
            deadline = time.monotonic() + 1
            while True:
                owner = self.connection.get_selection_owner(selection)
                if getattr(owner, "id", 0) not in {0, previous_id}:
                    break
                if clipboard.returncode is not None or time.monotonic() >= deadline:
                    raise DriverError(
                        "CLIPBOARD_UNAVAILABLE", "Clipboard ownership was not acquired"
                    )
                await asyncio.sleep(0.01)
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

    # ------------------------------------------------------------------ files

    @staticmethod
    def _paths(values: Any) -> list[str]:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not values or len(values) > 500:
            raise DriverError("INVALID_ARGUMENT", "Pass one path or a list of paths")
        paths = []
        missing = []
        for value in values:
            if not isinstance(value, str) or not value:
                raise DriverError("INVALID_ARGUMENT", "Paths must be non-empty strings")
            path = os.path.abspath(os.path.expanduser(value))
            if not os.path.exists(path):
                missing.append(value)
            paths.append(path)
        if missing:
            raise DriverError("NOT_FOUND", "Missing files: " + ", ".join(missing[:10]))
        return paths

    @staticmethod
    def _stage(paths: list[str]) -> str:
        """Collect files from any folders in one fresh folder of symbolic links."""
        base = "/tmp/opencuria-cua-files"
        os.makedirs(base, mode=0o700, exist_ok=True)
        for entry in os.scandir(base):
            with contextlib.suppress(OSError):
                if time.time() - entry.stat(follow_symlinks=False).st_mtime > 86400:
                    shutil.rmtree(entry.path, ignore_errors=True)
        names = [os.path.basename(path) for path in paths]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise DriverError(
                "INVALID_ARGUMENT",
                "Several files share a name: " + ", ".join(duplicates[:5]),
            )
        folder = os.path.join(base, uuid.uuid4().hex[:12])
        os.mkdir(folder, 0o700)
        for path, name in zip(paths, names):
            os.symlink(path, os.path.join(folder, name))
        return folder

    def _find_dialog(
        self, bound: BoundWindow, before: set[int] | None
    ) -> dict[str, Any] | None:
        owner = self._metadata(bound.window)
        best, best_score = None, 0
        for window in self._list_windows():
            if window["id"] == bound.window.id or (
                before is not None and window["id"] in before
            ):
                continue
            public = self._public_window(window)
            score = 0
            if window.get("transient_for") == bound.window.id:
                score += 4
            if owner.get("pid") and window.get("pid") == owner["pid"]:
                score += 2
            if public["window_type"] == "dialog" or public["modal"]:
                score += 2
            if FILE_DIALOG_TITLE.search(window.get("title", "")):
                score += 1
            if before is not None:
                score += 1
            if score > best_score:
                best, best_score = window, score
        return best if best_score >= 3 else None

    async def _choose_files(
        self, bound: BoundWindow, args: dict[str, Any]
    ) -> dict[str, Any]:
        paths = self._paths(args.get("paths"))
        timeout = min(120.0, max(1.0, float(args.get("timeoutMs") or 15000) / 1000))
        before = None
        if (
            args.get("triggerElementId") is not None
            or args.get("triggerCoordinates") is not None
        ):
            before = {w["id"] for w in self._list_windows()}
            if args.get("triggerElementId") is not None:
                await self._click_element(
                    bound, self._ref(bound, args["triggerElementId"]), {}
                )
            else:
                x, y = self._point(bound, {"coordinates": args["triggerCoordinates"]})
                await self._click(bound, x, y, 1)
            self._mark()
        deadline = time.monotonic() + timeout
        dialog = None
        while dialog is None:
            dialog = self._find_dialog(bound, before)
            if dialog is None:
                if time.monotonic() >= deadline:
                    raise DriverError(
                        "NOT_FOUND", "No file dialog appeared for this window"
                    )
                await asyncio.sleep(0.1)
        target = self._bind(dialog["id"])
        await asyncio.sleep(0.3)
        root = None
        while root is None:
            root = self._root_for(target)
            if root is None:
                if time.monotonic() >= deadline:
                    raise DriverError(
                        "UNSUPPORTED_ACTION",
                        "The file dialog exposes no accessibility tree",
                    )
                await asyncio.sleep(0.2)
        staged = None
        location = paths[0]
        if len(paths) > 1:
            staged = self._stage(paths)
            location = staged + "/"
        entry = await self._location_entry(target, deadline)
        await self._fill_location(target, entry, location)
        names = [os.path.basename(path) for path in paths]
        await self._focus(target, entry)
        await self._press(target, "Return")
        if staged is None:
            closed = await self._wait_closed(target, 3)
        else:
            await self._select_all_files(target, names, deadline)
            closed = False
        if not closed:
            await self._press_accept(target)
            closed = await self._wait_closed(target, 4)
        if not closed:
            notice = self._dialog_text(target)
            raise DriverError(
                "ACTION_FAILED",
                "The file dialog did not accept the selection"
                + (f": {notice}" if notice else ""),
            )
        self._mark()
        return {"files": paths, "dialog": dialog.get("title", ""), "staged": staged}

    async def _location_entry(self, target: BoundWindow, deadline: float) -> Ref:
        """Open the typed-location field (GTK Ctrl+L) or use the dialog's file name field."""
        for attempt in range(3):
            tree = await self._tree(target, {"fresh": True})
            fields = [
                node for node in tree["nodes"]
                if "editable" in node["states"] and "showing" in node["states"]
                and node["role"] in {"text", "entry"}
            ]  # fmt: skip
            focused = [node for node in fields if "focused" in node["states"]]
            named = [
                node
                for node in fields
                if re.search(
                    r"location|file ?name|name|pfad|dateiname|ort",
                    f"{node['name']} {node['context']}",
                    re.I,
                )
            ]
            choice = (focused or named or fields)[:1]
            if choice and (attempt > 0 or focused or named):
                return self._ref(target, choice[0]["id"])
            await self._press(target, "Control_L+l")
            self._mark()
            await self._settle(target)
            if time.monotonic() >= deadline:
                break
        raise DriverError(
            "NOT_FOUND", "The file dialog has no location or file name field"
        )

    async def _fill_location(self, target: BoundWindow, entry: Ref, text: str) -> None:
        """Set the path and remove inline completion that GTK appends."""
        bus = self.atspi
        assert bus is not None
        info = self._info(entry)
        for _ in range(4):
            if "EditableText" in info["interfaces"]:
                bus.call(
                    entry.bus, entry.path, EDITABLE, "SetTextContents", (text,), "(s)"
                )
            else:
                await self._set_value(target, entry, text)
            await asyncio.sleep(0.35)
            actual = self._text(entry)
            if (
                actual != text
                and actual.startswith(text)
                and "EditableText" in info["interfaces"]
            ):
                bus.call(
                    entry.bus,
                    entry.path,
                    EDITABLE,
                    "DeleteText",
                    (len(text), len(actual)),
                    "(ii)",
                )
                await asyncio.sleep(0.15)
                actual = self._text(entry)
            if actual == text:
                return
        raise DriverError(
            "ACTION_FAILED", f"The location field shows {actual!r} instead of the path"
        )

    async def _select_all_files(
        self, target: BoundWindow, names: list[str], deadline: float
    ) -> None:
        wanted = set(names)
        while True:
            await asyncio.sleep(0.25)
            tree = await self._tree(target, {"fresh": True})
            cells = [
                node for node in tree["nodes"]
                if node["name"] in wanted and node["role"] in OPTION_ROLES | {"text", "label", "icon"}
            ]  # fmt: skip
            if {node["name"] for node in cells} >= wanted:
                break
            if time.monotonic() >= deadline:
                raise DriverError(
                    "ACTION_FAILED", "The staged files did not appear in the dialog"
                )
        # Click the first file like a user (item-view objects can be transient),
        # then select the whole staged folder.
        first = self._lookup(target, cells[0]["id"])
        x, y = self._center(first)
        await self._click(target, x, y, 1)
        await asyncio.sleep(0.15)
        await self._press(target, "Control_L+a")
        await asyncio.sleep(0.2)
        tree = await self._tree(target, {"fresh": True})
        selected = {
            node["name"]
            for node in tree["nodes"]
            if node["name"] in wanted and "selected" in node["states"]
        }
        if selected and selected < wanted:
            raise DriverError("UNSUPPORTED_ACTION", "This dialog accepts only one file")

    async def _press_accept(self, target: BoundWindow) -> None:
        tree = await self._tree(target, {"fresh": True})
        buttons = [
            n
            for n in tree["nodes"]
            if n["role"] in {"push button", "button"}
            and ("enabled" in n["states"] or "sensitive" in n["states"])
        ]
        choice = [n for n in buttons if "is-default" in n["states"]] or [
            n for n in buttons if ACCEPT_NAMES.match(n["name"].replace("_", ""))
        ]
        if choice:
            await self._click_element(target, self._ref(target, choice[0]["id"]), {})
        else:
            await self._press(target, "Return")

    async def _wait_closed(self, target: BoundWindow, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            try:
                if target.window.get_attributes().map_state != X.IsViewable:
                    return True
            except error.XError:
                return True
        return False

    def _dialog_text(self, target: BoundWindow) -> str:
        with contextlib.suppress(Exception):
            root = self._root_for(target)
            if root:
                nodes, _ = self._walk(*root, 800)
                texts = [
                    n["name"]
                    for n in nodes
                    if n["role"] in {"label", "static", "text", "alert"} and n["name"]
                ]
                return "; ".join(texts[-3:])[:300]
        return ""

    def _drop_files(
        self, bound: BoundWindow, x: int, y: int, paths: list[str]
    ) -> dict[str, Any]:
        """Drop files with the XDND protocol, as a file manager would."""
        uris = ["file://" + quote(path) for path in paths]
        payload = ("\r\n".join(uris) + "\r\n").encode()
        connection = display.Display()
        try:
            root = connection.screen().root
            atom = connection.intern_atom
            names = (
                "XdndAware", "XdndProxy", "XdndSelection", "XdndEnter", "XdndPosition",
                "XdndStatus", "XdndLeave", "XdndDrop", "XdndFinished", "XdndActionCopy",
                "TARGETS", "text/uri-list", "UTF8_STRING", "text/plain",
                "text/plain;charset=utf-8", "_OPENCURIA_TIME",
            )  # fmt: skip
            atoms = {name: atom(name) for name in names}
            target = connection.create_resource_object("window", bound.window.id)
            aware = target.get_full_property(atoms["XdndAware"], X.AnyPropertyType)
            if not aware or not len(aware.value):
                raise DriverError(
                    "UNSUPPORTED_ACTION", "This window does not accept dropped files"
                )
            version = min(5, int(aware.value[0]))
            proxy = target.get_full_property(atoms["XdndProxy"], Xatom.WINDOW)
            receiver = (
                connection.create_resource_object("window", int(proxy.value[0]))
                if proxy and len(proxy.value) else target
            )  # fmt: skip
            source = root.create_window(
                -10, -10, 1, 1, 0, 0, window_class=X.InputOnly,
                event_mask=X.PropertyChangeMask,
            )  # fmt: skip
            source.change_property(atoms["_OPENCURIA_TIME"], Xatom.STRING, 8, b"t")
            connection.flush()
            stamp = X.CurrentTime
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if connection.pending_events():
                    event = connection.next_event()
                    if event.type == X.PropertyNotify:
                        stamp = event.time
                        break
                else:
                    time.sleep(0.005)
            source.set_selection_owner(atoms["XdndSelection"], stamp)
            connection.flush()
            state = {"accepted": None, "finished": None, "served": 0}

            def message(kind: str, data: list[int]) -> None:
                receiver.send_event(
                    protocol.event.ClientMessage(
                        window=target,
                        client_type=atoms[kind],
                        data=(32, data + [0] * (5 - len(data))),
                    ),
                    event_mask=0,
                )
                connection.flush()

            def handle(event: Any) -> None:
                if event.type == X.SelectionRequest:
                    prop = event.property or event.target
                    if event.target == atoms["TARGETS"]:
                        event.requestor.change_property(
                            prop, Xatom.ATOM, 32,
                            [atoms["TARGETS"], atoms["text/uri-list"], atoms["UTF8_STRING"], atoms["text/plain"]],
                        )  # fmt: skip
                    elif event.target in {
                        atoms["text/uri-list"], atoms["UTF8_STRING"], atoms["text/plain"],
                        atoms["text/plain;charset=utf-8"],
                    }:  # fmt: skip
                        event.requestor.change_property(prop, event.target, 8, payload)
                        state["served"] += 1
                    else:
                        prop = X.NONE
                    event.requestor.send_event(
                        protocol.event.SelectionNotify(
                            time=event.time, requestor=event.requestor,
                            selection=event.selection, target=event.target, property=prop,
                        ),
                        event_mask=0,
                    )  # fmt: skip
                    connection.flush()
                elif event.type == X.ClientMessage and event.window.id == source.id:
                    _, data = event.data
                    if event.client_type == atoms["XdndStatus"]:
                        state["accepted"] = bool(data[1] & 1)
                    elif event.client_type == atoms["XdndFinished"]:
                        state["finished"] = bool(data[1] & 1) if version >= 5 else True

            def pump(until: Any, seconds: float) -> None:
                end = time.monotonic() + seconds
                while not until() and time.monotonic() < end:
                    if connection.pending_events():
                        handle(connection.next_event())
                    else:
                        time.sleep(0.005)

            message("XdndEnter", [source.id, version << 24, atoms["text/uri-list"]])
            for offset in (0, 1, 0):
                state["accepted"] = None
                message(
                    "XdndPosition",
                    [
                        source.id,
                        0,
                        ((x + offset) << 16) | (y & 0xFFFF),
                        stamp,
                        atoms["XdndActionCopy"],
                    ],
                )
                pump(lambda: state["accepted"] is not None, 2)
            if not state["accepted"]:
                message("XdndLeave", [source.id])
                raise DriverError(
                    "ACTION_FAILED",
                    "The drop target refused the files at this position",
                )
            message("XdndDrop", [source.id, 0, stamp])
            pump(lambda: state["finished"] is not None, 6)
            source.destroy()
            connection.flush()
            if state["finished"] is None:
                raise DriverError(
                    "ACTION_FAILED", "The drop was not confirmed by the application"
                )
            if not state["finished"]:
                raise DriverError(
                    "ACTION_FAILED", "The application rejected the dropped files"
                )
            return {"files": paths, "accepted": True}
        finally:
            connection.close()

    # --------------------------------------------------------------- dispatch

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
            if self.atspi is not None and not self.atspi.listening:
                for bound in self.windows.values():
                    bound.cache = None
                    bound.mirror = None
                try:
                    self.atspi.listen()
                except Exception as exc:  # noqa: BLE001 - settle falls back to a fixed wait
                    log.warning(
                        "computer_use_events_unavailable", error_type=type(exc).__name__
                    )
            return {}
        if op == "end":
            self.end()
            return {}
        if not self.execution_id:
            raise DriverError(
                "EXECUTION_ENDED", "Native operation requires an active execution"
            )
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
            await self._launch(app)
            self._mark()
            return {}
        if op in {"windows", "apps", "inventory"}:
            await self._settle(None)
            windows = [self._public_window(window) for window in self._list_windows()]
            if op == "windows":
                return {"items": windows}
            apps = self._apps(windows)
            return {"items": apps} if op == "apps" else {"apps": apps, "browsers": []}
        if op == "bind":
            identifier = args.get("windowId")
            if identifier not in {w["id"] for w in self._list_windows()}:
                raise DriverError(
                    "WINDOW_CLOSED", "Window is not present in the current inventory"
                )
            return {"identity": self._bind(identifier).identity}
        bound = self._bound(args)
        if op == "state":
            await self._settle(bound)
            result: dict[str, Any] = {}
            if args.get("text"):
                result.update(await self._tree(bound, args))
            if args.get("image"):
                region = args.get("region")
                if isinstance(region, int) and not isinstance(region, bool):
                    args = {
                        **args,
                        "region": self._to_window(
                            bound, self._extents(self._lookup(bound, region))
                        ),
                    }
                result.update(self._capture(bound, args))
            return result
        if op == "bounds":
            rect = self._extents(self._lookup(bound, args.get("elementId")))
            return self._to_screenshot(bound, rect)
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
            await asyncio.sleep(0.15)
            self._require_focus(bound)
            self._mark()
            return {}
        if op == "press_key":
            repeat = args.get("repeat", 1)
            if not isinstance(repeat, int) or not 1 <= repeat <= 100:
                raise DriverError(
                    "INVALID_ARGUMENT", "repeat must be between 1 and 100"
                )
            for _ in range(repeat):
                await self._press(
                    bound, args.get("key", ""), args.get("durationMs", 25)
                )
        elif op == "click":
            if args.get("elementId") is not None:
                await self._click_element(
                    bound, self._ref(bound, args["elementId"]), args
                )
            else:
                x, y = self._point(bound, args)
                button = {
                    "left": 1,
                    "middle": 2,
                    "right": 3,
                    "l": 1,
                    "m": 2,
                    "r": 3,
                }.get(args.get("mouseButton", "left"))
                count = args.get("clickCount", 1)
                if (
                    button is None
                    or not isinstance(count, int)
                    or not 1 <= count <= 100
                ):
                    raise DriverError(
                        "INVALID_ARGUMENT", "Unsupported mouse button or click count"
                    )
                await self._click(
                    bound,
                    x,
                    y,
                    button,
                    count,
                    args.get("durationMs", 0),
                    args.get("key"),
                )
        elif op == "focus":
            await self._focus(bound, self._ref(bound, args.get("elementId")))
        elif op == "secondary":
            ref = self._ref(bound, args.get("elementId"))
            wanted = str(args.get("action", "")).casefold()
            actions = self._info(ref)["actions"]
            lowered = [action.casefold() for action in actions]
            if wanted not in lowered:
                raise DriverError(
                    "UNSUPPORTED_ACTION",
                    f"Element {args.get('elementId')} has no action {args.get('action')!r}; available: "
                    + (", ".join(a for a in actions if a) or "none"),
                )
            if not self.atspi.call(
                ref.bus, ref.path, ACTION, "DoAction", (lowered.index(wanted),), "(i)"
            )[0]:
                raise DriverError("ACTION_FAILED", "Accessibility action was rejected")
        elif op == "type_text":
            text = args.get("text")
            if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_INPUT_TEXT:
                raise DriverError(
                    "INVALID_ARGUMENT", "Text must be a string of at most 64 KiB"
                )
            if args.get("elementId") is not None:
                ref = self._ref(bound, args["elementId"])
                info = self._info(ref)
                if (
                    "EditableText" not in info["interfaces"]
                    or "Text" not in info["interfaces"]
                ):
                    if not _has(info["states"], "editable"):
                        raise DriverError(
                            "UNSUPPORTED_ACTION", "Selected element is not editable"
                        )
                    # Chromium exposes editable web fields without EditableText.
                    await self._focus(bound, ref)
                    await self._paste_text(bound, text)
                else:
                    self._insert(ref, text)
            else:
                await self._paste_text(bound, text)
        elif op == "set_value":
            result = await self._set_value(
                bound, self._ref(bound, args.get("elementId")), args.get("value")
            )
            self._mark()
            return result
        elif op == "select_text":
            await self._select_text(
                bound, self._ref(bound, args.get("elementId")), args
            )
        elif op == "type_characters":
            text = args.get("text")
            if not isinstance(text, str) or len(text.encode()) > MAX_INPUT_TEXT:
                raise DriverError("INVALID_ARGUMENT", "Printable text is required")
            sent = await self._type_characters(bound, text)
            self._mark()
            return {"charactersSent": sent}
        elif op == "scroll":
            if args.get("elementId") is not None:
                ref = self._ref(bound, args["elementId"])
                x, y = await self._visible_center(bound, ref)
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
                or not 1 <= pixels <= 100000
            ):
                raise DriverError(
                    "INVALID_ARGUMENT",
                    "Scroll requires a direction and 1..100000 pixels",
                )
            for _ in range(min(1000, max(1, int(pixels // 40)))):
                await self._click(bound, x, y, button)
        elif op == "scroll_into_view":
            ref = self._ref(bound, args.get("elementId"))
            if not self.atspi.call(
                ref.bus, ref.path, COMPONENT, "ScrollTo", (SCROLL_ANYWHERE,), "(u)"
            )[0]:
                raise DriverError(
                    "ACTION_FAILED", "The application could not scroll to the element"
                )
        elif op == "drag":
            points = []
            for key in ("from", "to"):
                if args.get(f"{key}ElementId") is not None:
                    points.append(
                        await self._visible_center(
                            bound, self._ref(bound, args[f"{key}ElementId"])
                        )
                    )
                else:
                    points.append(self._point(bound, args, key))
            start, end = points
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
        elif op == "choose_files":
            result = await self._choose_files(bound, args)
            self._mark()
            return result
        elif op == "drop_files":
            paths = self._paths(args.get("paths"))
            if args.get("elementId") is not None:
                x, y = await self._visible_center(
                    bound, self._ref(bound, args["elementId"])
                )
            else:
                x, y = self._point(bound, args)
            result = await asyncio.to_thread(self._drop_files, bound, x, y, paths)
            self._mark()
            return result
        else:
            raise DriverError(
                "UNKNOWN_OPERATION", "Unsupported native desktop operation"
            )
        self._mark()
        return {}

    async def _launch(self, app: Any) -> None:
        """Start a desktop entry and report an immediate failure with its reason."""
        import shlex
        import subprocess
        import tempfile

        command = re.sub(r"%[fFuUdDnNickvm]", "", app.get_commandline() or "").strip()
        if not command or app.get_boolean("DBusActivatable"):
            app.launch([], None)
            return
        arguments = shlex.split(command)
        for attempt in range(2):
            log_file = tempfile.TemporaryFile()
            # A detached Popen: asyncio would kill the app when this driver exits.
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                start_new_session=True,
                close_fds=True,
            )
            deadline = time.monotonic() + 2.5
            while process.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            code = process.poll()
            if code is None:
                return  # still running: the window will appear
            log_file.seek(0)
            errors = log_file.read()[-4000:].decode("utf-8", errors="replace")
            if code == 0:
                return  # handed over to an already running instance
            if attempt == 0 and os.geteuid() == 0 and "--no-sandbox" in errors:
                # Chromium-based apps refuse to run as root without this flag;
                # the workspace VM is the isolation boundary.
                arguments.append("--no-sandbox")
                continue
            reason = " ".join(
                line.strip() for line in errors.splitlines()[-3:] if line.strip()
            )
            raise DriverError(
                "APP_FAILED",
                f"{app.get_name()} exited with code {code} right after starting"
                + (f": {reason[:400]}" if reason else ""),
            )

    async def pump_events(self) -> None:
        """Drain accessibility events between requests so caches stay accurate."""
        while True:
            await asyncio.sleep(0.25)
            if self.atspi is not None and self.atspi.listening:
                with contextlib.suppress(Exception):
                    self.atspi.pump()

    def _insert(self, ref: Ref, text: str) -> None:
        """Insert at the caret through EditableText, replacing a selection."""
        bus = self.atspi
        assert bus is not None
        position = max(0, int(bus.get(ref.bus, ref.path, TEXT, "CaretOffset")))
        for index in reversed(
            range(int(bus.call(ref.bus, ref.path, TEXT, "GetNSelections")[0]))
        ):
            start, end = bus.call(
                ref.bus, ref.path, TEXT, "GetSelection", (index,), "(i)"
            )
            if end > start:
                position = start
                if not bus.call(
                    ref.bus, ref.path, EDITABLE, "DeleteText", (start, end), "(ii)"
                )[0]:
                    raise DriverError(
                        "ACTION_FAILED", "Selected text could not be replaced"
                    )
        insertion_length, offset_length = self._text_lengths(ref, text)
        if not bus.call(
            ref.bus,
            ref.path,
            EDITABLE,
            "InsertText",
            (position, text, insertion_length),
            "(isi)",
        )[0]:
            raise DriverError("ACTION_FAILED", "Text insertion was rejected")
        with contextlib.suppress(Exception):
            bus.call(
                ref.bus,
                ref.path,
                TEXT,
                "SetCaretOffset",
                (position + offset_length,),
                "(i)",
            )

    def _incremental(self, bus_name: str) -> bool:
        """Only toolkits with complete change events may be read incrementally."""
        toolkit = self._toolkit(bus_name)
        return any(
            name in toolkit for name in ("chrom", "gtk", "gail", "gecko", "electron")
        )

    def _toolkit(self, bus_name: str) -> str:
        if bus_name not in self.toolkits:
            name = ""
            with contextlib.suppress(Exception):
                name = str(
                    self.atspi.get(
                        bus_name, ROOT, "org.a11y.atspi.Application", "ToolkitName"
                    )
                ).casefold()
            self.toolkits[bus_name] = name
        return self.toolkits[bus_name]

    def _text_lengths(self, ref: Ref, text: str) -> tuple[int, int]:
        """Use the bridge's insertion-length and character-offset conventions."""
        toolkit = self._toolkit(ref.bus)
        if toolkit.startswith("qt"):
            # Qt 5's AT-SPI bridge applies InsertText.length to QString.resize,
            # although AT-SPI specifies UTF-8 bytes. Bytes append garbage for
            # non-ASCII input; QString and Qt text offsets use UTF-16 units.
            units = len(text.encode("utf-16-le")) // 2
            return units, units
        return len(text.encode("utf-8")), len(text)

    def _to_window(
        self, bound: BoundWindow, rect: tuple[int, int, int, int]
    ) -> dict[str, int]:
        wx, wy, _, _ = self._geometry(bound.window)
        x, y, width, height = rect
        return {"x": x - wx, "y": y - wy, "width": width, "height": height}


def error_reply(exc: BaseException) -> dict[str, Any]:
    """An actionable protocol error; raw D-Bus failures are explained."""
    code, message = getattr(exc, "code", None), str(exc)
    if not isinstance(exc, DriverError) and any(
        name in f"{type(exc).__name__} {message}"
        for name in ("BadDrawable", "BadWindow")
    ):
        code, message = (
            "WINDOW_CLOSED",
            (
                "The window closed during the operation; find the window that replaced "
                "it with cua.listWindows() and bind it with cua.getApp(...)"
            ),
        )
    elif not isinstance(exc, DriverError) and "UnknownObject" in message:
        code, message = (
            "STALE_ELEMENT",
            (
                "An accessibility element disappeared during the operation; "
                "read the state again and retry"
            ),
        )
    elif not isinstance(exc, DriverError) and "g-dbus-error" in message:
        code, message = (
            "ACCESSIBILITY_ERROR",
            (
                "The application did not answer an accessibility request: "
                + message.rsplit(":", 1)[-1].strip()[:300]
            ),
        )
    return {
        "ok": False,
        "code": code if isinstance(code, str) else "NATIVE_ERROR",
        "error": message[:1500] or type(exc).__name__,
        "effect_status": "unknown",
    }


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
    pump: asyncio.Task | None = None
    try:
        driver = LinuxDriver()
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        pump = asyncio.create_task(driver.pump_events())
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
                reply = {"id": identifier, **error_reply(exc)}
            protocol_output.write(json.dumps(reply, ensure_ascii=False) + "\n")
            protocol_output.flush()
    except Exception as exc:  # noqa: BLE001 - keep native diagnostics on stderr
        log.error("computer_use_driver_failed", error_type=type(exc).__name__)
    finally:
        if pump is not None:
            pump.cancel()
        if driver is not None:
            driver.end()
            if driver.atspi is not None:
                with contextlib.suppress(Exception):
                    driver.atspi.unlisten()
            driver.connection.close()
        protocol_output.close()


if __name__ == "__main__":
    asyncio.run(main())
