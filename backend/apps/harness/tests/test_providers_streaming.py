"""Characterize shared timeout and SSE transport contracts across providers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from apps.harness.providers import _streaming as streaming
from apps.harness.providers.base import (
    ChatOptions,
    LLMMessage,
    ProviderHeaderTimeoutError,
    ProviderResponseError,
    ProviderStreamTimeoutError,
    ProviderTimeoutError,
)
from apps.harness.providers.chatgpt import ChatGPTAdapter
from apps.harness.providers.openai_compatible import OpenAICompatibleAdapter
from apps.harness.providers.openrouter import OpenRouterAdapter


@pytest.fixture(params=["openrouter", "chatgpt"])
def sse_adapter(request: pytest.FixtureRequest) -> Any:
    """Exercise the same transport contract through both wire protocols."""
    return _adapter(request.param)


def _adapter(name: str, client: httpx.AsyncClient | None = None) -> Any:
    if name == "openrouter":
        return OpenRouterAdapter("test-key", client=client)
    if name == "openai-compatible":
        return OpenAICompatibleAdapter("https://provider.test", client=client)

    async def credentials(*_args: Any, **_kwargs: Any) -> dict:
        return {"access": "test-token"}

    return ChatGPTAdapter(
        {"access": "test-token"}, fresh_credentials=credentials, client=client
    )


def _text_frame(adapter: Any, text: str) -> str:
    if adapter.name == "openrouter":
        return f'data: {{"choices":[{{"delta":{{"content":"{text}"}}}}]}}\n'
    return f'data: {{"type":"response.output_text.delta","delta":"{text}"}}\n'


class _BodyStream(httpx.AsyncByteStream):
    """Yield one body and optionally stall or fail on the next read."""

    def __init__(self, body: str = "", *, failure: Exception | None = None) -> None:
        self.body = body.encode()
        self.failure = failure
        self.reading = asyncio.Event()
        self.closed = asyncio.Event()
        self.stall = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            if self.body:
                yield self.body
            self.reading.set()
            if self.failure:
                raise self.failure
            if self.stall:
                await asyncio.Event().wait()
        finally:
            self.closed.set()


@pytest.mark.parametrize("phase", [None, 0, -1, 2, 10])
@pytest.mark.parametrize("deadline", [None, 99, 100, 103])
def test_effective_timeout_combines_phase_and_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch, phase: float | None, deadline: float | None
) -> None:
    """Disabled phases still honor the total cap, including an expired cap."""
    monkeypatch.setattr(streaming, "time", SimpleNamespace(monotonic=lambda: 100))
    positive_phase = phase if phase is not None and phase > 0 else None
    if deadline is None:
        expected = positive_phase
    else:
        remaining = max(deadline - 100, 0)
        expected = min(positive_phase, remaining) if positive_phase else remaining
    assert streaming.effective_timeout(phase, deadline) == expected


@pytest.mark.parametrize("total", [None, 0, -1, 2.5])
def test_stream_deadline_is_optional_and_monotonic(
    monkeypatch: pytest.MonkeyPatch, total: float | None
) -> None:
    """Only positive total durations create a deadline."""
    monkeypatch.setattr(streaming, "time", SimpleNamespace(monotonic=lambda: 100))
    expected = 100 + total if total is not None and total > 0 else None
    assert streaming.stream_deadline(ChatOptions(timeout_seconds=total)) == expected


@pytest.mark.parametrize("chunk_timeout", [None, 1])
async def test_sse_ignores_non_data_and_done_lines_until_eof(
    sse_adapter: Any, chunk_timeout: float | None
) -> None:
    """Keep whitespace, heartbeat, DONE and end-of-stream behavior identical."""
    body = (
        "\n: heartbeat\nevent: ignored\nid: 123\ndata:\n"
        "  "
        + _text_frame(sse_adapter, "first")
        + "data: [DONE]\n"
        + _text_frame(sse_adapter, "after-done")
    )
    response = httpx.Response(200, stream=_BodyStream(body))
    deltas = [
        delta
        async for delta in sse_adapter._parse_stream(
            response, chunk_timeout=chunk_timeout
        )
    ]
    assert [delta.text for delta in deltas] == ["first", "after-done"]


async def test_sse_idle_timeout_keeps_provider_error_type(sse_adapter: Any) -> None:
    """An idle read remains a retryable stream timeout with provider identity."""
    stream = _BodyStream(_text_frame(sse_adapter, "first"))
    stream.stall = True
    deltas = []
    with pytest.raises(ProviderStreamTimeoutError, match="SSE read timed out") as exc:
        async for delta in sse_adapter._parse_stream(
            httpx.Response(200, stream=stream), chunk_timeout=0.01
        ):
            deltas.append(delta)
    assert [delta.text for delta in deltas] == ["first"]
    assert exc.value.provider == sse_adapter.name
    assert isinstance(exc.value.__cause__, TimeoutError)
    assert stream.closed.is_set()


async def test_sse_expired_total_deadline_takes_precedence(sse_adapter: Any) -> None:
    """The total cap wins even when the idle timeout is disabled."""
    label = "OpenRouter" if sse_adapter.name == "openrouter" else "ChatGPT"
    stream = _BodyStream()
    stream.stall = True
    with pytest.raises(ProviderTimeoutError) as exc:
        async for _ in sse_adapter._parse_stream(
            httpx.Response(200, stream=stream), deadline=0, total_timeout=1.25
        ):
            pytest.fail("Expired streams must not emit a delta")
    assert type(exc.value) is ProviderTimeoutError
    assert str(exc.value) == f"{label} request timed out after 1250ms"
    assert exc.value.provider == sse_adapter.name


async def test_sse_cancellation_reaches_the_pending_read(sse_adapter: Any) -> None:
    """Cancelling a consumer cancels the current read without wrapping it."""
    stream = _BodyStream()
    stream.stall = True

    async def consume() -> None:
        async for _ in sse_adapter._parse_stream(
            httpx.Response(200, stream=stream), chunk_timeout=5
        ):
            pytest.fail("A stalled stream must not emit data")

    task = asyncio.create_task(consume())
    await asyncio.wait_for(stream.reading.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed.is_set()


async def test_sse_transport_failure_is_not_reclassified(sse_adapter: Any) -> None:
    """Provider chat_stream owns HTTP error mapping above the line reader."""
    failure = httpx.ReadError("connection dropped")
    stream = _BodyStream(failure=failure)
    with pytest.raises(httpx.ReadError) as exc:
        async for _ in sse_adapter._parse_stream(
            httpx.Response(200, stream=stream), chunk_timeout=1
        ):
            pytest.fail("A failed read must not emit data")
    assert exc.value is failure


async def test_sse_malformed_payload_remains_a_wire_protocol_error(
    sse_adapter: Any,
) -> None:
    """Framing leaves malformed JSON detection in the concrete adapter."""
    with pytest.raises(ProviderResponseError, match="Malformed SSE payload") as exc:
        async for _ in sse_adapter._parse_stream(
            httpx.Response(200, stream=_BodyStream("data: {invalid}\n"))
        ):
            pytest.fail("Malformed payloads must not emit data")
    assert exc.value.provider == sse_adapter.name


@pytest.mark.parametrize("provider", ["openrouter", "chatgpt", "openai-compatible"])
@pytest.mark.parametrize("phase", ["header", "total"])
async def test_request_header_wait_preserves_timeout_classification(
    provider: str, phase: str
) -> None:
    """Header and total expiry keep their original messages and provider IDs."""
    cancelled = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("A blocked request cannot return a response")

    options = ChatOptions(
        header_timeout_seconds=0.01 if phase == "header" else None,
        timeout_seconds=0.01 if phase == "total" else None,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(provider, client)
        with pytest.raises(ProviderTimeoutError) as exc:
            async for _ in adapter.chat_stream(
                "model", [LLMMessage(role="user", content="hello")], [], options
            ):
                pytest.fail("No response headers arrived")
    assert exc.value.provider == provider
    if phase == "header":
        assert type(exc.value) is ProviderHeaderTimeoutError
        assert exc.value.timeout_seconds == 0.01
        assert str(exc.value) == "Provider response headers timed out after 10ms"
    else:
        label = {
            "openrouter": "OpenRouter",
            "chatgpt": "ChatGPT",
            "openai-compatible": "OpenAI-compatible",
        }[provider]
        assert type(exc.value) is ProviderTimeoutError
        assert str(exc.value) == f"{label} request timed out after 10ms"
    assert isinstance(exc.value.__cause__, TimeoutError)
    assert cancelled.is_set()


def test_httpx_stream_timeout_disables_read() -> None:
    """httpx must not apply a read timeout between SSE tokens."""
    timeout = streaming.httpx_stream_timeout(ChatOptions())
    assert timeout.read is None
    assert timeout.connect == 300.0
    assert timeout.write == 300.0


def test_positive_timeout_treats_zero_as_disabled() -> None:
    """Zero and negative timeouts disable that phase, like OpenCode false."""
    assert streaming.positive_timeout(None) is None
    assert streaming.positive_timeout(0) is None
    assert streaming.positive_timeout(-1) is None
    assert streaming.positive_timeout(1.5) == 1.5
