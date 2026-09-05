"""
OpenRouter provider adapter (OpenAI-compatible, SSE streaming).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from .base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ToolSchema,
    Usage,
)

log = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
CHAT_COMPLETIONS_PATH = "/chat/completions"


class OpenRouterAdapter(ProviderAdapter):
    """LLM provider adapter for OpenRouter's OpenAI-compatible API."""

    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create an adapter.

        Args:
            api_key: OpenRouter API key (never logged).
            base_url: API base URL (defaults to OpenRouter).
            client: Optional shared httpx client (mainly for tests).
        """
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def chat_stream(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Stream a chat completion via server-sent events.

        Yields:
            Incremental :class:`Delta` objects (text, reasoning,
            tool-call fragments, usage, finish reason).

        Raises:
            ProviderAuthError: On HTTP 401/403.
            ProviderRateLimitError: On HTTP 429.
            ProviderTimeoutError: On network timeouts.
            ProviderResponseError: On other HTTP errors or
                malformed SSE payloads.
        """
        options = opts or ChatOptions()
        payload = self._build_payload(model, messages, tools, options)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}{CHAT_COMPLETIONS_PATH}"
        timeout = httpx.Timeout(options.timeout_seconds)
        logger = log.bind(provider=self.name, model=model)

        try:
            if self._client is not None:
                async with self._client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                ) as response:
                    async for delta in self._parse_stream(response):
                        yield delta
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        "POST",
                        url,
                        json=payload,
                        headers=headers,
                        timeout=timeout,
                    ) as response:
                        async for delta in self._parse_stream(response):
                            yield delta
        except ProviderAuthError:
            logger.warning("provider_auth_error")
            raise
        except ProviderRateLimitError:
            logger.warning("provider_rate_limit_error")
            raise
        except ProviderResponseError:
            logger.warning("provider_response_error")
            raise
        except httpx.TimeoutException as exc:
            logger.warning("provider_timeout")
            raise ProviderTimeoutError(
                f"OpenRouter request timed out: {exc}",
                provider=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("provider_http_error")
            raise ProviderResponseError(
                f"OpenRouter request failed: {exc}",
                provider=self.name,
            ) from exc

    def _build_payload(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions,
    ) -> dict[str, Any]:
        """Build the OpenAI-compatible request payload."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [self._message_to_dict(m) for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if opts.temperature is not None:
            payload["temperature"] = opts.temperature
        if opts.max_tokens is not None:
            payload["max_tokens"] = opts.max_tokens
        effort = (opts.reasoning_effort or "").strip()
        if effort:
            payload["reasoning"] = {"effort": effort}
        return payload

    @staticmethod
    def _message_to_dict(message: LLMMessage) -> dict[str, Any]:
        """Convert an LLMMessage to the OpenAI wire format."""
        data: dict[str, Any] = {"role": message.role}
        if message.content is not None:
            data["content"] = message.content
        if message.tool_calls:
            converted: list[dict[str, Any]] = []
            for call in message.tool_calls:
                if "function" in call:
                    converted.append(call)
                else:
                    converted.append(
                        {
                            "id": call.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": call.get("name", ""),
                                "arguments": call.get("arguments", ""),
                            },
                        }
                    )
            data["tool_calls"] = converted
        if message.tool_call_id is not None:
            data["tool_call_id"] = message.tool_call_id
        return data

    async def _parse_stream(self, response: httpx.Response) -> AsyncIterator[Delta]:
        """Validate the HTTP status and parse SSE lines into deltas."""
        if response.status_code in (401, 403):
            body = await self._read_body_snippet(response)
            raise ProviderAuthError(
                f"OpenRouter auth failed ({response.status_code}): {body}",
                provider=self.name,
            )
        if response.status_code == 429:
            body = await self._read_body_snippet(response)
            raise ProviderRateLimitError(
                f"OpenRouter rate limit ({response.status_code}): {body}",
                provider=self.name,
            )
        if response.status_code >= 400:
            body = await self._read_body_snippet(response)
            raise ProviderResponseError(
                f"OpenRouter error ({response.status_code}): {body}",
                provider=self.name,
                status_code=response.status_code,
            )

        async for line in response.aiter_lines():
            stripped = line.strip()
            if not stripped or stripped.startswith(":"):
                continue
            if not stripped.startswith("data:"):
                continue
            data = stripped[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            delta = self._parse_chunk(data)
            if delta is not None:
                yield delta

    @staticmethod
    async def _read_body_snippet(response: httpx.Response) -> str:
        """Read a short error-body snippet without raising."""
        try:
            body = await response.aread()
            return body.decode("utf-8", errors="replace")[:500]
        except Exception:
            return "<unreadable body>"

    def _parse_chunk(self, data: str) -> Delta | None:
        """Parse one SSE data payload into a Delta.

        Raises:
            ProviderResponseError: If the payload is not valid JSON.
        """
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                f"Malformed SSE payload: {data[:200]}",
                provider=self.name,
            ) from exc
        if not isinstance(chunk, dict):
            raise ProviderResponseError(
                f"Unexpected SSE payload type: {type(chunk).__name__}",
                provider=self.name,
            )

        usage: Usage | None = None
        raw_usage = chunk.get("usage")
        if isinstance(raw_usage, dict):
            usage = Usage(
                prompt_tokens=int(raw_usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(raw_usage.get("completion_tokens", 0) or 0),
                total_tokens=int(raw_usage.get("total_tokens", 0) or 0),
                cost=float(raw_usage.get("cost", 0.0) or 0.0),
            )

        choices = chunk.get("choices", [])
        if not choices:
            if usage is not None:
                return Delta(usage=usage)
            return None
        choice = choices[0] if isinstance(choices[0], dict) else {}
        finish_reason = choice.get("finish_reason")
        raw_delta = choice.get("delta", {})
        if not isinstance(raw_delta, dict):
            raw_delta = {}

        text = raw_delta.get("content") or ""
        reasoning = (
            raw_delta.get("reasoning_content") or raw_delta.get("reasoning") or ""
        )
        tool_calls = self._normalize_tool_calls(raw_delta.get("tool_calls") or [])

        if not text and not reasoning and not tool_calls:
            if usage is not None or finish_reason is not None:
                return Delta(usage=usage, finish_reason=finish_reason)
            return None
        return Delta(
            text=text if isinstance(text, str) else "",
            reasoning=reasoning if isinstance(reasoning, str) else "",
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _normalize_tool_calls(
        raw_calls: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Flatten OpenAI tool-call fragments into plain dicts."""
        normalized: list[dict[str, Any]] = []
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function", {})
            if not isinstance(function, dict):
                function = {}
            normalized.append(
                {
                    "index": call.get("index", 0),
                    "id": call.get("id", ""),
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", ""),
                }
            )
        return tuple(normalized)
