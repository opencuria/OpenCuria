"""External engine run execution under durable ownership."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from ..models import HarnessRunStatus, HarnessSessionStatus
from ..providers.base import LLMMessage
from ..runner import RunOptions

if TYPE_CHECKING:
    from ..harness_service import HarnessService
    from ..models import HarnessMessage, HarnessSession

log = structlog.get_logger("apps.harness.harness_service")


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
    from ..harness_service import _merge_skill_bodies, _tail_remainder
    from ..providers.base import Usage
    from .session_store import HarnessClaudeSessionStore

    key = str(session.id)
    context = service._runs.get(key, {})
    ownership = context.get("ownership")
    mcp_runtime = None
    accessor = None
    engine = None
    auth = None
    status = HarnessRunStatus.ERROR
    safe_error = ""
    public_error = ""
    cleanup_confirmed = True
    result = None
    allowed_metadata: dict[str, Any] = {}
    remainder = ""
    assistant_content = ""

    async def ensure_current() -> None:
        if ownership is not None and not await ownership.is_current_owner():
            raise ConflictError("Claude run ownership was superseded")

    try:
        if ownership is None:
            raise RuntimeError("Claude run ownership was not initialized")
        if not await ownership.start():
            raise ConflictError("Claude run ownership was superseded")

        ownership.start_heartbeat(on_lost=lambda: service._cancel_owned_task(key))
        context["cleanup_confirmed"] = True
        await ensure_current()
        if service._accessor_factory is None:
            raise RuntimeError("Claude engine requires a workspace accessor")
        accessor = await service._accessor_factory(str(session.workspace_id))
        # The same credential snapshot is fail-closed for native and SDK tools.
        mcp_snapshot = await sync_to_async(service._prepare_mcp_snapshot_for_run)(
            session, organization_id, accessor
        )
        tools = service._tools_for_session(key, session.agent_name or session.mode)
        if accessor is not None and (session.agent_name or "").lower() not in {
            "computeruse",
            "title",
            "compaction",
        }:
            import apps.harness.mcp_client.runtime as mcp_runtime_module

            mcp_runtime = mcp_runtime_module.McpRuntime()
            await mcp_runtime.setup(
                workspace=None,
                organization_id=organization_id,
                accessor=accessor,
                core_tool_names=tools.names(),
                snapshot=mcp_snapshot,
                owner_id=str(uuid.uuid4()),
            )
            mcp_runtime.register_tools(tools)
            if mcp_runtime.skipped:
                log.warning(
                    "mcp_discovery_health",
                    session_id=key,
                    skipped=mcp_runtime.skipped,
                )
        auth = await sync_to_async(service._resolve_engine_auth, thread_sensitive=True)(
            organization_id,
            context.get("initiated_by_id"),
            getattr(session, "connection_id", None),
        )
        session_store = HarnessClaudeSessionStore(
            session_id=session.id,
            external_session_id=session.external_session_id or "",
            # The SDK project key derives from its normalized cwd, not
            # the workspace UUID; the store still validates the key shape.
            project_key="",
        )
        run_options = RunOptions(
            depth=depth,
            max_depth=max_depth,
            history=history,
            session_id=key,
            workspace_id=str(session.workspace_id),
            organization_id=str(organization_id),
            current_user_message_id=str(context.get("user_message_id", "")),
            skills=_merge_skill_bodies(
                context.get("skill_bodies", []),
                mcp_snapshot.snapshot.plugin_skills if mcp_snapshot else [],
            ),
            on_permission=lambda **kw: service._on_permission(session, assistant, **kw),
            on_question=lambda **kw: service._on_question(session, assistant, **kw),
        )
        run_options.reasoning_effort = session.reasoning_effort or "high"

        async def on_owner(owner: dict[str, str], owner_phase: str) -> None:
            nonlocal cleanup_confirmed
            if owner_phase == "reserved":
                cleanup_confirmed = False
                context["cleanup_confirmed"] = False
                if not await ownership.bind_runner_lease(
                    str(owner.get("lease_id", "")), str(owner.get("epoch", ""))
                ):
                    raise ConflictError("Claude runner lease ownership was superseded")
                await ensure_current()
            elif owner_phase == "closing":
                cleanup_confirmed = False
                context["cleanup_confirmed"] = False
                if not await ownership.begin_close():
                    raise ConflictError("Claude runner lease ownership was superseded")
            elif owner_phase == "released":
                cleanup_confirmed = True
                context["cleanup_confirmed"] = True

        async def on_binding(external_id: str, metadata: dict[str, Any]) -> None:
            from .repositories import HarnessEngineRepository

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
                raise ConflictError("Claude session binding ownership was superseded")
            session.external_session_id = external_id
            session.engine_state = dict(metadata or {})

        async def emit(event: dict[str, Any]) -> None:
            await ensure_current()
            await service._on_claude_root_event(session, assistant, event)

        async def on_child_event(parent_call_id: str, event: dict[str, Any]) -> None:
            await ensure_current()
            await service._on_claude_child_event(key, parent_call_id, event)

        engine = service._engine_registry_for_run().create(
            engine_id,
            tools=tools,
            accessor=accessor,
            emit=emit,
            auth_env=auth.secret_env,
            external_session_id=session.external_session_id or "",
            session_store=session_store,
            on_binding=on_binding,
            on_child_event=on_child_event,
            on_owner=on_owner,
            evaluator=service.permissions.evaluator,
            effort=session.reasoning_effort or "high",
        )
        await ensure_current()
        result = await engine.run(
            prompt,
            session.agent_name or session.mode or "build",
            session.model or "sonnet",
            session.mode or "build",
            run_options,
        )
        await sync_to_async(assistant.refresh_from_db, thread_sensitive=True)()
        remainder = _tail_remainder(assistant.content or "", result.output or "")
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
        safe_error = (
            "aborted by user"
            if status == HarnessRunStatus.INTERRUPTED
            and service._user_abort_requested(session)
            else "Claude run was aborted"
            if status == HarnessRunStatus.INTERRUPTED
            else ""
        )
    except asyncio.CancelledError:
        status = HarnessRunStatus.INTERRUPTED
        user_abort = service._user_abort_requested(session)
        safe_error = "aborted by user" if user_abort else "Run interrupted unexpectedly"
        public_error = safe_error
        if ownership is not None and await ownership.is_owner_for_finalization():
            await sync_to_async(assistant.refresh_from_db, thread_sensitive=True)()
            context["pending_assistant_content"] = assistant.content or ""
        else:
            cleanup_confirmed = False
        raise
    except Exception as exc:
        status = HarnessRunStatus.ERROR
        public_error = "Claude run failed."
        try:
            safe_error = service._redact_claude_error(exc, auth)
        except Exception:
            safe_error = "Claude run failed"
        current_owner = (
            ownership is not None and await ownership.is_owner_for_finalization()
        )
        if not current_owner:
            cleanup_confirmed = False
        else:
            await sync_to_async(assistant.refresh_from_db, thread_sensitive=True)()
            context["pending_assistant_content"] = assistant.content or ""
            public_error = "Claude run failed."
        log.warning(
            "claude_harness_run_failed",
            session_id=key,
            error=safe_error,
        )
    finally:
        context["finalizing"] = True
        try:
            if mcp_runtime is not None:
                try:
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
                            assistant.refresh_from_db, thread_sensitive=True
                        )()
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
                        else "stop"
                    )
                    public_error = (
                        "aborted by user"
                        if status == HarnessRunStatus.INTERRUPTED
                        and service._user_abort_requested(session)
                        else "Run interrupted unexpectedly"
                        if status == HarnessRunStatus.INTERRUPTED
                        else "Claude run failed."
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
                        assistant.refresh_from_db, thread_sensitive=True
                    )()
                else:
                    # A replaced owner or unconfirmed process cleanup must
                    # not release durable admission or publish terminal UI.
                    context["cleanup_confirmed"] = False
                    if await ownership.is_owner_for_finalization():
                        if result is not None:
                            await sync_to_async(
                                assistant.refresh_from_db, thread_sensitive=True
                            )()
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
                            "Claude process cleanup could not be confirmed"
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
                await service._finalize_claude_children(session, assistant)
                if status != HarnessRunStatus.ERROR:
                    await service._finalize_run(session, assistant)
                else:
                    await service._finalize_owned_claude_error(session, assistant)
            else:
                # Tracking may be released locally, but the durable busy
                # state and unresolved CLOSING attempt remain the admission
                # fence for the next worker.
                service._drop_run_tracking(key)
