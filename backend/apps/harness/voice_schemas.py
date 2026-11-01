"""Bounded wire contracts for dictation and synchronized pointing recordings."""

from __future__ import annotations

import uuid
from typing import Literal

from ninja import Schema
from pydantic import Field, model_validator

MAX_DURATION_MS = 600_000
# Ten minutes at the requested 64 kbit/s uses about 4.8 MB; leave codec headroom.
# The base64 JSON fits the configured 12 MiB DATA_UPLOAD_MAX_MEMORY_SIZE.
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BASE64_LENGTH = 4 * ((MAX_AUDIO_BYTES + 2) // 3)


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
    """Original image pixels or a stored desktop screenshot coordinate system."""

    kind: Literal["image", "desktop"]
    path: str = Field(min_length=1, max_length=2048)
    width: int = Field(ge=1, le=32768)
    height: int = Field(ge=1, le=32768)


class VoicePointer(Schema):
    """One sampled position or discrete pointer event."""

    t_ms: int = Field(ge=0, le=MAX_DURATION_MS)
    event: Literal["move", "down", "up", "leave"]
    x: float | None = Field(default=None, allow_inf_nan=False)
    y: float | None = Field(default=None, allow_inf_nan=False)
    target: VoiceTarget | None = None


class VoiceFrame(Schema):
    """A desktop frame captured at the given recording-relative time."""

    t_ms: int = Field(ge=0, le=MAX_DURATION_MS)
    path: str = Field(min_length=1, max_length=2048)


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
    frames: list[VoiceFrame] = Field(max_length=1201)


class VoiceRecordingOut(Schema):
    """Entry point the user can include in their agent prompt."""

    path: str
