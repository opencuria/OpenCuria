"""Codex core contracts; never contacts a provider or needs real credentials."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

from apps.harness.access.file_types import FileContent
from apps.harness.engines.base import RunOptions
from apps.harness.engines.codex.auth import CodexAuthConfiguration
from apps.harness.engines.codex.engine import CodexEngine
from apps.harness.engines.codex.protocol import CodexProtocol, CodexProtocolError
from apps.harness.engines.codex.session_store import home_for, validate_snapshot
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.providers.base import LLMMessage
from apps.harness.steering import SteeringChannel
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import ToolRegistry
from apps.harness.tools.files import WriteTool

PINNED = Path(
    os.environ.get(
        "OPENCURIA_TEST_CODEX_BINARY",
        Path(__file__).resolve().parents[4]
        / ".opencuria/codex-test-runtime/package/bin/codex-app-server",
    )
)


class McpWriteTool(WriteTool):
    """A registered MCP edit verifies the retained extension permission boundary."""

    name = "mcp_test_write"


def extension_registry() -> ToolRegistry:
    """Keep core tools in the registry, but advertise only CLI extensions."""
    registry = default_tool_registry()
    registry.register(McpWriteTool())
    return registry


class ByteStream:
    def __init__(self, process):
        self.process = process
        self.root = Path("/workspace")
        self.stderr = asyncio.create_task(process.stderr.read())

    async def send(self, data):
        data = data.replace(
            b'"cwd":"/workspace"', b'"cwd":' + json.dumps(str(self.root)).encode()
        )
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def receive(self):
        return await self.process.stdout.read(65536)

    async def send_eof(self):
        self.process.stdin.close()

    async def wait_closed(self):
        return await self.process.wait()

    async def aclose(self):
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except TimeoutError:
            os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
        await self.stderr


class LocalAccessor:
    def __init__(self, root):
        self.root = root
        self.streams = []
        self.opened = []
        self.actions = []
        self.root.mkdir(parents=True)

    def path(self, path):
        return (
            self.root / path.removeprefix("/workspace/")
            if path.startswith("/workspace/")
            else self.root
            if path == "/workspace"
            else Path(path)
        )

    async def read_file(self, path, max_size=None):
        from apps.harness.access.runner_accessor import RunnerAccessorError

        try:
            data = self.path(path).read_bytes()
        except FileNotFoundError:
            raise RunnerAccessorError("File not found") from None
        return FileContent(
            content=data[:max_size],
            size=len(data),
            truncated=max_size is not None and len(data) > max_size,
        )

    read_file_no_follow = read_file

    async def write_file(self, path, content, mode=0o644):
        p = self.path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        p.chmod(mode)

    async def exec_wait(self, command, workdir="/workspace", env=None, timeout=None):
        # Local fixture emulates guest absolute path, preserving it in snapshots.
        argv = [x.replace("/workspace", str(self.root)) for x in command]
        if command[0] == "python3" and "restore" in command:
            target = self.path(command[-1]).parent / "restore.json"
            data = json.loads(target.read_bytes())
            data["home"] = str(self.path(command[-1]))
            target.write_text(json.dumps(data))
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        assert process.returncode == 0, err.decode()
        out = out.replace(str(self.root).encode(), b"/workspace")
        if command[0] == "python3" and "snapshot" in command:
            import hashlib

            parent = self.path(command[-1]).parent
            chunks = sorted(
                parent.glob("snapshot-*.part"),
                key=lambda path: int(path.stem.split("-")[-1]),
            )
            data = b"".join(p.read_bytes() for p in chunks)
            record = json.loads(data)
            record["home"] = command[-1]
            data = json.dumps(record, separators=(",", ":")).encode()
            for p in chunks:
                p.unlink()
            for index, offset in enumerate(range(0, len(data), 4 * 1024 * 1024)):
                (parent / f"snapshot-{index}.part").write_bytes(
                    data[offset : offset + 4 * 1024 * 1024]
                )
            out = (
                hashlib.sha256(data).hexdigest()
                + ":"
                + str((len(data) + 4 * 1024 * 1024 - 1) // (4 * 1024 * 1024))
            ).encode()
        return SimpleNamespace(
            exit_code=process.returncode, stdout=out.decode(), stderr=err.decode()
        )

    async def ensure_runtime_artifact(self, name, version):
        assert (name, version) == ("codex-agent", "0.166.0")
        return str(PINNED)

    async def open_process(self, argv, cwd, env, **kwargs):
        assert set(env) <= {"CODEX_HOME", "OPENCURIA_CODEX_API_KEY"}
        assert kwargs["owner"]["lease_id"]
        self.opened.append(argv)
        home = self.path(env.get("CODEX_HOME", "/workspace/home/.codex"))
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.path(cwd),
            env={
                "HOME": str(self.root / "home"),
                "PATH": "/usr/bin:/bin",
                "CODEX_HOME": str(home),
                **{k: v for k, v in env.items() if k != "CODEX_HOME"},
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stream = ByteStream(process)
        stream.root = self.root
        self.streams.append(stream)
        return stream

    async def desktop_action(self, action, args):
        self.actions.append(action)
        if action == "binding":
            return {"ok": True, "epoch": "test"}
        if action == "release":
            assert all(s.process.returncode is not None for s in self.streams)
            return {"ok": True, "lease_state": "released"}
        return {"ok": True, "epoch": "test", "lease_state": "reserved"}


class MemoryStore:
    def __init__(self, *, fail_save=False):
        self.entry = None
        self.fail_save = fail_save

    async def save(self, external_id, generation, snapshot):
        if self.fail_save:
            raise RuntimeError("Checkpoint storage unavailable")
        self.entry = {
            "uuid": generation,
            "generation": generation,
            "snapshot": snapshot,
        }

    async def load(self, external_id, home):
        validate_snapshot(self.entry["snapshot"], home)
        return self.entry


async def fixture_run(
    tmp_path,
    *,
    mode="build",
    agent="build",
    tool="mcp_test_write",
    args=None,
    code: str | None = None,
    evaluator: PermissionEvaluator | None = None,
    resume=False,
    cancel_on_native=False,
    fail_checkpoint=False,
    resume_modes=(),
    fixture_files=None,
    skills=(),
    namespace=None,
    response_item=None,
    registry=None,
    code_mode_only=False,
    prompt: str = "Local test only.",
    history: list[LLMMessage] | None = None,
    steering: SteeringChannel | None = None,
    request_hook: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    resume_prompt: str = "Continue the local conversation.",
):
    if not PINNED.exists():
        pytest.skip("Pinned Codex binary unavailable; set OPENCURIA_TEST_CODEX_BINARY")
    requests = []
    calls = 0

    async def responses(request):
        nonlocal calls
        body = await request.json()
        requests.append(body)
        if request_hook is not None:
            await request_hook(body)
        auxiliary = body.get("model") != "~test/model"
        if not auxiliary:
            calls += 1
        if not auxiliary and calls % 2 and response_item:
            item = {"id": f"native_{calls}", **response_item}
        elif not auxiliary and calls % 2 and tool:
            item = {
                "id": f"fc_{calls}",
                "type": "function_call",
                "call_id": f"call_{calls}",
                "name": tool,
                "arguments": json.dumps(
                    args or {"path": "note.txt", "content": "codex local test"}
                ),
            }
            if namespace:
                item["namespace"] = namespace
            if code is not None:
                item.update(type="custom_tool_call", input=code)
                del item["arguments"]
        else:
            item = {
                "id": f"msg_{calls}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "Local done.", "annotations": []}
                ],
            }
        rid = f"response_{calls}"
        events = [
            {
                "type": "response.created",
                "response": {"id": rid, "status": "in_progress", "output": []},
            },
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {
                "type": "response.completed",
                "response": {
                    "id": rid,
                    "status": "completed",
                    "output": [item],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            },
        ]
        return web.Response(
            text="".join(
                "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                for e in events
            ),
            content_type="text/event-stream",
        )

    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_post("/v1/responses", responses)

    async def models(_):
        if code is not None or code_mode_only:
            # Match models such as GPT-6.1-Sol that require code mode even when
            # the optional feature is disabled in the managed configuration.
            return web.json_response(
                {
                    "models": [
                        {
                            "slug": "~test/model",
                            "display_name": "Local code-mode-only model",
                            "description": "Local test only",
                            "base_instructions": "Use tools to complete the task.",
                            "default_reasoning_level": "low",
                            "supported_reasoning_levels": [],
                            "shell_type": "shell_command",
                            "tool_mode": "code_mode_only",
                            "visibility": "list",
                            "supported_in_api": True,
                            "priority": 0,
                            "supports_reasoning_summaries": False,
                            "support_verbosity": False,
                            "truncation_policy": {"mode": "tokens", "limit": 10000},
                            "supports_parallel_tool_calls": True,
                            "context_window": 32000,
                            "experimental_supported_tools": [],
                            "input_modalities": ["text", "image"],
                        }
                    ]
                }
            )
        return web.json_response({"data": []})

    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    accessor = LocalAccessor(tmp_path / "workspace")
    for path, content in (fixture_files or {}).items():
        await accessor.write_file("/workspace/" + path, content)
    store = MemoryStore(fail_save=fail_checkpoint)
    state = {}
    events = []
    permissions = []
    external = ""

    async def binding(external_id, metadata):
        nonlocal external
        external = external_id
        state.update(metadata)

    owner = None

    async def emit(event):
        events.append(event)
        if (
            cancel_on_native
            and event.get("type") == "tool_started"
            and event.get("tool") == "bash"
        ):
            owner.cancel()

    async def permission(**kwargs):
        permissions.append(kwargs)
        return "deny"

    session = str(uuid.uuid4())
    auth = CodexAuthConfiguration(
        "openrouter",
        uuid.uuid4(),
        {"api_key": "local-fake-only"},
        base_url=f"http://127.0.0.1:{port}/v1",
    )
    try:
        engine = CodexEngine(
            tools=registry or extension_registry(),
            accessor=accessor,
            auth=auth,
            emit=emit,
            evaluator=evaluator,
            session_store=store,
            on_binding=binding,
        )
        owner = asyncio.create_task(
            engine.run(
                prompt,
                agent,
                "openrouter/test/model",
                mode,
                RunOptions(
                    session_id=session,
                    on_permission=permission,
                    skills=list(skills),
                    history=history or [],
                    steering=steering,
                ),
            )
        )
        if cancel_on_native:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 35)
            assert engine._owner_released
            confirmed = engine.terminal.is_set() and engine.status in {
                "interrupted",
                "completed",
            }
            assert state["codex_dirty"] is not (confirmed and not fail_checkpoint)
            assert (store.entry is not None) is (confirmed and not fail_checkpoint)
            assert engine.engine_state["codex_dirty"] == state["codex_dirty"]
            assert not any(e["type"] == "step_finish" for e in events)
            return None, events, accessor, requests, permissions, state
        result = await asyncio.wait_for(owner, 35)
        for next_agent, next_mode in [(agent, mode)] if resume else resume_modes:
            shutil.rmtree(accessor.path(home_for(session)))
            engine = CodexEngine(
                tools=extension_registry(),
                accessor=accessor,
                auth=auth,
                emit=emit,
                evaluator=evaluator,
                session_store=store,
                on_binding=binding,
                external_session_id=external,
                engine_state=state,
            )
            result = await asyncio.wait_for(
                engine.run(
                    resume_prompt,
                    next_agent,
                    "openrouter/test/model",
                    next_mode,
                    RunOptions(
                        session_id=session,
                        on_permission=permission,
                        skills=list(skills),
                        history=[
                            *(history or []),
                            LLMMessage(role="user", content=prompt),
                        ],
                    ),
                ),
                35,
            )
        return result, events, accessor, requests, permissions, state
    finally:
        for stream in accessor.streams:
            if stream.process.returncode is None:
                await stream.aclose()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_real_codex_managed_tool_checkpoint_cold_resume(tmp_path):
    result, events, accessor, requests, permissions, state = await fixture_run(
        tmp_path, resume=True
    )
    assert result.output == "Local done."
    assert result.usage.total_tokens > 0
    assert not permissions
    assert not state["codex_dirty"]
    assert sum(e["type"] == "tool_completed" for e in events) == 2, events
    deltas = [e["delta"] for e in events if e["type"] == "part_updated"]
    assert sum("text" in delta for delta in deltas) == 2
    assert sum("context" in delta for delta in deltas) == 4
    assert accessor.path("/workspace/note.txt").read_text() == "codex local test"
    assert all(s.process.returncode is not None for s in accessor.streams)
    assert any("function_call_output" in json.dumps(r) for r in requests)


@pytest.mark.asyncio
async def test_real_codex_plan_managed_write_denied(tmp_path):
    result, events, accessor, requests, permissions, state = await fixture_run(
        tmp_path, mode="plan", agent="plan"
    )
    assert result.output == "Local done."
    assert permissions
    assert not accessor.path("/workspace/note.txt").exists()
    assert any(e["type"] == "tool_error" for e in events)


@pytest.mark.asyncio
async def test_real_codex_explore_write_denied(tmp_path):
    _, events, accessor, _, permissions, _ = await fixture_run(
        tmp_path, agent="explore"
    )
    assert not accessor.path("/workspace/note.txt").exists()
    assert not permissions
    # Stored signatures are a ceiling; live dispatch still rejects explore writes.
    assert not any(e["type"] == "tool_started" for e in events)


class QueueStream:
    def __init__(self):
        self.input = asyncio.Queue()
        self.output = asyncio.Queue()

    async def send(self, data):
        await self.output.put(json.loads(data))

    async def receive(self):
        return await self.input.get()

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_rpc_server_request_does_not_block_response_pump():
    stream = QueueStream()
    gate = asyncio.Event()

    async def handler(method, params):
        await gate.wait()
        return {"success": False}

    rpc = CodexProtocol(stream, handler)
    request = asyncio.create_task(rpc.request("test", {}))
    sent = await stream.output.get()
    await stream.input.put(
        (
            json.dumps({"id": 0, "method": "item/tool/call", "params": {}})
            + "\n"
            + json.dumps({"id": sent["id"], "result": {"ok": True}})
            + "\n"
        ).encode()
    )
    assert await asyncio.wait_for(request, 1) == {"ok": True}
    gate.set()
    assert (await stream.output.get())["id"] == 0
    await rpc.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [b"\xff\n", b"[]\n", b"{" + b"x" * (16 * 1024 * 1024)]
)
async def test_rpc_invalid_oversized_frames_fail_pending(payload):
    stream = QueueStream()
    rpc = CodexProtocol(stream, lambda *_: None)
    request = asyncio.create_task(rpc.request("test", {}))
    await stream.output.get()
    await stream.input.put(payload)
    with pytest.raises(CodexProtocolError):
        await request
    await rpc.close()


@pytest.mark.asyncio
async def test_rpc_timeout_never_retries():
    stream = QueueStream()
    rpc = CodexProtocol(stream, lambda *_: None)
    with pytest.raises(TimeoutError):
        await rpc.request("turn/start", {}, timeout=0.01)
    assert stream.output.qsize() == 1
    assert rpc.failure is not None
    await rpc.close()


@pytest.mark.asyncio
async def test_real_codex_build_native_shell_write_no_approvals(tmp_path):
    result, events, accessor, _, permissions, _ = await fixture_run(
        tmp_path,
        tool="exec_command",
        args={"cmd": "printf native-verified > native.txt", "yield_time_ms": 1000},
    )
    assert result.output == "Local done."
    assert not permissions
    assert accessor.path("/workspace/native.txt").exists(), events
    assert accessor.path("/workspace/native.txt").read_text() == "native-verified"
    assert sum(e["type"] == "tool_started" and e["tool"] == "bash" for e in events) == 1
    assert (
        sum(e["type"] == "tool_completed" and e["tool"] == "bash" for e in events) == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent,mode,deny,allowed,asks",
    [
        ("build", "build", False, True, 0),
        ("build", "plan", False, False, 1),
        ("general", "plan", False, True, 0),
        ("explore", "build", False, False, 0),
        ("build", "build", True, False, 0),
    ],
    ids=["build", "plan", "general-under-plan", "explore", "global-deny"],
)
async def test_real_codex_code_mode_managed_write_respects_permissions(
    tmp_path: Path,
    agent: str,
    mode: str,
    deny: bool,
    allowed: bool,
    asks: int,
) -> None:
    """Model-issued exec calls reach the shared managed-tool permission checks."""
    result, events, accessor, requests, permissions, _ = await fixture_run(
        tmp_path,
        agent=agent,
        mode=mode,
        tool="exec",
        code=(
            "text(await tools.mcp_test_write("
            '{path:"note.txt",content:"code-mode write"}));'
        ),
        evaluator=PermissionEvaluator(global_rules={"edit": "deny"}) if deny else None,
    )
    assert result.output == "Local done."
    assert "code-mode host is disabled" not in json.dumps(requests)
    assert len(permissions) == asks
    if allowed:
        assert accessor.path("/workspace/note.txt").read_text() == "code-mode write"
        assert any(e["type"] == "tool_completed" for e in events)
    else:
        assert not accessor.path("/workspace/note.txt").exists()
        assert any(e["type"] == "tool_error" for e in events)


@pytest.mark.asyncio
async def test_real_codex_code_mode_native_shell(tmp_path: Path) -> None:
    """Required code mode also supports native workspace commands in Build."""
    _, events, accessor, requests, permissions, _ = await fixture_run(
        tmp_path,
        tool="exec",
        code=(
            "text(await tools.exec_command({"
            'cmd:"printf code-mode-native > native.txt",yield_time_ms:1000}));'
        ),
    )
    assert not permissions
    assert accessor.path("/workspace/native.txt").read_text() == "code-mode-native"
    assert any(e["type"] == "tool_completed" for e in events)
    assert "code-mode host is disabled" not in json.dumps(requests)


@pytest.mark.asyncio
async def test_real_codex_code_mode_script_failure_is_returned(tmp_path: Path) -> None:
    """A script error reaches the model without running unrelated workspace tools."""
    result, events, accessor, requests, _, state = await fixture_run(
        tmp_path, tool="exec", code='throw new Error("local script failure");'
    )
    assert result.output == "Local done."
    assert "local script failure" in json.dumps(requests[-1]["input"])
    assert not any(e["type"] == "tool_started" for e in events)
    assert not accessor.path("/workspace/note.txt").exists()
    assert not state["codex_dirty"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_checkpoint", [False, True])
async def test_real_codex_cancel_interrupts_then_releases_process(
    tmp_path, fail_checkpoint
):
    _, events, accessor, _, _, state = await fixture_run(
        tmp_path,
        tool="exec_command",
        args={"cmd": "sleep 30", "yield_time_ms": 1000},
        cancel_on_native=True,
        fail_checkpoint=fail_checkpoint,
    )
    assert "release" in accessor.actions
    assert all(stream.process.returncode is not None for stream in accessor.streams)
    assert state["codex_dirty"] is fail_checkpoint


@pytest.mark.asyncio
async def test_real_codex_plan_build_plan_same_thread(tmp_path):
    """Cold resumes retain primary tool signatures while edit policy changes."""
    result, events, accessor, requests, permissions, state = await fixture_run(
        tmp_path,
        agent="plan",
        mode="plan",
        resume_modes=[("build", "build"), ("plan", "plan")],
    )
    assert result.output == "Local done."
    assert len(permissions) == 2
    assert sum(e["type"] == "tool_completed" for e in events) == 1
    assert sum(e["type"] == "tool_error" for e in events) == 2
    assert accessor.path("/workspace/note.txt").read_text() == "codex local test"
    assert not state["codex_dirty"]


@pytest.mark.asyncio
async def test_real_codex_general_native_shell_under_plan_parent(tmp_path):
    """The general subagent remains YOLO even with inherited parent plan mode."""
    _, events, accessor, _, permissions, _ = await fixture_run(
        tmp_path,
        agent="general",
        mode="plan",
        tool="exec_command",
        args={"cmd": "printf general > child.txt", "yield_time_ms": 1000},
    )
    assert not permissions
    assert accessor.path("/workspace/child.txt").read_text() == "general"
    assert any(e["type"] == "tool_completed" for e in events)
