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


class _MockBedrockClient:
    """Minimal bedrock-runtime client stub."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        error: Exception | None = None,
        capture: dict[str, Any] | None = None,
    ) -> None:
        self._events = events
        self._error = error
        self._capture = capture
        self.meta = MagicMock()
        self.meta.events = MagicMock()

    async def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        if self._capture is not None:
            self._capture["body"] = kwargs
        if self._error is not None:
            raise self._error
        return {"stream": _MockStream(self._events)}


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
    ) -> None:
        self._events = events or []
        self._error = error
        self._capture = capture or {}
        self.client_kwargs: list[dict[str, Any]] = []
        self.last_client: _MockBedrockClient | None = None

    def client(self, **kwargs: Any) -> _MockClientContext:
        self.client_kwargs.append(kwargs)
        client = _MockBedrockClient(
            self._events,
            error=self._error,
            capture=self._capture,
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
    assert finish.usage.total_tokens == 18


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
    assert BedrockAdapter._map_finish_reason("max_tokens") == "length"
    assert BedrockAdapter._map_finish_reason("tool_use") == "tool_calls"
