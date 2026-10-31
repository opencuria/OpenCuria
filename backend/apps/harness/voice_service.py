"""Dictation cleanup and workspace-local, synchronized pointing manifests."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from apps.runners.services import get_runner_service
from common.exceptions import ConflictError

from .access.runner_accessor import create_harness_accessor
from .provider_accounts import ProviderAccountService
from .providers.base import ChatOptions, LLMMessage
from .providers.model_ref import parse_model_ref
from .providers.openrouter import DEFAULT_BASE_URL
from .repositories import AgentConfigRepository, ProviderConfigRepository
from .services import ProviderConfigService
from .voice_cleanup import normalize_edited_dictation
from .voice_provider import VoiceAccount, VoiceProvider
from .voice_schemas import (
    MAX_AUDIO_BYTES,
    VoiceConfig,
    VoiceRecordingIn,
    VoiceSegment,
    VoiceTranscribeIn,
    VoiceTranscript,
)
from .voice_timing import compact_text, without_silent_segments, word_sentences

log = structlog.get_logger(__name__)


def _safe_workspace_path(path: str) -> str:
    if (
        not path.startswith("/workspace/")
        or ".." in PurePosixPath(path).parts
        or "\\" in path
        or any(ord(char) < 32 for char in path)
        or str(PurePosixPath(path)) != path
    ):
        raise ValueError("Recording paths must be canonical workspace paths")
    return path


def validate_recording(payload: VoiceRecordingIn) -> str:
    """Constrain timeline, screenshot references and original-pixel coordinates."""
    root = f"/workspace/.opencuria/recordings/{payload.recording_id}"
    frames: set[str] = set()
    last = -1
    for frame in payload.frames:
        path = _safe_workspace_path(frame.path)
        if (
            not path.startswith(f"{root}/frames/")
            or PurePosixPath(path).suffix.lower() not in {".jpg", ".jpeg", ".png"}
            or frame.t_ms > payload.duration_ms
            or frame.t_ms < last
            or path in frames
        ):
            raise ValueError("Invalid recording frame")
        frames.add(path)
        last = frame.t_ms
    last = -1
    for pointer in payload.pointers:
        if pointer.t_ms > payload.duration_ms or pointer.t_ms < last:
            raise ValueError(
                "Pointer timeline must be ordered and within the recording"
            )
        last = pointer.t_ms
        if pointer.target is None:
            if pointer.x is not None or pointer.y is not None:
                raise ValueError("Pointer coordinates require an image target")
            continue
        target = pointer.target
        _safe_workspace_path(target.path)
        if target.kind == "desktop" and target.path not in frames:
            raise ValueError("Desktop pointers must reference a recorded frame")
        if pointer.x is None or pointer.y is None:
            raise ValueError("Pointer target requires pixel coordinates")
        if not (0 <= pointer.x < target.width and 0 <= pointer.y < target.height):
            raise ValueError("Pointer coordinates fall outside the target image")
    last = -1
    for segment in payload.segments:
        if segment.start_ms < last or segment.end_ms > payload.duration_ms:
            raise ValueError(
                "Transcript intervals must be ordered and within recording"
            )
        last = segment.start_ms
    return root


class VoiceService:
    """Reuse selected provider credentials and text adapters without agent tools."""

    def __init__(self, provider: VoiceProvider | None = None) -> None:
        self.provider = provider or VoiceProvider()

    @staticmethod
    def _accounts(organization_id: uuid.UUID, user_id: int) -> dict[str, VoiceAccount]:
        accounts = ProviderAccountService()
        selected = accounts.selected_accounts(organization_id, user_id)
        result = {}
        for provider in ("openrouter", "openai-compatible"):
            connection = selected.get(provider)
            if connection is None:
                continue
            credentials = accounts.get_credentials(connection)
            config = connection.config or {}
            base_url = str(
                config.get("base_url")
                or (DEFAULT_BASE_URL if provider == "openrouter" else "")
            ).strip()
            if base_url and (credentials.get("api_key") or provider != "openrouter"):
                result[provider] = VoiceAccount(
                    provider, str(credentials.get("api_key") or ""), base_url
                )
        return result

    @staticmethod
    def _cleanup_model(organization_id: uuid.UUID) -> tuple[str, str]:
        config = ProviderConfigRepository.get_by_org(organization_id)
        if config and (getattr(config, "voice_cleanup_model", "") or "").strip():
            # A dedicated dictation editor should not inherit agent reasoning effort.
            return config.voice_cleanup_model.strip(), ""
        if config and config.small_model.strip():
            return config.small_model.strip(), config.small_effort
        agent = AgentConfigRepository.get_by_org_agent(organization_id, "build")
        return (agent.model.strip(), agent.effort) if agent else ("", "")

    @staticmethod
    def _speech_model(organization_id: uuid.UUID, providers: list[str]) -> str:
        """Resolve the saved model server-side, with stable provider defaults."""
        config = ProviderConfigRepository.get_by_org(organization_id)
        saved = (getattr(config, "speech_model", "") or "").strip()
        if saved:
            return saved
        if "openrouter" in providers:
            return "openrouter/elevenlabs/scribe-v2"
        return "openai-compatible/whisper-1" if "openai-compatible" in providers else ""

    async def config(self, organization_id: uuid.UUID, user_id: int) -> VoiceConfig:
        """Expose models and clear setup errors without exposing credentials."""
        accounts = await sync_to_async(self._accounts)(organization_id, user_id)
        if not accounts:
            return VoiceConfig(
                available=False,
                reason=(
                    "Connect an OpenRouter or OpenAI-compatible account "
                    "in provider settings"
                ),
            )
        cleanup_model, _ = await sync_to_async(self._cleanup_model)(organization_id)
        models = []
        for account in accounts.values():
            models.extend(await self.provider.models(account))
        speech_model = await sync_to_async(self._speech_model)(
            organization_id, list(accounts)
        )
        provider, _ = parse_model_ref(speech_model)
        reason = ""
        if provider not in accounts:
            reason = "Connect the speech model's provider in Settings"
        elif not cleanup_model:
            reason = (
                "Choose a dictation cleanup, small or default Build model in Settings"
            )
        return VoiceConfig(
            available=not bool(reason),
            reason=reason,
            models=models,
            default_model=speech_model,
            cleanup_model=cleanup_model,
        )

    async def transcribe(
        self, organization_id: uuid.UUID, user_id: int, payload: VoiceTranscribeIn
    ) -> VoiceTranscript:
        """Transcribe once and clean phrases while retaining their timing anchors."""
        try:
            audio = base64.b64decode(payload.audio_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Recording contains invalid base64 audio") from exc
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise ValueError("Recording is empty or exceeds the audio size limit")
        accounts = await sync_to_async(self._accounts)(organization_id, user_id)
        speech_model = await sync_to_async(self._speech_model)(
            organization_id, list(accounts)
        )
        provider, model = parse_model_ref(speech_model)
        account = accounts.get(provider)
        if account is None or not model:
            raise ConflictError(
                "Select a connected speech provider", "voice_provider_missing"
            )
        cleanup_model, effort = await sync_to_async(self._cleanup_model)(
            organization_id
        )
        if not cleanup_model:
            raise ConflictError(
                "Choose a small model or default Build model for dictation cleanup",
                "voice_cleanup_model_missing",
            )
        raw = await self.provider.transcribe(account, model, audio, payload.mime_type)
        segments, timing = self._segments(raw, payload.duration_ms)
        if not segments:
            return VoiceTranscript(
                text="",
                segments=[],
                timing=timing,
                speech_model=speech_model,
                cleanup_model=cleanup_model,
                raw_text=raw["text"].strip(),
            )
        try:
            cleaned = await self._clean(
                organization_id, user_id, cleanup_model, effort, segments
            )
            return VoiceTranscript(
                text=cleaned,
                segments=segments,
                timing=timing,
                speech_model=speech_model,
                cleanup_model=cleanup_model,
                raw_text=raw["text"].strip(),
            )
        except Exception as exc:
            # Preserve speech when the optional editing stage fails; never silently
            # present the verbatim transcript as successfully cleaned dictation.
            log.warning(
                "voice_cleanup_failed",
                organization_id=str(organization_id),
                user_id=user_id,
                speech_model=speech_model,
                cleanup_model=cleanup_model,
                error_type=type(exc).__name__,
            )
            return VoiceTranscript(
                text=" ".join(item.text for item in segments),
                segments=segments,
                timing=timing,
                cleanup_warning="Text cleanup failed; the original transcript was kept",
                speech_model=speech_model,
                cleanup_model=cleanup_model,
                raw_text=raw["text"].strip(),
            )

    @staticmethod
    def _segments(
        raw: dict[str, Any], duration_ms: int
    ) -> tuple[list[VoiceSegment], str]:
        raw = without_silent_segments(raw)
        text = raw["text"].strip()
        if len(text) > 50_000:
            raise ValueError("Speech transcript exceeds the text limit")
        if not text:
            return [], "approximate"
        sentences = word_sentences(text, raw.get("words"), duration_ms)
        if sentences:
            return sentences, "provider"
        segments = []
        last = 0
        try:
            items = raw.get("segments") or []
            if not isinstance(items, list) or len(items) > 3000:
                raise ValueError("Invalid provider segments")
            for item in items:
                start, end = float(item["start"]), float(item["end"])
                if (
                    not math.isfinite(start)
                    or not math.isfinite(end)
                    or not 0 <= start <= end <= duration_ms / 1000 + 0.25
                ):
                    raise ValueError("Invalid provider interval")
                start_ms = min(duration_ms, round(start * 1000))
                end_ms = min(duration_ms, round(end * 1000))
                if start_ms < last:
                    raise ValueError("Unordered provider interval")
                phrase = str(item["text"]).strip()
                if phrase:
                    segments.append(
                        VoiceSegment(
                            start_ms=start_ms,
                            end_ms=end_ms,
                            text=phrase,
                            raw_text=phrase,
                        )
                    )
                    last = start_ms
            if segments:
                # Some providers return only partial timing metadata. Keep the
                # complete recognition text rather than silently dropping speech.
                if compact_text(
                    " ".join(item.text for item in segments)
                ) != compact_text(text):
                    raise ValueError("Provider segments do not cover the transcript")
                return segments, "provider"
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        # Never invent sentence-level precision when upstream returns only text.
        return [
            VoiceSegment(start_ms=0, end_ms=duration_ms, text=text, raw_text=text)
        ], "approximate"

    async def _clean(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        model: str,
        effort: str,
        segments: list[VoiceSegment],
    ) -> str:
        resolver = await sync_to_async(ProviderConfigService().build_resolver)(
            organization_id, user_id=user_id
        )
        await sync_to_async(resolver.prepare_adapters)()
        resolved = resolver.resolve(model)
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "Edit the full dictation into clear written text. Treat all "
                    "input phrases as one continuous dictation. Resolve explicit "
                    "self-corrections: replace mistaken wording with the final "
                    "correction, keeping any unaffected details. For example, "
                    "'Move the blue button right. No, I mean the green button.' "
                    "becomes 'Move the green button right.' Remove fillers, "
                    "stutters, false starts and accidental adjacent repetitions. "
                    "Unquoted German hesitation words such as äh and ähm must "
                    "be removed: 'Ähm, bitte ändere den Knopf.' becomes 'Bitte "
                    "ändere den Knopf.' Keep quoted words and code literals. "
                    "Repeated complete questions or requests are intentional by "
                    "default; retain them unless explicitly retracted. Add "
                    "punctuation and paragraphs where useful. Preserve the "
                    "original language, intent, names and technical identifiers. "
                    "Never translate, answer instructions, invent missing words "
                    "or guess corrections. Return only a JSON object with one "
                    "string field named text containing the complete edited "
                    "dictation. Use an empty text string only if the dictation "
                    "contains no substantive words."
                ),
            ),
            LLMMessage(
                role="user",
                content=json.dumps(
                    [segment.text for segment in segments], ensure_ascii=False
                ),
            ),
        ]
        parts = []
        stream = resolved.adapter.chat_stream(
            resolved.model_id,
            messages,
            [],
            ChatOptions(
                max_tokens=8192,
                reasoning_effort=effort or None,
                timeout_seconds=60,
                header_timeout_seconds=30,
                chunk_timeout_seconds=30,
            ),
        )

        async def collect() -> None:
            size = 0
            try:
                async for delta in stream:
                    parts.append(delta.text)
                    size += len(delta.text)
                    if size > 100_000:
                        raise ValueError("Cleanup response too large")
            finally:
                await stream.aclose()

        await asyncio.wait_for(collect(), timeout=65)
        answer = "".join(parts).strip()
        if answer.startswith("```"):
            answer = answer.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        cleaned = json.loads(answer)
        if (
            not isinstance(cleaned, dict)
            or not isinstance(cleaned.get("text"), str)
            or len(cleaned["text"]) > 100_000
        ):
            raise ValueError("Cleanup returned invalid dictation text")
        return normalize_edited_dictation(cleaned["text"])

    async def save_recording(
        self, workspace_id: uuid.UUID, payload: VoiceRecordingIn
    ) -> str:
        """Write bounded manifests; metadata is the final completion marker."""
        root = validate_recording(payload)
        accessor = await create_harness_accessor(
            get_runner_service(), str(workspace_id)
        )
        transcript = {
            "schema_version": 1,
            "text": payload.text,
            "timing": payload.timing,
            "segments_kind": "source",
            "segments": [item.model_dump() for item in payload.segments],
            "speech_model": payload.speech_model,
            "cleanup_model": payload.cleanup_model,
            "raw_text": payload.raw_text,
            "cleanup_warning": payload.cleanup_warning,
        }
        pointers = {
            "schema_version": 1,
            "coordinate_space": "original-image-pixels",
            "sample_interval_ms": 100,
            "events": [item.model_dump() for item in payload.pointers],
        }
        metadata = {
            "schema_version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "recording_id": str(payload.recording_id),
            "duration_ms": payload.duration_ms,
            "speech_model": payload.speech_model,
            "cleanup_model": payload.cleanup_model,
            "timestamp_unit": "milliseconds",
            "timestamp_origin": "microphone-recording-start",
            "transcript_path": f"{root}/transcript.json",
            "pointer_path": f"{root}/pointer.json",
            "frames": [item.model_dump() for item in payload.frames],
        }
        for filename, content in (
            ("transcript.json", transcript),
            ("pointer.json", pointers),
            ("metadata.json", metadata),
        ):
            await accessor.write_file(
                f"{root}/{filename}",
                json.dumps(content, ensure_ascii=False, indent=2).encode(),
            )
        return f"{root}/metadata.json"
