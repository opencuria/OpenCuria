"""Subagent admission, delegation and result coordination."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
import structlog
from asgiref.sync import sync_to_async

from common.exceptions import AuthenticationError, NotFoundError

from .models import (
    HarnessMessage,
    HarnessSession,
)
from .providers.models_catalog import normalize_reasoning_effort
from .repositories import HarnessMessageRepository, HarnessSessionRepository
from .run_state import RunRegistry
from .session_settings_service import SessionSettingsService
from .tools.subagents import TaskArgs, validate_subagent_delegation

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SubagentCoordinatorPorts:
    """Explicit domain collaborators; no facade or sibling private state."""

    agent_configs_map: Callable[..., Any]
    catalog_efforts_for_model: Callable[..., Any]
    on_runner_event: Callable[..., Any]
    parent_stop_in_progress: Callable[..., Any]
    provider_turn_context: Callable[..., Any]
    create_session: Callable[..., Any]
    is_running: Callable[..., Any]
    messages: type[HarnessMessageRepository]
    run_registry: RunRegistry
    session_settings: SessionSettingsService
    sessions: type[HarnessSessionRepository]
    start_run: Callable[..., Any]


class SubagentCoordinator:
    """SubagentCoordinator domain service."""

    def __init__(self, ports: SubagentCoordinatorPorts) -> None:
        self.ports = ports

    async def _run_subagent_tool(
        self,
        *,
        parent: HarnessSession,
        args: TaskArgs,
        ctx: Any,
        subtask_id: str,
        organization_id: uuid.UUID,
        agent_configs: dict[str, dict[str, Any]] | None = None,
    ) -> Any:
        """Create a child session, run it to completion, return tool output.

        Model/effort routing comes from AgentConfig: custom mode uses the
        fixed config model/effort; inherit mode reuses the parent run model
        (``args.model_override`` always wins). Effort strategies
        lowest/medium/highest resolve against the catalog efforts of the
        inherited model; ``inherit`` reuses the parent reasoning effort.

        ``args.task_id`` resumes a previous child of the same parent
        (OpenCode parity): the follow-up prompt runs on the same child
        session with its persisted history instead of creating a fresh
        one. Unknown/missing ``task_id`` values fall back to a fresh
        child; foreign or busy children are rejected with ``ToolError``.
        A ``subagent_type`` mismatch on resume is ignored — the stored
        child agent keeps running.
        """
        from .tools.base import ToolError
        from .tools.subagents import TASK_OUTPUT_MAX_CHARS, build_task_result

        if ctx.depth >= ctx.max_depth:
            raise ToolError(
                f"Subagent depth limit reached (depth={ctx.depth}, "
                f"max_depth={ctx.max_depth}); nested task calls are not allowed.",
                tool="task",
            )
        agent = (args.agent or args.subagent_type or "general").strip().lower()
        configs = dict(agent_configs or {}) or self.ports.agent_configs_map(
            organization_id
        )
        row = configs.get(agent, {})
        inherit = bool(row.get("inherit_model", True))
        fixed_model = str(row.get("model") or "").strip()
        codex_parent = getattr(parent, "harness_id", "native") == "codex"
        effort_normalizer = (
            (lambda value: (value or "").strip().lower())
            if codex_parent
            else normalize_reasoning_effort
        )
        fixed_effort = effort_normalizer(str(row.get("effort") or ""))
        strategy = str(row.get("effort_strategy") or "inherit").strip() or "inherit"
        override = (args.model_override or "").strip()
        if inherit:
            model = (
                override or (getattr(ctx, "model", "") or "") or parent.model or ""
            ).strip()
        else:
            model = (
                override
                or fixed_model
                or (getattr(ctx, "model", "") or "")
                or parent.model
                or ""
            ).strip()
        if not model:
            raise ToolError(
                "No model available for subagent run",
                tool="task",
            )
        if codex_parent:
            try:
                model, _ = await sync_to_async(
                    self.ports.session_settings.validate_codex_config
                )(
                    parent,
                    model,
                    "",
                    account_ids=self.ports.provider_turn_context(parent).get(
                        "account_ids"
                    ),
                )
            except (ValueError, AuthenticationError, NotFoundError) as exc:
                raise ToolError(str(exc), tool="task") from exc
        parent_ctx = self.ports.run_registry.contexts.get(str(parent.id), {})
        message_id = parent_ctx.get("message_id")
        if not message_id:
            raise ToolError(
                "Parent assistant message missing for subagent run",
                tool="task",
            )
        parent_assistant = await sync_to_async(self.ports.messages.get_required)(
            message_id
        )
        child = await self._resolve_resume_child(parent, args.task_id)
        resumed = child is not None
        if resumed:
            agent = (
                child.agent_name or agent or "general"
            ).strip().lower() or "general"
        validate_subagent_delegation(parent.agent_name, agent)
        if inherit:
            if strategy in ("lowest", "medium", "highest"):
                if codex_parent:
                    from .engines.service import EngineCatalogService

                    models = await sync_to_async(EngineCatalogService().list_models)(
                        "codex",
                        organization_id,
                        parent.created_by_id,
                        account_ids=self.ports.provider_turn_context(parent).get(
                            "account_ids"
                        ),
                    )
                    catalog_efforts = next(
                        (list(m.reasoning_efforts) for m in models if m.id == model), []
                    )
                else:
                    catalog_efforts = self.ports.catalog_efforts_for_model(
                        organization_id,
                        model,
                        self.ports.provider_turn_context(parent).get("initiated_by_id"),
                        dict(
                            self.ports.provider_turn_context(parent).get("account_ids")
                            or {}
                        ),
                    )
                from .agents.effort_strategy import resolve_strategy_effort

                child_effort = resolve_strategy_effort(catalog_efforts, strategy)
            else:
                child_effort = effort_normalizer(parent.reasoning_effort or "")
        else:
            child_effort = fixed_effort
        if resumed:
            # Resume keeps the stored child routing: model_override still
            # wins, otherwise the existing child model/effort continue.
            assert child is not None
            if override:
                if codex_parent:
                    await sync_to_async(
                        self.ports.session_settings.validate_codex_config
                    )(
                        child,
                        override,
                        child.reasoning_effort,
                        account_ids=self.ports.provider_turn_context(parent).get(
                            "account_ids"
                        ),
                    )
                child = await sync_to_async(self.ports.sessions.set_model)(
                    child, override
                )
                model = override
            else:
                model = (child.model or model or "").strip()
                if not model:
                    raise ToolError(
                        "No model available for subagent run",
                        tool="task",
                    )
            child_effort = effort_normalizer(child.reasoning_effort or "")
        else:
            child = await sync_to_async(self.ports.create_session)(
                workspace_id=parent.workspace_id,
                organization_id=parent.organization_id,
                prompt=args.prompt,
                agent_name=agent,
                mode=parent.mode,
                model=model,
                reasoning_effort=child_effort,
                title=args.description[:255],
                parent_id=parent.id,
                user_id=getattr(parent, "created_by_id", None),
                skill_ids=list(parent.skill_ids or []),
            )
        await self.ports.on_runner_event(
            parent,
            parent_assistant,
            {
                "type": "subtask_started",
                "subtask_id": subtask_id,
                "child_session_id": str(child.id),
                "agent": agent,
                "description": args.description,
                "model": child.model or "",
                "reasoning_effort": child.reasoning_effort or "",
            },
        )
        status = "completed"
        assistant: HarnessMessage | None = None
        child_task: asyncio.Task[None] | None = None
        try:
            assistant = await self.ports.start_run(
                child,
                args.prompt,
                organization_id=organization_id,
                workspace_id=str(parent.workspace_id),
                max_depth=ctx.max_depth,
                user_id=getattr(parent, "created_by_id", None),
            )
            child_task = self.ports.run_registry.tasks.get(str(child.id))
            if child_task is not None:
                # Cancel execution explicitly below, not an already-settling owner.
                await asyncio.shield(child_task)
            # A stopped parent must not treat the child outcome as a tool
            # error the model can recover from by launching another subagent.
            if self.ports.parent_stop_in_progress(parent):
                raise asyncio.CancelledError()
            assistant = await sync_to_async(self.ports.messages.get_required)(
                assistant.id
            )
            output = assistant.content or ""
            if assistant.finish in ("error", "aborted"):
                error = assistant.error or "Subagent run interrupted"
                if output:
                    error += "\n\nPartial output:\n" + output[:TASK_OUTPUT_MAX_CHARS]
                raise ToolError(error, tool="task")
        except asyncio.CancelledError:
            tracked = child_task or self.ports.run_registry.tasks.get(str(child.id))
            if tracked is not None and not tracked.done():
                if not tracked.cancelling() and not self.ports.run_registry.finalizing(
                    str(child.id)
                ):
                    tracked.cancel()
                try:
                    await asyncio.shield(tracked)
                except (asyncio.CancelledError, Exception):
                    pass
            # A task cancelled before entry installs a replacement finalizer.
            # Await that owner without cancelling it before reading its outcome.
            finalizer = self.ports.run_registry.tasks.get(str(child.id))
            if finalizer is not None and finalizer is not tracked:
                with anyio.CancelScope(shield=True):
                    while not finalizer.done():
                        try:
                            await asyncio.shield(finalizer)
                        except asyncio.CancelledError:
                            # A parent stop can arrive while this child is already
                            # settling. Finish the join before propagating it below.
                            continue
            # Awaiting a cancelled child does not cancel this tool task. Only
            # cancellation of the waiting task/parent should stop the whole run.
            current = asyncio.current_task()
            parent_task = self.ports.run_registry.tasks.get(str(parent.id))
            if (
                (current is not None and current.cancelling())
                or str(parent.id) in self.ports.run_registry.aborts
                or (parent_task is not None and parent_task.cancelling())
            ):
                raise
            error = "Subagent run aborted"
            if assistant is not None:
                await sync_to_async(self.ports.messages.refresh, thread_sensitive=True)(
                    assistant
                )
                error = assistant.error or error
                if assistant.content:
                    error += (
                        "\n\nPartial output:\n"
                        + assistant.content[:TASK_OUTPUT_MAX_CHARS]
                    )
            log.warning(
                "harness_subagent_interrupted",
                session_id=str(parent.id),
                child_session_id=str(child.id),
                subtask_id=subtask_id,
                agent=agent,
                phase="startup" if assistant is None else "run",
            )
            await self.ports.on_runner_event(
                parent,
                parent_assistant,
                {
                    "type": "subtask_finished",
                    "subtask_id": subtask_id,
                    "child_session_id": str(child.id),
                    "agent": agent,
                    "status": "aborted",
                    "summary": error[:500],
                },
            )
            raise ToolError(
                f"Subagent '{agent}' failed (task_id: {child.id}): {error}",
                tool="task",
            )
        except Exception as exc:
            await self.ports.on_runner_event(
                parent,
                parent_assistant,
                {
                    "type": "subtask_finished",
                    "subtask_id": subtask_id,
                    "child_session_id": str(child.id),
                    "agent": agent,
                    "status": "error",
                    "summary": str(exc)[:500],
                },
            )
            raise ToolError(
                f"Subagent '{agent}' failed (task_id: {child.id}): {exc}",
                tool="task",
            ) from exc
        await self.ports.on_runner_event(
            parent,
            parent_assistant,
            {
                "type": "subtask_finished",
                "subtask_id": subtask_id,
                "child_session_id": str(child.id),
                "agent": agent,
                "status": status,
                "summary": output[:500],
            },
        )
        return await build_task_result(
            str(child.id),
            output,
            ctx,
            agent=agent,
            metadata={
                "subtask_id": subtask_id,
                "child_session_id": str(child.id),
                "task_id": str(child.id),
                "agent": agent,
                "status": status,
                "resumed": resumed,
            },
        )

    async def _resolve_resume_child(
        self, parent: HarnessSession, task_id: str | None
    ) -> HarnessSession | None:
        """Return the resumable child for *task_id* or ``None`` for fresh.

        A child resumes only when it exists and belongs to the same
        parent, workspace, and organization. Foreign or busy children
        are rejected so one parent can neither hijack nor queue onto
        another parent's subagent (sequential resume only, no
        update-while-running).
        """
        from .tools.base import ToolError

        raw = (task_id or "").strip()
        if not raw:
            return None
        try:
            child_id = uuid.UUID(raw)
        except (ValueError, TypeError, AttributeError):
            return None
        child = await sync_to_async(self.ports.sessions.get_by_id)(child_id)
        if child is None:
            # OpenCode parity: an unknown task_id starts a fresh child.
            return None
        if (
            child.parent_id != parent.id
            or child.workspace_id != parent.workspace_id
            or child.organization_id != parent.organization_id
        ):
            raise ToolError(
                f"task_id '{raw}' does not belong to this session",
                tool="task",
            )
        if self.ports.is_running(child.id):
            raise ToolError(
                f"Subagent '{raw}' is still running; wait for it to finish "
                "before resuming it",
                tool="task",
            )
        return child
