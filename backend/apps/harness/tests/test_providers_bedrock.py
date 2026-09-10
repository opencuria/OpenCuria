"""Tests for the Amazon Bedrock provider adapter (mocked Converse Stream)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from apps.harness.providers.base import (
    ChatOptions,
    LLMMessage,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    ToolSchema,
)
from botocore import UNSIGNED

from apps.harness.providers.bedrock import (
    AUTH_SETTINGS_MESSAGE,
    BedrockAdapter,
    is_context_overflow,
    resolve_bedrock_model_id,
)


class _MockStream:
    """Async iterator over Converse stream events."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _AioEventStreamLike:
    """Mirrors aiobotocore AioEventStream's broken async-iterator protocol.

    ``__anext__`` is an async generator (uses ``yield``). ``async for`` works
    because it calls ``__aiter__``; ``anext(stream)`` does not.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self.__anext__()

    async def __anext__(self) -> dict[str, Any]:  # type: ignore[misc]
        for event in self._events:
            yield event


class _MockBedrockClient:
    """Minimal bedrock-runtime client stub."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        error: Exception | None = None,
        capture: dict[str, Any] | None = None,
        stream_cls: type[_MockStream] | type[_AioEventStreamLike] = _MockStream,
    ) -> None:
        self._events = events
        self._error = error
        self._capture = capture
        self._stream_cls = stream_cls
        self.meta = MagicMock()
        self.meta.events = MagicMock()

    async def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        if self._capture is not None:
            self._capture["body"] = kwargs
        if self._error is not None:
            raise self._error
        return {"stream": self._stream_cls(self._events)}


class _MockClientContext:
    """Async context manager returning a mock client."""

    def __init__(self, client: _MockBedrockClient) -> None:
        self._client = client

    async def __aenter__(self) -> _MockBedrockClient:
        return self._client

    async def __aexit__(self, *args: Any) -> None:
        return None


class _MockSession:
    """Session stub that records client kwargs."""

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
        capture: dict[str, Any] | None = None,
        stream_cls: type[_MockStream] | type[_AioEventStreamLike] = _MockStream,
    ) -> None:
        self._events = events or []
        self._error = error
        self._capture = capture or {}
        self._stream_cls = stream_cls
        self.client_kwargs: list[dict[str, Any]] = []
        self.last_client: _MockBedrockClient | None = None

    def client(self, **kwargs: Any) -> _MockClientContext:
        self.client_kwargs.append(kwargs)
        client = _MockBedrockClient(
            self._events,
            error=self._error,
            capture=self._capture,
            stream_cls=self._stream_cls,
        )
        self.last_client = client
        return _MockClientContext(client)


def _adapter(
    *,
    credentials: dict[str, Any] | None = None,
    session: _MockSession | None = None,
    region: str = "us-east-1",
) -> tuple[BedrockAdapter, dict[str, Any]]:
    capture: dict[str, Any] = {}
    creds = credentials or {
        "access_key_id": "AKIA",
        "secret_access_key": "secret",
    }
    mock_session = session or _MockSession(capture=capture)
    adapter = BedrockAdapter(
        creds,
        region=region,
        session=mock_session,
    )
    return adapter, capture


async def _collect(adapter: BedrockAdapter, **kwargs: Any) -> list[Any]:
    deltas = []
    async for delta in adapter.chat_stream(
        kwargs.get("model", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
        kwargs.get("messages", [LLMMessage(role="user", content="Hi")]),
        kwargs.get("tools", []),
        kwargs.get("opts"),
    ):
        deltas.append(delta)
    return deltas


def test_resolve_bedrock_model_id_passthrough() -> None:
    """ARNs and prefixed ids are left unchanged."""
    arn = "arn:aws:bedrock:us-east-1:123:model/foo"
    assert resolve_bedrock_model_id(arn, "us-east-1") == arn
    assert resolve_bedrock_model_id("us.foo", "us-west-2") == "us.foo"
    assert resolve_bedrock_model_id("global.foo", "us-east-1") == "global.foo"


def test_resolve_bedrock_model_id_us_prefix() -> None:
    """US commercial regions prefix eligible Claude/Nova models."""
    model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert resolve_bedrock_model_id(model, "us-east-1") == f"us.{model}"
    assert resolve_bedrock_model_id(model, "us-gov-west-1") == model


def test_resolve_bedrock_model_id_eu_prefix() -> None:
    """EU regions prefix eligible models."""
    model = "anthropic.claude-3-haiku-20240307-v1:0"
    assert resolve_bedrock_model_id(model, "eu-central-1") == f"eu.{model}"
    assert resolve_bedrock_model_id("amazon.titan-text", "eu-central-1") == "amazon.titan-text"


def test_resolve_bedrock_model_id_apac_prefixes() -> None:
    """APAC regions use jp., apac., or au. prefixes."""
    claude = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert resolve_bedrock_model_id(claude, "ap-northeast-1") == f"jp.{claude}"
    assert resolve_bedrock_model_id(claude, "ap-northeast-2") == f"jp.{claude}"
    assert resolve_bedrock_model_id(claude, "ap-south-1") == f"apac.{claude}"
    sonnet_au = "anthropic.claude-sonnet-4-5"
    assert resolve_bedrock_model_id(sonnet_au, "ap-southeast-2") == f"au.{sonnet_au}"
    opus_au = "anthropic.claude-opus-4-6-v1"
    assert resolve_bedrock_model_id(opus_au, "ap-southeast-2") == f"au.{opus_au}"


def test_resolve_bedrock_model_id_deepseek_v32_no_prefix() -> None:
    """DeepSeek v3.2 is invoked without a cross-region prefix."""
    model = "deepseek.v3.2-v1:0"
    assert resolve_bedrock_model_id(model, "us-east-1") == model


def test_build_converse_body_messages_tools_and_system() -> None:
    """Request body maps system, messages, tools, and inference config."""
    adapter, _ = _adapter()
    messages = [
        LLMMessage(role="system", content="You are helpful."),
        LLMMessage(role="user", content="Hello"),
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "tool-1",
                    "name": "read",
                    "arguments": '{"path":"a.txt"}',
                }
            ],
        ),
        LLMMessage(role="tool", content="file body", tool_call_id="tool-1"),
        LLMMessage(role="user", content="Thanks"),
    ]
    tools = [
        ToolSchema(
            name="read",
            description="Read a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]
    body = adapter._build_converse_body(
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        messages,
        tools,
        ChatOptions(temperature=0.5, max_tokens=1024),
    )

    assert body["modelId"].startswith("us.")
    assert body["system"] == [{"text": "You are helpful."}]
    assert body["inferenceConfig"]["maxTokens"] == 1024
    assert body["inferenceConfig"]["temperature"] == 0.5
    assert body["toolConfig"]["toolChoice"] == {"auto": {}}
    assert body["toolConfig"]["tools"][0]["toolSpec"]["name"] == "read"

    converse = body["messages"]
    assert converse[0] == {"role": "user", "content": [{"text": "Hello"}]}
    assert converse[1]["role"] == "assistant"
    assert converse[1]["content"][0]["toolUse"]["toolUseId"] == "tool-1"
    assert converse[2]["role"] == "user"
    assert converse[2]["content"][0]["toolResult"]["toolUseId"] == "tool-1"
    merged = converse[2]["content"]
    assert merged[-1] == {"text": "Thanks"}


def test_build_converse_body_thinking_budget_mapping() -> None:
    """Claude thinking uses effort budgets and bumps maxTokens above budget."""
    adapter, _ = _adapter()
    body = adapter._build_converse_body(
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        [LLMMessage(role="user", content="think")],
        [],
        ChatOptions(reasoning_effort="high", max_tokens=1000),
    )
    thinking = body["additionalModelRequestFields"]["thinking"]
    assert thinking == {"type": "enabled", "budget_tokens": 16384}
    assert body["inferenceConfig"]["maxTokens"] == 16385
    assert "temperature" not in body["inferenceConfig"]


async def test_chat_stream_aioeventstream_protocol_with_chunk_timeout() -> None:
    """aiobotocore-shaped streams parse under the default wait_for path."""
    events = [
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    adapter, _ = _adapter(
        session=_MockSession(events, stream_cls=_AioEventStreamLike)
    )
    deltas = await _collect(adapter)
    assert deltas[0].text == "Hi"
    assert deltas[-1].finish_reason == "stop"


async def test_chat_stream_text_reasoning_tool_usage_finish() -> None:
    """Stream events map to text, reasoning, tool call, usage, and finish."""
    events = [
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hi"}}},
        {
            "contentBlockDelta": {
                "contentBlockIndex": 1,
                "delta": {"reasoningContent": {"text": "hmm"}},
            }
        },
        {
            "contentBlockStart": {
                "contentBlockIndex": 2,
                "start": {"toolUse": {"toolUseId": "t1", "name": "bash"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 2,
                "delta": {"toolUse": {"input": '{"cmd":'}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 2,
                "delta": {"toolUse": {"input": '"ls"}'}},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 2}},
        {"messageStop": {"stopReason": "tool_use"}},
        {
            "metadata": {
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 2,
                    "cacheWriteInputTokens": 1,
                }
            }
        },
    ]
    adapter, _ = _adapter(session=_MockSession(events))
    deltas = await _collect(adapter)

    assert deltas[0].text == "Hi"
    assert deltas[1].reasoning == "hmm"
    tool_delta = next(d for d in deltas if d.tool_calls)
    assert tool_delta.tool_calls[0]["id"] == "t1"
    assert tool_delta.tool_calls[0]["name"] == "bash"
    assert tool_delta.tool_calls[0]["arguments"] == '{"cmd":"ls"}'
    finish = deltas[-1]
    assert finish.finish_reason == "tool_calls"
    assert finish.usage is not None
    assert finish.usage.prompt_tokens == 10
    assert finish.usage.completion_tokens == 5
    # inputTokens is the inclusive total: cache subsets are NOT added.
    assert finish.usage.total_tokens == 15


async def test_chat_stream_throttling_event_raises_rate_limit() -> None:
    """In-stream throttling maps to ProviderRateLimitError."""
    events = [{"throttlingException": {"message": "Slow down"}}]
    adapter, _ = _adapter(session=_MockSession(events))
    with pytest.raises(ProviderRateLimitError):
        await _collect(adapter)


async def test_chat_stream_client_error_auth() -> None:
    """ClientError auth codes map to ProviderAuthError."""
    error = ClientError(
        {
            "Error": {"Code": "ExpiredTokenException", "Message": "expired"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "ConverseStream",
    )
    adapter, _ = _adapter(session=_MockSession(error=error))
    with pytest.raises(ProviderAuthError) as exc:
        await _collect(adapter)
    assert AUTH_SETTINGS_MESSAGE in str(exc.value)


async def test_chat_stream_client_error_throttling() -> None:
    """ClientError throttling maps to ProviderRateLimitError."""
    error = ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "rate"},
            "ResponseMetadata": {"HTTPStatusCode": 429},
        },
        "ConverseStream",
    )
    adapter, _ = _adapter(session=_MockSession(error=error))
    with pytest.raises(ProviderRateLimitError):
        await _collect(adapter)


async def test_chat_stream_validation_overflow_non_retryable() -> None:
    """Context overflow validation errors are non-retryable."""
    error = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "Input is too long for requested model",
            },
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "ConverseStream",
    )
    adapter, _ = _adapter(session=_MockSession(error=error))
    with pytest.raises(ProviderResponseError) as exc:
        await _collect(adapter)
    assert exc.value.is_retryable is False


def test_is_context_overflow_opencode_parity() -> None:
    """Bedrock overflow matches OpenCode provider-error.ts patterns."""
    from apps.harness.compaction import is_context_overflow_error

    positives = [
        "Input is too long for requested model",
        "Prompt has 100 tokens, but the configured context size is 50 tokens",
        "input (10 tokens) is longer than the model's context length (5 tokens)",
        "tokens in request more than max tokens allowed",
        "request entity too large",
        "This model's maximum context length is 200000 tokens (of 100 tokens)",
        "exceeds the limit of 100",
        "context length is only 10 tokens",
        "input length 10 exceeds the context length 5",
        "prompt too long; exceeded max context length",
        "too large for model with 5 maximum context length",
        "This request exceeds the context window",
        "model_context_window_exceeded",
        "400 (no body)",
        "413 status code (no body)",
    ]
    for message in positives:
        assert is_context_overflow(message), message
        assert is_context_overflow_error(RuntimeError(message)), message
    negatives = [
        "Throttling error: slow down",
        "Service unavailable: busy",
        "rate limit exceeded",
        "too many requests, retry later",
    ]
    for message in negatives:
        assert not is_context_overflow(message), message


async def test_chat_stream_validation_overflow_stream_event_non_retryable() -> None:
    """In-stream validation overflow is non-retryable and compaction-visible."""
    from apps.harness.compaction import is_context_overflow_error

    events = [
        {
            "validationException": {
                "message": "This request exceeds the context window"
            }
        }
    ]
    adapter, _ = _adapter(session=_MockSession(events))
    with pytest.raises(ProviderResponseError) as exc:
        await _collect(adapter)
    assert exc.value.is_retryable is False
    assert exc.value.response_body
    assert is_context_overflow_error(exc.value)


async def test_chat_stream_throttling_stream_event_retryable() -> None:
    """In-stream throttling stays retryable and is not overflow."""
    from apps.harness.compaction import is_context_overflow_error

    adapter, _ = _adapter(session=_MockSession([]))
    error = adapter._stream_error_from_event(
        {"throttlingException": {"message": "Slow down"}}
    )
    assert isinstance(error, ProviderRateLimitError)
    assert error.is_retryable is True
    assert not is_context_overflow_error(error)


def test_bearer_client_uses_unsigned_and_dummy_keys() -> None:
    """Bearer auth configures UNSIGNED signing and placeholder credentials."""
    adapter = BedrockAdapter({"bearer_token": "bedrock-key"})
    kwargs = adapter._client_kwargs()
    assert kwargs["aws_access_key_id"] == "unused"
    assert kwargs["aws_secret_access_key"] == "unused"
    assert kwargs["config"].signature_version == UNSIGNED


def test_access_key_client_passes_credentials() -> None:
    """Access-key auth forwards explicit AWS credentials to the client."""
    adapter = BedrockAdapter(
        {
            "access_key_id": "AKIAKEY",
            "secret_access_key": "sekret",
            "session_token": "tok",
        },
        region="eu-west-1",
    )
    kwargs = adapter._client_kwargs()
    assert kwargs["aws_access_key_id"] == "AKIAKEY"
    assert kwargs["aws_secret_access_key"] == "sekret"
    assert kwargs["aws_session_token"] == "tok"
    assert kwargs["region_name"] == "eu-west-1"
    assert "config" not in kwargs


async def test_bearer_auth_registers_before_sign_handler() -> None:
    """Bearer auth registers a before-sign handler on the runtime client."""
    session = _MockSession(
        [{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "x"}}}]
    )
    adapter = BedrockAdapter({"bearer_token": "tok"}, session=session)
    await _collect(adapter)
    assert session.last_client is not None
    session.last_client.meta.events.register_first.assert_called_once()
    args = session.last_client.meta.events.register_first.call_args[0]
    assert args[0] == "before-sign.bedrock-runtime.ConverseStream"


def test_map_finish_reason_variants() -> None:
    """Finish reason mapping covers Bedrock stop reasons."""
    assert BedrockAdapter._map_finish_reason("end_turn") == "stop"
    assert BedrockAdapter._map_finish_reason("stop_sequence") == "stop"
    assert BedrockAdapter._map_finish_reason("max_tokens") == "length"
    assert BedrockAdapter._map_finish_reason("tool_use") == "tool_calls"
    assert BedrockAdapter._map_finish_reason("content_filtered") == "content_filter"
    assert BedrockAdapter._map_finish_reason("guardrail_intervened") == "content_filter"
    assert BedrockAdapter._map_finish_reason("something_new") == "unknown"


async def test_chat_stream_reported_total_honored_without_double_count() -> None:
    """Reported totalTokens wins; cache subsets are never added on top."""
    events = [
        {"messageStop": {"stopReason": "content_filtered"}},
        {
            "metadata": {
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 4,
                    "cacheWriteInputTokens": 3,
                    "totalTokens": 15,
                }
            }
        },
    ]
    adapter, _ = _adapter(session=_MockSession(events))
    deltas = await _collect(adapter)
    finish = deltas[-1]
    assert finish.finish_reason == "content_filter"
    assert finish.usage is not None
    assert finish.usage.total_tokens == 15


async def test_chat_stream_stop_with_tool_calls_becomes_tool_calls() -> None:
    """Pending 'stop' + observed tool calls finishes as 'tool_calls'."""
    events = [
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": "t1", "name": "bash"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": "{}"}},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    adapter, _ = _adapter(session=_MockSession(events))
    deltas = await _collect(adapter)
    assert deltas[-1].finish_reason == "tool_calls"


async def test_chat_stream_parallel_tool_blocks_carry_index() -> None:
    """Two toolUse blocks (index 1/2) yield two deltas with index 1/2."""
    events = [
        {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"toolUse": {"toolUseId": "t1", "name": "bash"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 1,
                "delta": {"toolUse": {"input": '{"cmd":"a"}'}},
            }
        },
        {
            "contentBlockStart": {
                "contentBlockIndex": 2,
                "start": {"toolUse": {"toolUseId": "t2", "name": "read"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 2,
                "delta": {"toolUse": {"input": '{"path":"b"}'}},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {"contentBlockStop": {"contentBlockIndex": 2}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]
    adapter, _ = _adapter(session=_MockSession(events))
    deltas = await _collect(adapter)

    tool_deltas = [d for d in deltas if d.tool_calls]
    assert len(tool_deltas) == 2
    by_index = {d.tool_calls[0]["index"]: d.tool_calls[0] for d in tool_deltas}
    assert by_index[1]["id"] == "t1"
    assert by_index[1]["name"] == "bash"
    assert by_index[1]["arguments"] == '{"cmd":"a"}'
    assert by_index[2]["id"] == "t2"
    assert by_index[2]["name"] == "read"
    assert by_index[2]["arguments"] == '{"path":"b"}'


def test_tool_schema_projection_anyof_merges_properties() -> None:
    """Bedrock tool input schemas go through the OpenAI projection."""
    from apps.harness.providers._lowering import project_openai_tool_schema

    projected = project_openai_tool_schema(
        {"anyOf": [{"properties": {"a": {"type": "string"}}}]}
    )
    assert projected["type"] == "object"
    assert set(projected["properties"]) == {"a"}

    adapter, _ = _adapter()
    body = adapter._build_converse_body(
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        [LLMMessage(role="user", content="hi")],
        [
            ToolSchema(
                name="read",
                description="r",
                parameters={"anyOf": [{"properties": {"a": {"type": "string"}}}]},
            )
        ],
        ChatOptions(),
    )
    spec = body["toolConfig"]["tools"][0]["toolSpec"]
    assert spec["inputSchema"]["json"]["type"] == "object"
    assert set(spec["inputSchema"]["json"]["properties"]) == {"a"}


def test_tool_choice_mapping_variants() -> None:
    """auto/required/tool-name map; none omits toolConfig entirely."""
    adapter, _ = _adapter()
    user = [LLMMessage(role="user", content="hi")]
    tools = [ToolSchema(name="read", description="r", parameters={"type": "object"})]
    assert (
        adapter._build_converse_body("nova-micro", user, tools, ChatOptions())[
            "toolConfig"
        ]["toolChoice"]
        == {"auto": {}}
    )
    assert (
        adapter._build_converse_body(
            "nova-micro", user, tools, ChatOptions(tool_choice="required")
        )["toolConfig"]["toolChoice"]
        == {"any": {}}
    )
    assert (
        adapter._build_converse_body(
            "nova-micro", user, tools, ChatOptions(tool_choice="read")
        )["toolConfig"]["toolChoice"]
        == {"tool": {"name": "read"}}
    )
    assert (
        "toolConfig"
        not in adapter._build_converse_body(
            "nova-micro", user, tools, ChatOptions(tool_choice="none")
        )
    )


def test_tool_result_error_status_heuristic() -> None:
    """Runner failure text maps to status error; success stays success."""
    adapter, _ = _adapter()
    _, messages = adapter._convert_messages(
        [
            LLMMessage(
                role="tool",
                content="Tool 'read' failed: boom",
                tool_call_id="t1",
            ),
            LLMMessage(role="tool", content="file body", tool_call_id="t2"),
        ]
    )
    results = [
        block["toolResult"]
        for msg in messages
        for block in msg["content"]
        if "toolResult" in block
    ]
    assert results[0]["status"] == "error"
    assert results[1]["status"] == "success"


def test_user_image_part_becomes_bedrock_image_block() -> None:
    """image_url data-URL parts lower to typed Bedrock image blocks."""
    import base64

    raw = base64.b64encode(b"\x89PNGdata").decode()
    adapter, _ = _adapter()
    _, messages = adapter._convert_messages(
        [
            LLMMessage(
                role="user",
                content=[
                    {"type": "text", "text": "see"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{raw}"},
                    },
                    {"type": "text", "text": ""},
                ],
            )
        ]
    )
    blocks = messages[0]["content"]
    assert {"text": "see"} in blocks
    images = [b["image"] for b in blocks if "image" in b]
    assert images and images[0]["format"] == "png"
    assert images[0]["source"]["bytes"] == b"\x89PNGdata"
    assert not [b for b in blocks if b == {"text": ""}]


def test_user_unknown_image_mime_raises() -> None:
    """Unknown image MIMEs raise instead of being silently dropped."""
    import base64

    raw = base64.b64encode(b"svg").decode()
    adapter, _ = _adapter()
    with pytest.raises(ProviderResponseError, match="does not support image"):
        adapter._convert_messages(
            [
                LLMMessage(
                    role="user",
                    content=[
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/svg+xml;base64,{raw}"},
                        }
                    ],
                )
            ]
        )


def test_tool_image_part_becomes_bedrock_image_block() -> None:
    """Tool image_url parts lower to typed Bedrock image blocks."""
    import base64

    raw = base64.b64encode(b"\x89PNGdata").decode()
    adapter, _ = _adapter()
    _, messages = adapter._convert_messages(
        [
            LLMMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "t1",
                        "name": "read",
                        "arguments": '{"path":"cat.png"}',
                    }
                ],
            ),
            LLMMessage(
                role="tool",
                content=[
                    {"type": "text", "text": "Image read successfully"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{raw}"},
                    },
                ],
                tool_call_id="t1",
            ),
        ]
    )
    results = [
        block["toolResult"]
        for msg in messages
        for block in msg["content"]
        if "toolResult" in block
    ]
    assert results and results[0]["toolUseId"] == "t1"
    assert results[0]["status"] == "success"
    assert {"text": "Image read successfully"} in results[0]["content"]
    images = [b["image"] for b in results[0]["content"] if "image" in b]
    assert images and images[0]["format"] == "png"
    assert images[0]["source"]["bytes"] == b"\x89PNGdata"


def test_tool_pdf_file_part_is_skipped_without_crash() -> None:
    """PDF file parts in tool results are skipped (text survives).

    Converse ``toolResult`` blocks cannot carry ``document`` blocks, so
    PDFs are dropped there (documents still ride on user messages).
    """
    import base64

    raw = base64.b64encode(b"%PDF-data").decode()
    adapter, _ = _adapter()
    _, messages = adapter._convert_messages(
        [
            LLMMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "t1",
                        "name": "read",
                        "arguments": '{"path":"doc.pdf"}',
                    }
                ],
            ),
            LLMMessage(
                role="tool",
                content=[
                    {"type": "text", "text": "PDF read successfully"},
                    {
                        "type": "file",
                        "mime": "application/pdf",
                        "url": f"data:application/pdf;base64,{raw}",
                        "filename": "doc.pdf",
                    },
                ],
                tool_call_id="t1",
            ),
        ]
    )
    results = [
        block["toolResult"]
        for msg in messages
        for block in msg["content"]
        if "toolResult" in block
    ]
    assert results and results[0]["toolUseId"] == "t1"
    assert results[0]["status"] == "success"
    assert {"text": "PDF read successfully"} in results[0]["content"]
    assert not [b for b in results[0]["content"] if "document" in b]
    assert not [b for b in results[0]["content"] if "image" in b]


def test_user_pdf_file_part_becomes_document_block() -> None:
    """User PDF file parts still lower to document blocks (unchanged)."""
    import base64

    raw = base64.b64encode(b"%PDF-data").decode()
    adapter, _ = _adapter()
    _, messages = adapter._convert_messages(
        [
            LLMMessage(
                role="user",
                content=[
                    {"type": "text", "text": "see"},
                    {
                        "type": "file",
                        "mime": "application/pdf",
                        "url": f"data:application/pdf;base64,{raw}",
                        "filename": "doc.pdf",
                    },
                ],
            )
        ]
    )
    blocks = messages[0]["content"]
    assert {"text": "see"} in blocks
    docs = [b["document"] for b in blocks if "document" in b]
    assert docs and docs[0]["format"] == "pdf"
    assert docs[0]["name"] == "doc.pdf"
    assert docs[0]["source"]["bytes"] == b"%PDF-data"
