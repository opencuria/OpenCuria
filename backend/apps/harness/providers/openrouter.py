"""
OpenRouter provider adapter (OpenAI-compatible, SSE streaming).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx
import structlog

from .base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ProviderAuthError,
    ProviderHeaderTimeoutError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderStreamTimeoutError,
    ProviderTimeoutError,
    ToolSchema,
    Usage,
)

log = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
CHAT_COMPLETIONS_PATH = "/chat/completions"
HTTPX_POOL_TIMEOUT_SECONDS = 5.0


def positive_timeout(value: float | None) -> float | None:
    """Return *value* when it is a positive timeout, otherwise ``None``."""
    if value is None or value <= 0:
        return None
    return value


def httpx_stream_timeout(opts: ChatOptions) -> httpx.Timeout:
    """Build httpx timeouts: connect/write follow header timeout, read is off.

    SSE idle and header waits are enforced with ``asyncio.wait_for`` so
    httpx does not treat a long reasoning pause as a read timeout.
    """
    header = positive_timeout(opts.header_timeout_seconds)
    return httpx.Timeout(
        None,
        connect=header,
        read=None,
        write=header,
        pool=HTTPX_POOL_TIMEOUT_SECONDS,
    )


def effective_timeout(
    phase_seconds: float | None,
    deadline: float | None,
) -> float | None:
    """Combine a phase timeout with an optional wall-clock deadline."""
    phase = positive_timeout(phase_seconds)
    if deadline is None:
        return phase
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0.0
    if phase is None:
        return remaining
    return min(phase, remaining)


def stream_deadline(opts: ChatOptions) -> float | None:
    """Return a monotonic deadline when a total timeout is configured."""
    total = positive_timeout(opts.timeout_seconds)
    if total is None:
        return None
    return time.monotonic() + total


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
            ProviderHeaderTimeoutError: When headers do not arrive in time.
            ProviderStreamTimeoutError: When the SSE stream goes idle.
            ProviderTimeoutError: On network or total-request timeouts.
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
        timeout = httpx_stream_timeout(options)
        logger = log.bind(provider=self.name, model=model)
        deadline = stream_deadline(options)

        try:
            if self._client is not None:
                async for delta in self._stream_request(
                    self._client,
                    url,
                    payload,
                    headers,
                    timeout,
                    options,
                    deadline,
                ):
                    yield delta
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async for delta in self._stream_request(
                        client,
                        url,
                        payload,
                        headers,
                        timeout,
                        options,
                        deadline,
                    ):
                        yield delta
        except ProviderAuthError:
            logger.warning("provider_auth_error")
            raise
        except ProviderRateLimitError:
            logger.warning("provider_rate_limit_error")
            raise
        except ProviderTimeoutError:
            logger.warning("provider_timeout")
            raise
        except ProviderResponseError:
            logger.warning("provider_response_error")
            raise
        except httpx.TimeoutException as exc:
            logger.warning("provider_timeout")
            raise ProviderTimeoutError(
                "OpenRouter request timed out",
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
        if opts.tool_choice:
            payload["tool_choice"] = opts.tool_choice
        if opts.temperature is not None:
            payload["temperature"] = opts.temperature
        if opts.max_tokens is not None:
            payload["max_tokens"] = opts.max_tokens
        effort = (opts.reasoning_effort or "").strip()
        if effort:
            payload["reasoning"] = {"effort": effort}
        return payload

    async def _stream_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
        options: ChatOptions,
        deadline: float | None,
    ) -> AsyncIterator[Delta]:
        """Open one SSE stream with header and chunk idle timeouts."""
        stream_cm = client.stream(
            "POST",
            url,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        try:
            header_timeout = effective_timeout(
                options.header_timeout_seconds, deadline
            )
            try:
                if header_timeout is None:
                    response = await stream_cm.__aenter__()
                else:
                    response = await asyncio.wait_for(
                        stream_cm.__aenter__(), timeout=header_timeout
                    )
            except TimeoutError as exc:
                raise self._header_or_total_timeout(
                    options, deadline, header_timeout
                ) from exc
            async for delta in self._parse_stream(
                response,
                chunk_timeout=options.chunk_timeout_seconds,
                deadline=deadline,
                total_timeout=options.timeout_seconds,
            ):
                yield delta
        finally:
            with suppress(Exception):
                await stream_cm.__aexit__(None, None, None)

    def _header_or_total_timeout(
        self,
        options: ChatOptions,
        deadline: float | None,
        waited: float | None,
    ) -> ProviderTimeoutError:
        """Choose header vs total timeout after a wait_for expiry."""
        if deadline is not None and (deadline - time.monotonic()) <= 0:
            total = positive_timeout(options.timeout_seconds) or 0.0
            return ProviderTimeoutError(
                f"OpenRouter request timed out after {int(total * 1000)}ms",
                provider=self.name,
            )
        seconds = waited if waited is not None else 0.0
        return ProviderHeaderTimeoutError(
            provider=self.name,
            timeout_seconds=seconds,
        )

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

    async def _parse_stream(
        self,
        response: httpx.Response,
        *,
        chunk_timeout: float | None = None,
        deadline: float | None = None,
        total_timeout: float | None = None,
    ) -> AsyncIterator[Delta]:
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

        async for line in self._aiter_lines(
            response,
            chunk_timeout=chunk_timeout,
            deadline=deadline,
            total_timeout=total_timeout,
        ):
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

    async def _aiter_lines(
        self,
        response: httpx.Response,
        *,
        chunk_timeout: float | None,
        deadline: float | None,
        total_timeout: float | None,
    ) -> AsyncIterator[str]:
        """Yield SSE lines, aborting after chunk idle or total deadline."""
        iterator = response.aiter_lines()
        while True:
            timeout = effective_timeout(chunk_timeout, deadline)
            try:
                if timeout is None:
                    line = await anext(iterator)
                else:
                    line = await asyncio.wait_for(anext(iterator), timeout=timeout)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise self._chunk_or_total_timeout(
                    deadline, total_timeout
                ) from exc
            yield line

    def _chunk_or_total_timeout(
        self,
        deadline: float | None,
        total_timeout: float | None,
    ) -> ProviderTimeoutError:
        """Choose stream vs total timeout after a wait_for expiry."""
        if deadline is not None and (deadline - time.monotonic()) <= 0:
            total = positive_timeout(total_timeout) or 0.0
            return ProviderTimeoutError(
                f"OpenRouter request timed out after {int(total * 1000)}ms",
                provider=self.name,
            )
        return ProviderStreamTimeoutError(provider=self.name)

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
