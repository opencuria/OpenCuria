"""
OpenAI-compatible provider adapter (non-streaming chat completions).

Small generic adapter for self-hosted / HuggingFace UI-TARS style
endpoints: ``POST {base_url}/chat/completions`` with ``stream: False``,
like the Agent-S SDK. Primary use: Agent-S no-tools calls (worker,
reflection, grounding, text spans). Multimodal ``LLMMessage`` payloads
(incl. PNG data URLs) and optional ``temperature`` are forwarded;
standard OpenAI function schemas are sent when *tools* are given but
manually configured models are catalogued with ``supports_tools=False``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from ._lowering import project_openai_tool_schema
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
    ProviderTimeoutError,
    ToolSchema,
    Usage,
)
from .openrouter import (
    RESPONSE_BODY_MAX_CHARS,
    effective_timeout,
    httpx_stream_timeout,
    positive_timeout,
    stream_deadline,
)

log = structlog.get_logger(__name__)

CHAT_COMPLETIONS_PATH = "/chat/completions"


def _parse_base_url(base_url: str):
    """Parse *base_url*; require http/https + host (raises ValueError)."""
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("base_url must be an http(s) URL with a host")
    return parsed


def validate_base_url(base_url: str) -> str:
    """Validate an http/https base URL; return it without trailing slash.

    Pure public helper shared by the adapter constructor and the service
    layer (single source of truth: ``urlparse`` scheme + ``netloc`` check,
    so ``https://`` or ``http:///x`` are rejected while ``localhost``
    stays allowed). Raises ``ValueError`` on misuse.
    """
    cleaned = (base_url or "").strip()
    if not cleaned:
        raise ValueError("base_url is required")
    _parse_base_url(cleaned)
    return cleaned.rstrip("/")


def _message_to_dict(message: LLMMessage) -> dict[str, Any]:
    """Lower an LLMMessage to the OpenAI chat wire format.

    String content passes through; list content keeps text and
    ``image_url`` parts verbatim (Agent-S PNG data URLs included).
    Tool-role messages keep ``tool_call_id``; assistant tool calls
    replay in OpenAI shape.
    """
    if message.role == "assistant" and message.tool_calls:
        converted: list[dict[str, Any]] = []
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
        data: dict[str, Any] = {"role": "assistant", "content": message.content}
        data["tool_calls"] = converted
        if message.tool_call_id is not None:
            data["tool_call_id"] = message.tool_call_id
        return data
    data = {"role": message.role}
    if message.content is not None:
        data["content"] = message.content
    if message.tool_calls:
        data["tool_calls"] = list(message.tool_calls)
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    return data


class OpenAICompatibleAdapter(ProviderAdapter):
    """Generic OpenAI-compatible chat-completions adapter (no streaming)."""

    name = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create an adapter.

        Args:
            base_url: Endpoint base URL (required, http/https with host).
            api_key: Optional bearer token (empty = no auth header).
            client: Optional shared httpx client (mainly for tests).

        Note: any admin/org-configured external provider endpoint (e.g.
        OpenRouter-style) may be used here; ``localhost``/self-hosted
        bases stay allowed. Only the scheme + host presence is enforced.
        """
        cleaned = (base_url or "").strip()
        if not cleaned:
            raise ValueError("base_url must not be empty")
        self._base_url = validate_base_url(cleaned)
        self._api_key = api_key or ""
        self._client = client

    def _build_payload(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions,
    ) -> dict[str, Any]:
        """Build the non-streaming chat-completions payload."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_message_to_dict(message) for message in messages],
            "stream": False,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": project_openai_tool_schema(tool.parameters),
                    },
                }
                for tool in tools
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
        return payload

    async def chat_stream(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """POST one non-streaming completion; yield it as a single delta.

        Raises:
            ProviderAuthError: On HTTP 401/403.
            ProviderRateLimitError: On HTTP 429.
            ProviderTimeoutError: On network/total-request timeouts.
            ProviderResponseError: On other HTTP errors or malformed bodies.
        """
        options = opts or ChatOptions()
        payload = self._build_payload(model, messages, tools, options)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}{CHAT_COMPLETIONS_PATH}"
        timeout = httpx_stream_timeout(options)
        logger = log.bind(provider=self.name, model=model)
        deadline = stream_deadline(options)

        async def _run(client: httpx.AsyncClient) -> AsyncIterator[Delta]:
            import asyncio
            import time

            # Non-streaming total deadline: wait at most
            # min(header, remaining total) for headers, like the streaming
            # adapters combine phase + deadline (see effective_timeout).
            effective = effective_timeout(
                options.header_timeout_seconds, deadline
            )
            try:
                if effective is None:
                    response = await client.post(
                        url, json=payload, headers=headers,
                        timeout=timeout,
                    )
                else:
                    response = await asyncio.wait_for(
                        client.post(
                            url, json=payload, headers=headers,
                            timeout=timeout,
                        ),
                        timeout=effective,
                    )
            except TimeoutError as exc:
                raise self._header_or_total_timeout(options, deadline) from exc
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(
                    "OpenAI-compatible request timed out",
                    provider=self.name,
                ) from exc
            except httpx.HTTPError as exc:
                raise map_httpx_error(
                    exc, provider=self.name, label="OpenAI-compatible"
                ) from exc
            if response.status_code in (401, 403):
                raise ProviderAuthError(
                    f"OpenAI-compatible auth failed ({response.status_code}): "
                    f"{response.text[:500]}",
                    provider=self.name,
                    status_code=response.status_code,
                    response_headers=self._headers(response),
                    response_body=response.text[:RESPONSE_BODY_MAX_CHARS],
                )
            if response.status_code == 429:
                raise ProviderRateLimitError(
                    f"OpenAI-compatible rate limit ({response.status_code}): "
                    f"{response.text[:500]}",
                    provider=self.name,
                    status_code=response.status_code,
                    response_headers=self._headers(response),
                    response_body=response.text[:RESPONSE_BODY_MAX_CHARS],
                )
            if response.status_code >= 400:
                raise ProviderResponseError(
                    f"OpenAI-compatible error ({response.status_code}): "
                    f"{response.text[:500]}",
                    provider=self.name,
                    status_code=response.status_code,
                    response_headers=self._headers(response),
                    response_body=response.text[:RESPONSE_BODY_MAX_CHARS],
                )
            if deadline is not None and (deadline - time.monotonic()) <= 0:
                total = positive_timeout(options.timeout_seconds) or 0.0
                raise ProviderTimeoutError(
                    "OpenAI-compatible request timed out after "
                    f"{int(total * 1000)}ms",
                    provider=self.name,
                )
            yield self._parse_response(response)

        try:
            if self._client is not None:
                async for delta in _run(self._client):
                    yield delta
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async for delta in _run(client):
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
                exc, provider=self.name, label="OpenAI-compatible"
            )
            if isinstance(mapped, ProviderTimeoutError):
                logger.warning("provider_timeout")
            else:
                logger.warning("provider_http_error")
            raise mapped from exc

    def _header_or_total_timeout(
        self, options: ChatOptions, deadline: float | None
    ) -> ProviderTimeoutError:
        """Choose header vs total timeout after a wait_for expiry."""
        import time

        if deadline is not None and (deadline - time.monotonic()) <= 0:
            total = positive_timeout(options.timeout_seconds) or 0.0
            return ProviderTimeoutError(
                "OpenAI-compatible request timed out after "
                f"{int(total * 1000)}ms",
                provider=self.name,
            )
        seconds = positive_timeout(options.header_timeout_seconds) or 0.0
        return ProviderHeaderTimeoutError(
            provider=self.name,
            timeout_seconds=seconds,
        )

    @staticmethod
    def _headers(response: httpx.Response) -> dict[str, str]:
        """Return response headers with lowercased names and str values."""
        headers: dict[str, str] = {}
        try:
            for name, value in response.headers.items():
                headers[name.lower()] = str(value)
        except Exception:
            return {}
        return headers

    def _parse_response(self, response: httpx.Response) -> Delta:
        """Parse a non-streaming chat-completions body into one delta."""
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseError(
                "OpenAI-compatible response was not JSON",
                provider=self.name,
                status_code=response.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise ProviderResponseError(
                "OpenAI-compatible response was not an object",
                provider=self.name,
                status_code=response.status_code,
            )
        error_payload = body.get("error")
        if isinstance(error_payload, dict):
            message = str(error_payload.get("message", "") or "request failed")
            raise ProviderResponseError(
                f"OpenAI-compatible error: {message}",
                provider=self.name,
                status_code=response.status_code,
                response_body=message[:RESPONSE_BODY_MAX_CHARS],
            )
        choices = body.get("choices", [])
        text = ""
        finish_reason: str | None = None
        tool_calls: list[dict[str, Any]] = []
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            raw_finish = choice.get("finish_reason")
            finish_reason = self._map_finish_reason(raw_finish)
            message = choice.get("message", {})
            if isinstance(message, dict):
                content = message.get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts: list[str] = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") in ("text", "output_text"):
                            item = part.get("text")
                            if isinstance(item, str):
                                parts.append(item)
                            elif isinstance(item, dict) and isinstance(
                                item.get("value"), str
                            ):
                                parts.append(item["value"])
                    text = "".join(parts)
                raw_tool_calls = message.get("tool_calls")
                if isinstance(raw_tool_calls, list):
                    for raw_call in raw_tool_calls:
                        if not isinstance(raw_call, dict):
                            continue
                        function = raw_call.get("function")
                        if not isinstance(function, dict):
                            continue
                        name = str(function.get("name", "") or "")
                        arguments = function.get("arguments", "")
                        if isinstance(arguments, dict):
                            arguments = json.dumps(arguments)
                        if not isinstance(arguments, str):
                            continue
                        tool_calls.append(
                            {
                                "id": str(raw_call.get("id", "") or ""),
                                "name": name,
                                "arguments": arguments,
                            }
                        )
        usage = self._map_usage(body.get("usage"))
        if not text and not tool_calls and usage is None and finish_reason is None:
            raise ProviderResponseError(
                "OpenAI-compatible response had no choices or usage",
                provider=self.name,
                status_code=response.status_code,
            )
        return Delta(
            text=text,
            tool_calls=tuple(tool_calls),
            usage=usage,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _map_finish_reason(finish_reason: Any) -> str | None:
        """Normalize an OpenAI-Chat finish reason."""
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
        """Map an OpenAI-Chat usage block (honor reported total)."""
        if not isinstance(raw_usage, dict):
            return None
        prompt_tokens = int(raw_usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(raw_usage.get("completion_tokens", 0) or 0)
        reported_total = int(raw_usage.get("total_tokens", 0) or 0)
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
            cost=0.0,
        )
