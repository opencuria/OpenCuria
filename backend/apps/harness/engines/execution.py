"""External engine run execution under durable ownership."""

from __future__ import annotations

import asyncio
import math
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from asgiref.sync import sync_to_async

from apps.skills.services import SkillService
from common.exceptions import ConflictError

from ..models import HarnessRunStatus, HarnessSessionStatus
from ..providers.base import LLMMessage
from ..run_timeline import tail_remainder
from .base import RunOptions

if TYPE_CHECKING:
    from ..harness_service import HarnessService
    from ..models import HarnessMessage, HarnessSession

log = structlog.get_logger("apps.harness.harness_service")


def _cli_total_cost_update(result: Any) -> dict[str, float] | None:
    """Return the cumulative CLI cost to persist as the next run's baseline."""
    from .claude.runtime_state import CLI_TOTAL_COST_KEY

    metadata = getattr(result, "metadata", None)
    value = metadata.get(CLI_TOTAL_COST_KEY) if isinstance(metadata, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return {CLI_TOTAL_COST_KEY: float(value)}


async def execute_external_run(
    service: HarnessService,
    *,
    engine_id: str,
    session: HarnessSession,
    prompt: str,
    history: list[LLMMessage],
    assistant: HarnessMessage,
    organization_id: uuid.UUID,
    depth: int,
    max_depth: int,
) -> None:
    """Run an external engine under durable ownership and common persistence."""
    from ..providers.base import Usage
    from .external_runtime import prepare_external_runtime

    label = "Codex" if engine_id == "codex" else "Claude"
    key = str(session.id)
    context = service._runs.get(key, {})
    ownership = context.get("ownership")
    mcp_runtime = None
    accessor = None
    engine = None
    runtime = None
    status = HarnessRunStatus.ERROR
    safe_error = ""
    public_error = ""
    public_recovery_error = ""
    cleanup_confirmed = True
    result = None
    allowed_metadata: dict[str, Any] = {}
    remainder = ""
    assistant_content = ""

    async def ensure_current() -> None:
        if ownership is None:
            raise ConflictError(f"{label} run ownership was not initialized")
        check = (
            ownership.is_owner_for_finalization
            if engine_id == "codex" and context.get("engine_closing")
            else ownership.is_current_owner
        )
        if not await check():
            raise ConflictError(f"{label} run ownership was superseded")

    try:
        if ownership is None:
            raise RuntimeError(f"{label} run ownership was not initialized")
        if not await ownership.start():
            raise ConflictError(f"{label} run ownership was superseded")

        ownership.start_heartbeat(on_lost=lambda: service._cancel_owned_task(key))
        context["cleanup_confirmed"] = True
        await ensure_current()
        if service._accessor_factory is None:
            raise RuntimeError(f"{label} engine requires a workspace accessor")
        accessor = await service._accessor_factory(str(session.workspace_id))
        # The same credential snapshot is fail-closed for native and SDK tools.
        mcp_snapshot = await sync_to_async(service._prepare_mcp_snapshot_for_run)(
            session, organization_id
        )
        tools = service._tools_for_session(key, session.agent_name or session.mode)
        from ..services import ComputerUseConfigService

        computer_options = await sync_to_async(
            ComputerUseConfigService().get_or_default
        )(organization_id)
        if accessor is not None and (session.agent_name or "").lower() not in {
            "title",
            "summary",
            "compaction",
        }:
            import apps.harness.mcp_client.runtime as mcp_runtime_module

            mcp_runtime = mcp_runtime_module.McpRuntime()
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
            if session.agent_name == "computeruse":
                mcp_runtime.require_computer_use()
            if mcp_runtime.skipped:
                log.warning(
                    "mcp_discovery_health",
                    session_id=key,
                    skipped=mcp_runtime.skipped,
                )
        runtime = await prepare_external_runtime(
            service=service,
            engine_id=engine_id,
            session=session,
            organization_id=organization_id,
            context=context,
            ownership=ownership,
            ensure_current=ensure_current,
        )
        run_options = RunOptions(
            max_steps=computer_options["max_steps"]
            if session.agent_name == "computeruse"
            else None,
            depth=depth,
            max_depth=max_depth,
            history=history,
            session_id=key,
            workspace_id=str(session.workspace_id),
            organization_id=str(organization_id),
            current_user_message_id=str(context.get("user_message_id", "")),
            skills=SkillService.merge_bodies(
                context.get("skill_bodies", []),
                mcp_snapshot.snapshot.plugin_skills if mcp_snapshot else [],
            ),
            on_permission=lambda **kw: service.interactions.request_permission(
                session, assistant, **kw
            ),
            on_question=lambda **kw: service.interactions.ask_question(
                session, assistant, **kw
            ),
            steering=context.get("steering"),
            run_subagent=lambda args, ctx, subtask_id: service._run_subagent_tool(
                parent=session,
                args=args,
                ctx=ctx,
                subtask_id=subtask_id,
                organization_id=organization_id,
            ),
        )
        run_options.authorize_file_read = lambda path: (
            service.interactions.authorize_file_read(session, assistant, path)
        )
        run_options.reasoning_effort = session.reasoning_effort or (
            "" if engine_id == "codex" else "high"
        )

        async def on_owner(owner: dict[str, str], owner_phase: str) -> None:
            nonlocal cleanup_confirmed
            if owner_phase == "reserved":
                cleanup_confirmed = False
                context["cleanup_confirmed"] = False
                if not await ownership.bind_runner_lease(
                    str(owner.get("lease_id", "")), str(owner.get("epoch", ""))
                ):
                    raise ConflictError(
                        f"{label} runner lease ownership was superseded"
                    )
                await ensure_current()
            elif owner_phase == "closing":
                if engine_id == "codex":
                    # Checkpoint persistence follows runner-confirmed shutdown.
                    # Preserve this owner's finalization authority, not tool use.
                    await ownership.stop_heartbeat()
                    context["engine_closing"] = True
                cleanup_confirmed = False
                context["cleanup_confirmed"] = False
                if not await ownership.begin_close():
                    raise ConflictError(
                        f"{label} runner lease ownership was superseded"
                    )
                if engine_id == "codex" and (
                    await service._provider_turn_revocation_message(session)
                ):
                    raise ConflictError("Codex checkpoint authorization was revoked")
            elif owner_phase == "released":
                cleanup_confirmed = True
                context["cleanup_confirmed"] = True

        async def on_binding(external_id: str, metadata: dict[str, Any]) -> None:
            from .repositories import HarnessEngineRepository

            if (
                engine_id == "codex"
                and metadata.get("codex_dirty") is False
                and (await service._provider_turn_revocation_message(session))
            ):
                raise ConflictError("Codex checkpoint authorization was revoked")
            bound = await sync_to_async(
                HarnessEngineRepository.bind_external_session_if_owner,
                thread_sensitive=True,
            )(
                ownership.run_id,
                ownership.owner_token,
                session.id,
                external_session_id=external_id,
                engine_state=dict(metadata or {}),
            )
            if not bound:
                raise ConflictError(f"{label} session binding ownership was superseded")
            if external_id:
                session.external_session_id = external_id
            session.engine_state = {**(session.engine_state or {}), **(metadata or {})}

        async def emit(event: dict[str, Any]) -> None:
            await ensure_current()
            await service._on_external_root_event(session, assistant, event)

        async def on_child_event(parent_call_id: str, event: dict[str, Any]) -> None:
            await ensure_current()
            await service._on_external_child_event(key, parent_call_id, event)

        engine = service._engine_registry_for_run().create(
            engine_id,
            tools=tools,
            accessor=accessor,
            emit=emit,
            **runtime.engine_options,
            external_session_id=session.external_session_id or "",
            engine_state=dict(session.engine_state or {}),
            session_store=runtime.session_store,
            on_binding=on_binding,
            on_child_event=on_child_event,
            on_owner=on_owner,
            evaluator=service.permissions.evaluator,
            effort=session.reasoning_effort or ("" if engine_id == "codex" else "high"),
        )
        await ensure_current()
        try:
            result = await engine.run(
                prompt,
                session.agent_name or session.mode or "build",
                session.model or runtime.default_model,
                session.mode or "build",
                run_options,
            )
        except Exception:
            revocation_message = await service._provider_turn_revocation_message(
                session
            )
            if revocation_message:
                context["provider_revocation_message"] = revocation_message
                raise asyncio.CancelledError() from None
            raise
        if mcp_runtime is not None:
            from ..computer_use.recordings import append_video_to_output

            for path in await mcp_runtime.finish():
                result.output = append_video_to_output(result.output or "", path)
        await sync_to_async(service.messages.refresh, thread_sensitive=True)(assistant)
        remainder = tail_remainder(assistant.content or "", result.output or "")
        usage = result.usage or Usage()
        metadata = dict(getattr(result, "metadata", {}) or {})
        # Persist only bounded, explicitly secret-free SDK result fields.
        allowed_metadata = {
            name: metadata[name]
            for name in (
                "harness_id",
                "external_session_id",
                "cli_version",
                "sdk_version",
                "model",
                "reasoning_effort",
                "context_sources",
                "context_truncated",
            )
            if name in metadata
        }
        status = (
            HarnessRunStatus.INTERRUPTED
            if result.finish_reason == "aborted"
            else HarnessRunStatus.COMPLETED
        )
        revocation_message = await service._provider_turn_revocation_message(session)
        if revocation_message:
            status = HarnessRunStatus.INTERRUPTED
            context["provider_revocation_message"] = revocation_message
        safe_error = (
            str(context.get("provider_revocation_message"))
            if context.get("provider_revocation_message")
            else "aborted by user"
            if status == HarnessRunStatus.INTERRUPTED
            and service._user_abort_requested(session)
            else f"{label} run was aborted"
            if status == HarnessRunStatus.INTERRUPTED
            else ""
        )
    except asyncio.CancelledError:
        status = HarnessRunStatus.INTERRUPTED
        revocation_message = await service._provider_turn_revocation_message(session)
        if revocation_message:
            context["provider_revocation_message"] = revocation_message
            safe_error = revocation_message
            public_error = revocation_message
        else:
            user_abort = service._user_abort_requested(session)
            safe_error = (
                "aborted by user" if user_abort else "Run interrupted unexpectedly"
            )
            public_error = safe_error
        if ownership is not None and await ownership.is_owner_for_finalization():
            await sync_to_async(service.messages.refresh, thread_sensitive=True)(
                assistant
            )
            context["pending_assistant_content"] = assistant.content or ""
        else:
            cleanup_confirmed = False
        raise
    except Exception as exc:
        status = HarnessRunStatus.ERROR
        public_error = f"{label} run failed."
        try:
            safe_error = runtime.redact_error(exc) if runtime else f"{label} run failed"
        except Exception:
            safe_error = f"{label} run failed"
        revocation_message = await service._provider_turn_revocation_message(session)
        if revocation_message:
            context["provider_revocation_message"] = revocation_message
            status = HarnessRunStatus.INTERRUPTED
            safe_error = revocation_message
            public_error = revocation_message
        public_recovery_error = runtime.recovery_message(exc) if runtime else ""
        if public_recovery_error and not revocation_message:
            public_error = public_recovery_error
        current_owner = (
            ownership is not None and await ownership.is_owner_for_finalization()
        )
        if not current_owner:
            cleanup_confirmed = False
        else:
            await sync_to_async(service.messages.refresh, thread_sensitive=True)(
                assistant
            )
            context["pending_assistant_content"] = assistant.content or ""
            if public_recovery_error:
                public_error = public_recovery_error
        log.warning(
            "external_harness_run_failed",
            engine_id=engine_id,
            session_id=key,
            error=safe_error,
        )
    finally:
        context["finalizing"] = True
        try:
            if mcp_runtime is not None:
                try:
                    try:
                        await service._finish_mcp_recordings(
                            mcp_runtime, session, assistant
                        )
                    finally:
                        await mcp_runtime.aclose()
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    log.warning("mcp_runtime_close_interrupted", session_id=key)
                    cleanup_confirmed = False
                except Exception:
                    log.warning("mcp_runtime_close_failed", session_id=key)
                    cleanup_confirmed = False
            await service._cleanup_session_processes(
                workspace_id=str(session.workspace_id),
                session_id=key,
                reason="run_finished",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("claude_process_cleanup_failed", session_id=key)
            cleanup_confirmed = False
        finally:
            if ownership is not None:
                await ownership.stop_heartbeat()
                # A confirmed SDK lease release is the only proof that an
                # attempted CLI process can no longer overlap the next run.
                try:
                    if await ownership.is_current_owner():
                        await ownership.begin_close()
                except Exception:
                    cleanup_confirmed = False
                finalized = False
                if cleanup_confirmed and await ownership.is_owner_for_finalization():
                    if result is not None:
                        await sync_to_async(
                            service.messages.refresh, thread_sensitive=True
                        )(assistant)
                        assistant_content = assistant.content or ""
                        if remainder:
                            assistant_content += remainder
                    else:
                        assistant_content = context.get(
                            "pending_assistant_content",
                            assistant.content or "",
                        )
                    finish = (
                        "aborted"
                        if status == HarnessRunStatus.INTERRUPTED
                        else "error"
                        if status == HarnessRunStatus.ERROR
                        else str(getattr(result, "finish_reason", "stop") or "stop")
                    )
                    public_error = (
                        str(context.get("provider_revocation_message"))
                        if context.get("provider_revocation_message")
                        else "aborted by user"
                        if status == HarnessRunStatus.INTERRUPTED
                        and service._user_abort_requested(session)
                        else "Run interrupted unexpectedly"
                        if status == HarnessRunStatus.INTERRUPTED
                        else public_recovery_error
                        if status == HarnessRunStatus.ERROR and public_recovery_error
                        else f"{label} run failed."
                        if status == HarnessRunStatus.ERROR
                        else ""
                    )
                    usage = result.usage if result is not None else Usage()
                    finalized = await ownership.finalize_turn(
                        status=status,
                        finish=finish,
                        error=public_error,
                        assistant_content=assistant_content,
                        engine_meta=allowed_metadata or None,
                        session_usage=(
                            {
                                "prompt": usage.prompt_tokens,
                                "completion": usage.completion_tokens,
                                "total": usage.total_tokens,
                            }
                            if result is not None
                            and status == HarnessRunStatus.COMPLETED
                            else None
                        ),
                        session_cost=(
                            float(result.cost or 0.0)
                            if result is not None
                            and status == HarnessRunStatus.COMPLETED
                            else 0.0
                        ),
                        engine_state_updates=(
                            _cli_total_cost_update(result)
                            if result is not None
                            and status == HarnessRunStatus.COMPLETED
                            else None
                        ),
                        tail_remainder=(
                            remainder
                            if result is not None
                            and status == HarnessRunStatus.COMPLETED
                            else ""
                        ),
                    )
                if finalized:
                    session.status = HarnessSessionStatus.IDLE
                    await sync_to_async(
                        service.messages.refresh, thread_sensitive=True
                    )(assistant)
                else:
                    # A replaced owner or unconfirmed process cleanup must
                    # not release durable admission or publish terminal UI.
                    context["cleanup_confirmed"] = False
                    if await ownership.is_owner_for_finalization():
                        if result is not None:
                            await sync_to_async(
                                service.messages.refresh, thread_sensitive=True
                            )(assistant)
                            assistant_content = assistant.content or ""
                            if remainder:
                                assistant_content += remainder
                        else:
                            assistant_content = context.get(
                                "pending_assistant_content",
                                assistant.content or "",
                            )
                        finish = (
                            "aborted"
                            if status == HarnessRunStatus.INTERRUPTED
                            else "error"
                        )
                        safe_shell_error = (
                            f"{label} process cleanup could not be confirmed"
                            if status == HarnessRunStatus.COMPLETED
                            else public_error
                        )
                        await ownership.settle_shell(
                            finish=finish,
                            error=safe_shell_error,
                            assistant_content=assistant_content,
                            engine_meta=allowed_metadata or None,
                        )
            if context.get("cleanup_confirmed", True):
                await service._finalize_external_children(session, assistant)
                if status != HarnessRunStatus.ERROR:
                    await service._finalize_run(session, assistant)
                else:
                    await service._finalize_owned_claude_error(session, assistant)
            else:
                # Tracking may be released locally, but the durable busy
                # state and unresolved CLOSING attempt remain the admission
                # fence for the next worker.
                service._drop_run_tracking(key)
