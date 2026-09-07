"""Tests for the OpenRouter provider adapter (mocked SSE)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from apps.harness.providers.base import (
    DEFAULT_CHUNK_TIMEOUT_SECONDS,
    DEFAULT_HEADER_TIMEOUT_SECONDS,
    ChatOptions,
    LLMMessage,
    ProviderAuthError,
    ProviderHeaderTimeoutError,
    ProviderResponseError,
    ProviderStreamTimeoutError,
    ProviderTimeoutError,
    ToolSchema,
)
from apps.harness.providers.openrouter import (
    OpenRouterAdapter,
    httpx_stream_timeout,
    positive_timeout,
)


def _sse_payload(chunks: list[dict]) -> bytes:
    """Encode chunks as an SSE byte stream."""
    lines: list[str] = []
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk)}")
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode()


def _mock_client(payload: bytes, status_code: int = 200) -> httpx.AsyncClient:
    """Build an adapter client backed by a mock transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            content=payload,
            headers={"Content-Type": "text/event-stream"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _DelayedByteStream(httpx.AsyncByteStream):
    """Yield body chunks after optional per-chunk pauses."""

    def __init__(self, chunks: list[bytes], delays: list[float]) -> None:
        self._chunks = chunks
        self._delays = delays

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay, chunk in zip(self._delays, self._chunks):
            if delay > 0:
                await asyncio.sleep(delay)
            yield chunk


class _DelayedTransport(httpx.AsyncBaseTransport):
    """Async transport that can stall headers and/or body chunks."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        header_delay: float = 0.0,
        chunks: list[bytes] | None = None,
        chunk_delays: list[float] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._status_code = status_code
        self._header_delay = header_delay
        self._chunks = chunks or []
        self._chunk_delays = chunk_delays or [0.0] * len(self._chunks)
        self._error = error

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._error is not None:
            raise self._error
        if self._header_delay > 0:
            await asyncio.sleep(self._header_delay)
        return httpx.Response(
            self._status_code,
            headers={"Content-Type": "text/event-stream"},
            stream=_DelayedByteStream(self._chunks, self._chunk_delays),
            request=request,
        )


def _delayed_client(
    *,
    header_delay: float = 0.0,
    chunks: list[bytes] | None = None,
    chunk_delays: list[float] | None = None,
    error: Exception | None = None,
) -> httpx.AsyncClient:
    """Build a client with delayed headers or SSE chunks."""
    return httpx.AsyncClient(
        transport=_DelayedTransport(
            header_delay=header_delay,
            chunks=chunks,
            chunk_delays=chunk_delays,
            error=error,
        )
    )


def test_chat_options_timeout_defaults_match_opencode() -> None:
    """Header and chunk idle default to 300s; no total wall-clock cap."""
    opts = ChatOptions()
    assert opts.header_timeout_seconds == DEFAULT_HEADER_TIMEOUT_SECONDS == 300.0
    assert opts.chunk_timeout_seconds == DEFAULT_CHUNK_TIMEOUT_SECONDS == 300.0
    assert opts.timeout_seconds is None


def test_httpx_stream_timeout_disables_read() -> None:
    """httpx must not apply a read timeout between SSE tokens."""
    timeout = httpx_stream_timeout(ChatOptions())
    assert timeout.read is None
    assert timeout.connect == 300.0
    assert timeout.write == 300.0


def test_positive_timeout_treats_zero_as_disabled() -> None:
    """Zero and negative timeouts disable that phase, like OpenCode false."""
    assert positive_timeout(None) is None
    assert positive_timeout(0) is None
    assert positive_timeout(-1) is None
    assert positive_timeout(1.5) == 1.5


async def test_chat_stream_happy_path_text_toolcall_reasoning() -> None:
    """Text, reasoning, tool-call and usage deltas stream correctly."""
    payload = _sse_payload(
        [
            {
                "choices": [
                    {
                        "delta": {"content": "Hello"},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"reasoning_content": "thinking..."},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path": "a"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.0123,
                },
            },
        ]
    )
    adapter = OpenRouterAdapter(api_key="test-key", client=_mock_client(payload))
    deltas = [
        d
        async for d in adapter.chat_stream(
            "model-x",
            [LLMMessage(role="user", content="hi")],
            [ToolSchema(name="read", description="r")],
        )
    ]

    assert "".join(d.text for d in deltas) == "Hello"
    assert "".join(d.reasoning for d in deltas) == "thinking..."
    tool_calls = [c for d in deltas for c in d.tool_calls]
    assert tool_calls and tool_calls[0]["name"] == "read"
    assert tool_calls[0]["id"] == "call_1"
    usages = [d.usage for d in deltas if d.usage is not None]
    assert usages and usages[-1].total_tokens == 15
    assert usages[-1].prompt_tokens == 10
    assert usages[-1].cost == pytest.approx(0.0123)
    assert deltas[-1].finish_reason == "tool_calls"


async def test_chat_stream_auth_error() -> None:
    """HTTP 401 raises ProviderAuthError."""
    adapter = OpenRouterAdapter(
        api_key="bad-key", client=_mock_client(b"unauthorized", 401)
    )
    with pytest.raises(ProviderAuthError):
        async for _ in adapter.chat_stream("m", [], []):
            pass


async def test_chat_stream_error_enriches_response_context() -> None:
    """HTTP errors carry status, lowercased headers, body, retry hint."""
    from apps.harness.providers.base import ProviderRateLimitError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=b"slow down",
            headers={
                "Content-Type": "text/event-stream",
                "Retry-After": "2",
                "X-Should-Retry": "true",
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenRouterAdapter(api_key="k", client=client)
    with pytest.raises(ProviderRateLimitError) as exc_info:
        async for _ in adapter.chat_stream("m", [], []):
            pass
    error = exc_info.value
    assert error.status_code == 429
    assert error.response_headers.get("retry-after") == "2"
    assert error.response_headers.get("x-should-retry") == "true"
    assert "slow down" in error.response_body
    assert error.is_retryable is True


async def test_chat_stream_should_retry_false_hint() -> None:
    """x-should-retry: false surfaces as is_retryable=False on 500 errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            content=b"upstream",
            headers={"X-Should-Retry": "false"},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenRouterAdapter(api_key="k", client=client)
    with pytest.raises(ProviderResponseError) as exc_info:
        async for _ in adapter.chat_stream("m", [], []):
            pass
    error = exc_info.value
    assert error.status_code == 500
    assert error.is_retryable is False
    assert "upstream" in error.response_body


async def test_chat_stream_rate_limit_error() -> None:
    """HTTP 429 raises ProviderRateLimitError."""
    from apps.harness.providers.base import ProviderRateLimitError

    adapter = OpenRouterAdapter(api_key="k", client=_mock_client(b"slow down", 429))
    with pytest.raises(ProviderRateLimitError):
        async for _ in adapter.chat_stream("m", [], []):
            pass


async def test_chat_stream_timeout() -> None:
    """Transport timeouts surface as ProviderTimeoutError."""
    client = _delayed_client(error=httpx.ConnectTimeout("timed out"))
    adapter = OpenRouterAdapter(api_key="k", client=client)
    with pytest.raises(ProviderTimeoutError):
        async for _ in adapter.chat_stream("m", [], []):
            pass


async def test_chat_stream_header_timeout() -> None:
    """Stalled response headers raise ProviderHeaderTimeoutError."""
    payload = _sse_payload([{"choices": [{"delta": {"content": "Hi"}}]}])
    client = _delayed_client(header_delay=1.0, chunks=[payload], chunk_delays=[0.0])
    adapter = OpenRouterAdapter(api_key="k", client=client)
    with pytest.raises(ProviderHeaderTimeoutError, match="headers timed out"):
        async for _ in adapter.chat_stream(
            "m",
            [],
            [],
            ChatOptions(header_timeout_seconds=0.05, chunk_timeout_seconds=5.0),
        ):
            pass


async def test_chat_stream_chunk_idle_timeout() -> None:
    """Idle gaps between SSE chunks raise ProviderStreamTimeoutError."""
    first = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n'
    second = b"data: [DONE]\n"
    client = _delayed_client(
        chunks=[first, second],
        chunk_delays=[0.0, 1.0],
    )
    adapter = OpenRouterAdapter(api_key="k", client=client)
    with pytest.raises(ProviderStreamTimeoutError, match="SSE read timed out"):
        async for _ in adapter.chat_stream(
            "m",
            [],
            [],
            ChatOptions(header_timeout_seconds=5.0, chunk_timeout_seconds=0.05),
        ):
            pass


async def test_chat_stream_malformed_sse() -> None:
    """Invalid JSON in SSE raises ProviderResponseError."""
    adapter = OpenRouterAdapter(api_key="k", client=_mock_client(b"data: not-json{\n"))
    with pytest.raises(ProviderResponseError):
        async for _ in adapter.chat_stream("m", [], []):
            pass


def test_adapter_requires_api_key() -> None:
    """Empty API key is rejected eagerly."""
    with pytest.raises(ValueError):
        OpenRouterAdapter(api_key="")


def test_build_payload_includes_reasoning_effort() -> None:
    """Reasoning effort is sent as OpenRouter reasoning.effort."""
    adapter = OpenRouterAdapter(api_key="k")
    payload = adapter._build_payload(
        "m",
        [],
        [],
        ChatOptions(reasoning_effort="high"),
    )
    assert payload["reasoning"] == {"effort": "high"}


def test_build_payload_omits_reasoning_when_unset() -> None:
    """No reasoning object is sent when effort is empty."""
    adapter = OpenRouterAdapter(api_key="k")
    payload = adapter._build_payload("m", [], [], ChatOptions())
    assert "reasoning" not in payload


def test_build_payload_includes_tool_choice_when_set() -> None:
    """tool_choice is forwarded when ChatOptions sets it."""
    adapter = OpenRouterAdapter(api_key="k")
    payload = adapter._build_payload(
        "m",
        [],
        [],
        ChatOptions(tool_choice="none"),
    )
    assert payload["tool_choice"] == "none"


def test_build_payload_omits_tool_choice_when_unset() -> None:
    """No tool_choice is sent when ChatOptions leaves it empty."""
    adapter = OpenRouterAdapter(api_key="k")
    payload = adapter._build_payload("m", [], [], ChatOptions())
    assert "tool_choice" not in payload


def test_message_to_dict_preserves_multimodal_content() -> None:
    """Image parts are serialized into the OpenAI request payload."""
    adapter = OpenRouterAdapter(api_key="k")
    parts = [
        {"type": "text", "text": "see image"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
        },
    ]
    payload = adapter._build_payload(
        "m",
        [LLMMessage(role="user", content=parts)],
        [],
        ChatOptions(),
    )
    assert payload["messages"][0]["content"] == parts
