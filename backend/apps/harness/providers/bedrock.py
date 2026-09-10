"""
Amazon Bedrock provider adapter (Converse Stream API).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import aioboto3
import structlog
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

from ..compaction import (
    CONTEXT_OVERFLOW_PATTERNS as _SHARED_OVERFLOW_PATTERNS,
)
from ..compaction import (
    NON_OVERFLOW_EXCLUSIONS as _SHARED_OVERFLOW_EXCLUSIONS,
)
from ..compaction import (
    is_context_overflow_error as _shared_is_context_overflow_error,
)
from ._lowering import (
    bedrock_media_block,
    is_tool_error_text,
    project_openai_tool_schema,
)
from .base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ToolSchema,
    Usage,
)
from .openrouter import effective_timeout, positive_timeout, stream_deadline

log = structlog.get_logger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_MAX_TOKENS = 8192
AUTH_SETTINGS_MESSAGE = "Check Bedrock credentials in organization settings."

#: Cross-region inference profile prefixes (passthrough when already present).
CROSS_REGION_PREFIXES = ("global.", "us.", "eu.", "jp.", "apac.", "au.")

#: Effort → thinking budget for Anthropic Claude on Bedrock (Converse
#: ``additionalModelRequestFields.thinking``).
THINKING_BUDGET_BY_EFFORT: dict[str, int] = {
    "low": 1024,
    "medium": 4096,
    "high": 16384,
    "xhigh": 32768,
}

#: US commercial regions — prefix eligible models (not GovCloud).
_US_PREFIX_MODELS = (
    "nova-micro",
    "nova-lite",
    "nova-pro",
    "nova-premier",
    "nova-2",
    "claude",
    "deepseek.r1",
)

#: EU regions that participate in cross-region inference.
_EU_REGIONS = (
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-north-1",
    "eu-central-1",
    "eu-south-1",
    "eu-south-2",
)

#: EU prefix-eligible model id fragments.
_EU_PREFIX_MODELS = ("claude", "nova-lite", "nova-micro", "llama3", "pixtral")

#: Tokyo/APAC prefix-eligible model id fragments.
_APAC_PREFIX_MODELS = ("claude", "nova-lite", "nova-micro", "nova-pro")

#: Australia-only Claude models.
_AU_CLAUDE_MODELS = (
    "anthropic.claude-opus-4-6",
    "anthropic.claude-sonnet-4-5",
    "anthropic.claude-haiku",
)

#: Bedrock overflow detection reuses the shared OpenCode-parity patterns from
#: :mod:`apps.harness.compaction` (single source of truth — no divergent list).
#: Kept as module aliases so existing imports keep working.
_CONTEXT_OVERFLOW_PATTERNS = tuple(
    pattern.pattern for pattern in _SHARED_OVERFLOW_PATTERNS
)

_CONTEXT_OVERFLOW_EXCLUSIONS = tuple(
    pattern.pattern for pattern in _SHARED_OVERFLOW_EXCLUSIONS
)


def resolve_bedrock_model_id(model_id: str, region: str) -> str:
    """Apply Bedrock cross-region inference profile prefixes when required.

    Mirrors OpenCode's ``amazon-bedrock`` provider matrix: passthrough ARNs
    and ids that already carry a cross-region prefix; otherwise prefix by
    region group and model family.
    """
    if model_id.startswith("arn:"):
        return model_id
    if any(model_id.startswith(prefix) for prefix in CROSS_REGION_PREFIXES):
        return model_id
    if "deepseek.v3.2" in model_id.lower() or "deepseek-v3.2" in model_id.lower():
        return model_id

    resolved_region = region or DEFAULT_REGION
    region_prefix = resolved_region.split("-")[0]

    if region_prefix == "us":
        if resolved_region.startswith("us-gov"):
            return model_id
        if any(fragment in model_id for fragment in _US_PREFIX_MODELS):
            return f"us.{model_id}"
        return model_id

    if region_prefix == "eu":
        region_ok = any(item in resolved_region for item in _EU_REGIONS)
        model_ok = any(fragment in model_id for fragment in _EU_PREFIX_MODELS)
        if region_ok and model_ok:
            return f"eu.{model_id}"
        return model_id

    if region_prefix != "ap":
        return model_id

    if resolved_region in ("ap-southeast-2", "ap-southeast-4"):
        if any(fragment in model_id for fragment in _AU_CLAUDE_MODELS):
            return f"au.{model_id}"
        return model_id

    if resolved_region in ("ap-northeast-1", "ap-northeast-2", "ap-northeast-3"):
        if any(fragment in model_id for fragment in _APAC_PREFIX_MODELS):
            return f"jp.{model_id}"
        return model_id

    if any(fragment in model_id for fragment in _APAC_PREFIX_MODELS):
        return f"apac.{model_id}"
    return model_id


def is_context_overflow(message: str) -> bool:
    """Return True when a Bedrock validation message indicates context overflow.

    Delegates to the shared :func:`compaction.is_context_overflow_error`
    classification (OpenCode ``provider-error.ts`` parity) so Bedrock and
    compaction never diverge. Accepts a plain message string for the
    validation/stream call sites.
    """
    return _shared_is_context_overflow_error(RuntimeError(message))


def _is_anthropic_claude_model(model_id: str) -> bool:
    """Return True when *model_id* refers to an Anthropic Claude on Bedrock."""
    lowered = model_id.lower()
    return "anthropic.claude" in lowered or (
        "claude" in lowered and "anthropic" in lowered
    )


def _message_text(content: str | list[dict[str, Any]] | None) -> str:
    """Extract plain text from harness message content.

    Non-text parts (image/media/reasoning) contribute nothing here;
    media is lowered separately via :func:`bedrock_media_block`.
    """
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


def _tool_choice_block(tool_choice: str | None) -> dict[str, Any] | None:
    """Map harness tool_choice to a Converse ``toolChoice`` block.

    Mirrors OpenCode ``bedrock-converse.ts`` ``lowerToolChoice``:
    ``auto`` → ``{"auto": {}}``, ``none`` → no toolConfig at all,
    ``required`` → ``{"any": {}}``, tool name → ``{"tool": {"name"}}``.
    ``None``/unknown defaults to ``{"auto": {}}``.
    """
    if tool_choice == "none":
        return None
    if tool_choice == "required":
        return {"any": {}}
    if tool_choice and tool_choice not in ("auto",):
        return {"tool": {"name": tool_choice}}
    return {"auto": {}}


def _user_content_blocks(
    content: str | list[dict[str, Any]] | None,
    *,
    provider: str,
) -> list[dict[str, Any]]:
    """Lower user content to Converse blocks, keeping media and text.

    Empty text blocks are filtered (Converse rejects ``{"text": ""}``).
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        media = bedrock_media_block(part, provider=provider)
        if media is not None:
            blocks.append(media)
            continue
        if part.get("type") in ("text", "input_text", "output_text"):
            text = part.get("text")
            if isinstance(text, str) and text:
                blocks.append({"text": text})
    return blocks


def _tool_result_content(
    content: str | list[dict[str, Any]] | None,
    *,
    provider: str,
) -> list[dict[str, Any]]:
    """Lower tool output to Converse ``toolResult.content`` blocks.

    Converse ``toolResult`` blocks cannot carry ``document`` blocks
    (Bedrock API limitation, OpenCode parity: documents only ride on
    user messages). PDF/``file`` parts in tool results are therefore
    skipped (text output survives); image parts are kept, unknown media
    is dropped without crashing.
    """
    if isinstance(content, str):
        return [{"text": content}] if content else [{"text": ""}]
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "file":
            # Documents are not allowed inside toolResult — skip the
            # part, keep the surrounding text (see docstring).
            continue
        media = bedrock_media_block(part, provider=provider)
        if media is not None:
            # Only image media is meaningful inside tool results;
            # a document block would be rejected there, so skip it.
            if "image" not in media:
                continue
            blocks.append(media)
            continue
        if part.get("type") in ("text", "input_text", "output_text"):
            text = part.get("text")
            if isinstance(text, str) and text:
                blocks.append({"text": text})
    if not blocks:
        text = _message_text(content)
        blocks.append({"text": text})
    return blocks


def _parse_tool_arguments(raw: str) -> dict[str, Any]:
    """Parse tool-call arguments JSON, defaulting to an empty object."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _reasoning_texts(content: str | list[dict[str, Any]] | None) -> list[str]:
    """Extract reasoning content parts (no signature metadata available)."""
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("reasoning", "reasoning_content", "reasoning_text"):
            text = part.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return texts


def _merge_message(
    messages: list[dict[str, Any]],
    role: str,
    content: list[dict[str, Any]],
) -> None:
    """Append *content* to the last message when roles match (Converse rule)."""
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(content)
        return
    messages.append({"role": role, "content": content})


class _ToolCallState:
    """Accumulates one in-flight Bedrock tool-use block."""

    def __init__(self, tool_use_id: str, name: str, index: int) -> None:
        self.tool_use_id = tool_use_id
        self.name = name
        self.index = index
        self.input_parts: list[str] = []

    def append_input(self, fragment: str) -> None:
        """Append a partial JSON input fragment."""
        if fragment:
            self.input_parts.append(fragment)

    def as_delta(self) -> Delta:
        """Build a completed tool-call delta."""
        arguments = "".join(self.input_parts)
        return Delta(
            tool_calls=(
                {
                    "index": self.index,
                    "id": self.tool_use_id,
                    "name": self.name,
                    "arguments": arguments,
                },
            )
        )


class BedrockAdapter(ProviderAdapter):
    """LLM provider adapter for Amazon Bedrock Converse Stream."""

    name = "amazon-bedrock"

    def __init__(
        self,
        credentials: dict[str, Any],
        region: str = DEFAULT_REGION,
        endpoint_url: str | None = None,
        session: Any | None = None,
    ) -> None:
        """Create an adapter.

        Args:
            credentials: Either SigV4 keys
                (``access_key_id``, ``secret_access_key``, optional
                ``session_token``) or ``bearer_token`` for API-key auth.
            region: AWS region for the Bedrock runtime endpoint.
            endpoint_url: Optional custom Bedrock endpoint override.
            session: Optional ``aioboto3.Session`` (mainly for tests).
        """
        self._credentials = dict(credentials)
        self._region = region or DEFAULT_REGION
        self._endpoint_url = endpoint_url
        self._session = session
        self._bearer_token = str(self._credentials.get("bearer_token", "") or "")

    async def chat_stream(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Stream a chat completion via Bedrock Converse Stream.

        Yields:
            Incremental :class:`Delta` objects (text, reasoning, tool calls,
            usage, finish reason).

        Raises:
            ProviderAuthError: On invalid or expired credentials.
            ProviderRateLimitError: On throttling (429).
            ProviderTimeoutError: On request or stream idle timeouts.
            ProviderResponseError: On validation or server errors.
        """
        options = opts or ChatOptions()
        body = self._build_converse_body(model, messages, tools, options)
        logger = log.bind(provider=self.name, model=body["modelId"])
        deadline = stream_deadline(options)
        session = self._session or aioboto3.Session()

        try:
            async with session.client(**self._client_kwargs()) as client:
                if self._bearer_token:
                    self._register_bearer_auth(client, self._bearer_token)
                header_timeout = effective_timeout(
                    options.header_timeout_seconds, deadline
                )
                try:
                    if header_timeout is None:
                        response = await client.converse_stream(**body)
                    else:
                        response = await asyncio.wait_for(
                            client.converse_stream(**body),
                            timeout=header_timeout,
                        )
                except TimeoutError as exc:
                    raise ProviderTimeoutError(
                        "Bedrock request timed out waiting for stream",
                        provider=self.name,
                    ) from exc

                stream = response.get("stream")
                if stream is None:
                    raise ProviderResponseError(
                        "Bedrock converse_stream returned no stream",
                        provider=self.name,
                    )

                async for delta in self._parse_stream(
                    stream,
                    chunk_timeout=options.chunk_timeout_seconds,
                    deadline=deadline,
                    total_timeout=options.timeout_seconds,
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
        except ClientError as exc:
            logger.warning("provider_client_error")
            raise self._map_client_error(exc) from exc
        except Exception as exc:
            logger.warning("provider_unexpected_error")
            raise ProviderResponseError(
                f"Bedrock request failed: {exc}",
                provider=self.name,
            ) from exc

    def _client_kwargs(self) -> dict[str, Any]:
        """Build ``aioboto3`` client kwargs for ``bedrock-runtime``."""
        kwargs: dict[str, Any] = {
            "service_name": "bedrock-runtime",
            "region_name": self._region,
        }
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url

        if self._bearer_token:
            # Bearer auth: use UNSIGNED so botocore skips SigV4; a before-sign
            # handler injects ``Authorization: Bearer <token>`` instead.
            kwargs["aws_access_key_id"] = "unused"
            kwargs["aws_secret_access_key"] = "unused"
            kwargs["config"] = Config(signature_version=UNSIGNED)
            return kwargs

        access_key = str(self._credentials.get("access_key_id", "") or "")
        secret_key = str(self._credentials.get("secret_access_key", "") or "")
        if not access_key or not secret_key:
            raise ProviderAuthError(
                AUTH_SETTINGS_MESSAGE,
                provider=self.name,
                is_retryable=False,
            )
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
        session_token = self._credentials.get("session_token")
        if session_token:
            kwargs["aws_session_token"] = str(session_token)
        return kwargs

    @staticmethod
    def _register_bearer_auth(client: Any, bearer_token: str) -> None:
        """Register a before-sign hook that injects Bedrock bearer auth.

        Bedrock API keys use ``Authorization: Bearer …`` instead of SigV4.
        ``Config(signature_version=UNSIGNED)`` prevents the SigV4 signer from
        running; this handler sets the bearer header on the unsigned request.
        """

        def _inject_bearer(request: Any, **kwargs: Any) -> None:
            request.headers["Authorization"] = f"Bearer {bearer_token}"

        client.meta.events.register_first(
            "before-sign.bedrock-runtime.ConverseStream",
            _inject_bearer,
        )

    def _build_converse_body(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions,
    ) -> dict[str, Any]:
        """Build the Converse Stream request body.

        Generation gap (documented): :class:`ChatOptions` only carries
        ``temperature``/``max_tokens``, so ``topP``/``stopSequences``
        (sent by OpenCode ``fromRequest``) and ``topK`` via
        ``additionalModelRequestFields.top_k`` cannot be set —
        ``additionalModelRequestFields`` only carries the Claude
        thinking config.
        """
        model_id = resolve_bedrock_model_id(model, self._region)
        system_blocks, converse_messages = self._convert_messages(messages)
        body: dict[str, Any] = {"modelId": model_id, "messages": converse_messages}

        if system_blocks:
            body["system"] = system_blocks

        max_tokens = opts.max_tokens or DEFAULT_MAX_TOKENS
        inference: dict[str, Any] = {"maxTokens": max_tokens}

        effort = (opts.reasoning_effort or "").strip().lower()
        additional_fields: dict[str, Any] | None = None
        if effort and _is_anthropic_claude_model(model_id):
            budget = THINKING_BUDGET_BY_EFFORT.get(effort, THINKING_BUDGET_BY_EFFORT["medium"])
            # Claude extended thinking requires maxTokens > budget_tokens and
            # temperature unset (OpenCode transform.ts omits temperature for Claude).
            max_tokens = max(max_tokens, budget + 1)
            inference["maxTokens"] = max_tokens
            additional_fields = {
                "thinking": {"type": "enabled", "budget_tokens": budget},
            }
        elif opts.temperature is not None:
            inference["temperature"] = opts.temperature

        body["inferenceConfig"] = inference

        if additional_fields is not None:
            body["additionalModelRequestFields"] = additional_fields

        tool_choice = _tool_choice_block(opts.tool_choice)
        if tools and tool_choice is not None:
            body["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {
                                "json": project_openai_tool_schema(tool.parameters)
                            },
                        }
                    }
                    for tool in tools
                ],
                "toolChoice": tool_choice,
            }

        return body

    def _convert_messages(
        self,
        messages: list[LLMMessage],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split system prompts and convert harness messages to Converse form."""
        system_parts: list[str] = []
        converse_messages: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                text = _message_text(message.content)
                if text:
                    system_parts.append(text)
                continue

            if message.role == "user":
                blocks = _user_content_blocks(message.content, provider=self.name)
                if blocks:
                    _merge_message(converse_messages, "user", blocks)
                continue

            if message.role == "assistant":
                blocks: list[dict[str, Any]] = []
                text_parts: list[str] = []
                reasoning_texts: list[str] = []
                if message.tool_calls:
                    for call in message.tool_calls:
                        function = call.get("function", call)
                        if not isinstance(function, dict):
                            function = {}
                        tool_use_id = str(call.get("id", "") or "")
                        name = str(function.get("name", call.get("name", "")) or "")
                        arguments = function.get("arguments", call.get("arguments", ""))
                        blocks.append(
                            {
                                "toolUse": {
                                    "toolUseId": tool_use_id,
                                    "name": name,
                                    "input": _parse_tool_arguments(
                                        arguments if isinstance(arguments, str) else json.dumps(arguments)
                                    ),
                                }
                            }
                        )
                    text_parts.append(_message_text(message.content))
                    reasoning_texts.extend(_reasoning_texts(message.content))
                else:
                    text_parts.append(_message_text(message.content))
                    reasoning_texts.extend(_reasoning_texts(message.content))
                for reasoning_text in reasoning_texts:
                    if reasoning_text:
                        blocks.append(
                            {
                                "reasoningContent": {
                                    "reasoningText": {"text": reasoning_text},
                                }
                            }
                        )
                for text_part in text_parts:
                    if text_part:
                        blocks.append({"text": text_part})
                if blocks:
                    _merge_message(converse_messages, "assistant", blocks)
                continue

            if message.role == "tool":
                content_blocks = _tool_result_content(
                    message.content, provider=self.name
                )
                output_text = _message_text(message.content)
                tool_use_id = str(message.tool_call_id or "")
                _merge_message(
                    converse_messages,
                    "user",
                    [
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "content": content_blocks,
                                # LLMMessage carries no error marker; the
                                # runner writes failures as "Permission …:",
                                # "Unknown tool …" or "Tool '…' failed:"
                                # (see _lowering.is_tool_error_text).
                                "status": (
                                    "error"
                                    if is_tool_error_text(output_text)
                                    else "success"
                                ),
                            }
                        }
                    ],
                )

        system_blocks = [{"text": "\n".join(system_parts)}] if system_parts else []
        return system_blocks, converse_messages

    async def _parse_stream(
        self,
        stream: Any,
        *,
        chunk_timeout: float | None,
        deadline: float | None,
        total_timeout: float | None,
    ) -> AsyncIterator[Delta]:
        """Parse Converse Stream events into harness deltas."""
        active_tools: dict[int, _ToolCallState] = {}
        pending_finish: str | None = None
        pending_usage: Usage | None = None
        has_tool_calls = False
        # aiobotocore AioEventStream is an async iterable whose __anext__
        # is an async generator (yield). anext(stream) therefore returns
        # that generator, which cannot be awaited. aiter() is what
        # ``async for`` uses and yields a real async iterator.
        iterator = aiter(stream)

        while True:
            timeout = effective_timeout(chunk_timeout, deadline)
            try:
                if timeout is None:
                    event = await anext(iterator)
                else:
                    event = await asyncio.wait_for(
                        anext(iterator), timeout=timeout
                    )
            except StopAsyncIteration:
                if pending_finish is not None or pending_usage is not None:
                    # OpenCode onHalt parity (bedrock-converse.ts:622-623):
                    # pending "stop" with tool calls observed becomes
                    # "tool_calls" so the runner dispatches tools.
                    reason = pending_finish or "stop"
                    if reason == "stop" and has_tool_calls:
                        reason = "tool_calls"
                    yield Delta(
                        usage=pending_usage,
                        finish_reason=reason,
                    )
                return
            except TimeoutError as exc:
                if deadline is not None and (deadline - time.monotonic()) <= 0:
                    total = positive_timeout(total_timeout) or 0.0
                    raise ProviderTimeoutError(
                        f"Bedrock request timed out after {int(total * 1000)}ms",
                        provider=self.name,
                    ) from exc
                raise ProviderTimeoutError(
                    "Bedrock stream idle timeout",
                    provider=self.name,
                ) from exc

            for delta in self._event_to_deltas(event, active_tools):
                if delta.tool_calls:
                    has_tool_calls = True
                yield delta

            stream_error = self._stream_error_from_event(event)
            if stream_error is not None:
                raise stream_error

            stop = event.get("messageStop")
            if isinstance(stop, dict):
                pending_finish = self._map_finish_reason(
                    str(stop.get("stopReason", "") or "")
                )

            metadata = event.get("metadata")
            if isinstance(metadata, dict):
                pending_usage = self._map_usage(metadata.get("usage"))

        # pragma: no cover - loop exits via StopAsyncIteration

    def _event_to_deltas(
        self,
        event: dict[str, Any],
        active_tools: dict[int, _ToolCallState],
    ) -> list[Delta]:
        """Map one Converse stream event to zero or more deltas."""
        deltas: list[Delta] = []

        block_start = event.get("contentBlockStart")
        if isinstance(block_start, dict):
            start = block_start.get("start")
            if isinstance(start, dict):
                tool_use = start.get("toolUse")
                if isinstance(tool_use, dict):
                    index = int(block_start.get("contentBlockIndex", 0))
                    active_tools[index] = _ToolCallState(
                        str(tool_use.get("toolUseId", "") or ""),
                        str(tool_use.get("name", "") or ""),
                        index,
                    )

        block_delta = event.get("contentBlockDelta")
        if isinstance(block_delta, dict):
            delta_payload = block_delta.get("delta")
            if isinstance(delta_payload, dict):
                text = delta_payload.get("text")
                if isinstance(text, str) and text:
                    deltas.append(Delta(text=text))

                reasoning = delta_payload.get("reasoningContent")
                if isinstance(reasoning, dict):
                    reasoning_text = reasoning.get("text")
                    if isinstance(reasoning_text, str) and reasoning_text:
                        deltas.append(Delta(reasoning=reasoning_text))

                tool_delta = delta_payload.get("toolUse")
                if isinstance(tool_delta, dict):
                    index = int(block_delta.get("contentBlockIndex", 0))
                    state = active_tools.get(index)
                    fragment = tool_delta.get("input")
                    if state is not None and isinstance(fragment, str):
                        state.append_input(fragment)

        block_stop = event.get("contentBlockStop")
        if isinstance(block_stop, dict):
            index = int(block_stop.get("contentBlockIndex", 0))
            state = active_tools.pop(index, None)
            if state is not None:
                deltas.append(state.as_delta())

        return deltas

    def _stream_error_from_event(self, event: dict[str, Any]) -> ProviderResponseError | ProviderRateLimitError | None:
        """Map provider errors encoded in stream exception events.

        OpenCode ``bedrock-converse.ts`` parity: throttling is retryable,
        internal/server/stream errors are retryable, and validation errors
        are non-retryable only when they classify as context overflow
        (``is_context_overflow``); other validation errors carry no retry
        hint. Overflow errors also carry ``response_body`` so
        :func:`compaction.is_context_overflow_error` detects them.
        """
        throttling = event.get("throttlingException")
        if isinstance(throttling, dict):
            message = str(throttling.get("message", "") or "Bedrock throttling")
            return ProviderRateLimitError(
                message,
                provider=self.name,
                status_code=429,
                response_body=message[:2000],
                is_retryable=True,
            )

        for key in (
            "internalServerException",
            "serviceUnavailableException",
            "modelStreamErrorException",
        ):
            payload = event.get(key)
            if isinstance(payload, dict):
                message = str(payload.get("message", "") or f"Bedrock {key}")
                return ProviderResponseError(
                    message,
                    provider=self.name,
                    response_body=message[:2000],
                    is_retryable=True,
                )

        validation = event.get("validationException")
        if isinstance(validation, dict):
            message = str(validation.get("message", "") or "Bedrock validation error")
            overflow = is_context_overflow(message)
            return ProviderResponseError(
                message,
                provider=self.name,
                response_body=message[:2000],
                is_retryable=False if overflow else None,
            )

        return None

    @staticmethod
    def _map_finish_reason(stop_reason: str) -> str:
        """Map Bedrock ``stopReason`` to harness finish reasons."""
        if stop_reason in ("end_turn", "stop_sequence"):
            return "stop"
        if stop_reason == "tool_use":
            return "tool_calls"
        if stop_reason == "max_tokens":
            return "length"
        if stop_reason in ("content_filtered", "guardrail_intervened"):
            return "content_filter"
        return "unknown"

    @staticmethod
    def _map_usage(raw_usage: Any) -> Usage | None:
        """Map Bedrock usage metadata to harness :class:`Usage`.

        ``inputTokens`` is the inclusive total (cache subsets included),
        so cache counts are never added on top (OpenCode totalTokens
        policy: honor the provider total, else input+output).
        """
        if not isinstance(raw_usage, dict):
            return None
        prompt_tokens = int(raw_usage.get("inputTokens", 0) or 0)
        completion_tokens = int(raw_usage.get("outputTokens", 0) or 0)
        reported_total = raw_usage.get("totalTokens")
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

    def _map_client_error(self, exc: ClientError) -> ProviderError:
        """Map ``botocore`` ``ClientError`` to harness provider errors."""
        response = exc.response or {}
        error = response.get("Error", {})
        code = str(error.get("Code", "") or "")
        message = str(error.get("Message", "") or str(exc))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")

        auth_codes = {
            "AccessDeniedException",
            "ExpiredTokenException",
            "InvalidSignatureException",
            "UnauthorizedException",
            "UnrecognizedClientException",
            "InvalidClientTokenId",
            "SignatureDoesNotMatch",
        }
        if code in auth_codes or status in (401, 403):
            return ProviderAuthError(
                f"{AUTH_SETTINGS_MESSAGE} ({code or status}): {message}",
                provider=self.name,
                status_code=status,
                response_body=message[:2000],
                is_retryable=False,
            )

        if code == "ThrottlingException" or status == 429:
            return ProviderRateLimitError(
                message,
                provider=self.name,
                status_code=status or 429,
                response_body=message[:2000],
                is_retryable=True,
            )

        if code == "ValidationException":
            overflow = is_context_overflow(message)
            return ProviderResponseError(
                message,
                provider=self.name,
                status_code=status,
                response_body=message[:2000],
                is_retryable=False if overflow else None,
            )

        if code in ("InternalServerException", "ServiceUnavailableException"):
            return ProviderResponseError(
                message,
                provider=self.name,
                status_code=status,
                response_body=message[:2000],
                is_retryable=True,
            )

        return ProviderResponseError(
            message,
            provider=self.name,
            status_code=status,
            response_body=message[:2000],
        )
