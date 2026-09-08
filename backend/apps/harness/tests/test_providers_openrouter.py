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


def test_map_finish_reason_normalizes_variants() -> None:
    """Finish reasons normalize incl. hyphenated content-filter."""
    assert OpenRouterAdapter._map_finish_reason(None) is None
    assert OpenRouterAdapter._map_finish_reason("stop") == "stop"
    assert OpenRouterAdapter._map_finish_reason("length") == "length"
    assert OpenRouterAdapter._map_finish_reason("content_filter") == "content_filter"
    assert OpenRouterAdapter._map_finish_reason("content-filter") == "content_filter"
    assert OpenRouterAdapter._map_finish_reason("function_call") == "tool_calls"
    assert OpenRouterAdapter._map_finish_reason("tool_calls") == "tool_calls"
    assert OpenRouterAdapter._map_finish_reason("something_new") == "unknown"


def test_map_usage_total_fallback_and_cost() -> None:
    """Reported total wins; else prompt+completion; cost preserved."""
    usage = OpenRouterAdapter._map_usage(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 0, "cost": 0.5}
    )
    assert usage is not None
    assert usage.total_tokens == 15
    assert usage.cost == pytest.approx(0.5)
    usage_reported = OpenRouterAdapter._map_usage(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 99}
    )
    assert usage_reported is not None
    assert usage_reported.total_tokens == 99
    assert OpenRouterAdapter._map_usage("nope") is None


def test_parse_chunk_hyphenated_content_filter() -> None:
    """A 'content-filter' finish_reason normalizes to 'content_filter'."""
    adapter = OpenRouterAdapter(api_key="k")
    delta = adapter._parse_chunk('{"choices": [{"delta": {}, "finish_reason": "content-filter"}]}')
    assert delta is not None
    assert delta.finish_reason == "content_filter"


def test_parse_chunk_error_chunk_uses_code_message_format() -> None:
    """SSE error chunks raise 'code: message' and are compaction-visible."""
    import json

    from apps.harness.compaction import is_context_overflow_error

    adapter = OpenRouterAdapter(api_key="k")
    payload = json.dumps(
        {"error": {"code": "context_length_exceeded", "message": "too many tokens"}}
    )
    with pytest.raises(ProviderResponseError) as exc_info:
        adapter._parse_chunk(payload)
    error = exc_info.value
    assert str(error) == "context_length_exceeded: too many tokens"
    assert error.is_retryable is False
    assert is_context_overflow_error(error)


def test_parse_chunk_generic_error_chunk_raises_response_error() -> None:
    """Non-overflow SSE error chunks still surface as ProviderResponseError."""
    import json

    adapter = OpenRouterAdapter(api_key="k")
    payload = json.dumps({"error": {"code": "server_error", "message": "boom"}})
    with pytest.raises(ProviderResponseError, match="server_error: boom"):
        adapter._parse_chunk(payload)


async def test_http_overflow_body_is_compaction_visible() -> None:
    """HTTP error bodies with overflow text are detected by compaction."""
    from apps.harness.compaction import is_context_overflow_error

    body = b'{"error": {"message": "This request exceeds the context window"}}'
    adapter = OpenRouterAdapter(api_key="k", client=_mock_client(body, 400))
    with pytest.raises(ProviderResponseError) as exc_info:
        async for _ in adapter.chat_stream("m", [], []):
            pass
    assert is_context_overflow_error(exc_info.value)


def test_tool_schema_projection_merges_anyof() -> None:
    """anyOf variants merge into properties with type object + additionalProperties false."""
    from apps.harness.providers._lowering import project_openai_tool_schema

    schema = {
        "anyOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"b": {"type": "number"}}},
        ]
    }
    projected = project_openai_tool_schema(schema)
    assert projected["type"] == "object"
    assert set(projected["properties"]) == {"a", "b"}
    assert projected["additionalProperties"] is False


def test_tool_schema_projection_strips_null_variants() -> None:
    """Null anyOf variants are stripped; single record merges into parent."""
    from apps.harness.providers._lowering import project_openai_tool_schema

    schema = {
        "anyOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "null"},
        ]
    }
    projected = project_openai_tool_schema(schema)
    assert projected["type"] == "object"
    assert set(projected.get("properties", {})) == {"a"}


def test_build_payload_tool_choice_mapping() -> None:
    """auto/none/required pass through; tool names become function choice."""
    adapter = OpenRouterAdapter(api_key="k")
    assert (
        adapter._build_payload("m", [], [], ChatOptions(tool_choice="required"))[
            "tool_choice"
        ]
        == "required"
    )
    payload = adapter._build_payload("m", [], [], ChatOptions(tool_choice="read"))
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "read"}}


def test_build_payload_projects_tool_schema() -> None:
    """Tools are sent through the OpenAI schema projection."""
    adapter = OpenRouterAdapter(api_key="k")
    payload = adapter._build_payload(
        "m",
        [],
        [
            ToolSchema(
                name="read",
                description="r",
                parameters={
                    "anyOf": [{"properties": {"a": {"type": "string"}}}],
                },
            )
        ],
        ChatOptions(),
    )
    params = payload["tools"][0]["function"]["parameters"]
    assert params["type"] == "object"
    assert set(params["properties"]) == {"a"}


def test_system_update_wrapped_as_user() -> None:
    """Second+ system messages become <system-update> user text."""
    adapter = OpenRouterAdapter(api_key="k")
    payload = adapter._build_payload(
        "m",
        [
            LLMMessage(role="system", content="sys1"),
            LLMMessage(role="user", content="hi"),
            LLMMessage(role="system", content="upd <x>"),
        ],
        [],
        ChatOptions(),
    )
    assert payload["messages"][0] == {"role": "system", "content": "sys1"}
    wrapped = payload["messages"][-1]
    assert wrapped["role"] == "user"
    assert wrapped["content"].startswith("<system-update>\n")
    assert "&lt;x&gt;" in wrapped["content"]


def test_assistant_reasoning_replay_and_tool_text_join() -> None:
    """Assistant reasoning parts replay; tool dict content joins text."""
    adapter = OpenRouterAdapter(api_key="k")
    payload = adapter._build_payload(
        "m",
        [
            LLMMessage(
                role="assistant",
                content=[
                    {"type": "text", "text": "ans"},
                    {"type": "reasoning", "text": "think"},
                ],
                tool_calls=[{"id": "c1", "name": "read", "arguments": {"p": "a"}}],
            ),
            LLMMessage(
                role="tool",
                content=[
                    {"type": "text", "text": "out"},
                    {"type": "image_url", "image_url": {"url": "data:x"}},
                ],
                tool_call_id="c1",
            ),
        ],
        [],
        ChatOptions(),
    )
    assistant = payload["messages"][0]
    assert assistant["content"] == "ans"
    assert assistant["reasoning_content"] == "think"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"p": "a"}'
    assert payload["messages"][1] == {
        "role": "tool",
        "content": "out",
        "tool_call_id": "c1",
    }
