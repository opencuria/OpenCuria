"""Readable timeline of a pointing or app recording, the entry point for agents.

Every spoken phrase carries its interval; pointers, taps, screens and app
events follow in time order, each naming the frame image it refers to. The
JSON files next to it keep the complete data.
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
)

# A press that moved less than this (frame pixels) is a tap, not a swipe.
_TAP_SLOP = 24
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


def _where(pointer: VoicePointer, root: str) -> str:
    target = pointer.target
    if target is None:
        return ""
    surface = _frame_name(target.path, root) if target.kind != "image" else target.path
    return f"at {_point(pointer.x, pointer.y)} on {surface}"


def _press_lines(pointers: list[VoicePointer], root: str) -> list[_Line]:
    """Taps, long presses and swipes of an app recording."""
    lines: list[_Line] = []
    start: VoicePointer | None = None
    last: VoicePointer | None = None

    def finish(end: VoicePointer | None) -> None:
        if start is None:
            return
        end = end or last or start
        moved = (
            math.dist((start.x or 0, start.y or 0), (end.x or 0, end.y or 0))
            >= _TAP_SLOP
            and end.target is not None
        )
        held = end.t_ms - start.t_ms >= _LONG_PRESS_MS
        pointing = start.mode == "point"
        verb = (
            ("point and drag" if pointing else "swipe")
            if moved
            else ("point" if pointing else ("long press" if held else "tap"))
        )
        what = _describe_element(start.element)
        text = f"{verb}  {what + '  ' if what else ''}{_where(start, root)}".rstrip()
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
        "Coordinates are pixels of the named frame (a screenshot of the app at "
        "that moment)."
        if is_app
        else "Coordinates are pixels of the named desktop frame or image."
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
    lines.extend(
        _press_lines(payload.pointers, root)
        if is_app
        else _hover_lines(payload.pointers, root)
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
