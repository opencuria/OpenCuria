"""Agentic loop for the harness (M4).

:func:`HarnessRunner.run` drives one agent turn: it composes the system
prompt, calls the provider with permission-filtered tool schemas,
streams deltas via an ``emit`` callback, gates ``ask`` tools through an
``on_permission`` callback, executes approved tools, and repeats until
a text-only answer, an optional last step, an abort, or a doom-loop
guard.

Permission ``ask`` and question tools wait until the user resolves them
or the run is aborted. An optional timeout still auto-denies (permissions)
or raises ``ToolError`` (questions) when tests set one. Cancellation
(``asyncio.CancelledError``) is re-raised after emitting an ``aborted``
event so callers still observe cancellation.

Independent tool calls in one step run concurrently. Results are fed
back to the model in the original emitted call order. A single tool
failure does not cancel its siblings.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, Protocol

import structlog

from .access.base import HARNESS_WORKSPACE_ROOT, WorkspaceAccessor
from .agents.definitions import (
    SMALL_MODEL,
    AgentDefinition,
    get_agent,
    subagent_descriptions,
)
from .compaction import (
    CONTEXT_OVERFLOW_COMPACTION_ERROR,
    ModelLimits,
    apply_compaction,
    apply_overflow_replay,
    build_compaction_prompt,
    ensure_current_user,
    find_previous_summary,
    is_overflow,
    select,
)
from .constants import DEFAULT_MAX_DEPTH
from .engines import base as engine_contracts
from .max_steps import MAX_STEPS_PROMPT, MAX_STEPS_TOOL_ERROR
from .media.context import AuthorizeRead, hydrate_image_messages, limit_image_messages
from .permissions.evaluator import PermissionEvaluator
from .prompts.composer import compose_system_prompt
from .provider_retry import (
    RETRY_MAX_RETRIES,
    is_retryable_provider_error,
    retry_delay,
)
from .providers.base import (
    ChatOptions,
    LLMMessage,
    ToolSchema,
    Usage,
)
from .providers.context_errors import is_context_overflow_error
from .providers.resolver import ResolvedModel
from .tools.base import ToolContext, ToolRegistry
from .tools.invocation import (
    AuthorizedToolDispatcher,
    _PendingToolCall,
    _ToolCallOutcome,
)

log = structlog.get_logger(__name__)

#: Max fingerprints kept for the doom-loop guard. The guard only reads
#: the last 3 entries, so trimming is behavior-preserving.
RECENT_CALLS_MAXLEN = 10

#: Notice appended when the provider stops a step via content filter.
#: Tool calls (if any) are skipped in that case.
CONTENT_FILTER_NOTICE = (
    "\n\n[Notice: the response was stopped by a content filter; "
    "tool calls were skipped.]"
)

#: Notice appended when a length stop survives the bounded retry.
LENGTH_TRUNCATION_NOTICE = (
    "\n\n[Notice: the response was truncated due to length limits.]"
)

#: How many consecutive identical tool+input calls trigger doom-loop ask.
DOOM_LOOP_REPEATS = 3


class ModelResolver(Protocol):
    """Resolve a model ref to an adapter plus bare provider model id."""

    def __call__(self, model_ref: str) -> ResolvedModel:
        """Return provider adapter and limits for *model_ref*."""
        ...


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Parse raw tool arguments (JSON string or dict) into a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_last_step(step: int, max_steps: int | None) -> bool:
    """Return True when *step* is the optional configured last step."""
    return max_steps is not None and step >= max_steps


class HarnessRunner(AuthorizedToolDispatcher):
    """Agentic loop: provider + tools + permissions + streaming events."""

    def __init__(
        self,
        *,
        model_resolver: ModelResolver,
        tools: ToolRegistry,
        evaluator: PermissionEvaluator | None = None,
        accessor: WorkspaceAccessor | None = None,
        emit: engine_contracts.EmitCallback | None = None,
        chat_options: ChatOptions | None = None,
    ) -> None:
        """Create a runner.

        Args:
            model_resolver: Callable ``resolve(model_ref) -> ResolvedModel`` used
                for per-step adapter selection in multi-provider setups.
            tools: Registry of executable tools.
            evaluator: Base (global/org) permission evaluator.
            accessor: Workspace accessor for tools and context files.
            emit: Async callback receiving streaming event dicts.
            chat_options: Optional per-request provider settings.
        """
        self._resolve_model = model_resolver
        super().__init__(tools=tools, accessor=accessor, emit=emit, evaluator=evaluator)
        self._log = log
        self.chat_options = chat_options or ChatOptions()

    async def _send(self, event: dict[str, Any]) -> None:
        """Emit *event* without breaking the loop on emitter errors."""
        try:
            await self._emit(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("emit_failed", error=str(exc), event_type=event.get("type"))

    async def _run_step_tools(
        self,
        *,
        calls: list[_PendingToolCall],
        recent_calls: list[str],
        ctx: ToolContext,
        agent: AgentDefinition,
        mode: str,
        step: int,
        depth: int,
        max_depth: int,
        opts: engine_contracts.RunOptions,
    ) -> list[_ToolCallOutcome]:
        """Execute *calls* concurrently; return outcomes in original order."""
        doom_flags: list[bool] = []
        for call in calls:
            fingerprint = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
            recent_calls.append(fingerprint)
            recent_calls[:] = recent_calls[-RECENT_CALLS_MAXLEN:]
            doom_flags.append(
                len(recent_calls) >= DOOM_LOOP_REPEATS
                and len(set(recent_calls[-DOOM_LOOP_REPEATS:])) == 1
            )
        tasks = [
            asyncio.create_task(
                self._dispatch_tool_call(
                    call=call,
                    ctx=ctx,
                    agent=agent,
                    mode=mode,
                    step=step,
                    depth=depth,
                    max_depth=max_depth,
                    doom_loop=doom_loop,
                    opts=opts,
                ),
                name=f"harness-tool-{call.call_id}",
            )
            for call, doom_loop in zip(calls, doom_flags)
        ]
        try:
            raw = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                # gather has already cancelled its children. Do not interrupt
                # their cancellation handlers a second time while they clean up.
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        outcomes: list[_ToolCallOutcome] = []
        for call, item in zip(calls, raw):
            if isinstance(item, asyncio.CancelledError):
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise item
                # gather completed normally: only this tool was interrupted,
                # not the owning run. Preserve siblings and let the model recover.
                item = RuntimeError("Tool execution interrupted unexpectedly")
            if isinstance(item, BaseException):
                log.warning(
                    "tool_dispatch_failed",
                    tool=call.name,
                    call_id=call.call_id,
                    error=str(item),
                )
                await self._send(
                    {
                        "type": "tool_error",
                        "step": step,
                        "call_id": call.call_id,
                        "queued_id": call.queued_id,
                        "tool": call.name,
                        "error": str(item),
                    }
                )
                outcomes.append(
                    self._tool_error_outcome(call, f"Tool '{call.name}' failed: {item}")
                )
                continue
            outcomes.append(item)
        return outcomes

    async def run(
        self,
        prompt: str,
        agent_name: str,
        model: str,
        mode: str = "build",
        opts: engine_contracts.RunOptions | None = None,
    ) -> engine_contracts.RunResult:
        """Run one agent turn and return the aggregated result.

        Args:
            prompt: User prompt for this turn.
            agent_name: Static agent definition name (unknown raises).
            model: Provider model identifier.
            mode: ``plan`` or ``build``.
            opts: Run settings (steps, history, permission callback).

        Returns:
            The final text, step count, summed usage/cost, and a finish
            reason (``stop`` | ``max_steps``).

        Raises:
            KeyError: For unknown agent names.
            ValueError: For invalid modes or empty prompts.
            asyncio.CancelledError: On abort (after emitting ``aborted``).
        """
        options = opts or engine_contracts.RunOptions()
        # Per-run once-approval cache: never shared across runs, even
        # when the caller reuses the same RunOptions object.
        options.once_approved = set()
        if mode not in ("plan", "build"):
            raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        agent = get_agent(agent_name)  # raises KeyError for unknown agents
        effective_model = model
        if agent.model_override == SMALL_MODEL and options.small_model:
            effective_model = options.small_model
        max_steps = options.max_steps or agent.steps
        max_depth = options.max_depth if options.max_depth > 0 else DEFAULT_MAX_DEPTH
        depth = max(0, options.depth)
        if depth > max_depth:
            raise ValueError(f"depth {depth} exceeds max_depth {max_depth}")
        cwd = options.cwd or HARNESS_WORKSPACE_ROOT

        try:
            return await self._run_inner(
                prompt=prompt,
                agent=agent,
                model=effective_model,
                mode=mode,
                opts=options,
                cwd=cwd,
                max_steps=max_steps,
                depth=depth,
                max_depth=max_depth,
            )
        except asyncio.CancelledError:
            await self._send({"type": "aborted", "reason": "cancelled"})
            raise

    async def _run_inner(
        self,
        *,
        prompt: str,
        agent: AgentDefinition,
        model: str,
        mode: str,
        opts: engine_contracts.RunOptions,
        cwd: str,
        max_steps: int | None,
        depth: int = 0,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> engine_contracts.RunResult:
        """Execute the step loop (cancellation handled by :meth:`run`)."""
        schemas = self._filtered_schemas(agent, mode, depth=depth, max_depth=max_depth)
        composed = await compose_system_prompt(
            agent=agent,
            mode=mode,
            tools=schemas,
            subagents=subagent_descriptions(),
            accessor=self.accessor,
            cwd=cwd,
            skills=list(opts.skills or []),
        )
        messages: list[LLMMessage] = [
            LLMMessage(role="system", content=composed.system),
            *list(opts.history or []),
            LLMMessage(
                role="user",
                content=prompt,
                message_id=opts.current_user_message_id or "",
            ),
        ]

        async def authorize_image(path: str) -> bool:
            decision = self._decide(agent, "read", path, mode)
            if decision != "ask":
                return decision == "allow"
            return await self._resolve_ask(
                tool_name="read",
                action=path,
                title=f"Read {path}",
                call_id=f"media-{path}",
                step=0,
                doom_loop=False,
                opts=opts,
            )

        authorize_read = opts.authorize_file_read or authorize_image
        if self.accessor is not None:
            messages = await hydrate_image_messages(
                messages, self.accessor, authorize_read=authorize_read
            )
        ctx = ToolContext(
            session_id=opts.session_id or "session",
            workspace_id=opts.workspace_id or "workspace",
            accessor=self.accessor
            or _MissingAccessor(workspace_id=opts.workspace_id or "workspace"),
            agent_name=agent.name,
            directory=cwd,
            depth=depth,
            max_depth=max_depth,
            model=model,
            registry=self.tools,
            run_subagent=opts.run_subagent,
            on_question=opts.on_question,
            question_timeout=opts.question_timeout,
        )

        total_usage = Usage()
        total_cost = 0.0
        recent_calls: list[str] = []
        limits = ModelLimits(
            context_length=opts.context_length,
            max_output_tokens=opts.max_output_tokens,
        )
        last_step_prompt = opts.last_step_prompt_tokens
        last_step_completion = opts.last_step_completion_tokens
        last_step_total = opts.last_step_total_tokens

        step = 0
        while True:
            step += 1
            is_last = _is_last_step(step, max_steps)
            if step == 1 and is_overflow(
                prompt_tokens=last_step_prompt,
                completion_tokens=last_step_completion,
                total_tokens=last_step_total,
                limits=limits,
                auto=opts.auto_compact,
            ):
                messages = await self._maybe_compact_history(
                    messages=messages,
                    agent=agent,
                    model=model,
                    mode=mode,
                    opts=opts,
                    limits=limits,
                )
            await self._send({"type": "step_start", "step": step})
            (
                text,
                calls,
                usage,
                messages,
                finish_reason,
            ) = await self._provider_step_with_overflow_retry(
                model=model,
                messages=messages,
                schemas=schemas,
                step=step,
                agent=agent,
                mode=mode,
                opts=opts,
                is_last_step=is_last,
            )
            last_step_prompt = usage.prompt_tokens
            last_step_completion = usage.completion_tokens
            last_step_total = usage.total_tokens
            total_usage = total_usage.merge(usage)
            total_cost = total_usage.cost
            await self._send(
                {
                    "type": "step_finish",
                    "step": step,
                    "tokens": {
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                    },
                    "cost": usage.cost,
                }
            )
            if finish_reason == "length":
                (
                    text,
                    calls,
                    retry_usage,
                    messages,
                    finish_reason,
                ) = await self._length_retry_once(
                    model=model,
                    messages=messages,
                    schemas=schemas,
                    step=step,
                    agent=agent,
                    mode=mode,
                    opts=opts,
                    text=text,
                    calls=calls,
                    usage=usage,
                    is_last_step=is_last,
                )
                if retry_usage is not None:
                    usage = retry_usage
                    last_step_prompt = usage.prompt_tokens
                    last_step_completion = usage.completion_tokens
                    last_step_total = usage.total_tokens
                    total_usage = total_usage.merge(usage)
                    total_cost = total_usage.cost
                    await self._send(
                        {
                            "type": "step_finish",
                            "step": step,
                            "tokens": {
                                "prompt_tokens": usage.prompt_tokens,
                                "completion_tokens": usage.completion_tokens,
                                "total_tokens": usage.total_tokens,
                            },
                            "cost": usage.cost,
                        }
                    )
            if finish_reason == "content_filter":
                messages.append(LLMMessage(role="assistant", content=text))
                return engine_contracts.RunResult(
                    output=f"{text}{CONTENT_FILTER_NOTICE}",
                    steps=step,
                    usage=total_usage,
                    cost=total_cost,
                    finish_reason="content_filter",
                )
            if finish_reason == "length":
                messages.append(LLMMessage(role="assistant", content=text))
                return engine_contracts.RunResult(
                    output=f"{text}{LENGTH_TRUNCATION_NOTICE}",
                    steps=step,
                    usage=total_usage,
                    cost=total_cost,
                    finish_reason="length",
                )
            if is_last:
                if calls:
                    await self._reject_last_step_tools(calls, step)
                return engine_contracts.RunResult(
                    output=text,
                    steps=step,
                    usage=total_usage,
                    cost=total_cost,
                    finish_reason="max_steps",
                )
            if not calls:
                messages.append(LLMMessage(role="assistant", content=text))
                steered = await self._take_steering(opts, authorize_read=authorize_read)
                if steered:
                    # The user spoke while the model was answering: continue
                    # the turn with their message instead of stopping.
                    messages.extend(steered)
                    if is_overflow(
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                        limits=limits,
                        auto=opts.auto_compact,
                    ):
                        messages = await self._maybe_compact_history(
                            messages=messages,
                            agent=agent,
                            model=model,
                            mode=mode,
                            opts=opts,
                            limits=limits,
                        )
                    continue
                if is_overflow(
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    limits=limits,
                    auto=opts.auto_compact,
                ):
                    await self._maybe_compact_history(
                        messages=messages,
                        agent=agent,
                        model=model,
                        mode=mode,
                        opts=opts,
                        limits=limits,
                    )
                return engine_contracts.RunResult(
                    output=text,
                    steps=step,
                    usage=total_usage,
                    cost=total_cost,
                    finish_reason="stop",
                )
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=text or None,
                    tool_calls=[
                        {
                            "id": call.call_id,
                            "name": call.name,
                            "arguments": call.raw_arguments,
                        }
                        for call in calls
                    ],
                )
            )
            outcomes = await self._run_step_tools(
                calls=calls,
                recent_calls=recent_calls,
                ctx=ctx,
                agent=agent,
                mode=mode,
                step=step,
                depth=depth,
                max_depth=max_depth,
                opts=opts,
            )
            for outcome in outcomes:
                messages.append(outcome.message)
            if is_overflow(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                limits=limits,
                auto=opts.auto_compact,
            ):
                messages = await self._maybe_compact_history(
                    messages=messages,
                    agent=agent,
                    model=model,
                    mode=mode,
                    opts=opts,
                    limits=limits,
                )
            # Tool results must stay adjacent to their calls, so a steer joins
            # the conversation only after every outcome (and any compaction).
            messages.extend(
                await self._take_steering(opts, authorize_read=authorize_read)
            )

    async def _take_steering(
        self,
        opts: engine_contracts.RunOptions,
        *,
        authorize_read: AuthorizeRead | None = None,
    ) -> list[LLMMessage]:
        """Deliver queued steers and return them as user messages."""
        if opts.steering is None:
            return []
        entries = await opts.steering.take()
        steered: list[LLMMessage] = []
        for entry in entries:
            content: Any = entry.content
            if self.accessor is not None:
                hydrated = await hydrate_image_messages(
                    [LLMMessage(role="user", content=content)],
                    self.accessor,
                    authorize_read=authorize_read or opts.authorize_file_read,
                )
                content = hydrated[0].content
            steered.append(LLMMessage(role="user", content=content))
        return steered

    def _last_step_request(
        self,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        is_last: bool,
    ) -> tuple[list[LLMMessage], list[ToolSchema], ChatOptions]:
        """Build provider inputs for a normal or last-step turn."""
        if not is_last:
            return messages, schemas, self.chat_options
        return (
            [*messages, LLMMessage(role="assistant", content=MAX_STEPS_PROMPT)],
            [],
            replace(self.chat_options, tool_choice="none"),
        )

    async def _reject_last_step_tools(
        self, calls: list[_PendingToolCall], step: int
    ) -> None:
        """Fail leaked last-step tool calls without executing them."""
        for call in calls:
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "queued_id": call.queued_id,
                    "tool": call.name,
                    "error": MAX_STEPS_TOOL_ERROR,
                }
            )

    async def _provider_step_with_overflow_retry(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        step: int,
        agent: AgentDefinition,
        mode: str,
        opts: engine_contracts.RunOptions,
        is_last_step: bool = False,
    ) -> tuple[str, list[_PendingToolCall], Usage, list[LLMMessage], str]:
        """Run one provider step, compacting and retrying once on overflow errors."""
        request_messages, request_schemas, request_opts = self._last_step_request(
            messages, schemas, is_last_step
        )
        try:
            text, calls, usage, finish = await self._provider_step_with_transient_retry(
                model=model,
                messages=request_messages,
                schemas=request_schemas,
                step=step,
                chat_options=request_opts,
            )
            return text, calls, usage, messages, finish
        except Exception as exc:
            if not is_context_overflow_error(exc):
                raise
            compacted = await self._maybe_compact_history(
                messages=messages,
                agent=agent,
                model=model,
                mode=mode,
                opts=opts,
                limits=ModelLimits(
                    context_length=opts.context_length,
                    max_output_tokens=opts.max_output_tokens,
                ),
                overflow=True,
            )
            retry_messages, retry_schemas, retry_opts = self._last_step_request(
                compacted, schemas, is_last_step
            )
            try:
                (
                    text,
                    calls,
                    usage,
                    finish,
                ) = await self._provider_step_with_transient_retry(
                    model=model,
                    messages=retry_messages,
                    schemas=retry_schemas,
                    step=step,
                    chat_options=retry_opts,
                )
            except Exception as retry_exc:
                if is_context_overflow_error(retry_exc):
                    raise RuntimeError(CONTEXT_OVERFLOW_COMPACTION_ERROR) from retry_exc
                raise
            return text, calls, usage, compacted, finish

    async def _length_retry_once(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        step: int,
        agent: AgentDefinition,
        mode: str,
        opts: engine_contracts.RunOptions,
        text: str,
        calls: list[_PendingToolCall],
        usage: Usage,
        is_last_step: bool = False,
    ) -> tuple[str, list[_PendingToolCall], Usage | None, list[LLMMessage], str]:
        """Retry one length-stopped step after compaction (bounded to 1).

        Returns the retried ``(text, calls, usage, messages, finish)``;
        ``usage`` is None when compaction had nothing to summarize and the
        original length stop stands.
        """
        compacted = await self._maybe_compact_history(
            messages=messages,
            agent=agent,
            model=model,
            mode=mode,
            opts=opts,
            limits=ModelLimits(
                context_length=opts.context_length,
                max_output_tokens=opts.max_output_tokens,
            ),
        )
        if compacted is messages:
            return text, calls, None, messages, "length"
        request_messages, request_schemas, request_opts = self._last_step_request(
            compacted, schemas, is_last_step
        )
        retried = await self._provider_step_with_transient_retry(
            model=model,
            messages=request_messages,
            schemas=request_schemas,
            step=step,
            chat_options=request_opts,
        )
        retried_text, retried_calls, retried_usage, retried_finish = retried
        return retried_text, retried_calls, retried_usage, compacted, retried_finish

    async def _provider_step_with_transient_retry(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        step: int,
        chat_options: ChatOptions | None = None,
    ) -> tuple[str, list[_PendingToolCall], Usage, str]:
        """Run one provider step, retrying timeouts, transport drops, and 5xx."""
        attempt = 0
        while True:
            try:
                return await self._provider_step(
                    model=model,
                    messages=messages,
                    schemas=schemas,
                    step=step,
                    chat_options=chat_options,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not is_retryable_provider_error(exc) or attempt >= RETRY_MAX_RETRIES:
                    raise
                attempt += 1
                delay = retry_delay(attempt, error=exc)
                log.warning(
                    "provider_step_retry",
                    attempt=attempt,
                    delay_seconds=delay,
                    error=str(exc),
                )
                await self._send(
                    {
                        "type": "retry_scheduled",
                        "step": step,
                        "attempt": attempt,
                        "delay_seconds": delay,
                        "error": str(exc),
                    }
                )
                await asyncio.sleep(delay)
                await self._send(
                    {"type": "retry_attempt", "step": step, "attempt": attempt}
                )

    async def _provider_step(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        step: int,
        chat_options: ChatOptions | None = None,
    ) -> tuple[str, list[_PendingToolCall], Usage, str]:
        """Stream one provider step and accumulate text/tool calls/usage.

        Tool fragments are grouped by stream key (OpenCode ``finishAll``
        equivalent: every slot with a name is finalized at step end).
        A usable ``index`` (neither missing, ``None`` nor ``""``) is the
        stream key verbatim — numeric Chat/Bedrock indexes stay numeric,
        Responses ``item_id`` strings stay strings (never cast to int).
        Fragments without a usable index are grouped by their call ``id``
        so arguments of two different call IDs are never concatenated
        into one string.

        Ordered ``tool_queued`` contract: on
        the *first fragment* for each slot a ``tool_queued``
        ``{type, step, call_id, tool, title, arguments}`` event is sent
        immediately at the observed provider position — never delayed to
        step end — so the timeline creates the pending tool row before
        any later text delta. A single provider :class:`Delta` may carry
        text/reasoning/tool together; deltas are processed in stream
        order and fragments within one delta in wire order, which is the
        only representable provider order (the :class:`Delta` shape does
        not encode intra-delta text-vs-tool precedence).

        ``call_id`` stability: the queued id is the real provider id
        when the first fragment carries one, otherwise a unique
        provisional ``call-{step}-{n}`` (or ``call-{step}-{index}`` for
        indexed slots without an id). When the real id arrives in a
        later fragment for the same slot the slot is rebound to it; the
        queued provisional id is carried as ``queued_id`` so persistence
        can rebind that exact row, and the final call uses the real id.

        Final call order preserves provider positions: numeric stream
        keys (explicit ordered blocks) sort numerically; all other keys
        (Responses ``item_id``, synthetic ``call-{step}-{n}`` and
        ``{index}#{n}`` split keys) keep first-seen observed order
        instead of lexicographic string sorting.
        """
        fragments: dict[Any, dict[str, Any]] = {}
        id_to_key: dict[str, Any] = {}
        split_to_key: dict[tuple[str, str], Any] = {}
        slot_order: dict[Any, int] = {}
        order_counter = 0
        next_slot = 0
        current_key: Any = None
        text_parts: list[str] = []
        usage = Usage()
        finish_reason = ""
        resolved = self._resolve_model(model)

        def _provisional_call_id(key: Any, slot_id: str) -> str:
            """Return the stable queued id for a newly opened slot."""
            if slot_id:
                return slot_id
            if isinstance(key, str):
                return key
            return f"call-{step}-{key}"

        async def _emit_queued(key: Any) -> None:
            slot = fragments[key]
            queued_id = _provisional_call_id(key, str(slot.get("id", "") or ""))
            slot["queued_id"] = queued_id
            name = str(slot.get("name", "") or "")
            raw = str(slot.get("arguments", "") or "")
            try:
                title = self._tool_title(name, _parse_arguments(raw))
            except Exception:  # pragma: no cover - title never breaks loop
                title = name
            await self._send(
                {
                    "type": "tool_queued",
                    "step": step,
                    "call_id": queued_id,
                    "tool": name,
                    "title": title,
                    "arguments": raw,
                }
            )

        async for delta in resolved.adapter.chat_stream(
            resolved.model_id,
            limit_image_messages(messages),
            schemas,
            chat_options or self.chat_options,
        ):
            if delta.usage is not None:
                usage = usage.merge(delta.usage)
            if delta.finish_reason:
                finish_reason = str(delta.finish_reason)
            if delta.text:
                text_parts.append(delta.text)
                await self._send(
                    {
                        "type": "part_updated",
                        "step": step,
                        "delta": {"text": delta.text},
                    }
                )
            if delta.reasoning:
                await self._send(
                    {
                        "type": "part_updated",
                        "step": step,
                        "delta": {"reasoning": delta.reasoning},
                    }
                )
            for fragment in delta.tool_calls or ():
                if not isinstance(fragment, dict):
                    continue
                # Usable stream key: an explicit ``index`` (numeric Chat /
                # Bedrock index or Responses ``item_id`` string) used
                # verbatim — never cast to int. Missing key, ``None`` and
                # ``""`` all mean "no stream key" (Bedrock emits no index).
                # ``0``/``False`` are usable keys (never treated as
                # missing): bools stay verbatim keys like any other index.
                raw_index = fragment.get("index")
                has_usable_index = raw_index is not None and (
                    not isinstance(raw_index, str) or raw_index != ""
                )
                fragment_id = fragment.get("id") or ""
                fragment_name = fragment.get("name") or ""
                if not has_usable_index:
                    if fragment_id:
                        # Group by call id: same id continues its slot, a
                        # new id opens a new slot — never concatenate
                        # arguments of two different call IDs.
                        existing = id_to_key.get(fragment_id)
                        if existing is not None and existing in fragments:
                            key = existing
                        else:
                            key = f"call-{step}-{next_slot}"
                            next_slot += 1
                            id_to_key[fragment_id] = key
                    elif (
                        fragment_name
                        and current_key is not None
                        and current_key in fragments
                        and str(fragments[current_key].get("id", "") or "")
                    ):
                        # After a fully-identified slot, an id-less fragment
                        # cannot be a continuation — open a fresh slot.
                        key = f"call-{step}-{next_slot}"
                        next_slot += 1
                    elif (
                        fragment_name
                        and current_key is not None
                        and current_key in fragments
                        and str(fragments[current_key].get("name", "") or "")
                        and str(fragments[current_key].get("name", "") or "")
                        != fragment_name
                    ):
                        # Id-less fragment starting a *different* tool name:
                        # new slot instead of gluing two tools into slot 0.
                        # (Same-name/args-only fragments continue the slot
                        # so fragmented arguments stay concatenated.)
                        key = f"call-{step}-{next_slot}"
                        next_slot += 1
                    elif current_key is not None and current_key in fragments:
                        # Fragmented-arguments continuation of the active
                        # id-less slot.
                        key = current_key
                    else:
                        key = f"call-{step}-{next_slot}"
                        next_slot += 1
                else:
                    key = raw_index
                    if fragment_id:
                        known = id_to_key.get(fragment_id)
                        if known is not None and known in fragments and known != key:
                            # Same call id already accumulating under a
                            # different key: keep one slot per call id.
                            key = known
                        else:
                            # Same stream index reused for a new call id
                            # (provider restarted the slot): never concat
                            # arguments of two different call IDs — split
                            # into a side slot.
                            slot_for_key = fragments.get(key)
                            slot_id = (
                                str(slot_for_key.get("id", "") or "")
                                if slot_for_key
                                else ""
                            )
                            if (
                                slot_for_key is not None
                                and slot_id
                                and slot_id != fragment_id
                            ):
                                split_pair = (str(raw_index), fragment_id)
                                split_key = split_to_key.get(split_pair)
                                if split_key is None or split_key not in fragments:
                                    split_key = f"{raw_index}#{next_slot}"
                                    next_slot += 1
                                    split_to_key[split_pair] = split_key
                                    id_to_key[fragment_id] = split_key
                                key = split_key
                            else:
                                id_to_key[fragment_id] = key
                if key not in fragments:
                    fragments[key] = {"id": "", "name": "", "arguments": ""}
                    slot_order[key] = order_counter
                    order_counter += 1
                slot = fragments[key]
                if fragment_id and not slot.get("id"):
                    slot["id"] = fragment_id
                if fragment_name and not slot.get("name"):
                    slot["name"] = fragment_name
                args = fragment.get("arguments", "")
                if isinstance(args, dict):
                    args = json.dumps(args)
                if args:
                    slot["arguments"] += str(args)
                # tool_queued on *first fragment* for each slot: the slot
                # just opened (or already existed but has never been
                # queued, e.g. created before its name arrived). Uses the
                # provisional id when no real id is known yet; the service
                # rebinds when the real id arrives later. A
                # nameless slot cannot emit yet (no tool known).
                if slot.get("name") and not slot.get("queued_id"):
                    await _emit_queued(key)
                current_key = key
        calls: list[_PendingToolCall] = []
        # Preserve provider positions: numeric stream keys (explicit
        # ordered blocks) sort numerically; every other key keeps
        # first-seen observed order (never lexicographic string order,
        # so "item-10" stays after "item-2" when observed that way).
        numeric_keys = sorted(
            (
                key
                for key in fragments
                if isinstance(key, int) and not isinstance(key, bool)
            ),
        )
        numeric_set = set(numeric_keys)
        observed_keys = [
            key
            for key, _ in sorted(slot_order.items(), key=lambda item: item[1])
            if key not in numeric_set
        ]
        for key in [*numeric_keys, *observed_keys]:
            slot = fragments[key]
            name = str(slot.get("name", "") or "")
            if not name:
                continue
            raw = str(slot.get("arguments", "") or "")
            slot_id = str(slot.get("id", "") or "")
            if slot_id:
                call_id = slot_id
            else:
                call_id = str(slot.get("queued_id", "") or "")
                if not call_id:
                    call_id = _provisional_call_id(key, "")
            calls.append(
                _PendingToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=_parse_arguments(raw),
                    raw_arguments=raw,
                    queued_id=str(slot.get("queued_id", "") or ""),
                )
            )
        return "".join(text_parts), calls, usage, finish_reason

    async def _maybe_compact_history(
        self,
        *,
        messages: list[LLMMessage],
        agent: AgentDefinition,
        model: str,
        mode: str,
        opts: engine_contracts.RunOptions,
        limits: ModelLimits,
        overflow: bool = False,
    ) -> list[LLMMessage]:
        """Summarize history via the hidden compaction agent."""
        if agent.name == "compaction":
            return messages
        selection = select(messages, limits)
        if not selection.head:
            return messages
        previous_summary = find_previous_summary(messages)
        prompt = build_compaction_prompt(
            selection.head,
            previous_summary=previous_summary,
        )
        if not prompt.strip():
            return messages
        try:
            child = HarnessRunner(
                model_resolver=self._resolve_model,
                tools=self.tools,
                evaluator=self.evaluator,
                accessor=self.accessor,
                emit=None,
                chat_options=self.chat_options,
            )
            result = await child.run(
                prompt,
                "compaction",
                model,
                mode,
                engine_contracts.RunOptions(
                    auto_approve=True,
                    history=[],
                    auto_compact=False,
                    session_id=opts.session_id,
                    workspace_id=opts.workspace_id,
                    organization_id=opts.organization_id,
                ),
            )
            summary = (result.output or "").strip()
            if not summary:
                return messages
            compacted, tail_start_id = apply_compaction(messages, summary, limits)
            if overflow:
                compacted = apply_overflow_replay(messages, compacted)
            else:
                compacted = ensure_current_user(messages, compacted)
            await self._send(
                {
                    "type": "compaction",
                    "summary": summary[:32_768],
                    "auto": True,
                    "overflow": overflow,
                    "tail_start_id": tail_start_id,
                }
            )
            return compacted
        except Exception as exc:  # pragma: no cover - compaction must not abort
            log.warning("compaction_failed", error=str(exc))
            return messages


class _MissingAccessor(WorkspaceAccessor):
    """Fallback accessor that fails loudly when no accessor is wired."""

    async def exec_wait(
        self, command, workdir=HARNESS_WORKSPACE_ROOT, env=None, timeout=None
    ):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def read_file(self, path, max_size=None):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def write_file(self, path, content, mode=0o644):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def list_dir(self, path):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def desktop_action(self, action, args=None, timeout=None):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_start(  # type: ignore[no-untyped-def]
        self,
        command,
        workdir="/workspace",
        env=None,
        name="",
        *,
        session_id=None,
        kind="persistent",
    ):
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_list(self, *, session_id=None):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_get(self, process_id, *, session_id=None):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_stop(self, process_id, *, session_id=None):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_restart(self, process_id, *, session_id=None):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_delete(self, process_id):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def open_process(  # type: ignore[no-untyped-def]
        self, command, workdir="/workspace", env=None, timeout=None, *, owner=None
    ):
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def open_tcp(self, host, port, tls=False, server_hostname=None, timeout=None):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")
