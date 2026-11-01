"""Speech-to-text HTTP transport using existing selected provider accounts."""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx

from common.exceptions import ConflictError

from .providers.openrouter import DEFAULT_BASE_URL

_FORMATS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/aac": "aac",
    "audio/flac": "flac",
}
# Public catalog, keyed by base URL, contains no credentials or user data.
_catalog: dict[str, tuple[float, list[dict[str, str]]]] = {}


@dataclass(frozen=True)
class VoiceAccount:
    """Resolved server-side connection, never serialized to the client."""

    provider: str
    api_key: str
    base_url: str = DEFAULT_BASE_URL


class VoiceProviderError(Exception):
    """Safe public error that never contains provider bodies or credentials."""

    def __init__(self, message: str, *, status: int = 502) -> None:
        self.status = status
        super().__init__(message)


class VoiceProvider:
    """Bounded STT requests with portable structured-output fallback."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def models(self, account: VoiceAccount) -> list[dict[str, str]]:
        """Discover OpenRouter STT models; compatible services accept custom IDs."""
        if account.provider != "openrouter":
            return [{"id": "openai-compatible/whisper-1", "name": "Whisper"}]
        key = account.base_url.rstrip("/")
        cached = _catalog.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        async with httpx.AsyncClient(timeout=10) as client:
            transport = self.client or client
            try:
                response = await transport.get(
                    f"{key}/models", params={"output_modalities": "transcription"}
                )
                response.raise_for_status()
                catalog = response.json()
                if not isinstance(catalog, dict):
                    raise ValueError("Invalid catalog")
                data = catalog.get("data", [])
                if not isinstance(data, list):
                    raise ValueError("Invalid catalog models")
                models = [
                    {
                        "id": f"openrouter/{item['id']}",
                        "name": str(item.get("name") or item["id"]),
                    }
                    for item in data
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                ][:250]
                if not models:
                    raise ValueError("Empty catalog")
            except (httpx.HTTPError, ValueError, TypeError):
                # A custom ID remains possible while catalog service is unavailable.
                models = [
                    {
                        "id": "openrouter/elevenlabs/scribe-v2",
                        "name": "Scribe v2",
                    },
                    {
                        "id": "openrouter/openai/whisper-large-v3",
                        "name": "Whisper large v3",
                    },
                ]
            _catalog[key] = (time.monotonic() + 300, models)
            return models

    async def transcribe(
        self, account: VoiceAccount, model: str, audio: bytes, mime_type: str
    ) -> dict[str, Any]:
        """Prefer provider timestamps and retry plain JSON only if unsupported."""
        try:
            return await asyncio.wait_for(
                self._transcribe(account, model, audio, mime_type), timeout=180
            )
        except asyncio.TimeoutError as exc:
            raise VoiceProviderError(
                "Speech transcription timed out; please retry", status=504
            ) from exc

    async def _transcribe(
        self, account: VoiceAccount, model: str, audio: bytes, mime_type: str
    ) -> dict[str, Any]:
        fmt = _FORMATS.get(mime_type.split(";", 1)[0].strip().lower())
        if fmt is None:
            raise ValueError("Unsupported recording format")
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10)) as client:
            transport = self.client or client
            try:
                for verbose in (True, False):
                    kwargs: dict[str, Any] = {
                        "headers": {"Authorization": f"Bearer {account.api_key}"}
                    }
                    options: dict[str, Any] = {
                        "model": model,
                        "temperature": 0,
                        "response_format": "verbose_json" if verbose else "json",
                    }
                    if verbose:
                        options["timestamp_granularities"] = ["segment", "word"]
                    if account.provider == "openrouter":
                        kwargs["json"] = {
                            **options,
                            "input_audio": {
                                "data": base64.b64encode(audio).decode("ascii"),
                                "format": fmt,
                            },
                        }
                    else:
                        if verbose:
                            options.pop("timestamp_granularities")
                            options["timestamp_granularities[]"] = ["segment", "word"]
                        kwargs["data"] = options
                        kwargs["files"] = {
                            "file": (
                                f"dictation.{fmt}",
                                audio,
                                mime_type.split(";", 1)[0],
                            )
                        }
                    response = await transport.post(
                        f"{account.base_url.rstrip('/')}/audio/transcriptions", **kwargs
                    )
                    if response.status_code in (400, 422) and verbose:
                        continue
                    if response.status_code in (401, 403):
                        raise VoiceProviderError(
                            "The speech provider rejected its credentials", status=409
                        )
                    if response.status_code == 429:
                        raise VoiceProviderError(
                            "Speech provider rate limit reached; please retry",
                            status=429,
                        )
                    if response.status_code in (400, 404, 415, 422):
                        raise VoiceProviderError(
                            "The speech model rejected this recording format or "
                            "settings; try another speech model",
                            status=409,
                        )
                    if response.is_error:
                        raise VoiceProviderError("Speech transcription failed")
                    result = response.json()
                    if not isinstance(result, dict) or not isinstance(
                        result.get("text"), str
                    ):
                        raise VoiceProviderError(
                            "Speech provider returned no transcript"
                        )
                    return result
            except httpx.TimeoutException as exc:
                raise VoiceProviderError(
                    "Speech transcription timed out; please retry", status=504
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise VoiceProviderError(
                    "Speech provider could not be reached"
                ) from exc
        raise ConflictError("Speech model does not support transcription")
