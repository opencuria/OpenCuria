"""Tests for the OpenRouter provider adapter (mocked SSE)."""

from __future__ import annotations

import json

import httpx
import pytest

from apps.harness.providers.base import (
    ChatOptions,
    LLMMessage,
    ProviderAuthError,
    ProviderResponseError,
    ProviderTimeoutError,
    ToolSchema,
)
from apps.harness.providers.openrouter import OpenRouterAdapter


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
            ChatOptions(timeout_seconds=5.0),
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


async def test_chat_stream_rate_limit_error() -> None:
    """HTTP 429 raises ProviderRateLimitError."""
    from apps.harness.providers.base import ProviderRateLimitError

    adapter = OpenRouterAdapter(api_key="k", client=_mock_client(b"slow down", 429))
    with pytest.raises(ProviderRateLimitError):
        async for _ in adapter.chat_stream("m", [], []):
            pass


async def test_chat_stream_timeout() -> None:
    """Transport timeouts surface as ProviderTimeoutError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenRouterAdapter(api_key="k", client=client)
    with pytest.raises(ProviderTimeoutError):
        async for _ in adapter.chat_stream("m", [], []):
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
