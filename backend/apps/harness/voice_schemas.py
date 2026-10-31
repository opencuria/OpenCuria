"""Bounded wire contracts for dictation and synchronized pointing recordings."""

from __future__ import annotations

import uuid
from typing import Literal

from ninja import Schema
from pydantic import Field, model_validator

MAX_DURATION_MS = 300_000
# Keeps base64 JSON below Django's default 2.5 MiB body limit, even with metadata.
MAX_AUDIO_BYTES = 1_500_000


class VoiceModel(Schema):
    """A transcription model, independent of the agent's chat model."""

    id: str
    name: str


class VoiceConfig(Schema):
    """Secret-free capabilities and resource limits for the composer."""

    available: bool
    models: list[VoiceModel] = Field(default_factory=list)
    default_model: str = ""
    reason: str = ""
    max_duration_ms: int = MAX_DURATION_MS
    max_audio_bytes: int = MAX_AUDIO_BYTES


class VoiceTranscribeIn(Schema):
    """One complete, independently decodable browser recording."""

    audio_base64: str = Field(min_length=1, max_length=2_000_000)
    mime_type: str = Field(max_length=100)
    model: str = Field(min_length=1, max_length=255)
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
    """Cleaned text with original phrase boundaries, or explicit coarse timing."""

    text: str
    segments: list[VoiceSegment]
    timing: Literal["provider", "approximate"]
    cleanup_warning: str = ""


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
    timing: Literal["provider", "approximate"]
    segments: list[VoiceSegment] = Field(max_length=3000)
    pointers: list[VoicePointer] = Field(max_length=12_000)
    frames: list[VoiceFrame] = Field(max_length=601)


class VoiceRecordingOut(Schema):
    """Entry point the user can include in their agent prompt."""

    path: str
