"""Readable timeline of a pointing or app recording, the entry point for agents.

Every spoken phrase carries its interval; pointers, taps, screens, shown
images and file lines, and app events follow in time order, each naming the
frame, image or file it refers to. The JSON files next to it keep the
complete data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .voice_schemas import (
    VoiceAppEvent,
    VoiceElement,
    VoiceFrame,
    VoicePointer,
    VoiceRecordingIn,
    VoiceTarget,
    VoiceView,
)

# A press that moved less than this (frame pixels) is a tap, not a swipe.
_TAP_SLOP = 24
# On an image, at least this share of its longer side.
_IMAGE_SLOP = 0.02
# A press held this long is a long press.
_LONG_PRESS_MS = 600
# Hovering pointers (webapp) get at most one line per this interval.
_HOVER_INTERVAL_MS = 1000
_SCREEN_TEXTS = 12


@dataclass(frozen=True)
class _Line:
    """One timeline entry; ``order`` keeps simultaneous entries stable."""

    t_ms: int
    order: int
    text: str


def _clock(ms: int) -> str:
    """``mm:ss.s`` from the start of the recording."""
    tenths = max(0, ms) // 100
    minutes, tenths = divmod(tenths, 600)
    return f"{minutes:02d}:{tenths // 10:02d}.{tenths % 10}"


def _duration(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _quoted(text: str) -> str:
    """Text on one line in double quotes."""
    flat = " ".join(text.split()).replace('"', "'")
    return f'"{flat}"'


def _frame_name(path: str, root: str) -> str:
    prefix = f"{root}/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _point(x: float | None, y: float | None) -> str:
    return f"{round(x or 0)},{round(y or 0)}"


def _describe_element(element: VoiceElement | None) -> str:
    """E.g. ``button "Add" (testID add-button)``; empty without details."""
    if element is None:
        return ""
    name = element.text or element.label
    kind = element.role or (element.type and element.type.split(".")[-1]) or ""
    parts = [kind] if kind else []
    if name:
        parts.append(_quoted(name))
    details = []
    if element.label and element.text and element.label != element.text:
        details.append(f"label {_quoted(element.label)}")
    if element.test_id:
        details.append(f"testID {element.test_id}")
    if element.interactive is False:
        details.append("not tappable")
    if details:
        parts.append(f"({', '.join(details)})")
    return " ".join(parts)


def _line(pointer: VoicePointer) -> str:
    """``line 17`` (with its column when known) of a file pointer."""
    column = f", column {round(pointer.x or 0) + 1}" if pointer.x else ""
    return f"line {round(pointer.y or 0) + 1}{column}"


def _where(pointer: VoicePointer, root: str) -> str:
    target = pointer.target
    if target is None:
        return ""
    if target.kind == "file":
        return f"{_line(pointer)} of {target.path}"
    surface = target.path if target.kind == "image" else _frame_name(target.path, root)
    return f"at {_point(pointer.x, pointer.y)} on {surface}"


def _moved(start: VoicePointer, end: VoicePointer) -> bool:
    """A press that went somewhere else: another line, or beyond the slop."""
    target = end.target
    if target is None:
        return False
    if target.kind == "file":
        return round(end.y or 0) != round(start.y or 0)
    slop = _TAP_SLOP
    if target.kind == "image":
        slop = max(slop, round(_IMAGE_SLOP * max(target.width, target.height)))
    return math.dist((start.x or 0, start.y or 0), (end.x or 0, end.y or 0)) >= slop


def _press_lines(pointers: list[VoicePointer], root: str) -> list[_Line]:
    """Taps, long presses, pointing and swipes of a finger (mobile)."""
    lines: list[_Line] = []
    start: VoicePointer | None = None
    last: VoicePointer | None = None

    def finish(end: VoicePointer | None) -> None:
        if start is None:
            return
        end = end or last or start
        moved = _moved(start, end)
        held = end.t_ms - start.t_ms >= _LONG_PRESS_MS
        pointing = start.mode == "point"
        verb = (
            ("point and drag" if pointing else "swipe")
            if moved
            else ("point" if pointing else ("long press" if held else "tap"))
        )
        target = start.target
        if target is not None and target.kind == "file":
            text = f"{verb}  {_where(start, root)}"
            if moved:
                text += f" to {_line(end)}"
            quoted = start.element.text if start.element else ""
            if quoted:
                text += f"  {_quoted(quoted)}"
        else:
            what = _describe_element(start.element)
            text = (
                f"{verb}  {what + '  ' if what else ''}{_where(start, root)}"
            ).rstrip()
            if moved:
                text += f" to {_point(end.x, end.y)}"
        lines.append(_Line(start.t_ms, 2, text))

    for pointer in pointers:
        if pointer.event == "down":
            finish(None)
            start = pointer
            last = pointer
        elif pointer.event in {"move", "up"} and start is not None:
            if pointer.target is not None:
                last = pointer
            if pointer.event == "up":
                finish(pointer if pointer.target is not None else last)
                start = None
        elif pointer.event == "leave" and start is not None:
            finish(last)
            start = None
    finish(None)
    return lines


def _hover_lines(pointers: list[VoicePointer], root: str) -> list[_Line]:
    """Mouse positions on images and the desktop (webapp), thinned out."""
    lines: list[_Line] = []
    previous_surface: str | None = None
    previous_t = -_HOVER_INTERVAL_MS
    for pointer in pointers:
        if pointer.event == "leave" or pointer.target is None:
            if previous_surface is not None:
                lines.append(_Line(pointer.t_ms, 2, "pointer left the image/desktop"))
            previous_surface = None
            continue
        surface = pointer.target.path
        if pointer.event in {"down", "up"}:
            verb = "click" if pointer.event == "down" else "release"
            lines.append(_Line(pointer.t_ms, 2, f"{verb}  {_where(pointer, root)}"))
        elif (
            surface != previous_surface
            or pointer.t_ms - previous_t >= _HOVER_INTERVAL_MS
        ):
            lines.append(_Line(pointer.t_ms, 2, f"pointer  {_where(pointer, root)}"))
            previous_t = pointer.t_ms
        previous_surface = surface
    return lines


def _describe_view(target: VoiceTarget, view: VoiceView) -> str:
    region = view.region
    if target.kind == "file":
        if region is None:
            return f"{target.path}  (all {target.height} lines)"
        lines = f"lines {region[1] + 1}–{region[1] + region[3]} of {target.height}"
        if view.approximate:
            return f"{target.path}  around {lines} (rendered preview)"
        return f"{target.path}  {lines}"
    size = f"{target.width}×{target.height}"
    if region is None:
        return f"{target.path} ({size})"
    return (
        f"{target.path} ({size}), visible {region[0]},{region[1]} "
        f"{region[2]}×{region[3]}"
    )


def _view_lines(views: list[VoiceView]) -> list[_Line]:
    """Images and files the user showed, and when they went back."""
    lines: list[_Line] = []
    previous: VoiceTarget | None = None
    for view in views:
        if view.target is None:
            if previous is not None:
                lines.append(_Line(view.t_ms, 0, f"closed  {previous.path}"))
            previous = None
            continue
        lines.append(_Line(view.t_ms, 0, f"view  {_describe_view(view.target, view)}"))
        previous = view.target
    return lines


def _frame_line(frame: VoiceFrame, root: str, app: bool) -> _Line:
    name = _frame_name(frame.path, root)
    if not app:
        return _Line(frame.t_ms, 0, f"desktop frame  {name}")
    shown = " · ".join(frame.texts[:_SCREEN_TEXTS])
    more = " …" if len(frame.texts) > _SCREEN_TEXTS else ""
    texts = f"  shows: {shown}{more}" if shown else ""
    return _Line(frame.t_ms, 0, f"screen  {name}{texts}")


def _event_line(event: VoiceAppEvent) -> _Line:
    if event.kind == "network":
        method = event.method or "GET"
        if event.status:
            outcome = f"→ {event.status}"
        elif event.message:
            outcome = f"→ failed: {' '.join(event.message.split())}"
        else:
            outcome = "→ no response"
        took = f" ({event.duration_ms} ms)" if event.duration_ms is not None else ""
        return _Line(event.t_ms, 3, f"request  {method} {event.url} {outcome}{took}")
    message = " ".join(event.message.split())
    if event.kind == "error":
        return _Line(event.t_ms, 3, f"app error  {message}")
    return _Line(event.t_ms, 3, f"app log ({event.level or 'info'})  {message}")


def render_recording_timeline(
    payload: VoiceRecordingIn, root: str, saved_at: datetime
) -> str:
    """Markdown timeline of a validated recording stored under ``root``."""
    app = payload.app
    is_app = app is not None or any(
        pointer.target is not None and pointer.target.kind == "app"
        for pointer in payload.pointers
    )
    if app is not None:
        version = f" {app.version}" if app.version else ""
        platform = "iOS" if app.platform == "ios" else "Android"
        title = f"# App recording: {app.name}{version} ({platform})"
    else:
        title = "# App recording" if is_app else "# Pointing recording"
    timing = (
        "speech timing from the speech model"
        if payload.timing == "provider"
        else "approximate speech timing (the speech model returned none)"
    )
    surfaces = (
        "Screen coordinates are pixels of the named frame (a screenshot of the "
        "app at that moment)."
        if is_app
        else "Desktop coordinates are pixels of the named frame."
    )
    surfaces += (
        " Image coordinates are pixels of the original image; file lines count from 1."
    )
    lines: list[_Line] = []
    for segment in payload.segments:
        lines.append(
            _Line(
                segment.start_ms,
                1,
                f"said ({_clock(segment.start_ms)}–{_clock(segment.end_ms)})  "
                f"{_quoted(segment.text)}",
            )
        )
    lines.extend(_frame_line(frame, root, is_app) for frame in payload.frames)
    lines.extend(_view_lines(payload.views))
    # Fingers (mobile) carry a mode; a hovering mouse (webapp) does not.
    lines.extend(
        _press_lines(
            [pointer for pointer in payload.pointers if pointer.mode is not None],
            root,
        )
    )
    lines.extend(
        _hover_lines(
            [pointer for pointer in payload.pointers if pointer.mode is None], root
        )
    )
    lines.extend(_event_line(event) for event in payload.events)
    lines.sort(key=lambda line: (line.t_ms, line.order))
    files = ["transcript.json", "pointer.json"]
    if payload.events or app is not None:
        files.append("events.json")
    files.append("metadata.json")
    body = [
        title,
        "",
        f"Saved {saved_at.strftime('%Y-%m-%d %H:%M UTC')} · length "
        f"{_duration(payload.duration_ms)} · {timing}.",
        "Times are minutes:seconds from the start of the recording. "
        f"{surfaces} Frames are in `{root}/frames/`; complete data in "
        f"{', '.join(f'`{name}`' for name in files)} next to this file.",
        "",
        "## Dictation (cleaned)",
        "",
        payload.text.strip() or "(no speech recognized)",
        "",
        "## Timeline",
        "",
    ]
    body.extend(f"- {_clock(line.t_ms)}  {line.text}" for line in lines)
    if not lines:
        body.append("(nothing was recorded)")
    return "\n".join(body) + "\n"
