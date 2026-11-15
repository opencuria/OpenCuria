"""Behavior, security and failure tests for dictation and pointing manifests."""

from __future__ import annotations

import base64
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from django.test import Client
from pydantic import ValidationError

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.providers.base import Delta
from apps.harness.voice_provider import VoiceAccount, VoiceProvider, VoiceProviderError
from apps.harness.voice_schemas import (
    MAX_AUDIO_BYTES,
    MAX_DURATION_MS,
    VoiceConfig,
    VoiceRecordingIn,
    VoiceSegment,
    VoiceTranscribeIn,
    VoiceTranscript,
)
from apps.harness.voice_service import VoiceService, validate_recording
from common.utils import hash_token


@pytest.fixture
def recording() -> dict:
    """An image and one synchronized desktop frame."""
    root = f"/workspace/.opencuria/recordings/{uuid.uuid4()}"
    path = f"{root}/frames/000001.jpg"
    return {
        "recording_id": root.rsplit("/", 1)[1],
        "duration_ms": 1000,
        "text": "Change this.",
        "timing": "provider",
        "segments": [{"start_ms": 10, "end_ms": 800, "text": "Change this."}],
        "pointers": [
            {
                "t_ms": 500,
                "event": "down",
                "x": 100,
                "y": 50,
                "target": {
                    "kind": "desktop",
                    "path": path,
                    "width": 1920,
                    "height": 1080,
                },
            }
        ],
        "frames": [{"t_ms": 0, "path": path}],
    }


def test_valid_recording(recording: dict) -> None:
    """The output directory is server-derived from the UUID."""
    payload = VoiceRecordingIn(**recording)
    assert validate_recording(payload).endswith(recording["recording_id"])


@pytest.fixture
def ten_minute_recording(recording: dict) -> dict:
    """A continuously changing desktop with pointer movement and late speech."""
    root = f"/workspace/.opencuria/recordings/{recording['recording_id']}"
    recording["duration_ms"] = MAX_DURATION_MS
    recording["segments"] = [
        {"start_ms": 599_000, "end_ms": MAX_DURATION_MS, "text": "Change this."}
    ]
    recording["frames"] = [
        {"t_ms": index * 500, "path": f"{root}/frames/{index:06d}.jpg"}
        for index in range(1201)
    ]
    recording["pointers"] = [
        {
            **recording["pointers"][0],
            "event": "move",
            "t_ms": index * 100,
            "target": {
                **recording["pointers"][0]["target"],
                "path": recording["frames"][index // 5]["path"],
            },
        }
        for index in range(6001)
    ]
    return recording


def test_ten_minute_recording_and_limits(ten_minute_recording: dict) -> None:
    """Audio, speech, frame and pointer timelines all accept the same duration."""
    config = VoiceConfig(available=True)
    assert config.max_duration_ms == 600_000
    assert config.max_audio_bytes == 8 * 1024 * 1024
    validate_recording(VoiceRecordingIn(**ten_minute_recording))
    ten_minute_recording["duration_ms"] += 1
    with pytest.raises(ValidationError):
        VoiceRecordingIn(**ten_minute_recording)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("endpoint", ["transcribe", "recordings"])
def test_full_length_requests_pass_http_body_limits(
    harness_workspace, monkeypatch, ten_minute_recording, endpoint
) -> None:
    """Exercise Django's actual JSON body reader, not just direct schema parsing."""
    service = SimpleNamespace(
        transcribe=AsyncMock(
            return_value=VoiceTranscript(
                text="hello", segments=[], timing="approximate"
            )
        ),
        save_recording=AsyncMock(return_value="/workspace/test.json"),
    )
    monkeypatch.setattr("apps.harness.voice_api.VoiceService", lambda: service)
    token = "voice-long-recording-key"
    APIKey.objects.create(
        user=harness_workspace.created_by,
        name="voice",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[
            APIKeyPermission.HARNESS_RUN.value,
            APIKeyPermission.TERMINAL_ACCESS.value,
        ],
    )
    body = ten_minute_recording if endpoint == "recordings" else {
        "audio_base64": base64.b64encode(b"a" * MAX_AUDIO_BYTES).decode(),
        "mime_type": "audio/webm;codecs=opus",
        "duration_ms": MAX_DURATION_MS,
    }
    response = Client().post(
        f"/api/v1/workspaces/{harness_workspace.id}/voice/{endpoint}/",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(harness_workspace.runner.organization_id),
    )
    assert response.status_code == 200, response.content
    assert service.transcribe.await_count + service.save_recording.await_count == 1


async def test_audio_over_byte_limit_never_reaches_provider() -> None:
    """Base64 padding can fit the string bound while decoded audio is too large."""
    provider = SimpleNamespace(transcribe=AsyncMock())
    with pytest.raises(ValueError, match="audio size limit"):
        await VoiceService(provider).transcribe(
            uuid.uuid4(), 1, VoiceTranscribeIn(
                audio_base64=base64.b64encode(b"a" * (MAX_AUDIO_BYTES + 1)).decode(),
                mime_type="audio/webm", duration_ms=MAX_DURATION_MS,
            )
        )
    provider.transcribe.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/../etc/passwd",
        "/etc/passwd",
        "/workspace/a/../../x",
        "/workspace/a\\b.png",
        "/workspace//a.png",
        "/workspace/a\n.png",
    ],
)
def test_recording_rejects_untrusted_image_paths(recording: dict, path: str) -> None:
    """No traversal, foreign paths, alternate separators or control characters."""
    recording["pointers"][0]["target"].update(kind="image", path=path)
    with pytest.raises(ValueError):
        validate_recording(VoiceRecordingIn(**recording))


@pytest.mark.parametrize("change", ["frame_path", "unknown_frame", "future", "bounds"])
def test_invalid_recording_references(recording: dict, change: str) -> None:
    """Desktop paths and timing cannot lie outside this recording."""
    if change == "frame_path":
        recording["frames"][0]["path"] = "/workspace/foreign.jpg"
    elif change == "unknown_frame":
        recording["pointers"][0]["target"]["path"] += ".png"
    elif change == "future":
        recording["pointers"][0]["t_ms"] = 1001
    else:
        recording["pointers"][0]["x"] = -1
    with pytest.raises(ValueError):
        validate_recording(VoiceRecordingIn(**recording))


def test_nan_coordinates_rejected(recording: dict) -> None:
    """Non-finite coordinates must never reach agent-readable files."""
    recording["pointers"][0]["x"] = float("nan")
    with pytest.raises(ValidationError):
        VoiceRecordingIn(**recording)


@pytest.mark.parametrize(
    "raw,timing",
    [
        (
            {
                "text": "hello",
                "segments": [{"start": 0.1, "end": 0.5, "text": "hello"}],
            },
            "provider",
        ),
        ({"text": "hello"}, "approximate"),
        (
            {"text": "hello", "segments": [{"start": float("nan"), "end": 1}]},
            "approximate",
        ),
        ({"text": "hello", "segments": [{"start": 1, "end": 0}]}, "approximate"),
    ],
)
def test_transcript_timing_is_honest(raw: dict, timing: str) -> None:
    """Text-only and malformed upstream intervals get explicit coarse timing."""
    segments, actual = VoiceService._segments(raw, 1000)
    assert actual == timing
    assert segments[0].text == "hello"
    assert 0 <= segments[0].start_ms <= segments[0].end_ms <= 1000


async def test_provider_verbose_fallback() -> None:
    """Unsupported verbose output retries once without timestamp options."""
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body["temperature"] == 0
        if body["response_format"] == "verbose_json":
            return httpx.Response(400, json={"error": "unsupported format"})
        return httpx.Response(200, json={"text": "hello"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await VoiceProvider(client).transcribe(
            VoiceAccount("openrouter", "secret"),
            "test",
            b"audio",
            "audio/webm;codecs=opus",
        )
    assert result == {"text": "hello"}
    assert len(requests) == 2
    assert "timestamp_granularities" not in requests[-1]
    assert requests[-1]["input_audio"]["format"] == "webm"


@pytest.mark.parametrize("status,expected", [(401, 409), (429, 429), (500, 502)])
async def test_provider_errors_redact_body(status: int, expected: int) -> None:
    """Upstream bodies may contain secrets and must never escape through REST."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="SECRET API KEY")
        )
    ) as client:
        with pytest.raises(VoiceProviderError) as exc:
            await VoiceProvider(client).transcribe(
                VoiceAccount("openrouter", "secret"), "test", b"audio", "audio/wav"
            )
    assert exc.value.status == expected
    assert "SECRET" not in str(exc.value)


async def test_provider_timeout() -> None:
    """Timeout errors keep raw request data private."""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(VoiceProviderError) as exc:
            await VoiceProvider(client).transcribe(
                VoiceAccount("openrouter", "secret"), "test", b"audio", "audio/wav"
            )
    assert exc.value.status == 504
    assert "secret" not in str(exc.value)


async def test_cleanup_has_no_tools_and_edits_complete_dictation(monkeypatch) -> None:
    """The editing call cannot execute speech or lose its phrase alignment."""

    async def stream(model, messages, tools, opts):
        assert tools == []
        assert model == "small"
        assert json.loads(messages[-1].content) == ["äh change this", "um"]
        yield Delta(text='{"text":"Change this."}')

    resolver = SimpleNamespace(
        prepare_adapters=lambda: None,
        resolve=lambda model: SimpleNamespace(
            model_id="small", adapter=SimpleNamespace(chat_stream=stream)
        ),
    )
    monkeypatch.setattr(
        "apps.harness.voice_service.ProviderConfigService.build_resolver",
        lambda *args, **kwargs: resolver,
    )
    result = await VoiceService()._clean(
        uuid.uuid4(),
        1,
        "small",
        "",
        [
            VoiceSegment(start_ms=50, end_ms=500, text="äh change this"),
            VoiceSegment(start_ms=600, end_ms=900, text="um"),
        ],
    )
    assert result == "Change this."


async def test_cleanup_failure_keeps_transcript(monkeypatch) -> None:
    """An editing outage must not discard a successful transcript."""
    service = VoiceService(
        SimpleNamespace(transcribe=AsyncMock(return_value={"text": "original"}))
    )
    monkeypatch.setattr(
        service,
        "_accounts",
        lambda *args: {"openrouter": VoiceAccount("openrouter", "secret")},
    )
    monkeypatch.setattr(service, "_cleanup_model", lambda *args: ("small", ""))
    monkeypatch.setattr(service, "_speech_model", lambda *args: "openrouter/test")
    monkeypatch.setattr(
        service, "_clean", AsyncMock(side_effect=RuntimeError("outage"))
    )
    response = await service.transcribe(
        uuid.uuid4(),
        1,
        VoiceTranscribeIn(
            audio_base64=base64.b64encode(b"audio").decode(),
            mime_type="audio/webm",
            model="openrouter/test",
            duration_ms=1000,
        ),
    )
    assert response.text == "original"
    assert response.speech_model == "openrouter/test"
    assert response.cleanup_model == "small"
    assert response.cleanup_warning
    assert response.timing == "approximate"


async def test_save_recording_writes_completion_marker_last(
    monkeypatch, recording
) -> None:
    """Incomplete uploads never expose a completed metadata entry point."""
    accessor = SimpleNamespace(write_file=AsyncMock())
    monkeypatch.setattr(
        "apps.harness.voice_service.create_harness_accessor",
        AsyncMock(return_value=accessor),
    )
    result = await VoiceService().save_recording(
        uuid.uuid4(), VoiceRecordingIn(**recording)
    )
    calls = accessor.write_file.call_args_list
    assert [call.args[0].rsplit("/", 1)[-1] for call in calls] == [
        "transcript.json",
        "pointer.json",
        "metadata.json",
    ]
    assert result == calls[-1].args[0]
    assert json.loads(calls[1].args[1])["coordinate_space"] == "original-image-pixels"
    assert json.loads(calls[1].args[1])["unchanged_positions_omitted"] is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "permission,endpoint,status",
    [
        ([], "transcribe", 403),
        ([APIKeyPermission.HARNESS_RUN.value], "transcribe", 200),
        ([APIKeyPermission.HARNESS_RUN.value], "recordings", 403),
        (
            [
                APIKeyPermission.HARNESS_RUN.value,
                APIKeyPermission.TERMINAL_ACCESS.value,
            ],
            "recordings",
            200,
        ),
    ],
)
def test_voice_api_permission_boundaries(
    harness_workspace, monkeypatch, recording, permission, endpoint, status
) -> None:
    """Persisting artifacts needs workspace-write authority beyond dictation."""
    service = SimpleNamespace(
        transcribe=AsyncMock(
            return_value=VoiceTranscript(
                text="hello", segments=[], timing="approximate"
            )
        ),
        save_recording=AsyncMock(return_value="/workspace/test.json"),
    )
    monkeypatch.setattr("apps.harness.voice_api.VoiceService", lambda: service)
    token = "voice-test-key"
    APIKey.objects.create(
        user=harness_workspace.created_by,
        name="voice",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permission,
    )
    body = (
        recording
        if endpoint == "recordings"
        else {
            "audio_base64": "YQ==",
            "mime_type": "audio/webm",
            "model": "test",
            "duration_ms": 1000,
        }
    )
    response = Client().post(
        f"/api/v1/workspaces/{harness_workspace.id}/voice/{endpoint}/",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(harness_workspace.runner.organization_id),
    )
    assert response.status_code == status
    assert service.transcribe.await_count + service.save_recording.await_count == (
        1 if status == 200 else 0
    )


@pytest.mark.parametrize("audio", ["not-base64", ""])
async def test_invalid_audio_is_rejected_before_provider(monkeypatch, audio) -> None:
    """Malformed audio never triggers a billable request."""
    provider = SimpleNamespace(transcribe=AsyncMock())
    with pytest.raises(ValueError):
        await VoiceService(provider).transcribe(
            uuid.uuid4(),
            1,
            VoiceTranscribeIn(
                audio_base64=audio, mime_type="audio/webm", model="test", duration_ms=1
            ),
        )
    provider.transcribe.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
def test_voice_api_rejects_other_owners(harness_workspace, monkeypatch) -> None:
    """Even an org member with run permission cannot read another owner's config."""
    from django.contrib.auth import get_user_model

    from apps.organizations.models import Membership, MembershipRole

    other = get_user_model().objects.create_user(email="other-voice@example.com")
    Membership.objects.create(
        user=other,
        organization=harness_workspace.runner.organization,
        role=MembershipRole.MEMBER,
    )
    token = "other-voice-key"
    APIKey.objects.create(
        user=other,
        name="voice",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_RUN.value],
    )
    service = SimpleNamespace(config=AsyncMock())
    monkeypatch.setattr("apps.harness.voice_api.VoiceService", lambda: service)
    response = Client().get(
        f"/api/v1/workspaces/{harness_workspace.id}/voice/config/",
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(harness_workspace.runner.organization_id),
    )
    assert response.status_code == 404
    service.config.assert_not_awaited()


@pytest.mark.parametrize("body", [[], {"data": None}, {"data": []}])
async def test_malformed_catalog_has_safe_fallback(body) -> None:
    """A catalog outage or shape change must not prevent custom model selection."""
    from apps.harness.voice_provider import _catalog

    _catalog.clear()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        models = await VoiceProvider(client).models(
            VoiceAccount("openrouter", "secret")
        )
    assert models[0]["id"] == "openrouter/elevenlabs/scribe-v2"
    _catalog.clear()


@pytest.mark.django_db(transaction=True)
def test_cleanup_model_falls_back_to_build(organization) -> None:
    """New users need no unrelated small-model setting if Build is configured."""
    from apps.harness.repositories import AgentConfigRepository

    AgentConfigRepository.upsert(
        organization,
        "build",
        model="openrouter/test",
        effort="low",
        inherit_model=False,
        effort_strategy="fixed",
    )
    assert VoiceService._cleanup_model(organization.id) == ("openrouter/test", "low")


@pytest.mark.parametrize(
    "small_model,expected",
    [("openrouter/small", "openrouter/small"), ("", "openrouter/build")],
)
async def test_voice_config_exposes_effective_cleanup_model(
    monkeypatch, small_model: str, expected: str
) -> None:
    """The composer shows the model actually chosen by the existing fallback."""
    monkeypatch.setattr(
        "apps.harness.voice_service.ProviderConfigRepository.get_by_org",
        lambda org: SimpleNamespace(small_model=small_model, small_effort="low"),
    )
    monkeypatch.setattr(
        "apps.harness.voice_service.AgentConfigRepository.get_by_org_agent",
        lambda org, agent: SimpleNamespace(model="openrouter/build", effort="high"),
    )
    provider = SimpleNamespace(
        models=AsyncMock(return_value=[{"id": "openrouter/speech", "name": "Speech"}])
    )
    service = VoiceService(provider)
    monkeypatch.setattr(
        service,
        "_accounts",
        lambda *args: {"openrouter": VoiceAccount("openrouter", "secret")},
    )

    config = await service.config(uuid.uuid4(), 1)

    assert config.available
    assert config.cleanup_model == expected
    assert config.default_model == "openrouter/elevenlabs/scribe-v2"
    assert "secret" not in config.model_dump_json()


def test_partial_provider_segments_keep_all_speech() -> None:
    """Timing metadata covering only part of a transcript cannot lose its rest."""
    segments, timing = VoiceService._segments(
        {
            "text": "Change this button. Keep the colors.",
            "segments": [{"start": 0, "end": 1, "text": "Change this button."}],
        },
        3000,
    )
    assert timing == "approximate"
    assert segments[0].text == "Change this button. Keep the colors."
    assert segments[0].raw_text == segments[0].text


@pytest.mark.parametrize("punctuate_words", [True, False])
def test_word_timing_reconstructs_sentences(punctuate_words: bool) -> None:
    """Use actual first/last word times, even when punctuation is only in text."""
    raw = {
        "text": "Change this. Keep that!",
        "words": [
            {"word": "Change", "start": 0.2, "end": 0.4},
            {"word": "this." if punctuate_words else "this", "start": 0.5, "end": 0.8},
            {"word": "Keep", "start": 1.3, "end": 1.5},
            {"word": "that!" if punctuate_words else "that", "start": 1.6, "end": 1.9},
        ],
        # Sentences reconstructed from words must take precedence over phrases.
        "segments": [{"start": 0, "end": 2, "text": "Change this. Keep that!"}],
    }
    segments, timing = VoiceService._segments(raw, 2500)
    assert timing == "provider"
    assert [(item.start_ms, item.end_ms, item.text) for item in segments] == [
        (200, 800, "Change this."),
        (1300, 1900, "Keep that!"),
    ]
    assert all(item.raw_text == item.text for item in segments)


@pytest.mark.parametrize(
    "words",
    [
        [{"word": "Hello", "start": 0, "end": 0.4}],
        [{"word": "Hello there.", "start": float("nan"), "end": 0.9}],
        [{"word": "Hello there.", "start": 0, "end": float("inf")}],
        [{"word": "Hello there.", "start": 0.9, "end": 0.2}],
        [{"word": "Hello there.", "start": -0.1, "end": 0.9}],
        [{"word": "Hello there.", "start": 0, "end": 1.2}],
        [{"word": None, "start": 0, "end": 0.9}],
        "unexpected shape",
    ],
)
def test_invalid_or_partial_words_fall_back_to_segments(words) -> None:
    """Malformed and incomplete words cannot lose speech or fabricate timings."""
    segments, timing = VoiceService._segments(
        {
            "text": "Hello there.",
            "words": words,
            "segments": [{"start": 0.1, "end": 0.9, "text": "Hello there."}],
        },
        1000,
    )
    assert timing == "provider"
    assert [(item.start_ms, item.end_ms, item.raw_text) for item in segments] == [
        (100, 900, "Hello there.")
    ]


def test_partial_words_without_segments_keep_full_transcript() -> None:
    """The final fallback always includes the complete recognized text."""
    segments, timing = VoiceService._segments(
        {
            "text": "Hello there.",
            "words": [{"word": "Hello", "start": 0.1, "end": 0.4}],
        },
        1000,
    )
    assert timing == "approximate"
    assert segments[0].text == "Hello there."
    assert (segments[0].start_ms, segments[0].end_ms) == (0, 1000)


def test_word_punctuation_without_transcript_punctuation() -> None:
    """Word punctuation can identify boundaries without changing source text."""
    segments, timing = VoiceService._segments(
        {
            "text": "Hello there How are you",
            "words": [
                {"word": "Hello", "start": 0.1, "end": 0.2},
                {"word": "there.", "start": 0.3, "end": 0.4},
                {"word": "How", "start": 1, "end": 1.1},
                {"word": "are", "start": 1.2, "end": 1.3},
                {"word": "you?", "start": 1.4, "end": 1.5},
            ],
        },
        2000,
    )
    assert timing == "provider"
    assert [item.text for item in segments] == ["Hello there", "How are you"]
    assert [item.start_ms for item in segments] == [100, 1000]


def test_sentence_boundary_inside_one_word_keeps_measured_phrase() -> None:
    """A combined word token has no measured internal time and is never split."""
    segments, timing = VoiceService._segments(
        {
            "text": "Hello. There!",
            "words": [{"word": "Hello. There!", "start": 0.1, "end": 0.9}],
        },
        1000,
    )
    assert timing == "provider"
    assert len(segments) == 1
    assert segments[0].text == "Hello. There!"


@pytest.mark.parametrize("silent", [True, False])
def test_confident_nonspeech_is_removed_without_text_heuristics(silent: bool) -> None:
    """Only acoustic provider metadata can suppress a hallucinated phrase."""
    raw = {
        "text": "Vielen Dank. Change this.",
        "segments": [
            {
                "start": 0,
                "end": 1,
                "text": "Vielen Dank.",
                "no_speech_prob": 0.98,
                "avg_logprob": -2 if silent else -0.1,
            },
            {
                "start": 2,
                "end": 3,
                "text": "Change this.",
                "no_speech_prob": 0.02,
                "avg_logprob": -0.1,
            },
        ],
        "words": [
            {"word": "Vielen", "start": 0.1, "end": 0.3},
            {"word": "Dank.", "start": 0.4, "end": 0.8},
            {"word": "Change", "start": 2.1, "end": 2.3},
            {"word": "this.", "start": 2.4, "end": 2.8},
        ],
    }
    segments, timing = VoiceService._segments(raw, 4000)
    assert timing == "provider"
    assert [item.text for item in segments] == (
        ["Change this."] if silent else ["Vielen Dank.", "Change this."]
    )
    assert raw["text"] == "Vielen Dank. Change this."


@pytest.mark.parametrize("silent", [True, False])
def test_repeated_text_is_not_discarded_without_acoustic_evidence(silent: bool) -> None:
    """Repeated legitimate dictation and incomplete metadata remain recoverable."""
    raw = {
        "text": "Vielen Dank. Vielen Dank.",
        "segments": [
            {
                "start": 0,
                "end": 1,
                "text": "Vielen Dank.",
                "no_speech_prob": 0.98 if silent else 0.01,
                "avg_logprob": -2,
            },
        ],
    }
    segments, timing = VoiceService._segments(raw, 3000)
    assert timing == "approximate"
    assert segments[0].text == raw["text"]


async def test_saved_speech_model_is_authoritative(monkeypatch) -> None:
    """A stale composer cannot override the model configured in Settings."""
    provider = SimpleNamespace(transcribe=AsyncMock(return_value={"text": "hello"}))
    service = VoiceService(provider)
    monkeypatch.setattr(
        service,
        "_accounts",
        lambda *args: {"openrouter": VoiceAccount("openrouter", "secret")},
    )
    monkeypatch.setattr(
        service, "_speech_model", lambda *args: "openrouter/saved-speech"
    )
    monkeypatch.setattr(service, "_cleanup_model", lambda *args: ("cleanup", ""))
    monkeypatch.setattr(service, "_clean", AsyncMock(return_value="hello"))
    result = await service.transcribe(
        uuid.uuid4(),
        1,
        VoiceTranscribeIn(
            audio_base64="YXVkaW8=",
            mime_type="audio/webm",
            duration_ms=1000,
            model="openrouter/stale-composer",
        ),
    )
    assert provider.transcribe.await_args.args[1] == "saved-speech"
    assert result.speech_model == "openrouter/saved-speech"
    assert result.raw_text == "hello"
    assert not result.cleanup_warning


async def test_config_catalog_available_before_cleanup_setup(monkeypatch) -> None:
    """Settings can choose speech and cleanup models in either order."""
    provider = SimpleNamespace(
        models=AsyncMock(return_value=[{"id": "openrouter/speech", "name": "Speech"}])
    )
    service = VoiceService(provider)
    monkeypatch.setattr(
        service,
        "_accounts",
        lambda *args: {"openrouter": VoiceAccount("openrouter", "secret")},
    )
    monkeypatch.setattr(service, "_speech_model", lambda *args: "openrouter/speech")
    monkeypatch.setattr(service, "_cleanup_model", lambda *args: ("", ""))
    result = await service.config(uuid.uuid4(), 1)
    assert not result.available
    assert result.models[0].id == "openrouter/speech"
    assert "cleanup" in result.reason


@pytest.mark.django_db(transaction=True)
def test_voice_cleanup_override_and_saved_speech_defaults(organization) -> None:
    """Voice overrides are independent of unrelated helper-model defaults."""
    from apps.harness.services import ProviderConfigService

    service = ProviderConfigService()
    service.save_config(
        organization_id=organization.id,
        small_model="openrouter/small",
        small_effort="high",
        speech_model="openrouter/speech",
        voice_cleanup_model="openrouter/editor",
    )
    assert VoiceService._cleanup_model(organization.id) == ("openrouter/editor", "")
    assert (
        VoiceService._speech_model(organization.id, ["openrouter"])
        == "openrouter/speech"
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "permissions,status", [([], 403), ([APIKeyPermission.HARNESS_RUN.value], 200)]
)
def test_voice_settings_config_permission_boundary(
    harness_workspace, monkeypatch, permissions, status
) -> None:
    """Org-level Settings discovery does not require or bypass workspace access."""
    from apps.harness.voice_schemas import VoiceConfig

    service = SimpleNamespace(
        config=AsyncMock(return_value=VoiceConfig(available=True))
    )
    monkeypatch.setattr("apps.harness.voice_api.VoiceService", lambda: service)
    token = "voice-settings-key"
    APIKey.objects.create(
        user=harness_workspace.created_by,
        name="voice-settings",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    response = Client().get(
        "/api/v1/voice/config/",
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(harness_workspace.runner.organization_id),
    )
    assert response.status_code == status
    assert service.config.await_count == (1 if status == 200 else 0)


@pytest.mark.parametrize(
    "intervals",
    [
        [{"start": -1, "end": 0.5, "text": "Hello"}],
        [{"start": 0, "end": 10, "text": "Hello"}],
        [
            {"start": 0.5, "end": 0.8, "text": "Hel"},
            {"start": 0.1, "end": 0.3, "text": "lo"},
        ],
    ],
)
def test_impossible_provider_timing_is_not_repaired_into_fake_precision(intervals):
    """Only plausible measured timestamps may be marked as provider timing."""
    segments, timing = VoiceService._segments(
        {"text": "Hello", "segments": intervals}, 1000
    )
    assert timing == "approximate"
    assert segments[0].text == "Hello"


async def test_global_cleanup_is_independent_of_immutable_source_timing(
    monkeypatch,
) -> None:
    """Edited text may resolve corrections while ASR source words stay timed."""
    original = [
        VoiceSegment(
            start_ms=0,
            end_ms=1000,
            text="Move blue right.",
            raw_text="Move blue right.",
        ),
        VoiceSegment(
            start_ms=1500, end_ms=2300, text="No, green.", raw_text="No, green."
        ),
        VoiceSegment(start_ms=2500, end_ms=3000, text="Where?", raw_text="Where?"),
        VoiceSegment(start_ms=3500, end_ms=4000, text="Where?", raw_text="Where?"),
    ]

    async def stream(model, messages, tools, options):
        assert "Resolve explicit self-corrections" in messages[0].content
        assert "Repeated complete questions" in messages[0].content
        assert json.loads(messages[1].content) == [item.text for item in original]
        yield Delta(text='{"text":"Move green right. Where? Where?"}')

    resolver = SimpleNamespace(
        prepare_adapters=lambda: None,
        resolve=lambda model: SimpleNamespace(
            model_id="editor", adapter=SimpleNamespace(chat_stream=stream)
        ),
    )
    monkeypatch.setattr(
        "apps.harness.voice_service.ProviderConfigService.build_resolver",
        lambda *args, **kwargs: resolver,
    )
    cleaned = await VoiceService()._clean(uuid.uuid4(), 1, "editor", "", original)
    assert cleaned == "Move green right. Where? Where?"
    assert original[0].text == original[0].raw_text == "Move blue right."
    assert original[0].start_ms == 0


async def test_transcription_keeps_source_segments_with_global_cleaned_text(
    monkeypatch,
) -> None:
    """A corrected color is not falsely presented as an earlier spoken word."""
    raw = {
        "text": "Move blue. No, green.",
        "segments": [
            {"start": 0, "end": 1, "text": "Move blue."},
            {"start": 1, "end": 2, "text": "No, green."},
        ],
    }
    service = VoiceService(SimpleNamespace(transcribe=AsyncMock(return_value=raw)))
    monkeypatch.setattr(
        service,
        "_accounts",
        lambda *args: {"openrouter": VoiceAccount("openrouter", "secret")},
    )
    monkeypatch.setattr(service, "_speech_model", lambda *args: "openrouter/speech")
    monkeypatch.setattr(service, "_cleanup_model", lambda *args: ("cleanup", ""))
    monkeypatch.setattr(service, "_clean", AsyncMock(return_value="Move green."))
    result = await service.transcribe(
        uuid.uuid4(),
        1,
        VoiceTranscribeIn(
            audio_base64="YXVkaW8=", mime_type="audio/webm", duration_ms=2000
        ),
    )
    assert result.text == "Move green."
    assert result.segments_kind == "source"
    assert [item.text for item in result.segments] == ["Move blue.", "No, green."]
    assert [item.start_ms for item in result.segments] == [0, 1000]


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "Ähm, bitte verschiebe den grünen Knopf.",
            "bitte verschiebe den grünen Knopf.",
        ),
        ("Äh… ähm, ändere den Knopf.", "ändere den Knopf."),
        ("  Ähm, npm run build  ", "npm run build"),
        ("Uh, run git status.", "run git status."),
        ("Ähm", ""),
        (
            '"Ähm," soll wörtlich im Label stehen.',
            '"Ähm," soll wörtlich im Label stehen.',
        ),
        (
            "„Ähm,“ soll wörtlich im Label stehen.",
            "„Ähm,“ soll wörtlich im Label stehen.",
        ),
        ("`ähm` is the variable name.", "`ähm` is the variable name."),
        (
            "Um das Element zu bewegen, ziehe es.",
            "Um das Element zu bewegen, ziehe es.",
        ),
        ("Das Wort ähm soll im Label stehen.", "Das Wort ähm soll im Label stehen."),
        ("Ähm ist das gesuchte Wort.", "Ähm ist das gesuchte Wort."),
        ("Ähm: ein Füllwort.", "Ähm: ein Füllwort."),
        ("uhmHandler is undefined.", "uhmHandler is undefined."),
    ],
)
def test_cleanup_normalization_only_removes_clear_unquoted_initial_hesitation(
    text, expected
):
    """The fallback cannot rewrite quoted fillers, prepositions or identifiers."""
    from apps.harness.voice_cleanup import normalize_edited_dictation

    assert normalize_edited_dictation(text) == expected


async def test_cleanup_strips_unquoted_hesitation_left_by_editor(monkeypatch) -> None:
    """A model cleanup miss must not reintroduce the observed leading Ähm."""

    async def stream(model, messages, tools, options):
        yield Delta(text='{"text":"Ähm, bitte ändere den Knopf."}')

    resolver = SimpleNamespace(
        prepare_adapters=lambda: None,
        resolve=lambda model: SimpleNamespace(
            model_id="editor", adapter=SimpleNamespace(chat_stream=stream)
        ),
    )
    monkeypatch.setattr(
        "apps.harness.voice_service.ProviderConfigService.build_resolver",
        lambda *args, **kwargs: resolver,
    )
    source = VoiceSegment(
        start_ms=500,
        end_ms=2000,
        text="Ähm, bitte ändere den Knopf.",
        raw_text="Ähm, bitte ändere den Knopf.",
    )
    result = await VoiceService()._clean(uuid.uuid4(), 1, "editor", "", [source])
    assert result == "bitte ändere den Knopf."
    assert source.raw_text == source.text == "Ähm, bitte ändere den Knopf."
