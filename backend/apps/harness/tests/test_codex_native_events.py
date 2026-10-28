"""Native CLI prompt/tool contracts and provider-independent UI event projection."""

from __future__ import annotations

import base64
import json
import shutil
import uuid
from pathlib import Path

import pytest

from apps.harness.agents.definitions import get_agent
from apps.harness.engines.base import RunOptions
from apps.harness.engines.codex.configuration import runtime_settings
from apps.harness.engines.codex.engine import CodexEngine
from apps.harness.engines.codex.media import image_attachments
from apps.harness.engines.codex.session_store import (
    capture,
    home_for,
    materialize_skills,
    restore_snapshot,
    state_action,
)
from apps.harness.engines.codex.tools import ManagedToolInvoker
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.prompts.opencuria import opencuria_context
from apps.harness.tests.conftest import TINY_PNG
from apps.harness.tests.test_codex_core import LocalAccessor, fixture_run
from apps.harness.tests.test_codex_core_safety import auth
from apps.harness.tools import default_tool_registry


def test_codex_keeps_native_capabilities_and_small_stable_extension_surface() -> None:
    settings, _, _ = runtime_settings(auth(), "chatgpt/gpt-6.1-sol", readonly=False)
    assert settings["features.memories"] is True
    assert settings["approval_policy"] == "never"
    assert settings["sandbox_mode"] == "danger-full-access"
    for key in (
        "agents.enabled",
        "features.multi_agent",
        "web_search",
        "mcp_servers",
        "features.image_generation",
        "features.plugins",
        "project_doc_max_bytes",
        "skills.include_instructions",
        "features.skip_host_skill_discovery",
    ):
        assert key not in settings
    invoker = ManagedToolInvoker(default_tool_registry(), None, lambda _: None)
    names = {
        x["name"] for x in invoker.schemas(get_agent("build"), "build", RunOptions())
    }
    assert names == {"question", "todowrite"} | {
        x.name for x in default_tool_registry().list() if x.name.startswith("process_")
    }
    assert invoker.schemas(get_agent("plan"), "plan", RunOptions()) == invoker.schemas(
        get_agent("build"), "build", RunOptions()
    )


@pytest.mark.asyncio
async def test_real_runtime_retains_native_base_prompt_search_and_delegation(tmp_path):
    _, _, _, requests, _, _ = await fixture_run(tmp_path, tool="", resume=True)
    turns = [x for x in requests if x.get("model") == "~test/model"]
    assert len(turns) >= 2
    for request in turns:
        context = json.dumps(request.get("input", []))
        assert "You are a coding agent running in the Codex" in context
        for paragraph in opencuria_context(get_agent("build")).split("\n\n"):
            assert context.count(paragraph) == 1
        assert get_agent("build").system_prompt not in context
        assert (
            "Native delegation, plugins, MCP discovery and hooks are disabled"
            not in context
        )
    request = turns[-1]
    names = {x.get("name") for x in request["tools"]}
    assert "multi_agent_v1" in names
    assert "exec_command" in names
    assert "write" not in names and "task" not in names and "bash" not in names
    assert any(x["type"] == "web_search" for x in request["tools"])


@pytest.mark.asyncio
async def test_real_native_patch_keeps_final_answer_after_file_changes(
    tmp_path: Path,
) -> None:
    """Turn diff snapshots must not duplicate edits or bury the final answer."""
    patch = (
        "*** Begin Patch\n"
        "*** Add File: first.txt\n+first\n"
        "*** Add File: second.txt\n+second\n"
        "*** End Patch\n"
    )
    result, events, accessor, requests, permissions, _ = await fixture_run(
        tmp_path,
        tool="exec_command",
        args={"cmd": "apply_patch <<'PATCH'\n" + patch + "PATCH"},
    )
    assert result.output == "Local done."
    assert not permissions
    assert accessor.path("/workspace/first.txt").exists(), requests[-1]["input"]
    assert accessor.path("/workspace/first.txt").read_text() == "first\n"
    assert accessor.path("/workspace/second.txt").read_text() == "second\n"
    patches = [event for event in events if event["type"] == "patch"]
    assert len(patches) == 2
    assert {event["path"] for event in patches} == {
        str(accessor.path("/workspace/first.txt")),
        str(accessor.path("/workspace/second.txt")),
    }
    assert len({event["call_id"] for event in patches}) == 1
    last_patch = max(i for i, event in enumerate(events) if event["type"] == "patch")
    answer = "".join(
        event.get("delta", {}).get("text", "") for event in events[last_patch + 1 :]
    )
    assert answer == "Local done."


@pytest.mark.asyncio
async def test_native_items_keep_payloads_errors_and_unknown_tools(tmp_path):
    events = []

    async def emit(event):
        events.append(event)

    engine = CodexEngine(
        tools=default_tool_registry(), accessor=None, auth=auth(), emit=emit
    )
    items = [
        {
            "id": "search",
            "type": "webSearch",
            "query": "native search",
            "results": [{"url": "https://example.com"}],
        },
        {
            "id": "mcp",
            "type": "mcpToolCall",
            "server": "docs",
            "tool": "lookup",
            "arguments": {"topic": "API"},
            "result": {"content": [{"type": "text", "text": "found"}]},
        },
        {
            "id": "new",
            "type": "futureNativeTool",
            "tool": "new_capability",
            "arguments": {"x": 1},
            "result": "kept",
        },
        {
            "id": "failed",
            "type": "mcpToolCall",
            "server": "docs",
            "tool": "lookup",
            "status": "failed",
            "error": {"message": "local failure"},
        },
        {"id": "abandoned", "type": "webSearch", "query": "pending"},
    ]
    for item in items:
        await engine.projector.item(item, completed=False)
        if item["id"] != "abandoned":
            await engine.projector.item(item, completed=True)
            await engine.projector.item(item, completed=True)
    await engine.projector.finish()
    terminals = [x for x in events if x["type"] in {"tool_completed", "tool_error"}]
    assert len(terminals) == len(items)
    assert terminals[0]["tool"] == "websearch"
    assert "example.com" in terminals[0]["output"]
    assert terminals[1]["tool"] == "tool_call" and "found" in terminals[1]["output"]
    assert "new_capability" in terminals[2]["output"]
    assert terminals[3]["type"] == terminals[4]["type"] == "tool_error"
    assert terminals[4]["tool"] == "websearch"


@pytest.mark.asyncio
async def test_image_generation_enriches_prompt_and_uses_authorized_no_follow_media():
    events, reads = [], []

    class Accessor:
        async def read_file_no_follow(self, path, max_size):
            from apps.harness.access.file_types import FileContent

            reads.append(path)
            return FileContent(content=TINY_PNG, size=len(TINY_PNG))

    async def emit(event):
        events.append(event)

    engine = CodexEngine(
        tools=default_tool_registry(), accessor=Accessor(), auth=auth(), emit=emit
    )
    engine.agent, engine.mode, engine.opts = get_agent("build"), "build", RunOptions()
    item = {
        "id": "image",
        "type": "imageGeneration",
        "status": "in_progress",
        "result": "",
    }
    await engine.projector.item(item, completed=False)
    item.update(
        status="completed",
        savedPath="/workspace/generated.png",
        revisedPrompt="A green icon",
        result="not-for-json-output",
    )
    await engine.projector.item(item, completed=True)
    completed = events[-1]
    assert completed["tool"] == "image_generation"
    assert reads == ["/workspace/generated.png"]
    assert (
        completed["attachments"][0]["url"]
        == "data:image/png;base64," + base64.b64encode(TINY_PNG).decode()
    )
    assert "not-for-json-output" not in json.dumps(events)
    assert "A green icon" in events[-2]["arguments"]
    engine.invoker.evaluator = PermissionEvaluator(global_rules={"read": "deny"})
    assert (
        await image_attachments(
            item, accessor=engine.accessor, authorize_read=engine._authorize_read
        )
        == []
    )
    assert len(reads) == 1
    item["savedPath"] = "/etc/private.png"
    assert (
        await image_attachments(
            item, accessor=engine.accessor, authorize_read=engine._authorize_read
        )
        == []
    )


@pytest.mark.asyncio
async def test_memory_files_and_selected_skills_survive_cold_restore(tmp_path):
    accessor = LocalAccessor(tmp_path / "workspace")
    home = home_for(str(uuid.uuid4()))
    await state_action(accessor, "fresh", home)
    await accessor.write_file(home + "/sessions/2026/10/22/rollout-test.jsonl", b"{}\n")
    await accessor.write_file(home + "/memories/MEMORY.md", b"Project decisions")
    await accessor.write_file(
        home + "/memories/skills/testing/SKILL.md", b"Testing notes"
    )
    await accessor.write_file(home + "/auth.json", b"excluded")
    await materialize_skills(accessor, home, ["A selected skill"])
    assert (
        "A selected skill"
        in accessor.path(home + "/skills/opencuria-skill-001/SKILL.md").read_text()
    )
    record = await capture(accessor, home)
    assert "auth.json" not in {x["path"] for x in record["files"]}
    shutil.rmtree(accessor.path(home))
    await restore_snapshot(accessor, home, record)
    assert (
        accessor.path(home + "/memories/MEMORY.md").read_text() == "Project decisions"
    )
    await materialize_skills(accessor, home, [])
    assert not accessor.path(home + "/skills/opencuria-skill-001").exists()
    with pytest.raises(ValueError, match="skill"):
        await materialize_skills(accessor, home, ["x" * 65537])


@pytest.mark.asyncio
async def test_memory_citation_is_a_tool_not_answer_text():
    events = []

    async def emit(event):
        events.append(event)

    engine = CodexEngine(
        tools=default_tool_registry(), accessor=None, auth=auth(), emit=emit
    )
    engine.items, engine.streamed, engine.redactors = {}, {}, {}
    item = {
        "id": "answer",
        "type": "agentMessage",
        "text": "Done",
        "memoryCitation": {
            "entries": [
                {
                    "path": "MEMORY.md",
                    "lineStart": 3,
                    "lineEnd": 7,
                    "note": "Project rules",
                }
            ],
            "threadIds": [],
        },
    }
    await engine._item(item, completed=True)
    await engine._item(item, completed=True)
    assert len([x for x in events if x["type"] == "tool_completed"]) == 1
    assert next(x for x in events if x["type"] == "tool_completed")["tool"] == "memory"
    assert engine.items["answer"]["text"] == "Done"


@pytest.mark.asyncio
async def test_native_child_dynamic_tools_use_child_policy_and_timeline():
    root, child = [], []

    async def emit(event):
        root.append(event)

    async def emit_child(parent, event):
        child.append((parent, event))

    engine = CodexEngine(
        tools=default_tool_registry(),
        accessor=None,
        auth=auth(),
        emit=emit,
        on_child_event=emit_child,
    )
    engine.agent, engine.model, engine.mode = get_agent("build"), "model", "build"
    engine.opts = RunOptions()
    engine.thread_id, engine.turn_id = "root", "turn"
    engine.turn_ready.set()
    engine.allowed = {"question"}
    engine.projector.children["child"] = {
        "parent_tool_use_id": "spawn",
        "finished": False,
    }
    result = await engine._server_request(
        "item/tool/call",
        {
            "threadId": "child",
            "turnId": "child-turn",
            "callId": "q",
            "tool": "question",
            "arguments": {"questions": [{"question": "No child questions"}]},
        },
    )
    assert not result["success"]
    assert child and all(parent == "spawn" for parent, _ in child)
    assert root == []
    engine.projector.children["child"]["finished"] = True
    with pytest.raises(RuntimeError, match="Stale"):
        await engine._server_request(
            "item/tool/call", {"threadId": "child", "turnId": "child-turn"}
        )


@pytest.mark.asyncio
async def test_inline_generated_image_is_bounded_and_not_a_remote_url():
    engine = CodexEngine(tools=default_tool_registry(), accessor=None, auth=auth())
    encoded = base64.b64encode(TINY_PNG).decode()
    result = await image_attachments(
        {"type": "imageGeneration", "result": encoded},
        accessor=engine.accessor,
        authorize_read=engine._authorize_read,
    )
    assert result[0]["url"] == "data:image/png;base64," + encoded
    for value in ("https://example.com/image.png", "invalid base64", "x" * 8000001):
        assert (
            await image_attachments(
                {"type": "imageGeneration", "result": value},
                accessor=engine.accessor,
                authorize_read=engine._authorize_read,
            )
            == []
        )


@pytest.mark.asyncio
async def test_memory_checkpoint_rejects_symlinked_subtrees(tmp_path):
    accessor = LocalAccessor(tmp_path / "workspace")
    home = home_for(str(uuid.uuid4()))
    await state_action(accessor, "fresh", home)
    await accessor.write_file(home + "/sessions/2026/10/22/rollout-test.jsonl", b"{}\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "MEMORY.md").write_text("private")
    accessor.path(home + "/memories").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AssertionError):
        await capture(accessor, home)


@pytest.mark.asyncio
async def test_real_runtime_loads_workspace_native_mcp_docs_and_selected_skills(
    tmp_path,
):
    script = tmp_path / "native_mcp.py"
    script.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line)\n"
        " if 'id' not in m: continue\n"
        " method=m.get('method')\n"
        " if method=='initialize': r={'protocolVersion':'2024-11-05',"
        "'capabilities':{'tools':{}},'serverInfo':{'name':'docs','version':'1'}}\n"
        " elif method=='tools/list': r={'tools':[{'name':'lookup',"
        "'description':'Lookup local test docs','inputSchema':{'type':'object',"
        "'properties':{'topic':{'type':'string'}}}}]}\n"
        " elif method=='tools/call': r={'content':[{'type':'text',"
        "'text':'Native MCP result'}]}\n"
        " else:r={}\n"
        " sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':r})"
        "+'\\n');sys.stdout.flush()\n"
    )
    _, events, _, requests, _, _ = await fixture_run(
        tmp_path,
        tool="lookup",
        namespace="mcp__native_docs",
        args={"topic": "API"},
        fixture_files={
            ".codex/config.toml": (
                '[mcp_servers.native_docs]\ncommand="/usr/bin/python3"\n'
                f'args=["{script}"]\n'
            ).encode(),
            "AGENTS.md": b"NATIVE_PROJECT_RULES\n",
        },
        skills=["A selected skill"],
    )
    request = next(x for x in requests if x.get("model") == "~test/model")
    assert "NATIVE_PROJECT_RULES" in json.dumps(request["input"])
    assert "opencuria-skill-001" in json.dumps(request["input"])
    assert any(x.get("name") == "mcp__native_docs" for x in request["tools"])
    completed = next(x for x in events if x["type"] == "tool_completed")
    assert completed["tool"] == "tool_call"
    assert "Native MCP result" in completed["output"]


@pytest.mark.asyncio
async def test_real_runtime_native_web_search_item_is_projected(tmp_path):
    _, events, _, _, _, _ = await fixture_run(
        tmp_path,
        response_item={
            "type": "web_search_call",
            "status": "completed",
            "action": {"type": "search", "query": "OpenCuria native tools"},
        },
    )
    completed = [x for x in events if x["type"] == "tool_completed"]
    assert len(completed) == 1
    assert completed[0]["tool"] == "websearch"
    assert "OpenCuria native tools" in completed[0]["output"]


@pytest.mark.parametrize(
    ("action", "tool"), [("read", "read"), ("listFiles", "list"), ("search", "grep")]
)
def test_native_shell_actions_reuse_existing_file_display(action, tool):
    from apps.harness.engines.codex.events import presentation

    result = presentation(
        {
            "type": "commandExecution",
            "command": "cat src/main.py",
            "commandActions": [{"type": action, "path": "src/main.py", "query": "def"}],
        }
    )
    assert result[0] == tool
    assert result[2]["path"] == "src/main.py"


def test_dedicated_native_memory_output_uses_memory_display():
    from apps.harness.engines.codex.events import presentation

    result = presentation(
        {"type": "functionCallOutput", "name": "memory_lookup", "output": "remembered"}
    )
    assert result[0] == "memory"
    assert result[-1] == "remembered"
