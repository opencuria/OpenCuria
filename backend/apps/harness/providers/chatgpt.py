"""
ChatGPT Codex provider adapter (OAuth, Responses API SSE streaming).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any

import httpx
import structlog

from ._lowering import project_openai_tool_schema
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
from .chatgpt_oauth import (
    CODEX_API_ENDPOINT,
    OAuthTokens,
    refresh_tokens,
)
from .openrouter import (
    RESPONSE_BODY_MAX_CHARS,
    effective_timeout,
    httpx_stream_timeout,
    positive_timeout,
    stream_deadline,
)

log = structlog.get_logger(__name__)

USER_AGENT = "opencuria"
USAGE_NOT_INCLUDED_MESSAGE = (
    "To use Codex with your ChatGPT plan, upgrade to Plus: "
    "https://chatgpt.com/explore/plus."
)
RECONNECT_MESSAGE = (
    "ChatGPT authorization expired. Reconnect ChatGPT in organization settings."
)


class ChatGPTAdapter(ProviderAdapter):
    """LLM provider adapter for ChatGPT Plus/Pro via Codex OAuth."""

    name = "chatgpt"

    def __init__(
        self,
        credentials: dict[str, Any],
        on_tokens_refreshed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create an adapter backed by OAuth credentials.

        Args:
            credentials: Dict with ``access``, ``refresh``, ``expires``,
                ``account_id``, and optional ``residency``.
            on_tokens_refreshed: Async callback invoked after token refresh.
            client: Optional shared httpx client (mainly for tests).
        """
        self._credentials = dict(credentials)
        self._on_tokens_refreshed = on_tokens_refreshed
        self._client = client
        self._refresh_lock = asyncio.Lock()

    async def chat_stream(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Stream a chat completion via Codex Responses SSE.

        Yields:
            Incremental :class:`Delta` objects (text, reasoning, tool calls,
            usage, finish reason).

        Raises:
            ProviderAuthError: On HTTP 401 or plan usage errors.
            ProviderRateLimitError: On HTTP 429.
            ProviderHeaderTimeoutError: When headers do not arrive in time.
            ProviderStreamTimeoutError: When the SSE stream goes idle.
            ProviderTimeoutError: On network or total-request timeouts.
            ProviderResponseError: On other HTTP errors or malformed SSE.
        """
        options = opts or ChatOptions()
        await self._ensure_fresh_tokens()
        payload = self._build_payload(model, messages, tools, options)
        headers = self._build_headers()
        timeout = httpx_stream_timeout(options)
        logger = log.bind(provider=self.name, model=model)
        deadline = stream_deadline(options)

        try:
            if self._client is not None:
                async for delta in self._stream_request(
                    self._client,
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
                "ChatGPT request timed out",
                provider=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("provider_http_error")
            raise ProviderResponseError(
                f"ChatGPT request failed: {exc}",
                provider=self.name,
            ) from exc

    def _build_headers(self) -> dict[str, str]:
        """Build Codex request headers from stored credentials."""
        headers = {
            "Authorization": f"Bearer {self._credentials.get('access', '')}",
            "Content-Type": "application/json",
            "originator": "opencuria",
            "User-Agent": USER_AGENT,
        }
        account_id = self._credentials.get("account_id")
        if account_id:
            headers["ChatGPT-Account-Id"] = str(account_id)
        residency = self._credentials.get("residency")
        if residency:
            headers["x-openai-internal-codex-residency"] = str(residency)
        return headers

    def _build_payload(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions,
    ) -> dict[str, Any]:
        """Build the Responses API request payload."""
        instructions, input_items = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {
                "effort": (opts.reasoning_effort or "medium").strip() or "medium",
                "summary": "auto",
            },
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": project_openai_tool_schema(tool.parameters),
                    "strict": False,
                }
                for tool in tools
            ]
        return payload

    @staticmethod
    def _message_text(content: str | list[dict[str, Any]] | None) -> str:
        """Extract plain text from harness message content."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("text", "input_text", "output_text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _tool_arguments(call: dict[str, Any]) -> str:
        """Return authoritative function-call arguments as a JSON string.

        Mirrors OpenCode ``ProviderShared.encodeJson``: dict input is
        serialized, strings pass through verbatim.
        """
        function = call.get("function", call)
        if not isinstance(function, dict):
            function = {}
        arguments = function.get("arguments", call.get("arguments", ""))
        if isinstance(arguments, dict):
            return json.dumps(arguments)
        return str(arguments if isinstance(arguments, str) else "")

    def _convert_messages(
        self,
        messages: list[LLMMessage],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Split system prompts into instructions and convert input items."""
        system_parts: list[str] = []
        input_items: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                text = self._message_text(message.content)
                if text:
                    system_parts.append(text)
                continue

            if message.role == "user":
                text = self._message_text(message.content)
                input_items.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    }
                )
                continue

            if message.role == "assistant":
                if message.tool_calls:
                    for call in message.tool_calls:
                        function = call.get("function", call)
                        if not isinstance(function, dict):
                            function = {}
                        call_id = str(call.get("id", call.get("call_id", "")) or "")
                        if not call_id:
                            # OpenCode requires call_id; without one the
                            # replay would be rejected, so fall back to a
                            # best-effort id derived from name/index.
                            fallback = (
                                function.get("name", call.get("name", ""))
                                or "tool"
                            )
                            call_id = f"{fallback}-{len(input_items)}"
                        input_items.append(
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": function.get("name", call.get("name", "")),
                                "arguments": ChatGPTAdapter._tool_arguments(call),
                            }
                        )
                    # NOTE: reasoning replay gap — LLMMessage carries no
                    # providerMetadata/item ids, so reasoning parts cannot be
                    # replayed as encrypted reasoning items under
                    # store:false (openai-responses.ts filters reasoning
                    # without encrypted_content). No fake replay is sent.
                    continue
                text = self._message_text(message.content)
                if text:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    )
                continue

            if message.role == "tool":
                output = self._message_text(message.content)
                call_id = str(message.tool_call_id or "")
                if not call_id:
                    # OpenCode requires call_id; keep a best-effort id so
                    # an empty call_id is never sent on the wire.
                    call_id = f"tool-{len(input_items)}"
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    }
                )

        return "\n".join(system_parts), input_items

    async def _ensure_fresh_tokens(self) -> None:
        """Refresh OAuth tokens when missing or expired."""
        access = str(self._credentials.get("access", "") or "")
        expires = int(self._credentials.get("expires") or 0)
        if access and expires > int(time.time() * 1000):
            return

        async with self._refresh_lock:
            access = str(self._credentials.get("access", "") or "")
            expires = int(self._credentials.get("expires") or 0)
            if access and expires > int(time.time() * 1000):
                return
            await self._refresh_credentials()

    async def _refresh_credentials(self) -> None:
        """Refresh tokens and persist them via the callback."""
        refresh = str(self._credentials.get("refresh", "") or "")
        if not refresh:
            raise ProviderAuthError(
                RECONNECT_MESSAGE,
                provider=self.name,
            )
        client = self._client
        tokens = await refresh_tokens(refresh, client=client)
        updated = self._credentials_from_tokens(tokens)
        if tokens.account_id:
            updated["account_id"] = tokens.account_id
        elif self._credentials.get("account_id"):
            updated["account_id"] = self._credentials["account_id"]
        if tokens.residency:
            updated["residency"] = tokens.residency
        elif self._credentials.get("residency"):
            updated["residency"] = self._credentials["residency"]
        self._credentials.update(updated)
        if self._on_tokens_refreshed is not None:
            await self._on_tokens_refreshed(dict(self._credentials))

    @staticmethod
    def _credentials_from_tokens(tokens: OAuthTokens) -> dict[str, Any]:
        """Convert :class:`OAuthTokens` into the stored credentials dict."""
        data: dict[str, Any] = {
            "access": tokens.access,
            "refresh": tokens.refresh,
            "expires": tokens.expires,
        }
        if tokens.account_id:
            data["account_id"] = tokens.account_id
        if tokens.residency:
            data["residency"] = tokens.residency
        return data

    async def _stream_request(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
        options: ChatOptions,
        deadline: float | None,
    ) -> AsyncIterator[Delta]:
        """Open one SSE stream with header and chunk idle timeouts."""
        stream_cm = client.stream(
            "POST",
            CODEX_API_ENDPOINT,
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
                f"ChatGPT request timed out after {int(total * 1000)}ms",
                provider=self.name,
            )
        seconds = waited if waited is not None else 0.0
        return ProviderHeaderTimeoutError(
            provider=self.name,
            timeout_seconds=seconds,
        )

    async def _parse_stream(
        self,
        response: httpx.Response,
        *,
        chunk_timeout: float | None = None,
        deadline: float | None = None,
        total_timeout: float | None = None,
    ) -> AsyncIterator[Delta]:
        """Validate HTTP status and parse Responses SSE events."""
        if response.status_code == 401:
            body = await self._read_body(response, RESPONSE_BODY_MAX_CHARS)
            headers = self._response_headers(response)
            raise ProviderAuthError(
                RECONNECT_MESSAGE,
                provider=self.name,
                status_code=response.status_code,
                response_headers=headers,
                response_body=body,
                is_retryable=False,
            )
        if response.status_code == 403:
            body = await self._read_body_snippet(response)
            headers = self._response_headers(response)
            full_body = await self._read_body(response, RESPONSE_BODY_MAX_CHARS)
            raise ProviderAuthError(
                f"ChatGPT auth failed ({response.status_code}): {body}",
                provider=self.name,
                status_code=response.status_code,
                response_headers=headers,
                response_body=full_body,
            )
        if response.status_code == 429:
            body = await self._read_body_snippet(response)
            headers = self._response_headers(response)
            full_body = await self._read_body(response, RESPONSE_BODY_MAX_CHARS)
            raise ProviderRateLimitError(
                f"ChatGPT rate limit ({response.status_code}): {body}",
                provider=self.name,
                status_code=response.status_code,
                response_headers=headers,
                response_body=full_body,
                is_retryable=True,
            )
        if response.status_code >= 400:
            body = await self._read_body_snippet(response)
            headers = self._response_headers(response)
            full_body = await self._read_body(response, RESPONSE_BODY_MAX_CHARS)
            raise ProviderResponseError(
                f"ChatGPT error ({response.status_code}): {body}",
                provider=self.name,
                status_code=response.status_code,
                response_headers=headers,
                response_body=full_body,
            )

        stream_state: dict[str, Any] = {}
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
            delta = self._parse_event(data, stream_state)
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
                f"ChatGPT request timed out after {int(total * 1000)}ms",
                provider=self.name,
            )
        return ProviderStreamTimeoutError(provider=self.name)

    @staticmethod
    def _stream_key(
        raw_id: Any, state: dict[str, Any], *, reuse_last: bool = False
    ) -> str:
        """Return the provider-local stream key for a function-call item.

        Non-empty string ids are used verbatim (parallel calls keep
        distinct keys); missing ids fall back to a deterministic
        per-stream counter so fragments still group stably. With
        ``reuse_last``, a missing id reuses the most recent key (used for
        ``done``/``delta`` continuations of an ``added`` slot that had no
        id); ``added`` always mints a fresh key so parallel id-less items
        never share one slot.
        """
        if isinstance(raw_id, str) and raw_id:
            state["last_key"] = raw_id
            return raw_id
        if reuse_last and isinstance(state.get("last_key"), str):
            return str(state["last_key"])
        counter = int(state.get("fallback_counter", 0) or 0)
        state["fallback_counter"] = counter + 1
        state["last_key"] = str(counter)
        return str(counter)

    def _parse_event(
        self, data: str, stream_state: dict[str, Any] | None = None
    ) -> Delta | None:
        """Parse one SSE data payload into a :class:`Delta`.

        Function-call streaming follows OpenCode's Responses protocol
        (``openai-responses.ts``): ``response.output_item.added`` opens a
        slot keyed by ``item.id``, ``response.function_call_arguments.delta``
        appends fragments keyed by ``item_id``, and
        ``response.output_item.done`` finalizes with authoritative arguments.

        ``stream_state`` is a per-stream scratch dict (owned by
        :meth:`_parse_stream`; ``None`` creates a throwaway one so direct
        calls stay stateless). It tracks ``args_sent`` (``item.id`` ->
        already-emitted argument chars) so the ``done`` event only carries
        the not-yet-sent suffix — the runner concatenates fragments per
        key, and re-sending the full arguments would double them.
        """
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                f"Malformed SSE payload: {data[:200]}",
                provider=self.name,
            ) from exc
        if not isinstance(event, dict):
            raise ProviderResponseError(
                f"Unexpected SSE payload type: {type(event).__name__}",
                provider=self.name,
            )

        event_type = str(event.get("type", "") or "")
        if event_type == "error" or event_type == "response.failed":
            self._raise_stream_error(event)
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                return Delta(text=delta)
            return None
        if event_type in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                return Delta(reasoning=delta)
            return None
        if event_type == "response.output_item.added":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "function_call":
                return None
            state = stream_state if stream_state is not None else {}
            state["has_tool_calls"] = True
            key = self._stream_key(item.get("id"), state)
            call_id = str(item.get("call_id", "") or "") or key
            name = str(item.get("name", "") or "")
            arguments = str(item.get("arguments", "") or "")
            if arguments:
                args_sent = state.setdefault("args_sent", {})
                args_sent[key] = int(args_sent.get(key, 0) or 0) + len(arguments)
            return Delta(
                tool_calls=(
                    {
                        "index": key,
                        "id": call_id,
                        "name": name,
                        "arguments": arguments,
                    },
                )
            )
        if event_type == "response.function_call_arguments.delta":
            item_id = event.get("item_id")
            fragment = event.get("delta")
            if not isinstance(fragment, str) or not fragment:
                return None
            state = stream_state if stream_state is not None else {}
            state["has_tool_calls"] = True
            key = self._stream_key(
                item_id if isinstance(item_id, str) and item_id else None,
                state,
                reuse_last=True,
            )
            args_sent = state.setdefault("args_sent", {})
            args_sent[key] = int(args_sent.get(key, 0) or 0) + len(fragment)
            return Delta(tool_calls=({"index": key, "arguments": fragment},))
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                state = stream_state if stream_state is not None else {}
                state["has_tool_calls"] = True
                key = self._stream_key(item.get("id"), state, reuse_last=True)
                call_id = str(item.get("call_id", "") or "") or key
                name = str(item.get("name", "") or "")
                full_arguments = str(item.get("arguments", "") or "")
                # finishWithInput semantics: ``done`` carries the
                # authoritative full arguments, but the runner concatenates
                # every fragment per key — so only the suffix not already
                # streamed via added/delta events is emitted (usually ""
                # when deltas were complete, or the full string when no
                # deltas arrived). Python slicing yields "" when the
                # recorded prefix is longer than the final string.
                args_sent = state.setdefault("args_sent", {})
                sent = int(args_sent.get(key, 0) or 0)
                rest = full_arguments[sent:] if sent else full_arguments
                if rest:
                    args_sent[key] = sent + len(rest)
                return Delta(
                    tool_calls=(
                        {
                            "index": key,
                            "id": call_id,
                            "name": name,
                            "arguments": rest,
                        },
                    )
                )
            return None
        if event_type in ("response.completed", "response.incomplete"):
            response = event.get("response")
            if not isinstance(response, dict):
                has_tools = bool(
                    stream_state is not None and stream_state.get("has_tool_calls")
                )
                return Delta(finish_reason="tool_calls" if has_tools else "stop")
            if response.get("status") == "failed":
                self._raise_stream_error(response)
            usage = self._usage_from_response(response)
            has_tools = bool(
                stream_state is not None and stream_state.get("has_tool_calls")
            )
            return Delta(
                usage=usage,
                finish_reason=self._map_finish_reason(response, has_tools),
            )
        return None

    @staticmethod
    def _map_finish_reason(response: dict[str, Any], has_tool_calls: bool) -> str:
        """Map ``incomplete_details.reason`` to harness finish reasons.

        OpenCode parity (openai-responses.ts ``mapFinishReason``): the
        top-level ``response.status`` ("completed"/"incomplete"/"failed")
        is a lifecycle marker, not a finish reason. Only ``failed`` is an
        error (handled via ``response.failed``/``error`` events upstream);
        here ``completed``/``incomplete`` both finish normally with the
        reason taken from ``incomplete_details.reason``.
        """
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason is None:
            return "tool_calls" if has_tool_calls else "stop"
        if reason == "max_output_tokens":
            return "length"
        if reason == "content_filter":
            return "content_filter"
        return "tool_calls" if has_tool_calls else "unknown"

    def _raise_stream_error(self, event: dict[str, Any]) -> None:
        """Raise a provider error for failed stream events.

        Mirrors OpenCode ``openai-responses.ts`` ``providerError``: when both
        code and message are present the message is prefixed as
        ``"code: message"``; ``context_length_exceeded`` (or any overflow
        text per :func:`compaction.is_context_overflow_error`) is raised as
        a non-retryable :class:`ProviderResponseError` whose message and
        ``response_body`` both carry the overflow text so downstream
        compaction detection finds it.
        """
        from ..compaction import is_context_overflow_error as _is_overflow

        error = event.get("error")
        if not isinstance(error, dict):
            error = event
        nested = event.get("response")
        nested_error = (
            nested.get("error") if isinstance(nested, dict) else None
        )
        code = str(error.get("code", "") or "")
        message = str(error.get("message", "") or "")
        if not code and isinstance(nested_error, dict):
            code = str(nested_error.get("code", "") or "")
        if not message and isinstance(nested_error, dict):
            message = str(nested_error.get("message", "") or "")
        if code == "usage_not_included":
            raise ProviderAuthError(
                USAGE_NOT_INCLUDED_MESSAGE,
                provider=self.name,
                is_retryable=False,
            )
        if code and message:
            text = f"{code}: {message}"
        else:
            text = message or code or "ChatGPT stream failed"
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
    def _usage_from_response(response: dict[str, Any]) -> Usage | None:
        """Map Responses API usage into harness :class:`Usage`.

        ``input_tokens`` is the inclusive total (``cached_tokens`` is a
        subset), so cached tokens are never added on top (OpenCode
        totalTokens policy: honor the provider total, else input+output).
        """
        raw_usage = response.get("usage")
        if not isinstance(raw_usage, dict):
            return None
        prompt_tokens = int(raw_usage.get("input_tokens", 0) or 0)
        completion_tokens = int(raw_usage.get("output_tokens", 0) or 0)
        reported_total = raw_usage.get("total_tokens")
        total_tokens = (
            int(reported_total)
            if reported_total is not None
            else prompt_tokens + completion_tokens
        )
        return Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    @staticmethod
    async def _read_body_snippet(response: httpx.Response) -> str:
        """Read a short error-body snippet without raising."""
        return await ChatGPTAdapter._read_body(response, 500)

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
