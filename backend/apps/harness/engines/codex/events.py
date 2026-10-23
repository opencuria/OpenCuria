"""Project native Codex items without changing the agent's tool contracts."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from ...access.base import guess_mime_type, sanitize_harness_path
from ..shared.media import _read_media_no_follow, data_url_block

if TYPE_CHECKING:
    from .engine import CodexEngine

MAX_OUTPUT = 100_000
NON_TOOL_ITEMS = {
    "userMessage",
    "hookPrompt",
    "agentMessage",
    "reasoning",
    "plan",
    "subAgentActivity",
    "contextCompaction",
}


def presentation(item: dict[str, Any]) -> tuple[str, str, dict[str, Any], str]:
    """Return canonical display tool, title, arguments and bounded native result."""
    kind = item.get("type", "tool")
    tool, title = "tool_call", str(item.get("tool") or item.get("name") or kind)
    args: dict[str, Any] = {"name": title, "native_type": kind}
    if "arguments" in item:
        args["arguments"] = item["arguments"]
    output: Any = item
    if kind == "commandExecution":
        tool, title = "bash", f"$ {item.get('command', '')}"
        args = {"command": item.get("command", ""), "cwd": item.get("cwd", "")}
        output = item.get("aggregatedOutput") or ""
        actions = item.get("commandActions") or []
        if any(x in args["command"] for x in ("/memories/", "$CODEX_HOME/memories")):
            tool, title = "memory", "Memory"
            args["operation"] = "Read/update native memory"
        elif len(actions) == 1:
            action = actions[0]
            path = action.get("path") or ""
            if action.get("type") == "read":
                tool, title = "read", f"Read {path}"
                args["path"] = path
            elif action.get("type") == "listFiles":
                tool, title = "list", f"List {path}".strip()
                args["path"] = path
            elif action.get("type") == "search":
                tool, title = "grep", f"Grep {action.get('query') or ''}".strip()
                args.update(path=path, pattern=action.get("query") or "")
    elif kind == "fileChange":
        tool, title = "edit", "Edit files"
        args = {"changes": item.get("changes", [])}
        output = args["changes"]
        if any("/memories/" in str(x.get("path", "")) for x in args["changes"]):
            tool, title = "memory", "Update memory"
            args["operation"] = "Update"
    elif kind == "webSearch":
        tool, title = "websearch", f"Search {item.get('query', '')}".strip()
        args = {"query": item.get("query", ""), "action": item.get("action")}
        output = {"results": item.get("results"), "action": item.get("action")}
    elif kind == "mcpToolCall":
        title = f"{item.get('server', 'MCP')}: {item.get('tool', 'tool')}"
        args = {
            "name": title,
            "server": item.get("server"),
            "arguments": item.get("arguments"),
        }
        output = item.get("error") or item.get("result") or ""
    elif kind == "imageGeneration":
        tool, title = "image_generation", "Generate image"
        args = {
            "prompt": item.get("revisedPrompt", ""),
            "transparent_background": item.get("transparentBackground"),
        }
        # The image result can be several MB of base64. Persist it only as a
        # checked attachment, never as truncated JSON or text.
        output = {k: item.get(k) for k in ("savedPath", "status", "failure")}
    elif kind == "imageView":
        tool, title = "read", f"Read {item.get('path', 'image')}"
        args = {"path": item.get("path", "")}
    elif kind == "collabAgentToolCall":
        name = str(item.get("tool", ""))
        tool = "task" if name == "spawnAgent" else "tool_call"
        title = "Subagent: " + str(item.get("prompt") or name)[:160]
        args = {
            "name": name,
            "prompt": item.get("prompt"),
            "receiver_thread_ids": item.get("receiverThreadIds", []),
        }
        output = item.get("agentsStates") or {}
    elif kind == "dynamicToolCall":
        args = {"name": title, "arguments": item.get("arguments")}
        output = item.get("contentItems") or ""
        if "memory" in title.lower():
            tool = "memory"
            args["operation"] = title
    elif kind == "functionCallOutput":
        output = item.get("output") or ""
        if "memor" in title.lower():
            tool = "memory"
            args["operation"] = title
    elif kind == "memory":
        tool, title = "memory", "Memory citations"
        args = {"operation": "Recall", "thread_ids": item.get("threadIds", [])}
        output = {"entries": item.get("entries", [])}
    return (
        tool,
        title[:500],
        args,
        (output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))[
            :MAX_OUTPUT
        ],
    )


class CodexEventProjector:
    """Balance native calls and project native child threads into existing subtasks."""

    def __init__(self, engine: CodexEngine) -> None:
        self.engine = engine
        self.calls: dict[str, tuple[str, str | None]] = {}
        self.finished: set[str] = set()
        self.children: dict[str, dict[str, Any]] = {}
        self.early: list[dict[str, Any]] = []
        self.child_text: dict[str, str] = {}

    async def send(self, event: dict[str, Any], parent: str | None = None) -> None:
        """Redact child events through the same boundary as parent events."""
        if parent and self.engine.on_child_event:
            from ..shared.security import redact_value

            await self.engine.on_child_event(
                parent, redact_value(event, self.engine.secret_values)
            )
        elif not parent:
            await self.engine._send(event)

    async def item(
        self, item: dict[str, Any], *, completed: bool, parent: str | None = None
    ) -> None:
        """Map observed tool items, including unknown future native tools."""
        kind, key = item.get("type"), str(item.get("id") or "")
        if kind == "subAgentActivity":
            if item.get("kind") == "started":
                await self.collaboration(
                    {
                        "id": key,
                        "tool": "spawnAgent",
                        "receiverThreadIds": [item["agentThreadId"]],
                        "prompt": item.get("agentPath") or "Subagent",
                        "model": item.get("model"),
                        "reasoningEffort": item.get("reasoningEffort"),
                    }
                )
            return
        if not key or kind in NON_TOOL_ITEMS:
            return
        if kind == "dynamicToolCall" and item.get("tool") in self.engine.allowed:
            return  # The managed dispatcher already owns this lifecycle.
        if len(self.calls) >= 4096:
            raise ValueError("Codex native tool count limit reached")
        tool, title, args, output = presentation(item)
        if key in self.finished:
            return
        if key not in self.calls:
            self.calls[key] = (tool, parent)
            event = {
                "step": 1,
                "call_id": key,
                "tool": tool,
                "title": title,
                "arguments": json.dumps(args, ensure_ascii=False),
            }
            await self.send({"type": "tool_queued", **event}, parent)
            await self.send({"type": "tool_started", **event}, parent)
        if kind == "collabAgentToolCall":
            await self.collaboration(item)
        if not completed:
            return
        self.finished.add(key)
        # Native image prompts/paths often arrive only on completion. Enrich
        # the existing call's input before its terminal result is persisted.
        await self.send(
            {
                "type": "tool_queued",
                "step": 1,
                "call_id": key,
                "tool": tool,
                "title": title,
                "arguments": json.dumps(args, ensure_ascii=False),
            },
            parent,
        )
        failed = (
            item.get("status") in {"failed", "declined", "interrupted"}
            or item.get("exitCode", 0) not in {0, None}
            or bool(item.get("error") or item.get("failure"))
            or item.get("success") is False
        )
        attachments = await self.image_attachments(item) if not failed else []
        await self.send(
            {
                "type": "tool_error" if failed else "tool_completed",
                "step": 1,
                "call_id": key,
                "tool": tool,
                "error" if failed else "output": output,
                "attachments": attachments,
            },
            parent,
        )
        if kind == "fileChange":
            for change in item.get("changes", []):
                await self.send(
                    {
                        "type": "patch",
                        "step": 1,
                        "call_id": key,
                        "tool": "edit",
                        "path": change.get("path", ""),
                        "unified_diff": str(change.get("diff") or "")[:131072],
                    },
                    parent,
                )

    async def image_attachments(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """Hydrate only bounded, authorized no-follow workspace images."""
        if item.get("type") not in {"imageGeneration", "imageView"}:
            return []
        raw_path = item.get("savedPath") or item.get("path")
        if not raw_path:
            encoded = item.get("result")
            if not isinstance(encoded, str) or not encoded or len(encoded) > 8_000_000:
                return []
            url = (
                encoded
                if encoded.startswith("data:")
                else "data:image/png;base64," + encoded
            )
            try:
                block = data_url_block(url)
            except ValueError:
                return []
            if block.get("type") != "image":
                return []
            return [
                {
                    "type": "file",
                    "mime": block["source"]["media_type"],
                    "url": url,
                    "filename": "generated.png",
                }
            ]
        if not self.engine.accessor:
            return []
        try:
            path = sanitize_harness_path(raw_path)
            if not path.startswith("/workspace/"):
                return []
            if not await self.engine._authorize_read(path):
                return []
            stored = await _read_media_no_follow(
                self.engine.accessor, path, max_size=6_000_000
            )
            mime = guess_mime_type(path)
            if stored.truncated or len(stored.content) > 6_000_000:
                return []
            if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                return []
            return [
                {
                    "type": "file",
                    "mime": mime,
                    "filename": path.rsplit("/", 1)[-1],
                    "url": f"data:{mime};base64,"
                    + base64.b64encode(stored.content).decode(),
                }
            ]
        except Exception:
            return []  # A missing preview must not fail an already executed tool.

    async def collaboration(self, item: dict[str, Any]) -> None:
        """Use native child IDs, not OpenCuria delegation or fabricated runs."""
        states = item.get("agentsStates") or {}
        for thread in item.get("receiverThreadIds", []):
            state = states.get(thread) or {}
            child = self.children.get(thread)
            if child is None:
                native_model = str(item.get("model") or "")
                provider = self.engine.model.split("/", 1)[0]
                model = (
                    provider + "/" + native_model.removeprefix("~")
                    if native_model
                    and not native_model.startswith(("chatgpt/", "openrouter/"))
                    else native_model or self.engine.model
                )
                child = self.children[thread] = {
                    "subtask_id": thread,
                    "parent_tool_use_id": item["id"],
                    "agent": "general",
                    "description": item.get("prompt") or "Subagent",
                    "model": model,
                    "reasoning_effort": item.get("reasoningEffort") or "",
                    "finished": False,
                }
                await self.engine._send({"type": "subtask_started", **child})
                pending, self.early = self.early, []
                for event in pending:
                    if event.get("params", {}).get("threadId") == thread:
                        await self.child_notification(event)
                    else:
                        self.early.append(event)
            status = state.get("status")
            if status in {
                "completed",
                "errored",
                "shutdown",
                "notFound",
                "interrupted",
            }:
                await self.finish_child(
                    thread,
                    "completed"
                    if status == "completed"
                    else "aborted"
                    if status == "interrupted"
                    else "error",
                    str(state.get("message") or ""),
                )

    async def finish_child(self, thread: str, status: str, summary: str) -> None:
        """Emit a single terminal subtask even if wait repeats the same state."""
        child = self.children.get(thread)
        if child and not child["finished"]:
            child["finished"] = True
            await self.engine._send(
                {
                    **child,
                    "type": "subtask_finished",
                    "status": status,
                    "summary": summary[:MAX_OUTPUT],
                }
            )

    async def child_notification(self, message: dict[str, Any]) -> None:
        """Keep child text/tools out of the parent's answer, with bounded buffering."""
        method, params = message.get("method", ""), message.get("params", {})
        thread = params.get("threadId")
        if not thread or method not in {
            "item/started",
            "item/completed",
            "turn/completed",
        }:
            return
        child = self.children.get(thread)
        if child is None:
            if len(self.early) < 32 and len(json.dumps(message)) < 65536:
                self.early.append(message)
            return
        if child["finished"]:
            if (
                method != "item/started"
                or (params.get("item") or {}).get("type") != "userMessage"
            ):
                return
            # send_input/resume_agent may start another native turn in the
            # same child. Open a new projected activity without replaying text.
            child["finished"] = False
            child["subtask_id"] = f"{thread}:{params.get('turnId', '')}"
            await self.engine._send({"type": "subtask_started", **child})
        child["turn_id"] = params.get("turnId") or (params.get("turn") or {}).get("id")
        parent = child["parent_tool_use_id"]
        if method == "turn/completed":
            turn = params.get("turn") or {}
            for item in turn.get("items", []):
                await self.child_item(item, parent)
            status = turn.get("status")
            await self.finish_child(
                thread,
                "completed"
                if status == "completed"
                else "aborted"
                if status == "interrupted"
                else "error",
                "",
            )
        else:
            item = params.get("item") or {}
            if method == "item/completed":
                await self.child_item(item, parent)
            else:
                await self.item(item, completed=False, parent=parent)

    async def child_item(self, item: dict[str, Any], parent: str) -> None:
        """Persist authoritative child text once and share native tool rendering."""
        if item.get("type") in {"agentMessage", "reasoning"}:
            key = str(item.get("id") or "")
            if key in self.child_text:
                return
            text = str(item.get("text") or "\n".join(item.get("summary") or []))
            self.child_text[key] = text[:MAX_OUTPUT]
            field = "text" if item["type"] == "agentMessage" else "reasoning"
            await self.send(
                {
                    "type": "part_updated",
                    "step": 1,
                    "delta": {field: text[:MAX_OUTPUT]},
                },
                parent,
            )
        else:
            await self.item(item, completed=True, parent=parent)

    async def finish(self) -> None:
        """Close abandoned native tools and subtasks on all exit paths."""
        for key, (tool, parent) in self.calls.items():
            if key not in self.finished:
                await self.send(
                    {
                        "type": "tool_error",
                        "step": 1,
                        "call_id": key,
                        "tool": tool,
                        "error": "Codex run ended before tool completion",
                    },
                    parent,
                )
                self.finished.add(key)
        for thread in self.children:
            await self.finish_child(thread, "aborted", "Parent Codex run ended")
