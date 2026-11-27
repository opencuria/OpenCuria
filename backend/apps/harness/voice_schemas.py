"""Bounded wire contracts for dictation and synchronized pointing recordings.

Pointing recordings come from the webapp (workspace images, the desktop) and
from the mobile app (workspace images and text files, the screen of an open
app with its taps, logs and backend requests). Limits match
`@opencuria/chat-core/visualRecording`.
"""

from __future__ import annotations

import math
import uuid
from typing import Annotated, Literal

from ninja import Schema
from pydantic import BeforeValidator, Field, model_validator

MAX_DURATION_MS = 600_000
# Ten minutes at the requested 64 kbit/s uses about 4.8 MB; leave codec headroom.
# The base64 JSON fits the configured 12 MiB DATA_UPLOAD_MAX_MEMORY_SIZE.
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BASE64_LENGTH = 4 * ((MAX_AUDIO_BYTES + 2) // 3)
MAX_FRAMES = 1201
MAX_APP_EVENTS = 2000
MAX_VIEWS = 1000
MAX_FRAME_TEXTS = 40
MAX_SHORT_TEXT = 120


def _whole_ms(value: object) -> object:
    """Round finite fractional milliseconds; leave everything else to `int`."""
    if isinstance(value, float) and math.isfinite(value):
        return round(value)
    return value


# Recording-relative time. Native clocks (iOS) report fractions of a millisecond.
RecordingMs = Annotated[
    int, BeforeValidator(_whole_ms), Field(ge=0, le=MAX_DURATION_MS)
]


class VoiceModel(Schema):
    """A transcription model, independent of the agent's chat model."""

    id: str
    name: str


class VoiceConfig(Schema):
    """Secret-free capabilities and resource limits for the composer."""

    available: bool
    models: list[VoiceModel] = Field(default_factory=list)
    default_model: str = ""
    cleanup_model: str = ""
    reason: str = ""
    max_duration_ms: int = MAX_DURATION_MS
    max_audio_bytes: int = MAX_AUDIO_BYTES


class VoiceTranscribeIn(Schema):
    """One complete, independently decodable browser recording."""

    audio_base64: str = Field(min_length=1, max_length=MAX_AUDIO_BASE64_LENGTH)
    mime_type: str = Field(max_length=100)
    # Accepted for older clients; model selection is resolved from saved settings.
    model: str = Field(default="", max_length=255)
    duration_ms: int = Field(ge=1, le=MAX_DURATION_MS)


class VoiceSegment(Schema):
    """A phrase and its interval relative to microphone recording start."""

    start_ms: int = Field(ge=0, le=MAX_DURATION_MS)
    end_ms: int = Field(ge=0, le=MAX_DURATION_MS)
    text: str = Field(max_length=50_000)
    raw_text: str = Field(default="", max_length=50_000)

    @model_validator(mode="after")
    def ordered(self) -> VoiceSegment:
        """Reject reversed intervals."""
        if self.end_ms < self.start_ms:
            raise ValueError("Segment end precedes start")
        return self


class VoiceTranscript(Schema):
    """Global edited text with independently timed ASR source phrases."""

    text: str
    segments: list[VoiceSegment]
    timing: Literal["provider", "approximate"]
    segments_kind: Literal["source"] = "source"
    cleanup_warning: str = ""
    raw_text: str = ""
    speech_model: str = ""
    cleanup_model: str = ""


class VoiceTarget(Schema):
    """Original image pixels, a recorded desktop or app frame, or a text file.

    A file is a grid of characters: ``width`` is its longest line's length,
    ``height`` its number of lines; pointer ``x``/``y`` are a zero-based column
    and line.
    """

    kind: Literal["image", "desktop", "app", "file"]
    path: str = Field(min_length=1, max_length=2048)
    # Files: up to a million lines of up to a million characters.
    width: int = Field(ge=1, le=1_000_000)
    height: int = Field(ge=1, le=1_000_000)

    @model_validator(mode="after")
    def pixel_size(self) -> VoiceTarget:
        """Images and frames stay within 32768 pixels per side."""
        if self.kind != "file" and max(self.width, self.height) > 32768:
            raise ValueError("Target is too large")
        return self


class VoiceElement(Schema):
    """The app control under a finger, or a file's line (role ``line``)."""

    role: str = Field(default="", max_length=40)
    label: str = Field(default="", max_length=MAX_SHORT_TEXT)
    text: str = Field(default="", max_length=MAX_SHORT_TEXT)
    test_id: str = Field(default="", max_length=MAX_SHORT_TEXT)
    type: str = Field(default="", max_length=80)
    interactive: bool | None = None
    bounds: tuple[int, int, int, int] | None = None

    @model_validator(mode="after")
    def positive_bounds(self) -> VoiceElement:
        """Bounds are a non-negative frame-pixel rectangle."""
        if self.bounds is not None and any(value < 0 for value in self.bounds):
            raise ValueError("Element bounds must be non-negative")
        return self


class VoicePointer(Schema):
    """One sampled position or discrete pointer event."""

    t_ms: RecordingMs
    event: Literal["move", "down", "up", "leave"]
    x: float | None = Field(default=None, allow_inf_nan=False)
    y: float | None = Field(default=None, allow_inf_nan=False)
    target: VoiceTarget | None = None
    # Touches (mobile): the finger used the surface, or only pointed at it.
    # Without a mode the pointer is a hovering mouse (webapp).
    mode: Literal["use", "point"] | None = None
    element: VoiceElement | None = None


class VoiceFrame(Schema):
    """A desktop or app frame captured at the given recording-relative time."""

    t_ms: RecordingMs
    path: str = Field(min_length=1, max_length=2048)
    # Visible texts of an app screen, in reading order.
    texts: list[str] = Field(default_factory=list, max_length=MAX_FRAME_TEXTS)

    @model_validator(mode="after")
    def short_texts(self) -> VoiceFrame:
        """Screen texts stay short."""
        if any(len(text) > MAX_SHORT_TEXT for text in self.texts):
            raise ValueError("Frame texts are too long")
        return self


class VoiceView(Schema):
    """What part of an image or file was on screen from ``t_ms`` on."""

    t_ms: RecordingMs
    # None once the user went back from it, e.g. to the chat.
    target: VoiceTarget | None = None
    # ``[x, y, width, height]`` in target units; the whole target when absent.
    region: tuple[int, int, int, int] | None = None
    # Estimated from a rendered preview's scroll position (Markdown, tables).
    approximate: bool = False


class VoiceAppEvent(Schema):
    """A log line, error or backend request an open app reported."""

    t_ms: RecordingMs
    kind: Literal["log", "error", "network"]
    level: Literal["debug", "info", "warn", "error"] | None = None
    message: str = Field(default="", max_length=1000)
    method: str = Field(default="", max_length=12)
    url: str = Field(default="", max_length=500)
    status: int | None = Field(default=None, ge=0, le=999)
    duration_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS * 10)


class VoiceAppContext(Schema):
    """The app an app recording shows."""

    app_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    version: str = Field(default="", max_length=64)
    platform: Literal["android", "ios"]


class VoiceRecordingIn(Schema):
    """Manifest for opt-in pointing; screenshots arrive through file upload."""

    recording_id: uuid.UUID
    duration_ms: int = Field(ge=1, le=MAX_DURATION_MS)
    text: str = Field(max_length=100_000)
    speech_model: str = Field(default="", max_length=255)
    cleanup_model: str = Field(default="", max_length=255)
    cleanup_warning: str = Field(default="", max_length=1000)
    raw_text: str = Field(default="", max_length=100_000)
    timing: Literal["provider", "approximate"]
    segments: list[VoiceSegment] = Field(max_length=3000)
    pointers: list[VoicePointer] = Field(max_length=12_000)
    frames: list[VoiceFrame] = Field(max_length=MAX_FRAMES)
    views: list[VoiceView] = Field(default_factory=list, max_length=MAX_VIEWS)
    events: list[VoiceAppEvent] = Field(default_factory=list, max_length=MAX_APP_EVENTS)
    app: VoiceAppContext | None = None


class VoiceRecordingOut(Schema):
    """Entry point the user can include in their agent prompt."""

    path: str
