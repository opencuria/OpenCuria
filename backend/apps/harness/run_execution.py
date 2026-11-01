"""Native execution with explicit runtime and persistence dependencies."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
import structlog
from asgiref.sync import sync_to_async

from apps.runners.services.workspace_plugin_runtime import WorkspacePluginRuntimeService
from apps.skills.services import SkillService
from common.exceptions import NotFoundError

from .engines.base import RunOptions
from .interaction_service import InteractionService
from .models import (
    HarnessMessage,
    HarnessSession,
)
from .provider_turns import ProviderTurnService
from .providers.base import ChatOptions, LLMMessage
from .providers.models_catalog import normalize_reasoning_effort
from .repositories import HarnessMessageRepository, HarnessSessionRepository
from .run_state import RunRegistry
from .run_timeline import (
    RunTimeline,
    tail_remainder,
)

log = structlog.get_logger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


FRONTEND_EVENT_STATUS = "harness.session_status"
FRONTEND_EVENT_STEER_QUEUE = "harness.steer_queue"


@dataclass(frozen=True)
class RunExecutionPorts:
    """Explicit collaborators for execution and finalization."""

    run_registry: RunRegistry
    accessor_factory: Callable[..., Any] | None
    cleanup_session_processes: Callable[..., Any]
    engine_registry_for_run: Callable[..., Any]
    execute_external_run: Callable[..., Any]
    finalize_run: Callable[..., Any]
    finish_mcp_recordings: Callable[..., Any]
    last_assistant_step_tokens: Callable[..., Any]
    on_runner_event: Callable[..., Any]
    provider_factory: Callable[..., Any] | None
    provider_turn_revocation_message: Callable[..., Any]
    provider_turn_service: ProviderTurnService
    record_run_interruption: Callable[..., Any]
    resolve_run_model_limits: Callable[..., Any]
    run_subagent_tool: Callable[..., Any]
    runner_factory: Callable[..., Any] | None
    user_abort_requested: Callable[..., Any]
    interactions: InteractionService
    messages: type[HarnessMessageRepository]
    plugin_runtime: WorkspacePluginRuntimeService
    sessions: type[HarnessSessionRepository]
    timeline: RunTimeline
    agent_configs: Callable[..., Any]


class RunExecution:
    """Execute a turn using injected runtime, interaction and persistence ports."""

    def __init__(self, ports: RunExecutionPorts) -> None:
        self.ports = ports

    async def execute(
        self,
        *,
        session: HarnessSession,
        prompt: str,
        history: list[LLMMessage],
        assistant: HarnessMessage,
        organization_id: uuid.UUID,
        depth: int,
        max_depth: int,
    ) -> None:
        """Run the loop, persist events, and finalize the assistant message."""
        from .providers.resolver import StaticModelResolver
        from .services import ProviderConfigService
        from .tools import default_tool_registry

        key = str(session.id)
        run_context = self.ports.run_registry.contexts.get(key, {})
        self.ports.run_registry.ensure(key).execution_started = True
        engine_id = (getattr(session, "harness_id", "native") or "native").strip()
        mcp_runtime = None
        phase = "startup"
        if engine_id != "native":
            await self.ports.execute_external_run(
                engine_id=engine_id,
                session=session,
                prompt=prompt,
                history=history,
                assistant=assistant,
                organization_id=organization_id,
                depth=depth,
                max_depth=max_depth,
            )
            return
        try:
            # Fail closed on missing plugin credentials before constructing provider
            # configuration or registering/calling any harness tools.
            mcp_snapshot = None
            agent_key = (session.agent_name or "").strip().lower()
            if agent_key not in {"title", "summary", "compaction"}:
                mcp_snapshot = await sync_to_async(self.ports.plugin_runtime.prepare)(
                    session.workspace_id, organization_id
                )
            model_resolver = None
            small_model = ""
            agent_configs: dict[str, dict[str, Any]] = {}
            if self.ports.provider_factory is not None:
                model_resolver = StaticModelResolver(
                    self.ports.provider_factory(organization_id)
                ).resolve
            else:
                config_service = ProviderConfigService()
                config = None
                try:
                    config = await sync_to_async(config_service.get_config)(
                        organization_id
                    )
                except NotFoundError:
                    config = None
                run_context = self.ports.run_registry.contexts.get(key, {})
                turn_id = run_context.get("provider_turn_id")

                def register_used(account_id: str) -> None:
                    if turn_id:
                        self.ports.provider_turn_service.register_use(
                            turn_id, account_id
                        )

                resolver = await sync_to_async(config_service.build_resolver)(
                    organization_id,
                    user_id=run_context.get("initiated_by_id"),
                    account_ids=dict(run_context.get("account_ids") or {}),
                    on_account_used=register_used if turn_id else None,
                )
                await sync_to_async(resolver.prepare_adapters)()
                model_resolver = resolver.resolve
                small_model = ((config.small_model if config else "") or "").strip()
                agent_configs = await sync_to_async(self.ports.agent_configs)(
                    organization_id
                )
                agent_name = (
                    (session.agent_name or session.mode or "build").strip().lower()
                )
                row = agent_configs.get(agent_name, {})
                agent_model = (
                    ""
                    if row.get("inherit_model")
                    else str(row.get("model") or "").strip()
                )
                agent_effort = (
                    ""
                    if row.get("inherit_model")
                    else normalize_reasoning_effort(str(row.get("effort") or ""))
                )
                model_default = session.model or agent_model
                if not model_default:
                    raise ValueError("No model configured for harness run")
                session.model = model_default
                run_effort = normalize_reasoning_effort(agent_effort)
                if not (session.reasoning_effort or "").strip() and run_effort:
                    session.reasoning_effort = run_effort
            model = session.model or "default"
            context_length = 0
            model_max_output_tokens = 0
            last_step_prompt_tokens = 0
            last_step_completion_tokens = 0
            last_step_total_tokens = 0
            context_length, model_max_output_tokens = await sync_to_async(
                self.ports.resolve_run_model_limits
            )(
                organization_id,
                model,
                run_context.get("initiated_by_id"),
                dict(run_context.get("account_ids") or {}),
            )
            (
                last_step_prompt_tokens,
                last_step_completion_tokens,
                last_step_total_tokens,
            ) = await sync_to_async(self.ports.last_assistant_step_tokens)(
                session.id, assistant.id
            )
            accessor = None
            if self.ports.accessor_factory is not None:
                accessor = await self.ports.accessor_factory(str(session.workspace_id))
            tools = default_tool_registry()
            from .services import ComputerUseConfigService

            computer_options = await sync_to_async(
                ComputerUseConfigService().get_or_default
            )(organization_id)
            # MCP plugin runtime: snapshot + connections are prepared BEFORE
            # the runner is constructed so any injected runner_factory sees
            # the same fully-registered ToolRegistry (an injected factory may
            # snapshot/copy instead of holding the live reference). Prepare
            # runs in the sync ORM context exactly once (snapshot +
            # credential resolution, no second ORM/decrypt round in setup).
            mcp_enabled = accessor is not None and (
                session.agent_name or ""
            ).strip().lower() not in (
                "title",
                "summary",
                "compaction",
            )
            if mcp_enabled:
                import apps.harness.mcp_client.runtime as mcp_runtime_module

                mcp_runtime = mcp_runtime_module.McpRuntime()
                # Reuse the already validated snapshot; setup does not repeat
                # credential lookup/decryption.
                await mcp_runtime.setup(
                    accessor=accessor,
                    core_tool_names=tools.names(),
                    prepared=mcp_snapshot,
                    owner_id=str(assistant.id),
                    session_id=key,
                    turn_id=str(assistant.id),
                    runtime_options=computer_options,
                )
                mcp_runtime.register_tools(tools)
                accessor = await mcp_runtime.execution_accessor(accessor)
                if agent_key == "computeruse":
                    mcp_runtime.require_computer_use()
                if mcp_runtime.skipped:
                    log.warning(
                        "mcp_discovery_health",
                        session_id=key,
                        skipped=mcp_runtime.skipped,
                    )
            if self.ports.runner_factory is not None:
                loop_runner = self.ports.runner_factory(
                    model_resolver=model_resolver,
                    tools=tools,
                    accessor=accessor,
                    emit=lambda event: self.ports.on_runner_event(
                        session, assistant, event
                    ),
                )
            else:
                effort = (session.reasoning_effort or "").strip() or None
                loop_runner = self.ports.engine_registry_for_run().create(
                    "native",
                    model_resolver=model_resolver,
                    tools=tools,
                    accessor=accessor,
                    emit=lambda event: self.ports.on_runner_event(
                        session, assistant, event
                    ),
                    chat_options=ChatOptions(reasoning_effort=effort),
                )
            opts = RunOptions(
                max_steps=computer_options["max_steps"]
                if agent_key == "computeruse"
                else None,
                depth=depth,
                max_depth=max_depth,
                history=history,
                session_id=key,
                workspace_id=str(session.workspace_id),
                organization_id=str(organization_id),
                small_model=small_model,
                current_user_message_id=str(
                    self.ports.run_registry.contexts.get(key, {}).get(
                        "user_message_id", ""
                    )
                ),
                skills=SkillService.merge_bodies(
                    self.ports.run_registry.contexts.get(key, {}).get(
                        "skill_bodies", []
                    ),
                    mcp_snapshot.snapshot.plugin_skills
                    if mcp_snapshot is not None
                    else [],
                ),
                steering=self.ports.run_registry.contexts.get(key, {}).get("steering"),
                context_length=context_length,
                max_output_tokens=model_max_output_tokens,
                last_step_prompt_tokens=last_step_prompt_tokens,
                last_step_completion_tokens=last_step_completion_tokens,
                last_step_total_tokens=last_step_total_tokens,
                on_permission=lambda **kw: self.ports.interactions.request_permission(
                    session, assistant, **kw
                ),
                on_question=lambda **kw: self.ports.interactions.ask_question(
                    session, assistant, **kw
                ),
                run_subagent=lambda args, ctx, subtask_id: self.ports.run_subagent_tool(
                    parent=session,
                    args=args,
                    ctx=ctx,
                    subtask_id=subtask_id,
                    organization_id=organization_id,
                    agent_configs=dict(agent_configs),
                ),
            )
            phase = "run"
            result = await loop_runner.run(
                prompt, session.agent_name or "build", model, session.mode, opts
            )
            if mcp_runtime is not None:
                from .computer_use.recordings import append_video_to_output

                for path in await mcp_runtime.finish():
                    result.output = append_video_to_output(result.output or "", path)
            if self.ports.user_abort_requested(session):
                raise asyncio.CancelledError()
            # Text deltas were already appended to the assistant message
            # incrementally (see RunTimeline.record); only genuinely new
            # tail content still needs persisting. The runner's final
            # ``output`` is the full assembled answer, so diff it against
            # what is already stored instead of appending blindly (content
            # drifts when a rerun/continuation re-emits a prefix). When a
            # remainder exists it also gets its own text part so message
            # content and parts never drift apart.
            await sync_to_async(self.ports.messages.refresh, thread_sensitive=True)(
                assistant
            )
            existing = assistant.content or ""
            tail = result.output or ""
            remainder = tail_remainder(existing, tail)
            if remainder:
                await sync_to_async(self.ports.messages.append_content)(
                    assistant, remainder
                )
                await self.ports.timeline.append_tail_text(
                    session, assistant, remainder
                )
            # Message usage is accumulated per step_finish so abort still
            # keeps completed steps. Session usage is applied once here.
            await sync_to_async(self.ports.sessions.add_usage)(
                session,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
                cost=result.cost,
            )
            await sync_to_async(self.ports.messages.complete)(
                assistant, finish=result.finish_reason
            )
            await self.ports.timeline.settle_open_stream_parts(assistant)
        except asyncio.CancelledError:
            revocation = await self.ports.provider_turn_revocation_message(session)
            if revocation:
                run_context["provider_revocation_message"] = revocation
                with anyio.CancelScope(shield=True):
                    await sync_to_async(self.ports.messages.complete)(
                        assistant, finish="aborted", error=revocation
                    )
                    await self.ports.timeline.fail_open_parts(
                        assistant, state="error", output=revocation, session=session
                    )
            else:
                await self.ports.record_run_interruption(
                    session, assistant, phase=phase
                )
            raise
        except Exception as exc:
            # SDK cancellation is level-triggered until its scopes are closed.
            revocation = await self.ports.provider_turn_revocation_message(session)
            public_error = revocation or str(exc)
            with anyio.CancelScope(shield=True):
                await sync_to_async(self.ports.messages.complete)(
                    assistant,
                    finish="aborted" if revocation else "error",
                    error=public_error,
                )
                await self.ports.timeline.fail_open_parts(
                    assistant, state="error", output=public_error, session=session
                )
            if not revocation:
                log.exception("harness_run_failed", session_id=key)
        finally:
            self.ports.run_registry.ensure(key).begin_finalization()
            # MCP scopes belong to this task and must close here, in LIFO order.
            # Even a cancellation during teardown must reach status finalization.
            try:
                try:
                    if mcp_runtime is not None:
                        try:
                            try:
                                await self.ports.finish_mcp_recordings(
                                    mcp_runtime, session, assistant
                                )
                            finally:
                                await mcp_runtime.aclose()
                        except asyncio.CancelledError:
                            current = asyncio.current_task()
                            if current is not None and current.cancelling():
                                raise
                            log.warning("mcp_runtime_close_interrupted", session_id=key)
                        except Exception:  # pragma: no cover - best effort
                            log.exception("mcp_runtime_close_failed", session_id=key)
                finally:
                    await self.ports.cleanup_session_processes(
                        workspace_id=str(session.workspace_id),
                        session_id=key,
                        reason="run_finished",
                    )
            finally:
                await self.ports.finalize_run(session, assistant)
