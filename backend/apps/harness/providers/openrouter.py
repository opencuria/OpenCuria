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

from ._lowering import (
    project_openai_tool_schema,
    wrap_system_update,
)
from ._transport import map_httpx_error
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

#: Cap for the enriched ``response_body`` stored on provider errors.
RESPONSE_BODY_MAX_CHARS = 2000


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
        except httpx.HTTPError as exc:
            mapped = map_httpx_error(
                exc, provider=self.name, label="OpenRouter"
            )
            if isinstance(mapped, ProviderTimeoutError):
                logger.warning("provider_timeout")
            else:
                logger.warning("provider_http_error")
            raise mapped from exc

    def _build_payload(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions,
    ) -> dict[str, Any]:
        """Build the OpenAI-compatible request payload.

        Mirrors OpenCode ``openai-chat.ts`` ``fromRequest``: tools go
        through the OpenAI schema projection, ``tool_choice`` maps
        ``auto``/``none``/``required`` verbatim and a specific tool name
        to ``{"type": "function", "function": {"name": ...}}``.

        Generation gap (documented, no fake fields): :class:`ChatOptions`
        only carries ``temperature``/``max_tokens``, so ``top_p``,
        ``frequency_penalty``, ``presence_penalty``, ``seed`` and
        ``stop`` (sent by OpenCode ``fromRequest``) cannot be set — they
        are intentionally omitted rather than invented.

        Usage gap: ``stream_options: {"include_usage": True}`` is always
        sent (OpenCode parity). The OpenRouter ``usage``/``prompt_cache_key``
        provider-options passthrough (``openrouter.ts:55-66``) has no
        :class:`ChatOptions` field to read from, so it is not sent.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._messages_to_dicts(messages),
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
                        "parameters": project_openai_tool_schema(t.parameters),
                    },
                }
                for t in tools
            ]
        if opts.tool_choice:
            if opts.tool_choice in ("auto", "none", "required"):
                payload["tool_choice"] = opts.tool_choice
            else:
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": opts.tool_choice},
                }
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
    def _messages_to_dicts(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """Convert a conversation, wrapping 2nd+ system messages.

        Mirrors OpenCode ``openai-chat.ts`` ``lowerMessages`` + shared
        ``wrappedSystemUpdate``: the first ``system`` message keeps the
        privileged role; later ones become in-order ``user`` text wrapped
        in ``<system-update>`` so temporal position is preserved.
        """
        converted: list[dict[str, Any]] = []
        seen_system = False
        for message in messages:
            if message.role == "system" and seen_system:
                text = OpenRouterAdapter._content_text(message.content)
                converted.append(
                    {"role": "user", "content": wrap_system_update(text)}
                )
                continue
            if message.role == "system":
                seen_system = True
            converted.append(OpenRouterAdapter._message_to_dict(message))
        return converted

    @staticmethod
    def _content_text(content: str | list[dict[str, Any]] | None) -> str:
        """Join text parts of harness content without crashing on dicts."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        texts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("text", "input_text", "output_text"):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "\n".join(texts)

    @staticmethod
    def _message_to_dict(message: LLMMessage) -> dict[str, Any]:
        """Convert an LLMMessage to the OpenAI wire format.

        Assistant messages replay ``tool_calls`` plus joined
        ``reasoning_content`` from ``reasoning`` content parts
        (openai-chat.ts ``lowerAssistantMessage`` parity). Tool results
        join text parts so image parts never crash the lowering.
        """
        if message.role == "assistant":
            return OpenRouterAdapter._assistant_to_dict(message)
        if message.role == "tool":
            data: dict[str, Any] = {
                "role": "tool",
                "content": OpenRouterAdapter._content_text(message.content)
                if not isinstance(message.content, str)
                else message.content,
            }
            if message.tool_call_id is not None:
                data["tool_call_id"] = message.tool_call_id
            return data
        data = {"role": message.role}
        if message.content is not None:
            data["content"] = message.content
        if message.tool_calls:
            converted: list[dict[str, Any]] = []
            for call in message.tool_calls:
                if "function" in call:
                    converted.append(call)
                else:
                    arguments = call.get("arguments", "")
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments)
                    converted.append(
                        {
                            "id": call.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": call.get("name", ""),
                                "arguments": arguments,
                            },
                        }
                    )
            data["tool_calls"] = converted
        if message.tool_call_id is not None:
            data["tool_call_id"] = message.tool_call_id
        return data

    @staticmethod
    def _assistant_to_dict(message: LLMMessage) -> dict[str, Any]:
        """Convert an assistant message with reasoning/tool-call replay."""
        data: dict[str, Any] = {"role": "assistant"}
        reasoning_parts: list[str] = []
        if isinstance(message.content, str):
            data["content"] = message.content
        elif isinstance(message.content, list):
            texts: list[str] = []
            for part in message.content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type in ("text", "input_text", "output_text"):
                    text = part.get("text")
                    if isinstance(text, str):
                        texts.append(text)
                elif part_type in ("reasoning", "reasoning_content", "reasoning_text"):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        reasoning_parts.append(text)
            data["content"] = "\n".join(texts) if texts else None
        else:
            data["content"] = None
        if message.tool_calls:
            converted = []
            for call in message.tool_calls:
                if "function" in call and isinstance(call.get("function"), dict):
                    converted.append(call)
                    continue
                arguments = call.get("arguments", "")
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments)
                converted.append(
                    {
                        "id": call.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": arguments,
                        },
                    }
                )
            data["tool_calls"] = converted
        if reasoning_parts:
            data["reasoning_content"] = "".join(reasoning_parts)
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
            headers = self._response_headers(response)
            full_body = await self._read_body(response, RESPONSE_BODY_MAX_CHARS)
            raise ProviderAuthError(
                f"OpenRouter auth failed ({response.status_code}): {body}",
                provider=self.name,
                status_code=response.status_code,
                response_headers=headers,
                response_body=full_body,
                is_retryable=self._should_retry_hint(headers),
            )
        if response.status_code == 429:
            body = await self._read_body_snippet(response)
            headers = self._response_headers(response)
            full_body = await self._read_body(response, RESPONSE_BODY_MAX_CHARS)
            raise ProviderRateLimitError(
                f"OpenRouter rate limit ({response.status_code}): {body}",
                provider=self.name,
                status_code=response.status_code,
                response_headers=headers,
                response_body=full_body,
                is_retryable=self._should_retry_hint(headers),
            )
        if response.status_code >= 400:
            body = await self._read_body_snippet(response)
            headers = self._response_headers(response)
            full_body = await self._read_body(response, RESPONSE_BODY_MAX_CHARS)
            raise ProviderResponseError(
                f"OpenRouter error ({response.status_code}): {body}",
                provider=self.name,
                status_code=response.status_code,
                response_headers=headers,
                response_body=full_body,
                is_retryable=self._should_retry_hint(headers),
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
        return await OpenRouterAdapter._read_body(response, 500)

    @staticmethod
    async def _read_body(response: httpx.Response, limit: int) -> str:
        """Read up to *limit* chars of the response body without raising."""
        try:
            body = await response.aread()
            return body.decode("utf-8", errors="replace")[:limit]
        except Exception:
            return "<unreadable body>"

    @staticmethod
    def _response_headers(response: httpx.Response) -> dict[str, str]:
        """Return response headers with lowercased names and str values."""
        headers: dict[str, str] = {}
        try:
            for name, value in response.headers.items():
                headers[name.lower()] = str(value)
        except Exception:
            return {}
        return headers

    @staticmethod
    def _should_retry_hint(headers: dict[str, str]) -> bool | None:
        """Parse the ``x-should-retry`` header (Pi provider-retry parity)."""
        hint = headers.get("x-should-retry", "").strip().lower()
        if hint == "true":
            return True
        if hint == "false":
            return False
        return None

    def _raise_stream_error(self, error: dict[str, Any]) -> None:
        """Raise a provider error for an SSE ``error`` chunk payload.

        Mirrors the ChatGPT ``"code: message"`` format: when both code and
        message are present the message is prefixed. ``context_length_exceeded``
        (or any overflow text per the shared compaction patterns) raises a
        non-retryable :class:`ProviderResponseError` whose message and
        ``response_body`` both carry the overflow text.
        """
        from ..compaction import is_context_overflow_error as _is_overflow

        code = str(error.get("code", "") or "")
        message = str(error.get("message", "") or "")
        if code and message:
            text = f"{code}: {message}"
        else:
            text = message or code or "OpenRouter stream failed"
        if code == "context_length_exceeded" or _is_overflow(RuntimeError(text)):
            raise ProviderResponseError(
                text,
                provider=self.name,
                response_body=text[:RESPONSE_BODY_MAX_CHARS],
                is_retryable=False,
            )
        raise ProviderResponseError(
            text,
            provider=self.name,
            response_body=text[:RESPONSE_BODY_MAX_CHARS],
        )

    @staticmethod
    def _map_finish_reason(finish_reason: Any) -> str | None:
        """Normalize an OpenAI-Chat finish reason (openai-chat.ts parity).

        Returns ``None`` only when no finish reason was provided (``None``
        stays ``None``); any other raw value maps to a known harness
        reason, falling back to ``"unknown"``.
        """
        if finish_reason is None:
            return None
        if finish_reason == "stop":
            return "stop"
        if finish_reason == "length":
            return "length"
        if finish_reason in ("content_filter", "content-filter"):
            return "content_filter"
        if finish_reason in ("function_call", "tool_calls"):
            return "tool_calls"
        return "unknown"

    @staticmethod
    def _map_usage(raw_usage: Any) -> Usage | None:
        """Map OpenAI-Chat usage (totalTokens policy: honor reported total)."""
        if not isinstance(raw_usage, dict):
            return None
        prompt_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(raw_usage.get("completion_tokens", 0) or 0)
        reported_total = int(raw_usage.get("total_tokens", 0) or 0)
        cost = float(raw_usage.get("cost", 0.0) or 0.0)
        if reported_total > 0:
            total_tokens = reported_total
        elif prompt_tokens > 0 or completion_tokens > 0:
            total_tokens = prompt_tokens + completion_tokens
        else:
            total_tokens = 0
        return Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=cost,
        )

    def _parse_chunk(self, data: str) -> Delta | None:
        """Parse one SSE data payload into a Delta.

        Raises:
            ProviderResponseError: If the payload is not valid JSON or
                carries an ``{"error": {...}}`` failure chunk (OpenRouter
                sends mid-stream failures as SSE-data with an error field).
                Error chunks use the ``"code: message"`` format and are
                classified as non-retryable context overflow when the code
                is ``context_length_exceeded`` or the text matches the
                shared overflow patterns.
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

        error_payload = chunk.get("error")
        if isinstance(error_payload, dict):
            self._raise_stream_error(error_payload)

        usage: Usage | None = None
        raw_usage = chunk.get("usage")
        usage = self._map_usage(raw_usage)

        choices = chunk.get("choices", [])
        if not choices:
            if usage is not None:
                return Delta(usage=usage)
            return None
        choice = choices[0] if isinstance(choices[0], dict) else {}
        raw_finish = choice.get("finish_reason")
        if raw_finish in ("network_error", "network-error"):
            raise ProviderResponseError(
                "Provider finish_reason: network_error",
                provider=self.name,
                is_retryable=True,
            )
        finish_reason = self._map_finish_reason(raw_finish)
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
