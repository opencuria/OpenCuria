"""Claude Code CLI engine hosted through the OpenCuria workspace transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from ...access.base import (
    HARNESS_WORKSPACE_ROOT,
    WorkspaceAccessor,
    sanitize_exec_workdir,
)
from ...agents.definitions import AgentDefinition, get_agent
from ...constants import DEFAULT_MAX_DEPTH
from ...permissions.evaluator import DEFAULT_GLOBAL_RULES, PermissionEvaluator
from ...prompts.composer import compose_system_prompt
from ...providers.base import ToolSchema, Usage
from ...runner import RunOptions, RunResult
from ...tools.base import ToolContext, ToolRegistry, ToolResult
from ...tools.todos import repository_for_session
from ..catalog import normalize_claude_effort, resolve_claude_sdk_model
from .context import build_user_content
from .errors import ClaudeTranscriptError
from .events import ClaudeEventNormalizer, StepState
from .mcp_bridge import mcp_attachment_content, sanitize_mcp_attachments
from .message_projection import FileSnapshot, NativeFileChangeTracker
from .message_projector import ClaudeMessageProjector
from .permissionbridge import PermissionBridge
from .policy import NATIVE_TOOL_KEYS, native_tools_for_claude, tool_schemas_for_claude
from .runtime_state import (
    MANAGED_STATE_PREFIX,
    ClaudeRuntimeState,
    validate_claude_skills,
)
from .security import StreamingRedactor, redact_text, redact_value
from .transport import ClaudeWorkspaceTransport

log = structlog.get_logger(__name__)
CLI_VERSION = "2.1.292"
SDK_VERSION = "0.2.164"
MAX_RESTORED_TRANSCRIPT_BYTES = 10 * 1024 * 1024
MAX_MANAGED_PROMPT_BYTES = 256 * 1024
CANCEL_CLEANUP_TIMEOUT_S = 3.0
EmitCallback = Callable[[dict[str, Any]], Awaitable[None]]
BindingCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
ChildEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
OwnerCallback = Callable[[dict[str, str], str], Awaitable[None]]


class ClaudeEngine:
    """Run one Claude Code session in the selected workspace."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        accessor: WorkspaceAccessor,
        emit: EmitCallback | None = None,
        auth_env: dict[str, str] | None = None,
        external_session_id: str = "",
        session_store: Any | None = None,
        on_binding: BindingCallback | None = None,
        on_child_event: ChildEventCallback | None = None,
        on_owner: OwnerCallback | None = None,
        evaluator: PermissionEvaluator | None = None,
        runtime_artifact_ensurer: Callable[..., Awaitable[Any]] | None = None,
        effort: str = "high",
        critical_event_errors: bool = False,
    ) -> None:
        """Bind harness runtime ports and session-scoped SDK state."""
        self.tools = tools
        self.accessor = accessor
        self._emit = emit or self._noop_emit
        self._critical_event_errors = critical_event_errors
        self.auth_env = dict(auth_env or {})
        self.external_session_id = (external_session_id or "").strip()
        self.session_store = session_store
        self.on_binding = on_binding
        self.on_child_event = on_child_event
        self.on_owner = on_owner
        self.evaluator = evaluator or PermissionEvaluator(
            global_rules=dict(DEFAULT_GLOBAL_RULES)
        )
        self._artifact_ensurer = runtime_artifact_ensurer
        if self._artifact_ensurer is not None and not callable(self._artifact_ensurer):
            raise TypeError("runtime_artifact_ensurer must be callable")
        self._default_effort = normalize_claude_effort(effort)
        self._options: RunOptions | None = None
        self._agent: AgentDefinition | None = None
        self._bridge: PermissionBridge | None = None
        self._mode = "build"
        self._model = ""
        self._cwd = HARNESS_WORKSPACE_ROOT
        self._steps = 0
        self._step_states: dict[str, StepState] = {"": StepState()}
        self._tool_events: dict[str, dict[str, Any]] = {}
        self._wire_to_registry: dict[str, str] = {}
        self._tool_results: dict[str, ToolResult] = {}
        self._tool_arguments: dict[str, dict[str, Any]] = {}
        self._native_file_snapshots: dict[str, FileSnapshot] = {}
        self._managed_root = ""
        self._runtime_state = ClaudeRuntimeState(
            accessor=accessor,
            auth_env=self.auth_env,
            session_store=session_store,
            root=MANAGED_STATE_PREFIX.rstrip("/"),
            sdk_version=SDK_VERSION,
        )
        self._subtask_by_tool: dict[str, str] = {}
        self._agent_parent_tool: dict[str, str] = {}
        self._agent_depth: dict[str, int] = {}
        self._agent_kind: dict[str, str] = {}
        self._agent_type_raw: dict[str, str] = {}
        self._started_subtasks: set[str] = set()
        self._finished_subtasks: set[str] = set()
        self._subtask_terminal_status: dict[str, str] = {}
        self._subtask_terminal_summary: dict[str, str] = {}
        self._stopped_subagents: set[str] = set()
        self._pending_task_tool_ids: set[str] = set()
        self._root_text: list[str] = []
        self._root_reasoning: list[str] = []
        self._stream_redactors: dict[tuple[str, str, int, str], StreamingRedactor] = {}
        self._pending_stream_text: dict[tuple[str, str, int, str], str] = {}
        self._early_queued_calls: set[str] = set()
        self._child_text: dict[str, str] = {}
        self._bound_session_ids: set[str] = set()
        self._managed_skill_names: set[str] = set()
        self._owner_released = False
        self._native_file_change_tracker = NativeFileChangeTracker(accessor)
        self._message_projector = ClaudeMessageProjector(
            self, cli_version=CLI_VERSION, sdk_version=SDK_VERSION
        )

    @staticmethod
    async def _noop_emit(_event: dict[str, Any]) -> None:
        """Discard events when no harness observer is configured."""

    def _event_secrets(self) -> tuple[str, ...]:
        """Return run-local credential values without exposing them to events."""
        return tuple(self.auth_env.values())

    def _sanitize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Sanitize the complete event again at each root/child sink boundary."""
        result = redact_value(event, self._event_secrets())
        return result if isinstance(result, dict) else {}

    async def _send(self, event: dict[str, Any]) -> None:
        """Forward events; durable tool lifecycle commit errors are fatal."""
        try:
            await self._emit(self._sanitize_event(event))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._critical_event_errors:
                from ...harness_service import CriticalHarnessPersistenceError

                if isinstance(exc, CriticalHarnessPersistenceError):
                    raise
            log.warning(
                "claude_engine_emit_failed",
                event_type=event.get("type"),
                error=type(exc).__name__,
            )

    async def _publish(
        self, event: dict[str, Any], *, parent_tool_use_id: str | None = None
    ) -> None:
        """Route nested events without swallowing durable lifecycle failures."""
        if not parent_tool_use_id:
            await self._send(event)
            return
        if self.on_child_event is None:
            return
        try:
            await self.on_child_event(parent_tool_use_id, self._sanitize_event(event))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._critical_event_errors:
                from ...harness_service import CriticalHarnessPersistenceError

                if isinstance(exc, CriticalHarnessPersistenceError):
                    raise
            log.warning("claude_child_event_failed", error=type(exc).__name__)

    @staticmethod
    def _bounded_session_dir(session_id: str) -> str:
        """Return a deterministic, traversal-safe state directory component."""
        candidate = (session_id or "").strip()
        if not candidate:
            raise ValueError("Claude engine requires a harness session id")
        try:
            return str(uuid.UUID(candidate))
        except ValueError:
            digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
            slug = re.sub(r"[^A-Za-z0-9_-]", "-", candidate).strip("-")[:40]
            return f"{slug or 'session'}-{digest}"

    @staticmethod
    def _valid_external_session_id(value: str) -> str:
        """Require Claude external transcript identifiers to be UUIDs."""
        return ClaudeRuntimeState.valid_external_session_id(value)

    @staticmethod
    def _sdk_version() -> str:
        """Return the installed SDK version, rejecting a mismatched runtime."""
        try:
            from claude_agent_sdk import __version__
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("claude-agent-sdk is not installed") from exc
        if __version__ != SDK_VERSION:
            raise RuntimeError(
                "Claude SDK version mismatch: "
                f"expected {SDK_VERSION}, got {__version__}"
            )
        return __version__

    async def _ensure_cli(self) -> str:
        """Provision the pinned Claude executable through the runner boundary."""
        ensure = self._artifact_ensurer or getattr(
            self.accessor, "ensure_runtime_artifact", None
        )
        if not callable(ensure):
            raise RuntimeError("Workspace accessor cannot provision Claude Code")
        try:
            workspace_id = uuid.UUID(str(self.accessor.workspace_id))
        except (ValueError, AttributeError):
            raise RuntimeError(
                "Claude runtime provisioning requires a workspace UUID"
            ) from None
        result = (
            await ensure(workspace_id, "claude-agent", CLI_VERSION)
            if self._artifact_ensurer is not None
            else await ensure("claude-agent", CLI_VERSION)
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Claude Code runtime provisioning failed")
        path = str(result.get("path") or "")
        if path != f"/opt/opencuria/runtimes/claude-agent/{CLI_VERSION}/claude":
            raise RuntimeError("Runner returned an invalid Claude Code runtime path")
        return path

    async def _ensure_managed_dir(self, path: str) -> None:
        """Create a managed directory and reject symlinked path components."""
        await self._runtime_state.ensure_managed_dir(path)

    async def _write_managed_file(self, path: str, payload: bytes) -> None:
        """Write an engine-generated config or prompt file with private mode."""
        await self._runtime_state.write_managed_file(path, payload)

    async def _restore_transcript(
        self, *, external_id: str, cwd: str, config_dir: str
    ) -> None:
        """Restore the authoritative mirror; never reuse missing local state."""
        if not external_id:
            return
        if self.session_store is None:
            raise ClaudeTranscriptError("is missing")
        from claude_agent_sdk._internal.session_store_validation import (
            _store_implements,
        )

        project_key = self._project_key
        if not re.fullmatch(r"[A-Za-z0-9-]{1,255}", project_key):
            raise ValueError("Claude SDK produced an unsafe project key")
        key = {"project_key": project_key, "session_id": external_id}
        entries = await self.session_store.load(key)
        if entries is None or entries == []:
            raise ClaudeTranscriptError("is missing")
        if not isinstance(entries, list):
            raise ValueError("Claude session store returned invalid transcript entries")
        transcript = self._jsonl_bytes(entries)
        project_dir = f"{config_dir}/projects/{project_key}"
        await self._ensure_managed_dir(project_dir)
        await self._write_managed_file(f"{project_dir}/{external_id}.jsonl", transcript)
        if not _store_implements(self.session_store, "list_subkeys"):
            return
        subpaths = await self.session_store.list_subkeys(key)
        if not isinstance(subpaths, list):
            raise ValueError("Claude session store returned invalid subpaths")
        session_dir = f"{project_dir}/{external_id}"
        total = len(transcript)
        for subpath in subpaths:
            if not isinstance(subpath, str) or not self._safe_subpath(subpath):
                continue
            entries = await self.session_store.load({**key, "subpath": subpath})
            if not isinstance(entries, list):
                continue
            metadata = [
                item
                for item in entries
                if isinstance(item, dict) and item.get("type") == "agent_metadata"
            ]
            transcript_entries = [
                item
                for item in entries
                if not (isinstance(item, dict) and item.get("type") == "agent_metadata")
            ]
            sub_transcript = self._jsonl_bytes(transcript_entries)
            total += len(sub_transcript)
            if total > MAX_RESTORED_TRANSCRIPT_BYTES:
                raise ClaudeTranscriptError("exceeds the restore limit")
            relative_dir, basename = subpath.rsplit("/", 1)
            remote_dir = f"{session_dir}/{relative_dir}"
            await self._ensure_managed_dir(remote_dir)
            if transcript_entries:
                await self._write_managed_file(
                    f"{remote_dir}/{basename}.jsonl", sub_transcript
                )
            if metadata:
                clean = {
                    name: value
                    for name, value in metadata[-1].items()
                    if name != "type"
                }
                raw = json.dumps(clean, ensure_ascii=False).encode()
                total += len(raw)
                if total > MAX_RESTORED_TRANSCRIPT_BYTES:
                    raise ClaudeTranscriptError("exceeds the restore limit")
                await self._write_managed_file(
                    f"{remote_dir}/{basename}.meta.json", raw
                )

    @staticmethod
    def _jsonl_bytes(entries: list[Any]) -> bytes:
        if not isinstance(entries, list):
            raise ValueError("Claude session store entries must be a list")
        rows: list[bytes] = []
        total = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Claude session store entry must be an object")
            row = (
                json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode()
                + b"\n"
            )
            total += len(row)
            if total > MAX_RESTORED_TRANSCRIPT_BYTES:
                raise ClaudeTranscriptError("exceeds the restore limit")
            rows.append(row)
        return b"".join(rows)

    @staticmethod
    def _safe_subpath(value: str) -> bool:
        if not value or len(value) > 255 or "\x00" in value:
            return False
        if value.startswith(("/", "\\")) or ":" in value:
            return False
        parts = re.split(r"[\\/]", value)
        return bool(
            parts[0] == "subagents"
            and all(part not in {"", ".", ".."} for part in parts)
            and all(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", part) for part in parts)
        )

    def _cli_environment(self, config_dir: str) -> dict[str, str]:
        self._runtime_state.auth_env = self.auth_env
        return self._runtime_state.cli_environment(config_dir)

    @staticmethod
    def _mcp_cli_config() -> dict[str, Any]:
        """Return the sole SDK server descriptor permitted by this engine."""
        return ClaudeRuntimeState.mcp_cli_config()

    async def _write_skills_plugin(
        self, path: str, skills: list[str]
    ) -> dict[str, str]:
        """Write selected skill bodies as a private, session-scoped Claude plugin."""
        return await self._runtime_state.write_skills_plugin(path, skills)

    def _schemas(
        self, agent: AgentDefinition, mode: str, depth: int, max_depth: int
    ) -> list[dict[str, Any]]:
        """Return only tools executed through the OpenCuria registry."""
        eligible = ToolRegistry()
        for tool in self.tools.list():
            name = (tool.name or "").strip().lower()
            permission_key = (tool.permission_key or name).strip().lower()
            if name == "task" or (agent.name == "explore" and name.startswith("mcp_")):
                continue
            if (
                name in {"question", "todowrite"}
                or name.startswith("process_")
                or name.startswith("mcp_")
                or permission_key.startswith("mcp_")
            ):
                eligible.register(tool)
        schemas = tool_schemas_for_claude(
            tools=eligible,
            agent=agent,
            evaluator=self.evaluator,
            mode=mode,
            depth=depth,
            max_depth=max_depth,
        )
        return schemas

    def _bridge_schemas_for_sdk(
        self, schemas: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Use source names when safe; otherwise preserve registry aliases."""
        registry_names = {tool.name.strip().lower() for tool in self.tools.list()}
        raw_by_name = {
            tool.name.strip().lower(): tool
            for tool in self.tools.list()
            if getattr(tool, "original_name", "")
        }
        original_counts: dict[str, int] = {}
        for tool in raw_by_name.values():
            original = str(tool.original_name).strip().lower()
            original_counts[original] = original_counts.get(original, 0) + 1

        result: list[dict[str, Any]] = []
        for schema in schemas:
            name = str(schema.get("name") or "")
            registered = raw_by_name.get(name.lower())
            original = str(getattr(registered, "original_name", "") or "")
            if (
                original
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", original)
                and original_counts.get(original.lower()) == 1
                and original.lower() not in registry_names
            ):
                schema = {**schema, "name": original}
            result.append(dict(schema))
        return result

    @staticmethod
    def _claude_system_prompt(system: str, mode: str) -> str:
        """Compose OpenCuria run context to append to native Claude Code."""
        return ClaudeRuntimeState.claude_system_prompt(system, mode)

    def _prompt_tools(self, schemas: list[dict[str, Any]]) -> list[ToolSchema]:
        """Describe only OpenCuria MCP tools; native contracts belong to the CLI."""
        return [
            ToolSchema(
                name=f"mcp__opencuria__{item['name']}",
                description=str(item.get("description") or item["name"]),
                parameters=dict(item.get("input_schema") or {}),
            )
            for item in schemas
        ]

    def _sdk_tools(self, schemas: list[dict[str, Any]]) -> list[Any]:
        from claude_agent_sdk import tool as sdk_tool

        definitions = []
        wire_schemas = self._bridge_schemas_for_sdk(schemas)
        self._wire_to_registry = {}
        seen_wire_names: set[str] = set()
        for wire_schema in wire_schemas:
            wire_name = str(wire_schema["name"])
            registry_name = wire_name
            try:
                registry_name = self.tools.get(wire_name).name
            except KeyError:
                sources = [
                    item
                    for item in self.tools.list()
                    if getattr(item, "original_name", "") == wire_name
                ]
                if len(sources) == 1:
                    registry_name = sources[0].name
            normalized_wire_name = wire_name.lower()
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", wire_name)
                or normalized_wire_name in seen_wire_names
            ):
                raise ValueError("OpenCuria tool names are not unique MCP identifiers")
            seen_wire_names.add(normalized_wire_name)
            self._wire_to_registry[wire_name] = registry_name
            definitions.append(
                sdk_tool(
                    wire_name,
                    str(wire_schema.get("description") or wire_name),
                    wire_schema.get("input_schema") or {"type": "object"},
                )(self._mcp_handler(wire_name, registry_name))
            )
        return definitions

    async def _native_pre_tool(
        self,
        call_id: str,
        source_tool: str,
        *,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Record a bounded before-image once before an allowed native edit."""
        if call_id in self._native_file_snapshots:
            return
        raw_arguments = arguments or self._tool_arguments.get(call_id, {})
        snapshot = await self._native_file_change_tracker.capture_before(
            source_tool, raw_arguments
        )
        if snapshot is not None:
            self._native_file_snapshots[call_id] = snapshot

    def _clear_native_snapshot(self, call_id: str) -> None:
        """Discard a pre-edit snapshot after failure or an abandoned call."""
        self._native_file_snapshots.pop(call_id, None)

    async def _native_post_tool(self, call_id: str, state: dict[str, Any]) -> None:
        """Emit the existing patch event for a successful native file change."""
        before = self._native_file_snapshots.pop(call_id, None)
        if before is None:
            return
        after = await self._native_file_change_tracker.capture(before.path)
        if after is None or after.text == before.text:
            return
        change = {
            "path": before.path,
            "unified_diff": self._native_file_change_tracker.diff_text(before, after),
        }
        if not change["unified_diff"]:
            return
        await self._publish(
            {
                "type": "patch",
                "step": state.get("step"),
                "call_id": call_id,
                "tool": state.get("tool", ""),
                "title": f"Patch {change['path']}",
                "path": change["path"],
                "unified_diff": change["unified_diff"],
            },
            parent_tool_use_id=state.get("parent_tool_use_id"),
        )

    async def _ensure_attachment_dir(self, path: str) -> None:
        """Prepare only a contained subdirectory below this run's state root."""
        if (
            not self._managed_root
            or not path.startswith(f"{self._managed_root}/")
            or path == self._managed_root
        ):
            raise ValueError("Claude attachment directory escaped its run root")
        await self._ensure_managed_dir(path)

    def _mcp_handler(
        self, wire_name: str, name: str
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        from mcp.server.lowlevel.server import request_ctx

        async def handle(arguments: dict[str, Any]) -> dict[str, Any]:
            options = self._options
            agent = self._agent
            bridge = self._bridge
            if options is None or agent is None or bridge is None:
                return self._mcp_error("Claude tool call is outside an active run")
            # Tool calls from separate turns may have identical arguments;
            # SDK metadata is the primary correlation mechanism. The FIFO
            # fallback is safe only when the SDK omits that correlation key.
            try:
                metadata = request_ctx.get().meta
            except LookupError:
                metadata = None
            extra = metadata.model_extra or {} if metadata is not None else {}
            nested = extra.get("claudecode") or extra.get("claudeCode") or {}
            nested = nested if isinstance(nested, dict) else {}
            preferred_id = str(
                extra.get("tool_use_id")
                or extra.get("claudecode/toolUseId")
                or nested.get("toolUseId")
                or nested.get("tool_use_id")
                or ""
            )
            # A supplied identity is authoritative. An invalid, duplicated, or
            # mismatched identity must never consume another call via FIFO.
            call_id = await bridge.take_mcp_call(
                name, arguments, preferred_call_id=preferred_id
            )
            if not call_id:
                return self._mcp_error("Tool call was not approved by OpenCuria")
            state = self._tool_events.get(call_id)
            if state is None:
                return self._mcp_error("Authorized tool call is no longer active")
            if state.get("permission_decision") != "allow":
                bridge.discard_mcp_call(call_id)
                await self._finish_tool_error(
                    call_id,
                    "MCP tool permission requires OpenCuria approval",
                    parent_id=state.get("parent_tool_use_id"),
                )
                return self._mcp_error(
                    "MCP tool permission requires OpenCuria approval"
                )
            try:
                tool_agent = get_agent(str(state.get("agent_name") or agent.name))
            except KeyError:
                tool_agent = agent
            context = ToolContext(
                session_id=options.session_id or "session",
                workspace_id=options.workspace_id or self.accessor.workspace_id,
                accessor=self.accessor,
                agent_name=tool_agent.name,
                directory=self._cwd,
                depth=int(state.get("depth", options.depth)),
                max_depth=options.max_depth or DEFAULT_MAX_DEPTH,
                model=self._model,
                parent_emit=self._send,
                registry=self.tools,
                evaluator=self.evaluator,
                run_subagent=options.run_subagent,
                call_id=call_id,
                on_question=options.on_question,
                question_timeout=options.question_timeout,
            )
            try:
                result = await self.tools.execute(name, arguments, context)
            except asyncio.CancelledError:
                self._tool_results.pop(call_id, None)
                self._bridge.discard_mcp_call(call_id)
                await self._finish_tool_error(
                    call_id,
                    "Tool execution was interrupted",
                    parent_id=state.get("parent_tool_use_id"),
                )
                raise
            except Exception as exc:
                if self._critical_event_errors:
                    from ...harness_service import CriticalHarnessPersistenceError

                    if isinstance(exc, CriticalHarnessPersistenceError):
                        raise
                message = "OpenCuria tool execution failed"
                result = ToolResult(output=message, metadata={"is_error": True})
                self._tool_results[call_id] = result
                return {
                    "content": [{"type": "text", "text": message}],
                    "is_error": True,
                }
            self._tool_results[call_id] = result
            state["tool_result_metadata"] = dict(result.metadata)
            if result.metadata.get("is_error"):
                self._tool_results[call_id] = result
                return {
                    "content": [{"type": "text", "text": result.output[:4000]}],
                    "is_error": True,
                }
            content = []
            if result.output:
                content.append({"type": "text", "text": result.output})
            try:
                content.extend(
                    await mcp_attachment_content(
                        result.attachments,
                        accessor=self.accessor,
                        managed_root=self._managed_root,
                        ensure_directory=self._ensure_attachment_dir,
                    )
                )
            except (ValueError, RuntimeError):
                return self._mcp_error("MCP media attachment was invalid or oversized")
            return {
                "content": content or [{"type": "text", "text": "Tool completed."}],
                "is_error": False,
            }

        return handle

    def _safe_error_text(self, error: Any, *, limit: int = 4000) -> str:
        """Return bounded, redacted error text safe for UI/model events."""
        from ...mcp_client.stdio import sanitize_stderr_excerpt

        text = redact_text(str(error or "Claude run failed"))
        for secret in self.auth_env.values():
            if isinstance(secret, str) and len(secret) >= 4:
                text = text.replace(secret, "[redacted]")
        text = sanitize_stderr_excerpt(text.encode("utf-8", errors="replace"))
        return text[:limit] or "Claude run failed"

    def _mcp_error(self, message: str) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": self._safe_error_text(message)}],
            "is_error": True,
        }

    async def _agent_definitions(self) -> dict[str, Any]:
        """Give SDK children their own scoped tools and the project/run context."""
        from claude_agent_sdk import AgentDefinition as SdkAgentDefinition

        from ...agents.definitions import list_agents

        definitions = {}
        options = self._options or RunOptions()
        for child in list_agents(mode="subagent"):
            if child.name == "computeruse":
                continue
            depth = options.depth + 1
            native = native_tools_for_claude(
                tools=self.tools,
                agent=child,
                evaluator=self.evaluator,
                mode=self._mode,
                depth=depth,
                max_depth=options.max_depth,
                selected_skills=sorted(self._managed_skill_names),
            )
            schemas = self._bridge_schemas_for_sdk(
                self._schemas(child, self._mode, depth, options.max_depth)
            )
            prompt = await compose_system_prompt(
                agent=child,
                mode=self._mode,
                accessor=self.accessor,
                cwd=self._cwd,
                tools=self._prompt_tools(schemas),
                native_tool_contracts=True,
                skills=list(options.skills) if child.name != "explore" else [],
                subagents={},
            )
            system = (
                self._claude_system_prompt(prompt.system, self._mode)
                if child.name != "explore"
                else prompt.system
            )
            if len(system.encode()) > MAX_MANAGED_PROMPT_BYTES:
                raise ValueError("Claude child prompt exceeds the managed size limit")
            definitions[child.name] = SdkAgentDefinition(
                description=child.description,
                prompt=system,
                tools=native + [item.name for item in self._prompt_tools(schemas)],
                mcpServers=["opencuria"] if schemas else [],
                permissionMode="default",
                model="inherit",
            )
        return definitions

    @staticmethod
    def _sdk_system_prompt() -> dict[str, Any]:
        """Retain Claude Code's native preset while SDK applies its options."""
        return ClaudeRuntimeState.sdk_system_prompt()

    @staticmethod
    def _settings(mode: str) -> dict[str, Any]:
        """Configure native features without bypassing permission hooks."""
        return ClaudeRuntimeState.settings(mode)

    def _build_command(
        self,
        *,
        binary: str,
        config_dir: str,
        mcp_path: str,
        prompt_path: str,
        model: str,
        effort: str,
        permission_mode: str,
        native_tools: list[str],
        external_session_id: str,
        fresh_session_id: str,
        has_session_store: bool,
        plugin_dir: str | None = None,
        settings_path: str | None = None,
    ) -> list[str]:
        return self._runtime_state.build_command(
            binary=binary,
            config_dir=config_dir,
            mcp_path=mcp_path,
            prompt_path=prompt_path,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            native_tools=native_tools,
            external_session_id=external_session_id,
            fresh_session_id=fresh_session_id,
            has_session_store=has_session_store,
            plugin_dir=plugin_dir,
            settings_path=settings_path,
        )

    def _permission_hooks(self) -> dict[str, list[Any]]:
        """Delegate SDK permission callbacks to the policy bridge."""
        if self._bridge is None:
            raise RuntimeError("Claude permission bridge is not initialized")
        return self._bridge.hooks(self)

    @staticmethod
    def _normalize_agent_type(value: str) -> str:
        """Map Claude task-type labels onto supported OpenCuria agents."""
        name = (value or "").strip().lower().replace("_", "-")
        return "explore" if name in {"explore", "explorer"} else "general"

    @staticmethod
    def _command_may_mutate(command: str) -> bool:
        """Compatibility helper for conservative shell mutation classification."""
        from .shellpolicy import shell_command_may_mutate

        return shell_command_may_mutate(command)

    @staticmethod
    def _event_arguments(source_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Normalize Claude native file arguments for harness events."""
        result = dict(arguments)
        if source_name in {"Read", "Write", "Edit", "NotebookEdit"}:
            path = result.pop("file_path", None) or result.pop("notebook_path", None)
            if path is not None:
                result["path"] = path
        return result

    async def _start_new_step(self, parent_tool_use_id: str | None = None) -> int:
        scope = parent_tool_use_id or ""
        state = self._step_states.setdefault(scope, StepState())
        if state.started:
            if state.active_calls or not state.awaiting_next_step:
                return state.step
            if state.current_message_id:
                return state.step
            await self._finish_step(scope)
        state.step += 1
        state.started = True
        state.awaiting_next_step = False
        if not parent_tool_use_id:
            self._steps = max(self._steps, state.step)
        await self._publish(
            {"type": "step_start", "step": state.step},
            parent_tool_use_id=parent_tool_use_id,
        )
        return state.step

    async def _finish_step(
        self,
        scope: str,
        usage: Usage | None = None,
        *,
        flush_redactors: bool = True,
    ) -> None:
        state = self._step_states.get(scope)
        if state is None or not state.started:
            return
        if flush_redactors:
            await self._flush_stream_redactors(parent=scope)
        if usage is None:
            usage = state.usage_by_message.get(state.current_message_id, Usage())
        finished_step = state.step
        finished_usage = usage
        # Commit locally before yielding to the observer: cancellation during an
        # emit must not make cleanup publish a second finish for this scope.
        state.started = False
        state.awaiting_next_step = False
        state.finished_usage = state.finished_usage.merge(finished_usage)
        await self._publish(
            {
                "type": "step_finish",
                "step": finished_step,
                "tokens": {
                    "prompt_tokens": finished_usage.prompt_tokens,
                    "completion_tokens": finished_usage.completion_tokens,
                    "total_tokens": finished_usage.total_tokens,
                },
                "cost": finished_usage.cost,
            },
            parent_tool_use_id=scope or None,
        )

    def _record_message_usage(
        self, state: StepState, message_id: str, raw: Any
    ) -> None:
        """Keep the latest complete snapshot for an API assistant message."""
        snapshot = (
            raw if isinstance(raw, Usage) else ClaudeEventNormalizer.message_usage(raw)
        )
        if snapshot is None or not message_id:
            return
        previous = state.usage_by_message.get(message_id, Usage())
        prompt_tokens = max(previous.prompt_tokens, snapshot.prompt_tokens)
        completion_tokens = max(previous.completion_tokens, snapshot.completion_tokens)
        state.usage_by_message[message_id] = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost=max(previous.cost, snapshot.cost),
        )

    async def _begin_message_turn(self, parent: str, message_id: str) -> StepState:
        """Give each API message its own step within its root/child scope."""
        state = self._step_states.setdefault(parent, StepState())
        if message_id and state.current_message_id != message_id:
            if state.current_message_id and state.started:
                await self._finish_step(parent)
            state.current_message_id = message_id
        if not state.started:
            await self._start_new_step(parent or None)
        return state

    async def _start_tool(self, call_id: str, *, parent_id: str | None) -> None:
        state = self._tool_events.get(call_id)
        if state is None or state.get("started"):
            return
        await self._flush_stream_redactors(parent=parent_id or "")
        state["started"] = True
        self._step_states.setdefault(parent_id or "", StepState()).active_calls.add(
            call_id
        )
        await self._publish(
            {
                "type": "tool_started",
                "step": state["step"],
                "call_id": call_id,
                "tool": state["tool"],
                "title": state["title"],
                "arguments": json.dumps(state["event_arguments"], ensure_ascii=False),
            },
            parent_tool_use_id=parent_id,
        )

    async def _finish_tool_completed(
        self,
        call_id: str,
        output: str,
        *,
        parent_id: str | None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        state = self._tool_events.pop(call_id, None)
        self._early_queued_calls.discard(call_id)
        self._tool_arguments.pop(call_id, None)
        tool_result = self._tool_results.pop(call_id, None)
        if state is None:
            self._native_file_snapshots.pop(call_id, None)
            return
        await self._flush_stream_redactors(parent=parent_id or "")
        output = redact_text(output, self._event_secrets())
        scope = str(parent_id or state.get("parent_tool_use_id") or "")
        step = self._step_states.setdefault(scope, StepState())
        step.active_calls.discard(call_id)
        if step.started and not step.active_calls:
            step.awaiting_next_step = True
        safe_attachments = sanitize_mcp_attachments(attachments)
        await self._publish(
            {
                "type": "tool_completed",
                "step": state["step"],
                "call_id": call_id,
                "tool": state["tool"],
                "output": output[:100000],
                "attachments": safe_attachments,
            },
            parent_tool_use_id=parent_id,
        )
        if state.get("source_tool") in {"Write", "Edit"}:
            await self._native_post_tool(call_id, state)
        if state.get("tool") in {"write", "edit"}:
            metadata = state.get("tool_result_metadata")
            if not isinstance(metadata, dict) and tool_result is not None:
                metadata = tool_result.metadata
            patch = metadata.get("unified_diff") if isinstance(metadata, dict) else None
            path = metadata.get("path") if isinstance(metadata, dict) else None
            if isinstance(patch, str) and patch and isinstance(path, str):
                await self._publish(
                    {
                        "type": "patch",
                        "step": state.get("step"),
                        "call_id": call_id,
                        "tool": state.get("tool", ""),
                        "title": f"Patch {path}",
                        "path": path,
                        "unified_diff": patch[: 128 * 1024],
                    },
                    parent_tool_use_id=state.get("parent_tool_use_id"),
                )

    async def _finish_tool_error(
        self, call_id: str, error: str, *, parent_id: str | None
    ) -> None:
        state = self._tool_events.pop(call_id, None)
        self._early_queued_calls.discard(call_id)
        self._tool_arguments.pop(call_id, None)
        self._tool_results.pop(call_id, None)
        if state is None:
            self._clear_native_snapshot(call_id)
            return
        self._clear_native_snapshot(call_id)
        await self._flush_stream_redactors(parent=parent_id or "")
        error = redact_text(error, self._event_secrets())
        scope = str(parent_id or state.get("parent_tool_use_id") or "")
        step = self._step_states.setdefault(scope, StepState())
        step.active_calls.discard(call_id)
        if step.started and not step.active_calls:
            step.awaiting_next_step = True
        await self._publish(
            {
                "type": "tool_error",
                "step": state["step"],
                "call_id": call_id,
                "tool": state["tool"],
                "error": str(error)[:4000],
            },
            parent_tool_use_id=parent_id,
        )

    async def _finish_subtask_from_tool(
        self, call_id: str, output: str, status: str
    ) -> None:
        task_id = self._subtask_by_tool.get(call_id, "")
        if task_id:
            await self._finish_sdk_subtask(task_id, call_id, status, output)

    async def _prompt_with_history(self, prompt: str) -> str | list[dict[str, Any]]:
        """Hydrate current prompt and non-resumed history into Claude blocks."""
        options = self._options
        return await build_user_content(
            prompt,
            accessor=self.accessor,
            history=options.history if options is not None else None,
            include_history=not bool(self.external_session_id),
            authorize_read=(
                options.authorize_file_read
                if options is not None and options.authorize_file_read is not None
                else self._authorize_media_read
            ),
        )

    async def _authorize_media_read(self, path: str) -> bool:
        """Apply the native Read policy to explicit user attachments as well."""
        if self._bridge is None or self._options is None:
            return False
        decision, tool, action, title = self._bridge.decide(
            "Read", {"file_path": path}, depth=self._options.depth
        )
        if decision == "deny":
            return False
        if decision == "allow":
            return True
        if self._options.on_permission is None:
            return False
        response = await self._options.on_permission(
            tool=tool,
            action=action,
            title=title,
            call_id=f"media-{uuid.uuid4()}",
            key="permission_read",
        )
        return response in {"once", "always", "allow", "approved", "yes"}

    async def _send_todos(self, parent_tool_use_id: str | None = None) -> None:
        try:
            sid = self._options.session_id if self._options else ""
            repo = repository_for_session(sid)
            stored = await sync_to_async(repo.list)(sid)
            todos = [
                {
                    "content": x.content,
                    "status": x.status,
                    "priority": x.priority,
                    "order": x.order,
                }
                for x in stored.items
            ]
        except Exception as exc:
            log.warning("claude_todos_snapshot_failed", error=type(exc).__name__)
            todos = []
        await self._publish(
            {"type": "todo_updated", "todos": todos},
            parent_tool_use_id=parent_tool_use_id,
        )

    def _stream_key(
        self, parent: str, session_id: str, index: int, slot: str
    ) -> tuple[str, str, int, str]:
        """Create a stable key for one stream content block."""
        return (parent, session_id, index, slot)

    async def _emit_stream_text(
        self, *, parent: str, state: StepState, field_name: str, text: str
    ) -> None:
        """Accumulate and emit only the already-redacted stream prefix."""
        if not text:
            return
        (state.text if field_name == "text" else state.reasoning).append(text)
        if parent:
            if field_name == "text":
                self._child_text[parent] = self._child_text.get(parent, "") + text
        elif field_name == "text":
            self._root_text.append(text)
        else:
            self._root_reasoning.append(text)
        await self._publish(
            {"type": "part_updated", "step": state.step, "delta": {field_name: text}},
            parent_tool_use_id=parent or None,
        )

    async def _flush_stream_redactor(self, key: str) -> None:
        """Flush one completed text/thinking block without losing event order."""
        redactor = self._stream_redactors.pop(key, None)
        if redactor is None:
            return
        parent, session, index, field_name = key
        state = self._step_states.setdefault(parent, StepState())
        if not state.started:
            await self._start_new_step(parent or None)
        await self._emit_stream_text(
            parent=parent,
            state=state,
            field_name=field_name,
            text=redactor.finish(),
        )
        self._pending_stream_text.pop(key, None)

    async def _flush_stream_redactors(self, *, parent: str | None = None) -> None:
        """Flush content-block tails before tools, step finishes, or final output."""
        for key in list(self._stream_redactors):
            key_parent = key[0]
            if parent is None or key_parent == parent:
                await self._flush_stream_redactor(key)

    async def _queue_stream_tool(
        self, *, parent: str, call_id: str, source_name: str
    ) -> None:
        """Project a known tool at its observed stream position, before policy."""
        if not call_id or call_id in self._early_queued_calls:
            return
        registry_name = ""
        if source_name in NATIVE_TOOL_KEYS:
            key = NATIVE_TOOL_KEYS[source_name]
            try:
                registry_name = self.tools.get(key).name
            except KeyError:
                return
        else:
            # MCP calls queue once the permission hook supplies exact input;
            # native CLI tools are observable here before arguments arrive.
            return

        await self._flush_stream_redactors(parent=parent)
        state = self._step_states.setdefault(parent, StepState())
        if not state.started:
            step = await self._start_new_step(parent or None)
        else:
            state.awaiting_next_step = False
            step = state.step
        self._early_queued_calls.add(call_id)
        await self._publish(
            {
                "type": "tool_queued",
                "step": step,
                "call_id": call_id,
                "tool": registry_name,
                "title": source_name,
                "arguments": "",
                "metadata": {
                    "source_tool": source_name,
                    "registry_tool": registry_name,
                },
            },
            parent_tool_use_id=parent or None,
        )

    async def _emit_partial(self, message: Any) -> None:
        """Normalize stream boundaries and emit safe partial text/tool events."""
        raw = ClaudeEventNormalizer.stream_event(message)
        if not raw:
            return
        parent = ClaudeEventNormalizer.stream_parent(message)
        state = self._step_states.setdefault(parent, StepState())
        event_type = str(raw.get("type") or "")

        if event_type == "message_start":
            api_message_id = ClaudeEventNormalizer.stream_message_id(message)
            if api_message_id and api_message_id != state.current_message_id:
                await self._flush_stream_redactors(parent=parent)
                state = await self._begin_message_turn(parent, api_message_id)
            snapshot = ClaudeEventNormalizer.stream_usage(message)
            if snapshot is not None:
                self._record_message_usage(state, *snapshot)
            return

        snapshot = ClaudeEventNormalizer.stream_usage(message)
        if snapshot is not None:
            message_id, message_usage = snapshot
            if not message_id:
                message_id = state.current_message_id
            if message_id:
                self._record_message_usage(
                    state,
                    message_id,
                    {
                        "input_tokens": message_usage.prompt_tokens,
                        "output_tokens": message_usage.completion_tokens,
                    },
                )

        if event_type == "content_block_start":
            message_id = (
                state.current_message_id
                or ClaudeEventNormalizer.stream_fallback_id(message)
            )
            if message_id and message_id != state.current_message_id:
                state = await self._begin_message_turn(parent, message_id)
            block = ClaudeEventNormalizer.stream_block_start(message)
            if block is None:
                return
            index, kind, tool_name, call_id = block
            message_id = (
                state.current_message_id
                or ClaudeEventNormalizer.stream_fallback_id(message)
            )
            if message_id:
                state.block_types[(message_id, index)] = kind
                order = state.block_order.setdefault(message_id, [])
                if (index, kind) not in order:
                    order.append((index, kind))
            if kind == "tool_use":
                await self._queue_stream_tool(
                    parent=parent, call_id=call_id, source_name=tool_name
                )
            return

        if event_type == "content_block_stop":
            index = ClaudeEventNormalizer.stream_block_stop(message)
            if index is None:
                return
            message_id = (
                state.current_message_id
                or ClaudeEventNormalizer.stream_fallback_id(message)
            )
            kind = state.block_types.get((message_id, index), "")
            if kind in {"text", "thinking"}:
                field_name = "text" if kind == "text" else "reasoning"
                key = self._stream_key(parent, message_id, index, field_name)
                await self._flush_stream_redactor(key)
            slot = "text" if kind == "text" else "thinking"
            if (
                message_id
                and kind in {"text", "thinking"}
                and (message_id, index, slot) in state.streamed
            ):
                state.completed_blocks.add((message_id, index, kind))
            return

        parsed = ClaudeEventNormalizer.partial_delta(message)
        if parsed is None:
            return
        parent, index, slot, field_name, text, fallback_id = parsed
        if not text:
            return
        message_id = state.current_message_id or fallback_id
        if not message_id:
            # SDK stream UUID is message-unique; session_id is not.
            message_id = str(getattr(message, "uuid", "") or "")
        if not message_id:
            return
        if not state.started:
            await self._start_new_step(parent or None)
        if slot == "text":
            state.current_message_id = message_id
        block_kind = "text" if slot == "text" else "thinking"
        state.block_types.setdefault((message_id, index), block_kind)
        order = state.block_order.setdefault(message_id, [])
        if (index, block_kind) not in order:
            order.append((index, block_kind))
        streamed_key = (message_id, index, slot)
        state.streamed[streamed_key] = state.streamed.get(streamed_key, "") + text
        key = self._stream_key(parent, message_id, index, field_name)
        redactor = self._stream_redactors.setdefault(
            key, StreamingRedactor(self._event_secrets())
        )
        previous_pending = redactor.pending_text
        previous_combined = self._pending_stream_text.get(key, "")
        visible_prefix = (
            previous_combined[: -len(previous_pending)]
            if previous_pending and previous_combined.endswith(previous_pending)
            else previous_combined
        )
        emitted = redactor.push(text)
        self._pending_stream_text[key] = (
            visible_prefix + emitted + redactor.pending_text
        )
        await self._emit_stream_text(
            parent=parent, state=state, field_name=field_name, text=emitted
        )

    async def _emit_assistant(self, message: Any) -> None:
        """Emit only content not already streamed for this API message."""
        parent, blocks = ClaudeEventNormalizer.assistant_blocks(message)
        state = self._step_states.setdefault(parent, StepState())
        message_id = ClaudeEventNormalizer.assistant_message_id(message)
        if message_id and message_id in state.completed_messages:
            return
        if message_id:
            state = await self._begin_message_turn(parent, message_id)
        elif not state.started:
            await self._start_new_step(parent or None)
        state.current_message_id = message_id or state.current_message_id
        raw_usage = getattr(message, "usage", None)
        self._record_message_usage(state, message_id, raw_usage)
        stream_state = {
            (index, slot): value
            for (api_id, index, slot), value in state.streamed.items()
            if api_id == message_id
        }
        _, deltas = ClaudeEventNormalizer.assistant_deltas(message, stream_state)
        delta_by_block = {
            (index, slot): (field_name, text)
            for index, slot, field_name, text in deltas
        }
        blocks_by_index = {index: block for index, block in enumerate(blocks)}
        ordered_blocks = state.block_order.get(message_id, [])
        if ordered_blocks:
            seen_indices = {index for index, _kind in ordered_blocks}
            ordered_blocks = [
                *ordered_blocks,
                *[
                    (index, ClaudeEventNormalizer.block_type(block))
                    for index, block in enumerate(blocks)
                    if index not in seen_indices
                ],
            ]
        else:
            ordered_blocks = [
                (index, ClaudeEventNormalizer.block_type(block))
                for index, block in enumerate(blocks)
            ]

        for index, kind in ordered_blocks:
            block = blocks_by_index.get(index)
            if block is None:
                continue
            kind = ClaudeEventNormalizer.block_type(block)
            if kind not in {"text", "thinking"}:
                if kind == "tool_use" and message_id:
                    state.completed_blocks.add((message_id, index, kind))
                continue
            if message_id and (message_id, index, kind) in state.completed_blocks:
                continue
            slot = "text" if kind == "text" else "thinking"
            field_name = "text" if kind == "text" else "reasoning"
            full_text = ClaudeEventNormalizer.content_text(block)
            if not full_text:
                continue
            key = self._stream_key(parent, message_id, index, field_name)
            streamed_key = (message_id, index, slot)
            accumulated = state.streamed.get(streamed_key, "")
            redactor = self._stream_redactors.get(key)
            if redactor is not None and full_text.startswith(accumulated):
                suffix = full_text[len(accumulated) :]
                prior_pending = redactor.pending_text
                prior_combined = self._pending_stream_text.get(key, "")
                visible_prefix = (
                    prior_combined[: -len(prior_pending)]
                    if prior_pending and prior_combined.endswith(prior_pending)
                    else prior_combined
                )
                combined = redactor.push(suffix, final=True)
                full_safe = (
                    combined[len(visible_prefix) :]
                    if combined.startswith(visible_prefix)
                    else combined
                )
                self._stream_redactors.pop(key, None)
                self._pending_stream_text.pop(key, None)
                if full_safe:
                    await self._emit_stream_text(
                        parent=parent,
                        state=state,
                        field_name=field_name,
                        text=full_safe,
                    )
            elif redactor is not None:
                await self._flush_stream_redactor(key)
                text = delta_by_block.get((index, slot), (field_name, full_text))[1]
                if text:
                    await self._emit_stream_text(
                        parent=parent,
                        state=state,
                        field_name=field_name,
                        text=redact_text(text, self._event_secrets()),
                    )
            elif not accumulated:
                await self._emit_stream_text(
                    parent=parent,
                    state=state,
                    field_name=field_name,
                    text=redact_text(full_text, self._event_secrets()),
                )
            elif not full_text.startswith(accumulated):
                text = delta_by_block.get((index, slot), (field_name, full_text))[1]
                if text:
                    await self._emit_stream_text(
                        parent=parent,
                        state=state,
                        field_name=field_name,
                        text=redact_text(text, self._event_secrets()),
                    )
            state.streamed[streamed_key] = full_text
            if message_id:
                state.completed_blocks.add((message_id, index, kind))

        if message_id:
            state.completed_messages.add(message_id)
            if len(state.completed_messages) > 2048:
                state.completed_messages.pop()

    async def _can_use_tool(
        self, source_name: str, arguments: dict[str, Any], context: Any
    ) -> Any:
        if self._bridge is None:
            from claude_agent_sdk import PermissionResultDeny

            return PermissionResultDeny(message="No active OpenCuria permission bridge")
        return await self._bridge.can_use_tool(self, source_name, arguments, context)

    @staticmethod
    def _result_usage(result):
        """Compatibility facade for consumers of the former engine helper."""
        return ClaudeEventNormalizer.result_usage(result)

    @staticmethod
    def _tool_output(value):
        """Compatibility facade for SDK-native tool-output normalization."""
        return ClaudeEventNormalizer.tool_output(value)

    async def _handle_message(self, message: Any) -> tuple[Any, str, Usage] | None:
        return await self._message_projector.handle_message(message)

    async def _handle_user_tool_results(self, message: Any) -> None:
        await self._message_projector.handle_user_tool_results(message)

    async def _handle_task_progress(self, message: Any) -> None:
        await self._message_projector.handle_task_progress(message)

    async def _handle_task_notification(self, message: Any) -> None:
        await self._message_projector.handle_task_notification(message)

    async def _handle_task_updated(self, message: Any) -> None:
        await self._message_projector.handle_task_updated(message)

    async def _finish_sdk_subtask(
        self, task: str, tool: str, status: str, summary: str
    ) -> None:
        await self._message_projector.finish_sdk_subtask(task, tool, status, summary)

    async def _revise_subtask_terminal_status(
        self, task: str, tool: str, status: str
    ) -> None:
        await self._message_projector.revise_subtask_terminal_status(task, tool, status)

    async def _bind_external_session(self, external_id: str) -> None:
        await self._message_projector.bind_external_session(external_id)

    async def _notify_owner(self, lease: Any, phase: str) -> None:
        """Publish fenced runner lease ownership to the run owner."""
        if self.on_owner is None:
            return
        await self.on_owner(
            {**lease.owner, "owner_id": lease.owner_id},
            phase,
        )

    async def run(
        self,
        prompt: str,
        agent_name: str,
        model: str,
        mode: str = "build",
        opts: RunOptions | None = None,
    ) -> RunResult:
        """Run one complete Claude session through the scoped workspace CLI."""
        if mode not in {"plan", "build"}:
            raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        options = opts or RunOptions()
        skills = validate_claude_skills(options.skills)
        options.once_approved = set()
        agent = get_agent(agent_name)
        if agent.name == "computeruse":
            raise ValueError("Claude engine does not run Agent-S computeruse agents")
        if agent.mode == "hidden":
            raise ValueError("Claude engine cannot run hidden helper agents")
        depth = max(0, int(options.depth or 0))
        max_depth = options.max_depth if options.max_depth > 0 else DEFAULT_MAX_DEPTH
        if depth > max_depth:
            raise ValueError(f"depth {depth} exceeds max_depth {max_depth}")
        self._options = options
        self._agent = agent
        self._mode = mode
        self._model = resolve_claude_sdk_model(model)
        self._cwd = sanitize_exec_workdir(options.cwd or HARNESS_WORKSPACE_ROOT)
        try:
            from claude_agent_sdk import project_key_for_directory
        except ImportError as exc:
            raise RuntimeError("claude-agent-sdk is not installed") from exc
        self._project_key = project_key_for_directory(self._cwd)
        if not re.fullmatch(r"[A-Za-z0-9-]{1,255}", self._project_key):
            raise ValueError("Claude SDK produced an unsafe project key")
        bind_project_key = getattr(self.session_store, "bind_project_key", None)
        if callable(bind_project_key):
            bind_project_key(self._project_key)
        self._steps = 0
        self._step_states = {"": StepState()}
        self._tool_events.clear()
        self._stream_redactors.clear()
        self._pending_stream_text.clear()
        self._early_queued_calls.clear()
        self._wire_to_registry.clear()
        self._tool_results.clear()
        self._tool_arguments.clear()
        self._subtask_by_tool.clear()
        self._agent_parent_tool.clear()
        self._agent_depth.clear()
        self._agent_kind.clear()
        self._agent_type_raw.clear()
        self._started_subtasks.clear()
        self._finished_subtasks.clear()
        self._subtask_terminal_status.clear()
        self._subtask_terminal_summary.clear()
        self._stopped_subagents.clear()
        self._pending_task_tool_ids.clear()
        self._root_text.clear()
        self._root_reasoning.clear()
        self._child_text.clear()
        self._bound_session_ids.clear()
        self._owner_released = False
        self._bridge = PermissionBridge(
            tools=self.tools,
            evaluator=self.evaluator,
            options=options,
            agent=agent,
            mode=mode,
            max_depth=max_depth,
            engine=self,
        )
        transcript = (
            self._valid_external_session_id(self.external_session_id)
            if self.external_session_id
            else ""
        )
        directory = self._bounded_session_dir(options.session_id)
        root = f"{MANAGED_STATE_PREFIX}{directory}"
        self._managed_root = root
        self._native_file_snapshots.clear()
        config = f"{root}/config"
        prompt_file = f"{root}/system-prompt.md"
        if self.external_session_id:
            resume_key = hashlib.sha256(self.external_session_id.encode()).hexdigest()[
                :16
            ]
            config += f"/resume-{resume_key}"
        await self._ensure_managed_dir(config)
        await self._ensure_managed_dir(root)
        self._sdk_version()
        binary = await self._ensure_cli()
        await self._restore_transcript(
            external_id=self.external_session_id, cwd=self._cwd, config_dir=config
        )
        schemas = self._schemas(agent, mode, depth, max_depth)
        sdk_schemas = self._bridge_schemas_for_sdk(schemas)
        skill_names = await self._write_skills_plugin(root, skills)
        self._managed_skill_names = set(skill_names)
        if agent.name == "explore":
            self._managed_skill_names.clear()
        composed = await compose_system_prompt(
            agent=agent,
            mode=mode,
            tools=self._prompt_tools(sdk_schemas),
            native_tool_contracts=True,
            subagents={},
            accessor=self.accessor,
            cwd=self._cwd,
            skills=skills,
        )
        system = (
            self._claude_system_prompt(composed.system, mode)
            if agent.name != "explore"
            else composed.system
        )
        if len(system.encode()) > MAX_MANAGED_PROMPT_BYTES:
            raise ValueError(
                "Composed Claude system prompt exceeds the managed size limit"
            )
        await self._write_managed_file(prompt_file, system.encode())
        effort = normalize_claude_effort(
            getattr(options, "reasoning_effort", "")
            or getattr(options, "claude_effort", "")
            or self._default_effort
        )
        settings = self._settings(mode)
        settings_path = f"{root}/settings.json"
        await self._write_managed_file(
            settings_path, json.dumps(settings, separators=(",", ":")).encode()
        )
        # The SDK's mirror batcher requires paths under /workspace even though
        # the SDK itself launches the CLI with no forwarded options on custom
        # transports. Set the mirrored CLI config root explicitly there.
        if not transcript:
            transcript = str(uuid.uuid4())
        argv = self._build_command(
            binary=binary,
            config_dir=config,
            mcp_path="",
            prompt_path=prompt_file,
            model=self._model,
            effort=effort,
            permission_mode="plan" if mode == "plan" else "default",
            native_tools=native_tools_for_claude(
                tools=self.tools,
                agent=agent,
                evaluator=self.evaluator,
                mode=mode,
                depth=depth,
                max_depth=max_depth,
                selected_skills=list(self._managed_skill_names),
            ),
            external_session_id=self.external_session_id,
            fresh_session_id=transcript,
            has_session_store=self.session_store is not None,
            plugin_dir=(
                f"{root}/opencuria-skills" if self._managed_skill_names else None
            ),
            settings_path=settings_path,
        )
        try:
            from claude_agent_sdk import (
                ClaudeAgentOptions,
                ClaudeSDKClient,
                ResultError,
                create_sdk_mcp_server,
            )
        except ImportError as exc:
            raise RuntimeError("claude-agent-sdk is not installed") from exc
        sdk_server = create_sdk_mcp_server(
            "opencuria", version="1.0.0", tools=self._sdk_tools(sdk_schemas)
        )
        native_cli_tools = native_tools_for_claude(
            tools=self.tools,
            agent=agent,
            evaluator=self.evaluator,
            mode=mode,
            depth=depth,
            max_depth=max_depth,
            selected_skills=list(self._managed_skill_names),
        )
        mirror_env = self._cli_environment(config)
        max_turns = options.max_steps or agent.steps
        if max_turns is not None:
            argv.extend(["--max-turns", str(max_turns)])
        sdk_options = ClaudeAgentOptions(
            cwd=self._cwd,
            env=mirror_env,
            can_use_tool=self._can_use_tool,
            hooks=self._permission_hooks(),
            mcp_servers={"opencuria": sdk_server},
            session_store=self.session_store,
            session_store_flush="eager",
            resume=self.external_session_id or None,
            session_id=transcript if not self.external_session_id else None,
            include_partial_messages=True,
            include_hook_events=True,
            forward_subagent_text=True,
            verbatim_prompts=True,
            setting_sources=[],
            strict_mcp_config=True,
            settings=settings_path,
            model=self._model,
            effort=effort,
            max_turns=max_turns,
            permission_mode="plan" if mode == "plan" else "default",
            system_prompt=self._sdk_system_prompt(),
            tools=native_cli_tools,
            agents=await self._agent_definitions(),
            skills=(
                sorted(self._managed_skill_names) if self._managed_skill_names else None
            ),
        )
        lease = None
        client = None
        transport = None
        run_error: BaseException | None = None
        close_error: BaseException | None = None
        owner_released = False
        try:
            from ...desktop_leases import DesktopLease

            owner_task = asyncio.current_task()
            lease = DesktopLease(
                self.accessor,
                kind="agent",
                owner_id=directory,
                on_failure=owner_task.cancel if owner_task else None,
            )
            await lease.reserve()
            await self._notify_owner(lease, "reserved")
            transport = ClaudeWorkspaceTransport(
                self.accessor,
                argv,
                cwd=self._cwd,
                env=self._cli_environment(config),
                owner=lease.owner,
            )
            client = ClaudeSDKClient(options=sdk_options, transport=transport)

            async def inputs():
                user_content = await self._prompt_with_history(prompt)
                yield {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": user_content,
                    },
                    "parent_tool_use_id": None,
                    "session_id": "default",
                    "origin": {"kind": "human"},
                }

            # Let the SDK own the input-stream lifecycle: its async-iterable
            # connect path keeps stdin available through task continuations,
            # then closes it once the session reaches idle.
            await client.connect(inputs())
            terminal = None
            output = ""
            usage = Usage()
            try:
                async for message in client.receive_messages():
                    outcome = await self._handle_message(message)
                    if outcome is not None:
                        terminal, output, usage = outcome
                        await self._bind_external_session(
                            str(terminal.session_id or "")
                        )
            except ResultError as exc:
                # The CLI deliberately exits nonzero after error_max_turns;
                # the SDK raises again after delivering its result message.
                if not (
                    terminal is not None
                    and terminal.subtype == "error_max_turns"
                    and isinstance(exc.data, dict)
                    and exc.data.get("subtype") == "error_max_turns"
                ):
                    raise
            if terminal is None:
                raise RuntimeError("Claude Code stream ended without a result message")
            await self._flush_stream_redactors()
            if not output:
                output = "".join(self._root_text)
            output = redact_text(output, self._event_secrets())
            await self._flush_open_steps(usage)
            finish = (
                "aborted"
                if getattr(terminal, "terminal_reason", "")
                in {"aborted_streaming", "aborted_tools"}
                else "max_steps"
                if terminal.subtype == "error_max_turns"
                else "stop"
            )
            return RunResult(
                output=output,
                steps=max(self._steps, int(terminal.num_turns or 0), 1),
                usage=usage,
                cost=usage.cost,
                finish_reason=finish,
                metadata={
                    "harness_id": "claude",
                    "external_session_id": str(terminal.session_id or transcript),
                    "cli_version": CLI_VERSION,
                    "sdk_version": SDK_VERSION,
                    "model": self._model,
                    "reasoning_effort": effort,
                    "context_sources": list(composed.sources),
                    "context_truncated": bool(composed.truncated),
                },
            )
        except asyncio.CancelledError as exc:
            for pending_call_id in list(self._native_file_snapshots):
                self._clear_native_snapshot(pending_call_id)
            run_error = exc

            await self._shield_cancelled_cleanup(client)
            raise
        except Exception as exc:
            for pending_call_id in list(self._native_file_snapshots):
                self._clear_native_snapshot(pending_call_id)
            run_error = exc
            await self._finalize_abandoned_tools(
                "Claude run ended before the tool completed"
            )
            for scope, state in list(self._step_states.items()):
                if state.started:
                    await self._finish_step(scope)
            raise
        finally:
            try:
                if client is not None:
                    try:
                        await self._disconnect_client(client)
                    except TimeoutError as exc:
                        log.warning("claude_client_disconnect_timeout")
                        close_error = exc
                        if transport is not None:
                            try:
                                await transport.close()
                            except Exception as transport_exc:
                                close_error = transport_exc
                    except asyncio.CancelledError:
                        if transport is not None:
                            try:
                                await asyncio.shield(transport.close())
                            except BaseException:
                                pass
                        raise
                    except Exception as exc:
                        log.warning(
                            "claude_client_disconnect_failed", error=type(exc).__name__
                        )
                        close_error = exc
                        if transport is not None:
                            try:
                                await transport.close()
                            except Exception as transport_exc:
                                close_error = transport_exc
            finally:
                if lease is not None:
                    try:
                        await asyncio.shield(lease.aclose())
                        owner_released = True
                        await asyncio.shield(self._notify_owner(lease, "released"))
                    except asyncio.CancelledError:
                        try:
                            await asyncio.shield(lease.aclose())
                            owner_released = True
                            await asyncio.shield(self._notify_owner(lease, "released"))
                        except BaseException as close_exc:
                            close_error = close_error or close_exc
                        if run_error is None:
                            raise
                    except Exception as exc:
                        log.warning(
                            "claude_agent_lease_close_failed", error=type(exc).__name__
                        )
                        close_error = exc
                        try:
                            await asyncio.shield(self._notify_owner(lease, "closing"))
                        except Exception:
                            pass
            if close_error is not None and run_error is None:
                raise RuntimeError(
                    "Claude process cleanup could not be confirmed"
                ) from None
            if close_error is not None:
                log.error(
                    "claude_process_cleanup_unconfirmed",
                    error=type(close_error).__name__,
                )
            if lease is not None and owner_released and close_error is None:
                self._owner_released = True
            self._native_file_snapshots.clear()
            self._options = None
            self._agent = None
            self._bridge = None
            self._wire_to_registry.clear()
            self._tool_events.clear()
            self._tool_results.clear()
            self._tool_arguments.clear()
            self._managed_skill_names.clear()

    async def _shield_cancelled_cleanup(self, client: Any) -> None:
        """Bound cleanup, tolerating repeated cancellation without blocking teardown."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CANCEL_CLEANUP_TIMEOUT_S
        cleanup_task: asyncio.Task[None] | None = None

        async def cleanup() -> None:
            async def bounded(
                awaitable: Awaitable[Any], *, reserve: float = 0.0
            ) -> None:
                remaining = deadline - loop.time() - reserve
                if remaining <= 0:
                    if asyncio.iscoroutine(awaitable):
                        awaitable.close()
                    return
                operation = asyncio.create_task(awaitable)
                try:
                    done, _pending = await asyncio.wait({operation}, timeout=remaining)
                    if operation in done:
                        operation.result()
                        return
                except (asyncio.CancelledError, Exception):
                    pass
                if not operation.done():
                    operation.cancel()
                    operation.add_done_callback(
                        lambda task: task.exception() if not task.cancelled() else None
                    )
                    reap_time = min(0.02, max(0.0, deadline - loop.time()))
                    if reap_time:
                        await asyncio.wait({operation}, timeout=reap_time)

            budget = CANCEL_CLEANUP_TIMEOUT_S
            if client is not None:
                await bounded(client.interrupt(), reserve=budget * 0.85)
            await bounded(self._flush_stream_redactors(), reserve=budget * 0.6)
            await bounded(
                self._finalize_abandoned_tools("Claude run was cancelled"),
                reserve=budget * 0.4,
            )
            # Preserve the cancellation signal even if a step-finish observer
            # stalls; step closure remains a bounded best-effort terminal event.
            await bounded(
                self._send({"type": "aborted", "reason": "cancelled"}),
                reserve=budget * 0.2,
            )
            for scope, state in list(self._step_states.items()):
                if state.started:
                    await bounded(self._finish_step(scope, flush_redactors=False))

        cleanup_task = asyncio.create_task(cleanup())
        while not cleanup_task.done() and loop.time() < deadline:
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup_task),
                    timeout=max(0.0, deadline - loop.time()),
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                continue
        if not cleanup_task.done():
            cleanup_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=0.01)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            # Don't await cleanup indefinitely: the run's finally block must
            # retain control so transport and lease release can proceed.
            cleanup_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )

    async def _disconnect_client(self, client: Any) -> None:
        """Disconnect in the same asyncio task that connected the SDK client."""
        import anyio

        with anyio.CancelScope(shield=True):
            await client.disconnect()

    async def _finalize_abandoned_tools(self, reason: str) -> None:
        """Close tool events that cannot receive a completion after run failure."""
        for call_id, state in list(self._tool_events.items()):
            self._tool_results.pop(call_id, None)
            self._tool_arguments.pop(call_id, None)
            if self._bridge is not None:
                self._bridge.discard_mcp_call(call_id)
            await self._finish_tool_error(
                call_id,
                reason,
                parent_id=state.get("parent_tool_use_id"),
            )
            self._pending_task_tool_ids.discard(call_id)
            if state.get("source_tool") in {"Task", "Agent"}:
                task_id = self._subtask_by_tool.get(call_id, "")
                if task_id:
                    await self._finish_sdk_subtask(task_id, call_id, "error", reason)
                else:
                    await self._finish_subtask_from_tool(call_id, reason, "error")

    def _reconcile_closed_root_usage(
        self, root: StepState | None, usage: Usage
    ) -> None:
        """Track result-only overhead without reopening a completed UI step."""
        if root is not None:
            # Result usage is authoritative at session completion. Do not emit
            # another step_finish for a step whose lifecycle is already closed.
            root.finished_usage = root.finished_usage.merge(usage)

    async def _flush_open_steps(self, usage: Usage) -> None:
        """Finish open scopes and reconcile cumulative result usage once."""
        for scope, state in list(self._step_states.items()):
            if scope and state.started:
                await self._finish_step(scope)
        root = self._step_states.get("")
        child_usage = Usage()
        for scope, state in self._step_states.items():
            if scope:
                child_usage = child_usage.merge(state.finished_usage)
        local_usage = (
            root.usage_by_message.get(root.current_message_id, Usage())
            if root is not None and root.started
            else Usage()
        )
        prior = (
            (root.finished_usage if root is not None else Usage())
            .merge(child_usage)
            .merge(local_usage)
        )
        residual = Usage(
            prompt_tokens=max(0, usage.prompt_tokens - prior.prompt_tokens),
            completion_tokens=max(0, usage.completion_tokens - prior.completion_tokens),
            total_tokens=max(0, usage.total_tokens - prior.total_tokens),
            cost=max(0.0, usage.cost - prior.cost),
        )
        if root is not None and root.started:
            final_usage = local_usage.merge(residual)
            await self._finish_step("", final_usage)
        else:
            self._reconcile_closed_root_usage(root, residual)
