"""Project Claude SDK messages and task lifecycle into harness events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from ...providers.base import Usage
from .events import ClaudeEventNormalizer, StepState
from .security import redact_text

if TYPE_CHECKING:
    from .engine import ClaudeEngine


log = structlog.get_logger("apps.harness.engines.claude.engine")


class ClaudeMessageProjector:
    """Translate SDK message and task events using the owning engine ports.

    The engine remains responsible for event sinks, tool execution, stream
    projection, and session state; this class only routes SDK lifecycle events.
    """

    def __init__(
        self, engine: ClaudeEngine, *, cli_version: str, sdk_version: str
    ) -> None:
        """Bind the engine and runtime metadata used for session projection."""
        self.engine = engine
        self.cli_version = cli_version
        self.sdk_version = sdk_version

    async def handle_message(self, message: Any) -> tuple[Any, str, Usage] | None:
        """Dispatch SDK messages to stream, task, and session projections."""
        engine = self.engine
        types = ClaudeEventNormalizer.message_types()
        if isinstance(message, types.stream):
            await engine._emit_partial(message)
            return None
        if isinstance(message, types.assistant):
            await engine._emit_assistant(message)
            return None
        if isinstance(message, types.user):
            await engine._handle_user_tool_results(message)
            return None
        if isinstance(message, types.task_started):
            task = str(message.task_id or "")
            tool = str(message.tool_use_id or "")
            if not tool and len(engine._pending_task_tool_ids) == 1:
                tool = next(iter(engine._pending_task_tool_ids))
            if tool:
                engine._pending_task_tool_ids.discard(tool)
                engine._subtask_by_tool[tool] = task
                if task:
                    task_data = message.data if isinstance(message.data, dict) else {}
                    task_type = str(task_data.get("task_type") or "general")
                    engine._agent_kind[task] = engine._normalize_agent_type(task_type)
                    engine._agent_type_raw[task] = task_type
                    engine._agent_parent_tool[task] = tool
                    base_depth = engine._tool_events.get(tool, {}).get("depth", 0)
                    engine._agent_depth[task] = int(base_depth) + 1
                state = engine._step_states.setdefault(tool, StepState())
                tool_state = engine._tool_events.get(tool)
                if tool_state is not None:
                    state.step = int(tool_state["step"])
                    state.started = True
                    state.active_calls.add(tool)
            if task and tool and task not in engine._started_subtasks:
                engine._started_subtasks.add(task)
                await engine._send(
                    {
                        "type": "subtask_started",
                        "subtask_id": task,
                        "child_session_id": "",
                        "agent": engine._agent_kind.get(task, "general"),
                        "description": redact_text(
                            str(message.description or ""), engine._event_secrets()
                        )[:500],
                        "model": engine._model,
                        "parent_tool_use_id": tool,
                    }
                )
            return None
        if isinstance(message, types.task_progress):
            await engine._handle_task_progress(message)
            return None
        if isinstance(message, types.task_notification):
            await engine._handle_task_notification(message)
            return None
        if isinstance(message, types.system):
            if message.subtype in {"init", "session_start", "session_started"}:
                data = message.data if isinstance(message.data, dict) else {}
                session_id = str(data.get("session_id") or "")
                if session_id:
                    await engine._bind_external_session(session_id)
            await engine._handle_task_updated(message)
            return None
        if isinstance(message, types.result):
            if message.session_id:
                await engine._bind_external_session(str(message.session_id))
            if message.is_error:
                errors = list(message.errors or [])
                detail = "; ".join(map(str, errors)) or str(
                    message.result or message.subtype or "Claude run failed"
                )
                raise RuntimeError(engine._safe_error_text(detail))
            usage = ClaudeEventNormalizer.result_usage(message)
            return message, str(message.result or ""), usage
        return None

    async def handle_user_tool_results(self, message: Any) -> None:
        """Finalize tool calls from SDK tool-result messages if hooks lagged."""
        engine = self.engine
        tool_results = ClaudeEventNormalizer.user_tool_results(message)
        for call_id, value, is_error in tool_results:
            state = engine._tool_events.get(call_id)
            if state is None:
                continue
            result = engine._tool_results.pop(call_id, None)
            output = engine._tool_output(result.output if result is not None else value)
            if is_error or result is not None and result.metadata.get("is_error"):
                await engine._finish_tool_error(
                    call_id,
                    output or "OpenCuria tool execution failed",
                    parent_id=state.get("parent_tool_use_id"),
                )
                continue
            await engine._finish_tool_completed(
                call_id,
                output,
                parent_id=state.get("parent_tool_use_id"),
                attachments=list(result.attachments or []) if result else [],
            )
            if state.get("tool") == "todowrite":
                await engine._send_todos(state.get("parent_tool_use_id"))

    async def handle_task_progress(self, message: Any) -> None:
        """Project SDK task progress while its parent task remains active."""
        engine = self.engine
        task = str(message.task_id or "")
        parent_tool = next(
            (
                call
                for call, task_id in engine._subtask_by_tool.items()
                if task_id == task
            ),
            "",
        )
        if not task or not parent_tool:
            return
        await engine._send(
            {
                "type": "subtask_updated",
                "subtask_id": task,
                "agent": engine._agent_kind.get(task, "general"),
                "description": redact_text(
                    str(message.description or ""), engine._event_secrets()
                )[:500],
                "last_tool_name": redact_text(
                    str(message.last_tool_name or ""), engine._event_secrets()
                )[:100],
                "usage": dict(message.usage or {}),
                "parent_tool_use_id": parent_tool,
            }
        )

    async def handle_task_notification(self, message: Any) -> None:
        """Map task notifications to one terminal subtask status."""
        engine = self.engine
        task = str(message.task_id or "")
        tool = str(message.tool_use_id or "")
        if not tool:
            tool = next(
                (
                    call
                    for call, task_id in engine._subtask_by_tool.items()
                    if task_id == task
                ),
                "",
            )
        if not tool or task in engine._finished_subtasks:
            return
        status = (
            "completed"
            if message.status == "completed"
            else "aborted"
            if message.status in {"stopped", "killed"}
            else "error"
        )
        await engine._finish_sdk_subtask(task, tool, status, str(message.summary or ""))

    async def handle_task_updated(self, message: Any) -> None:
        """Project authoritative task updates and mirrored-session failures."""
        engine = self.engine
        if message.subtype == "mirror_error":
            log.error(
                "claude_session_mirror_failed",
                error=engine._safe_error_text(
                    (message.data or {}).get("error", "session mirror failed"),
                ),
            )
            raise RuntimeError("Claude session transcript mirror failed")
        if message.subtype != "task_updated":
            return
        data = message.data if isinstance(message.data, dict) else {}
        patch = data.get("patch") if isinstance(data.get("patch"), dict) else {}
        status = str(patch.get("status") or "")
        if status not in {"completed", "failed", "killed", "stopped"}:
            return
        task = str(data.get("task_id") or "")
        tool = str(data.get("tool_use_id") or "")
        if not tool:
            tool = next(
                (
                    call
                    for call, task_id in engine._subtask_by_tool.items()
                    if task_id == task
                ),
                "",
            )
        terminal_status = str(patch.get("status") or "")
        status = (
            "completed"
            if terminal_status == "completed"
            else "aborted"
            if terminal_status in {"killed", "stopped"}
            else "error"
        )
        if not tool:
            return
        if task in engine._finished_subtasks:
            previous = engine._subtask_terminal_status.get(task, "")
            if status == "aborted" and previous != "aborted":
                await engine._revise_subtask_terminal_status(task, tool, status)
            return
        await engine._finish_sdk_subtask(
            task,
            tool,
            status,
            ClaudeEventNormalizer.tool_output(
                patch.get("result") or patch.get("error")
            ),
        )

    async def finish_sdk_subtask(
        self, task: str, tool: str, status: str, summary: str
    ) -> None:
        """Emit exactly one terminal subtask event and release its parent step."""
        engine = self.engine
        if not task or task in engine._finished_subtasks:
            return
        engine._finished_subtasks.add(task)
        engine._subtask_terminal_status[task] = status
        summary = redact_text(
            summary or engine._child_text.get(task, ""), engine._event_secrets()
        )
        await engine._send(
            {
                "type": "subtask_finished",
                "subtask_id": task,
                "child_session_id": "",
                "agent": engine._agent_kind.get(task, "general"),
                "status": status,
                "summary": summary[:500],
                "parent_tool_use_id": tool,
            }
        )
        if tool:
            engine._subtask_by_tool.pop(tool, None)
        engine._stopped_subagents.discard(task)
        engine._agent_parent_tool.pop(task, None)
        engine._agent_depth.pop(task, None)
        engine._agent_kind.pop(task, None)
        engine._agent_type_raw.pop(task, None)
        state = engine._step_states.get(tool)
        if state is not None:
            state.active_calls.discard(tool)
            state.awaiting_next_step = not bool(state.active_calls)
        engine._child_text.pop(task, None)

    async def revise_subtask_terminal_status(
        self, task: str, tool: str, status: str
    ) -> None:
        """Preserve a later authoritative abort after an optimistic completion."""
        engine = self.engine
        if status != "aborted":
            return
        event = {
            "type": "subtask_finished",
            "subtask_id": task,
            "child_session_id": "",
            "agent": engine._agent_kind.get(task, "general"),
            "status": status,
            "summary": engine._child_text.get(task, "")[:500],
            "parent_tool_use_id": tool,
        }
        await engine._send(event)
        engine._subtask_terminal_status[task] = status

    async def bind_external_session(self, external_id: str) -> None:
        """Bind each validated external transcript ID to this harness run."""
        engine = self.engine
        if not engine.on_binding:
            return
        try:
            external_id = engine._valid_external_session_id(external_id)
        except ValueError:
            return
        if external_id in engine._bound_session_ids:
            return
        engine._bound_session_ids.add(external_id)
        await engine.on_binding(
            external_id,
            {
                "harness_id": "claude",
                "cli_version": self.cli_version,
                "sdk_version": self.sdk_version,
                "model": engine._model,
                "mode": engine._mode,
            },
        )
