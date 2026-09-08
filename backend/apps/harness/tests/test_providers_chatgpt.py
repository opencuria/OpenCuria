"""Tests for the ChatGPT provider adapter (mocked SSE)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from apps.harness.providers.base import (
    ChatOptions,
    LLMMessage,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    ToolSchema,
)
from apps.harness.providers.chatgpt import (
    RECONNECT_MESSAGE,
    USAGE_NOT_INCLUDED_MESSAGE,
    ChatGPTAdapter,
)
from apps.harness.providers.chatgpt_oauth import CODEX_API_ENDPOINT


def _responses_sse(events: list[dict[str, Any]]) -> bytes:
    lines: list[str] = []
    for event in events:
        lines.append(f"data: {json.dumps(event)}")
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode()


def _credentials(
    *,
    access: str = "access-token",
    refresh: str = "refresh-token",
    expires: int | None = None,
    account_id: str = "acc-123",
) -> dict[str, Any]:
    return {
        "access": access,
        "refresh": refresh,
        "expires": expires if expires is not None else int(time.time() * 1000) + 3600_000,
        "account_id": account_id,
    }


def _mock_client(
    payload: bytes,
    status_code: int = 200,
    *,
    capture: dict[str, Any] | None = None,
) -> httpx.AsyncClient:
    """Build a client that serves one Codex SSE response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
            capture["headers"] = dict(request.headers)
            capture["body"] = json.loads(request.content.decode())
        assert str(request.url) == CODEX_API_ENDPOINT
        return httpx.Response(
            status_code,
            content=payload,
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_chat_stream_happy_path_text_toolcall_reasoning_usage() -> None:
    """Text, reasoning, tool call, and usage deltas stream correctly."""
    payload = _responses_sse(
        [
            {"type": "response.output_text.delta", "delta": "Hello"},
            {"type": "response.reasoning_summary_text.delta", "delta": "thinking"},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read",
                    "arguments": '{"path":"a"}',
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "input_tokens_details": {"cached_tokens": 2},
                    },
                },
            },
        ]
    )
    adapter = ChatGPTAdapter(_credentials(), client=_mock_client(payload))
    deltas = [
        d
        async for d in adapter.chat_stream(
            "gpt-5.4",
            [LLMMessage(role="user", content="hi")],
            [ToolSchema(name="read", description="r")],
        )
    ]

    assert "".join(d.text for d in deltas) == "Hello"
    assert "".join(d.reasoning for d in deltas) == "thinking"
    tool_calls = [c for d in deltas for c in d.tool_calls]
    assert tool_calls[0]["id"] == "call_1"
    assert tool_calls[0]["name"] == "read"
    usages = [d.usage for d in deltas if d.usage is not None]
    assert usages[-1].prompt_tokens == 10
    assert usages[-1].completion_tokens == 5
    assert usages[-1].total_tokens == 17
    assert deltas[-1].finish_reason == "completed"


async def test_build_payload_shaping() -> None:
    """Instructions, store:false, tools strict:false, no max_output_tokens."""
    capture: dict[str, Any] = {}
    payload = _responses_sse(
        [{"type": "response.completed", "response": {"usage": {"input_tokens": 1}}}]
    )
    adapter = ChatGPTAdapter(
        _credentials(),
        client=_mock_client(payload, capture=capture),
    )
    async for _ in adapter.chat_stream(
        "gpt-5.4",
        [
            LLMMessage(role="system", content="You are helpful."),
            LLMMessage(role="user", content="hi"),
            LLMMessage(
                role="assistant",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            ),
            LLMMessage(role="tool", content="ok", tool_call_id="call_1"),
        ],
        [ToolSchema(name="read", description="read files", parameters={"type": "object"})],
        ChatOptions(max_tokens=999, reasoning_effort="high"),
    ):
        pass

    body = capture["body"]
    assert body["instructions"] == "You are helpful."
    assert body["store"] is False
    assert body["stream"] is True
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "max_output_tokens" not in body
    assert body["tools"][0]["strict"] is False
    roles = [item.get("role") for item in body["input"] if item.get("type") == "message"]
    assert "system" not in roles
    assert body["input"][-1]["type"] == "function_call_output"


async def test_request_headers_include_account_and_residency() -> None:
    """Codex headers include bearer token, account id, and residency."""
    capture: dict[str, Any] = {}
    payload = _responses_sse(
        [{"type": "response.completed", "response": {"usage": {"input_tokens": 1}}}]
    )
    creds = _credentials()
    creds["residency"] = "eu"
    adapter = ChatGPTAdapter(creds, client=_mock_client(payload, capture=capture))
    async for _ in adapter.chat_stream("gpt-5.4", [], []):
        pass

    headers = capture["headers"]
    assert headers["authorization"] == "Bearer access-token"
    assert headers["chatgpt-account-id"] == "acc-123"
    assert headers["x-openai-internal-codex-residency"] == "eu"
    assert headers["originator"] == "opencuria"
    assert headers["user-agent"] == "opencuria"


async def test_expiry_refresh_dedup_and_callback() -> None:
    """Expired tokens refresh once and invoke on_tokens_refreshed."""
    refresh_calls = 0
    refreshed: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        if request.url.path == "/oauth/token":
            refresh_calls += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "access-new",
                    "refresh_token": "refresh-new",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(
            200,
            content=_responses_sse(
                [{"type": "response.completed", "response": {"usage": {"input_tokens": 1}}}]
            ),
            headers={"Content-Type": "text/event-stream"},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    creds = _credentials(expires=0, access="")

    async def _record_refresh(data: dict[str, Any]) -> None:
        refreshed.append(dict(data))

    adapter = ChatGPTAdapter(
        creds,
        on_tokens_refreshed=_record_refresh,
        client=client,
    )

    first, second = await asyncio.gather(
        _collect(adapter),
        _collect(adapter),
    )
    assert refresh_calls == 1
    assert refreshed and refreshed[0]["access"] == "access-new"
    assert refreshed[0]["refresh"] == "refresh-new"
    assert first == second == 1


async def _collect(adapter: ChatGPTAdapter) -> int:
    count = 0
    async for _ in adapter.chat_stream("gpt-5.4", [], []):
        count += 1
    return count


async def test_401_maps_to_provider_auth_error_without_refresh() -> None:
    """HTTP 401 tells the user to reconnect; no refresh attempt."""
    refresh_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        if request.url.path == "/oauth/token":
            refresh_calls += 1
        return httpx.Response(401, content=b"unauthorized", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ChatGPTAdapter(_credentials(), client=client)
    with pytest.raises(ProviderAuthError, match=RECONNECT_MESSAGE):
        async for _ in adapter.chat_stream("gpt-5.4", [], []):
            pass
    assert refresh_calls == 0


async def test_usage_not_included_maps_to_provider_auth_error() -> None:
    """usage_not_included stream errors surface upgrade guidance."""
    payload = _responses_sse(
        [
            {
                "type": "error",
                "error": {
                    "code": "usage_not_included",
                    "message": "plan does not include usage",
                },
            }
        ]
    )
    adapter = ChatGPTAdapter(_credentials(), client=_mock_client(payload))
    with pytest.raises(ProviderAuthError, match=USAGE_NOT_INCLUDED_MESSAGE):
        async for _ in adapter.chat_stream("gpt-5.4", [], []):
            pass


async def test_429_raises_rate_limit_error() -> None:
    """HTTP 429 raises ProviderRateLimitError with retry hint."""
    adapter = ChatGPTAdapter(
        _credentials(),
        client=_mock_client(b"slow down", status_code=429),
    )
    with pytest.raises(ProviderRateLimitError) as exc_info:
        async for _ in adapter.chat_stream("gpt-5.4", [], []):
            pass
    assert exc_info.value.is_retryable is True


async def test_response_failed_raises_provider_response_error() -> None:
    """response.failed events become ProviderResponseError."""
    payload = _responses_sse(
        [
            {
                "type": "response.failed",
                "error": {"code": "server_error", "message": "upstream failed"},
            }
        ]
    )
    adapter = ChatGPTAdapter(_credentials(), client=_mock_client(payload))
    with pytest.raises(ProviderResponseError, match="upstream failed"):
        async for _ in adapter.chat_stream("gpt-5.4", [], []):
            pass
