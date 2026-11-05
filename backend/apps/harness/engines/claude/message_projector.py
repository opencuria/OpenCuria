"""Project Claude SDK messages and task lifecycle into harness events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    UserMessage,
)
from claude_agent_sdk.types import Message

from ...agents.definitions import canonical_subagent_type
from ...providers.base import Usage
from ...redaction import redact_text
from ..base import RunOptions
from .events import ClaudeEventNormalizer, StepState
from .runtime_state import CLI_TOTAL_COST_KEY

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

    async def handle_message(
        self, message: Message
    ) -> tuple[ResultMessage, str, Usage] | None:
        """Dispatch SDK messages to stream, task, and session projections."""
        engine = self.engine
        if isinstance(message, StreamEvent):
            await engine._emit_partial(message)
            return None
        if isinstance(message, AssistantMessage):
            await engine._emit_assistant(message)
            return None
        if isinstance(message, UserMessage):
            await self.handle_user_tool_results(message)
            return None
        if isinstance(message, TaskStartedMessage):
            data = message.data if isinstance(message.data, dict) else {}
            run = engine._workflow_runs.get(str(message.tool_use_id or ""))
            if run is not None:
                # Remember the run's task id; later task updates may omit
                # the Workflow call id.
                run.task_id = str(message.task_id or "")
                return None
            await self.start_subtask(
                str(message.task_id or ""),
                str(message.tool_use_id or ""),
                str(message.description or ""),
                task_type=str(message.task_type or data.get("task_type") or ""),
            )
            return None
        if isinstance(message, TaskProgressMessage):
            await self.handle_task_progress(message)
            return None
        if isinstance(message, TaskNotificationMessage):
            await self.handle_task_notification(message)
            return None
        if isinstance(message, SystemMessage):
            if message.subtype in {"init", "session_start", "session_started"}:
                data = message.data if isinstance(message.data, dict) else {}
                session_id = str(data.get("session_id") or "")
                if session_id:
                    await self.bind_external_session(session_id)
            await self.handle_task_updated(message)
            return None
        if isinstance(message, ResultMessage):
            if message.session_id:
                await self.bind_external_session(str(message.session_id))
            usage = ClaudeEventNormalizer.result_usage(message)
            if message.is_error and message.subtype != "error_max_turns":
                errors = list(message.errors or [])
                detail = "; ".join(map(str, errors)) or str(
                    message.result or message.subtype or "Claude run failed"
                )
                raise RuntimeError(engine._safe_error_text(detail))
            return message, str(message.result or ""), usage
        return None

    def _subtask_tool(self, task: str, tool: str = "") -> str:
        """Resolve only admitted task identities, including late terminal updates."""
        engine = self.engine
        parent = engine._agent_parent_tool.get(task) or next(
            (
                call
                for call, task_id in engine._subtask_by_tool.items()
                if task_id == task
            ),
            "",
        )
        return parent if task and (not tool or tool == parent) else ""

    async def start_subtask(
        self,
        task: str,
        tool: str,
        description: str,
        *,
        task_type: str = "",
        agent_type: str | None = None,
    ) -> None:
        """Admit native agent delegations, never generic CLI background tasks.

        Task lifecycle frames also describe shells and monitors. Classify before
        consuming a pending Agent/Task call, and share admission with SDK hooks.
        """
        engine = self.engine
        if not task or task in engine._finished_subtasks:
            return
        try:
            canonical_task_type = canonical_subagent_type(task_type)
        except ValueError:
            canonical_task_type = ""
        if task_type and not canonical_task_type and task_type != "local_agent":
            return
        known_tool = self._subtask_tool(task)
        if not tool:
            tool = known_tool
            # A completely untyped frame cannot identify a pending delegation.
            if not tool and not task_type and agent_type is None:
                return
            if not tool and len(engine._pending_task_tool_ids) == 1:
                tool = next(iter(engine._pending_task_tool_ids))
        if not tool or (known_tool and known_tool != tool):
            return
        parent = engine._tool_events.get(tool, {})
        # The permission key "task" also covers TaskStop/TaskUpdate/etc.; only
        # the original native invocation proves an actual agent delegation.
        if not known_tool and parent.get("source_tool") not in {"Agent", "Task"}:
            return
        arguments = parent.get("event_arguments", {})
        requested_type = (
            arguments.get("subagent_type") if isinstance(arguments, dict) else ""
        )
        if agent_type is not None:
            raw_type = agent_type
        elif task in engine._agent_type_raw:
            raw_type = engine._agent_type_raw[task]
        else:
            raw_type = canonical_task_type or requested_type or ""
        try:
            canonical_type = canonical_subagent_type(str(raw_type))
        except ValueError:
            if agent_type is not None:
                engine._agent_type_raw[task] = agent_type
            return
        engine._pending_task_tool_ids.discard(tool)
        engine._subtask_by_tool[tool] = task
        engine._agent_parent_tool[task] = tool
        engine._agent_kind[task] = canonical_type
        engine._agent_type_raw[task] = canonical_type
        if task in engine._started_subtasks:
            return
        engine._agent_depth[task] = int(parent.get("depth", 0)) + 1
        state = engine._step_states.setdefault(tool, StepState())
        if parent:
            state.step = int(parent["step"])
            state.started = True
            state.active_calls.add(tool)
        engine._started_subtasks.add(task)
        await engine._send(
            {
                "type": "subtask_started",
                "subtask_id": task,
                "child_session_id": "",
                "agent": canonical_type,
                "description": redact_text(
                    description or canonical_type, engine._event_secrets()
                )[:500],
                "model": engine._model,
                "parent_tool_use_id": tool,
            }
        )

    async def handle_user_tool_results(self, message: UserMessage) -> None:
        """Finalize tool calls from SDK tool-result messages if hooks lagged."""
        engine = self.engine
        tool_results = ClaudeEventNormalizer.user_tool_results(message)
        for call_id, value, is_error in tool_results:
            state = engine._tool_events.get(call_id)
            if state is None:
                continue
            result = engine._tool_results.pop(call_id, None)
            output = ClaudeEventNormalizer.tool_output(
                result.output if result is not None else value
            )
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
                attachments=(
                    list(result.attachments or [])
                    if result
                    else ClaudeEventNormalizer.tool_attachments(value)
                ),
            )
            if state.get("tool") == "todowrite":
                await engine._send_todos(state.get("parent_tool_use_id"))

    async def handle_task_progress(self, message: Any) -> None:
        """Project SDK task progress while its parent task remains active."""
        engine = self.engine
        task = str(message.task_id or "")
        workflow = self._workflow_call(task, str(message.tool_use_id or ""))
        if workflow:
            data = message.data if isinstance(message.data, dict) else {}
            await self.handle_workflow_progress(workflow, data.get("workflow_progress"))
            return
        parent_tool = self._subtask_tool(
            task, str(getattr(message, "tool_use_id", None) or "")
        )
        if not parent_tool or task in engine._finished_subtasks:
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
        status = self._terminal_status(str(message.status or ""))
        workflow = self._workflow_call(task, str(message.tool_use_id or ""))
        if workflow:
            await self.close_workflow(workflow, status, str(message.summary or ""))
            return
        tool = self._subtask_tool(task, str(message.tool_use_id or ""))
        if not tool or task in engine._finished_subtasks:
            return
        await self.finish_sdk_subtask(task, tool, status, str(message.summary or ""))

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
        status = self._terminal_status(str(patch.get("status") or ""))
        workflow = self._workflow_call(task, str(data.get("tool_use_id") or ""))
        if workflow:
            await self.close_workflow(
                workflow,
                status,
                ClaudeEventNormalizer.tool_output(
                    patch.get("result") or patch.get("error")
                ),
            )
            return
        tool = self._subtask_tool(task, str(data.get("tool_use_id") or ""))
        if not tool:
            return
        if task in engine._finished_subtasks:
            previous = engine._subtask_terminal_status.get(task, "")
            if status == "aborted" and previous != "aborted":
                await self.revise_subtask_terminal_status(task, tool, status)
            return
        await self.finish_sdk_subtask(
            task,
            tool,
            status,
            ClaudeEventNormalizer.tool_output(
                patch.get("result") or patch.get("error")
            ),
        )

    @staticmethod
    def _terminal_status(value: str) -> str:
        """Map an SDK terminal task status to a subtask status."""
        if value == "completed":
            return "completed"
        if value in {"killed", "stopped"}:
            return "aborted"
        return "error"

    def _workflow_call(self, task: str, tool: str) -> str:
        """Return the authorized Workflow call a task frame belongs to."""
        runs = self.engine._workflow_runs
        if tool in runs:
            return tool
        if not task:
            return ""
        return next((call for call, run in runs.items() if run.task_id == task), "")

    async def start_workflow_agent(self, agent_id: str) -> None:
        """Admit a dynamic-workflow agent under the latest authorized Workflow.

        SubagentStart names no workflow, so an agent is admitted only while a
        root Workflow call OpenCuria authorized is running; otherwise it stays
        unmapped and its tool calls fail closed. It runs with the general
        subagent's scope and is keyed by its own agent id, because one
        Workflow call fans out to many agents with separate transcripts.
        """
        engine = self.engine
        if (
            not agent_id
            or agent_id in engine._started_subtasks
            or agent_id in engine._finished_subtasks
            or not engine._workflow_runs
        ):
            return
        run = engine._workflow_runs[next(reversed(engine._workflow_runs))]
        run.agents.append(agent_id)
        options = engine._options or RunOptions()
        engine._agent_parent_tool[agent_id] = agent_id
        engine._agent_kind[agent_id] = "general"
        engine._agent_type_raw[agent_id] = "general"
        engine._agent_depth[agent_id] = options.depth + 1
        engine._started_subtasks.add(agent_id)
        await engine._send(
            {
                "type": "subtask_started",
                "subtask_id": agent_id,
                "child_session_id": "",
                "agent": "general",
                "description": f"{run.label or 'Workflow'} · agent {len(run.agents)}",
                "model": engine._model,
                "parent_tool_use_id": agent_id,
            }
        )

    async def handle_workflow_progress(self, workflow: str, entries: Any) -> None:
        """Finish workflow agents the run reports as done or failed."""
        run = self.engine._workflow_runs.get(workflow)
        if run is None or not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "workflow_agent":
                continue
            agent_id = str(entry.get("agentId") or "")
            state = str(entry.get("state") or "")
            if agent_id not in run.agents or state not in {"done", "error"}:
                continue
            await self.finish_sdk_subtask(
                agent_id,
                agent_id,
                "completed" if state == "done" else "error",
                str(entry.get("resultPreview") or entry.get("error") or ""),
            )

    async def close_workflow(self, workflow: str, status: str, summary: str) -> None:
        """End a Workflow run and settle agents that reported no outcome."""
        run = self.engine._workflow_runs.pop(workflow, None)
        if run is None:
            return
        for agent_id in run.agents:
            await self.finish_sdk_subtask(agent_id, agent_id, status, summary)

    async def finish_sdk_subtask(
        self, task: str, tool: str, status: str, summary: str
    ) -> None:
        """Emit exactly one terminal subtask event and release its parent step."""
        engine = self.engine
        if not self._subtask_tool(task, tool) or task in engine._finished_subtasks:
            return
        engine._finished_subtasks.add(task)
        engine._subtask_terminal_status[task] = status
        summary = redact_text(
            summary or engine._child_text.get(tool, ""), engine._event_secrets()
        )
        engine._subtask_terminal_summary[task] = summary[:500]
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
        # Retain identity until run reset to correlate late authoritative aborts.
        engine._agent_parent_tool[task] = tool
        engine._agent_depth.pop(task, None)
        engine._agent_type_raw.pop(task, None)
        state = engine._step_states.get(tool)
        if state is not None:
            state.active_calls.discard(tool)
            state.awaiting_next_step = not bool(state.active_calls)
        engine._child_text.pop(tool, None)

    async def revise_subtask_terminal_status(
        self, task: str, tool: str, status: str
    ) -> None:
        """Preserve a later authoritative abort after an optimistic completion."""
        engine = self.engine
        if status != "aborted" or not self._subtask_tool(task, tool):
            return
        event = {
            "type": "subtask_finished",
            "subtask_id": task,
            "child_session_id": "",
            "agent": engine._agent_kind.get(task, "general"),
            "status": status,
            "summary": engine._subtask_terminal_summary.get(task, ""),
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
            external_id = engine._runtime_state.valid_external_session_id(external_id)
        except ValueError:
            return
        if external_id in engine._bound_session_ids:
            return
        engine._bound_session_ids.add(external_id)
        state: dict[str, Any] = {
            "harness_id": "claude",
            "cli_version": self.cli_version,
            "sdk_version": self.sdk_version,
            "model": engine._model,
            "mode": engine._mode,
        }
        # Binding replaces the stored engine state; carry the cost baseline over.
        if engine._cost_baseline > 0:
            state[CLI_TOTAL_COST_KEY] = engine._cost_baseline
        await engine.on_binding(external_id, state)
