"""Claude engine policy, launch and real-SDK local SSE tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import claude_agent_sdk
import pytest
from aiohttp import web
from pydantic import BaseModel, Field

from apps.harness.access.file_types import FileContent
from apps.harness.agents.definitions import get_agent
from apps.harness.engines.catalog import (
    CLAUDE_AGENT_SDK_VERSION,
    CLAUDE_CODE_CLI_VERSION,
)
from apps.harness.engines.claude.engine import ClaudeEngine
from apps.harness.engines.claude.policy import decide_tool, native_tools_for_claude
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.providers.base import LLMMessage
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import Tool, ToolContext, ToolRegistry, ToolResult

BUNDLED_CLAUDE = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


@cache
def require_pinned_cli() -> None:
    """Fail, rather than silently skip contracts, if SDK and runner pins drift."""
    assert claude_agent_sdk.__version__ == CLAUDE_AGENT_SDK_VERSION
    assert BUNDLED_CLAUDE.is_file(), "Pinned SDK CLI is unavailable"
    version = subprocess.check_output(
        [str(BUNDLED_CLAUDE), "--version"], text=True, timeout=15
    )
    assert version.split()[0] == CLAUDE_CODE_CLI_VERSION


def test_bundled_cli_matches_runner_artifact_and_sdk_pin() -> None:
    """Real-CLI contracts must exercise exactly the production runner artifact."""
    import hashlib

    require_pinned_cli()
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[4]
            / "runner/src/runtime/artifact_manifest.json"
        ).read_text()
    )["artifacts"]["claude-agent"]
    assert manifest["version"] == CLAUDE_CODE_CLI_VERSION
    digest = hashlib.sha256(BUNDLED_CLAUDE.read_bytes()).hexdigest()
    assert any(
        pin["sha256"] == digest and pin["size"] == BUNDLED_CLAUDE.stat().st_size
        for pin in manifest["platforms"].values()
    )


class FakeProcessStream:
    """Workspace-byte-stream adapter for a local bundled Claude CLI process."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        guest_root: Path,
        remote_config_dir: str,
        local_config_dir: str,
    ) -> None:
        self.process = process
        self.guest_workspace = str(guest_root / "workspace").encode()
        self.guest_config = local_config_dir.encode()
        self.remote_config = remote_config_dir.encode()
        self.stderr = b""
        self.frames: list[dict[str, Any]] = []
        self._stderr = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        if self.process.stderr is not None:
            self.stderr = await self.process.stderr.read()

    async def receive(self) -> bytes:
        assert self.process.stdout is not None
        line = await self.process.stdout.readline()
        if not line:
            return b""
        frame = json.loads(line)
        self.frames.append(frame)
        if frame.get("type") == "transcript_mirror":
            path = str(frame.get("filePath") or "")
            local = self.guest_config.decode()
            if path.startswith(local + "/"):
                frame["filePath"] = self.remote_config.decode() + path[len(local) :]
        return json.dumps(frame, ensure_ascii=False).encode() + b"\n"

    async def send(self, data: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def send_eof(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
            await self.process.stdin.wait_closed()

    async def wait_closed(self) -> int | None:
        return await self.process.wait()

    async def aclose(self) -> None:
        if self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        await self._stderr


class FixtureSessionStore:
    """In-memory transcript mirror used by real CLI fresh/resume tests."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self.load_calls: list[tuple[str, str, str]] = []

    @staticmethod
    def _key(key: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(key.get("project_key") or ""),
            str(key.get("session_id") or ""),
            str(key.get("subpath") or ""),
        )

    async def append(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        self.entries.setdefault(self._key(key), []).extend(entries)

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        normalized = self._key(key)
        self.load_calls.append(normalized)
        entries = self.entries.get(normalized)
        return list(entries) if entries is not None else None

    async def list_subkeys(self, _key: dict[str, Any]) -> list[str]:
        return []


class LocalSseWorkspaceAccessor:
    """Only fake runner boundary; the actual pinned SDK/CLI are used."""

    def __init__(self, root: Path, base_env: dict[str, str]) -> None:
        self.workspace_id = str(uuid.uuid4())
        self.root = root
        self.base_env = dict(base_env)
        self.commands: list[list[str]] = []
        self.opened: list[dict[str, Any]] = []
        self.lease_epoch = str(uuid.uuid4())
        self._files: dict[str, bytes] = {}
        self.project_key = ""
        self._remote_files: dict[str, bytes] = {}
        self.managed_processes: list[dict[str, Any]] = []

    def guest_path(self, path: str) -> Path:
        workspace_root = self.root / "workspace"
        if path == "/workspace":
            return workspace_root
        if path.startswith("/workspace/"):
            return workspace_root / path.removeprefix("/workspace/")
        if path == str(self.root / "guest"):
            return workspace_root
        if path.startswith(str(self.root / "guest") + "/"):
            return workspace_root / path.removeprefix(str(self.root / "guest") + "/")
        return Path(path)

    async def exec_wait(
        self,
        command: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        argv = [str(item) for item in command]
        self.commands.append(argv)
        if argv[:2] == ["mkdir", "-p"]:
            self.guest_path(argv[2]).mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(exit_code=0, stdout="", stderr="")
        if len(argv) >= 5 and argv[:2] == ["python3", "-c"]:
            base, target = self.guest_path(argv[-2]), self.guest_path(argv[-1])
            if target != base and base not in target.parents:
                return SimpleNamespace(exit_code=2, stdout="", stderr="")
            for component in target.relative_to(base).parts:
                base = base / component
                if base.is_symlink():
                    return SimpleNamespace(exit_code=3, stdout="", stderr="")
                base.mkdir(mode=0o700, parents=True, exist_ok=True)
                base.chmod(0o700)
            return SimpleNamespace(exit_code=0, stdout="", stderr="")
        raise AssertionError(f"Unexpected fixture command: {argv!r}")

    def real_path(self, path: str) -> Path:
        return self.guest_path(path)

    async def read_file_no_follow(
        self, path: str, max_size: int | None = None
    ) -> FileContent:
        from apps.harness.access.safe_files import read_file_no_follow

        return await read_file_no_follow(
            self, path, max_size=max_size, root="/workspace"
        )

    async def write_file(self, path: str, content: bytes, mode: int = 0o644) -> None:
        target = self.real_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(content))
        target.chmod(mode)
        if path.startswith("/workspace/.opencuria/"):
            self._remote_files[path] = bytes(content)
        elif path.startswith("/workspace/"):
            self._files[path] = bytes(content)
            relative = path.removeprefix("/workspace/")
            self._remote_files[f"/workspace/{relative}"] = bytes(content)

    async def read_file(self, path: str, max_size: int | None = None) -> FileContent:
        if path in self._files:
            return FileContent(content=self._files[path], size=len(self._files[path]))
        if path in self._remote_files:
            if path == "/workspace/approved-note.txt":
                candidate = self.real_path(path)
                content = (
                    candidate.read_bytes()
                    if candidate.is_file()
                    else self._remote_files[path]
                )
            else:
                content = self._remote_files[path]
            if max_size is not None:
                content = content[:max_size]
            return FileContent(
                content=content,
                size=len(self._remote_files[path]),
                truncated=len(content) < len(self._remote_files[path]),
            )
        target = self.real_path(path)
        if target.is_file():
            content = target.read_bytes()
        else:
            raise FileNotFoundError(path)
        full_size = len(content)
        if max_size is not None:
            content = content[:max_size]
        return FileContent(
            content=content, size=full_size, truncated=len(content) < full_size
        )

    async def desktop_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if action == "binding":
            return {"ok": True, "epoch": self.lease_epoch}
        if action == "reserve":
            return {"ok": True, "epoch": self.lease_epoch, "lease_state": "reserved"}
        if action == "release":
            return {"ok": True, "lease_state": "released"}
        if action == "renew":
            return {"ok": True, "lease_state": "reserved"}
        raise AssertionError(f"Unexpected lease action {action!r}")

    async def open_process(
        self,
        command: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        *,
        owner: dict[str, str] | None = None,
    ) -> FakeProcessStream:
        argv = list(command)
        if argv[:2] == ["python3", "-c"] or argv[:3] == ["python3", "-u", "-c"]:
            assert env in (None, {})
            assert len(argv) >= 5
            original = list(argv)
            argv[0] = sys.executable
            path_index = argv.index("-c") + 2
            argv[path_index] = str(self.guest_path(argv[path_index]))
            argv[path_index + 1] = str(self.guest_path(argv[path_index + 1]))
            cwd = self.guest_path(workdir)
            cwd.mkdir(parents=True, exist_ok=True)
            self.managed_processes.append(
                {
                    "argv": original,
                    "workdir": workdir,
                    "env": dict(env or {}),
                }
            )
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env={},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            return FakeProcessStream(
                process,
                guest_root=self.root,
                remote_config_dir="",
                local_config_dir="",
            )
        assert argv[0] == "/opt/opencuria/runtimes/claude-agent/2.1.310/claude"
        argv[0] = str(BUNDLED_CLAUDE)
        argv = [
            str(self.guest_path(item))
            if item.startswith("/workspace/.opencuria/")
            else item
            for item in argv
        ]
        remote_env = {**self.base_env, **(env or {})}
        remote_env["TMPDIR"] = str(self.root / "tmp")
        # Mirror and restore retain the guest project key despite the local cwd.
        remote_env["CLAUDE_CODE_PROJECT_DIR_NAME"] = "-workspace"
        Path(remote_env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
        guest_cwd = str(self.guest_path(workdir))
        if guest_cwd.startswith(str(self.root / "workspace")):
            remote_env["PWD"] = guest_cwd
        guest_config = str(remote_env.get("CLAUDE_CONFIG_DIR") or "")
        remote_config = guest_config
        if "CLAUDE_CONFIG_DIR" in remote_env and remote_env[
            "CLAUDE_CONFIG_DIR"
        ].startswith("/workspace/"):
            remote_env["CLAUDE_CONFIG_DIR"] = str(
                self.guest_path(remote_env["CLAUDE_CONFIG_DIR"])
            )
        guest_cwd = self.guest_path(workdir)
        guest_cwd.mkdir(parents=True, exist_ok=True)
        if not self.project_key:
            from claude_agent_sdk import project_key_for_directory

            self.project_key = project_key_for_directory(str(guest_cwd))
        cwd = guest_cwd
        self.opened.append(
            {
                "argv": argv,
                "cwd": str(cwd),
                "guest_cwd": str(guest_cwd),
                "env": dict(remote_env),
                "owner": owner,
            }
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=remote_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stream = FakeProcessStream(
            process,
            guest_root=self.root,
            remote_config_dir=remote_config,
            local_config_dir=str(self.guest_path(guest_config)),
        )
        self.opened[-1]["stream"] = stream
        return stream


class WriteNoteArgs(BaseModel):
    """Minimal local MCP fixture input."""

    text: str = Field(min_length=1)


class WriteNoteTool(Tool):
    """Record one test write without touching the filesystem."""

    name = "mcp_demo_server_write_note"
    permission_key = "mcp_demo_server_write_note"
    description = "Write a test note."
    args_schema = WriteNoteArgs

    def __init__(self) -> None:
        self.writes: list[str] = []

    def title(self, args: BaseModel) -> str:
        return f"Write note: {self.coerce_args(args).text}"

    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        validated = self.coerce_args(args)
        self.writes.append(validated.text)
        return ToolResult(output=f"saved: {validated.text}")


@pytest.fixture
def engine() -> ClaudeEngine:
    return ClaudeEngine(tools=default_tool_registry(), accessor=object())


@pytest.mark.asyncio
async def test_missing_resume_transcript_fails_without_fresh_or_local_fallback() -> (
    None
):
    from apps.harness.engines.claude.errors import ClaudeTranscriptError

    external_id = str(uuid.uuid4())
    store = FixtureSessionStore()
    engine = ClaudeEngine(
        tools=default_tool_registry(),
        accessor=object(),
        external_session_id=external_id,
        session_store=store,
    )
    engine._project_key = "workspace"
    with pytest.raises(ClaudeTranscriptError, match="Fork this conversation"):
        await engine._restore_transcript(
            external_id=external_id, cwd="/workspace", config_dir="/unused"
        )
    assert engine.external_session_id == external_id
    assert store.load_calls == [("workspace", external_id, "")]


@pytest.mark.asyncio
async def test_resume_without_authoritative_store_fails() -> None:
    from apps.harness.engines.claude.errors import ClaudeTranscriptError

    engine = ClaudeEngine(tools=ToolRegistry(), accessor=object())
    with pytest.raises(ClaudeTranscriptError, match="is missing"):
        await engine._restore_transcript(
            external_id=str(uuid.uuid4()), cwd="/workspace", config_dir="/unused"
        )


def test_oversized_transcript_retains_explicit_recovery_contract(monkeypatch) -> None:
    from apps.harness.engines.claude.errors import ClaudeTranscriptError

    monkeypatch.setattr(
        "apps.harness.engines.claude.engine.MAX_RESTORED_TRANSCRIPT_BYTES", 16
    )
    with pytest.raises(ClaudeTranscriptError, match="Fork this conversation"):
        ClaudeEngine._jsonl_bytes([{"text": "x" * 17}])


def test_cli_argv_is_explicit_bounded_and_resume_is_uuid() -> None:
    engine = ClaudeEngine(tools=default_tool_registry(), accessor=object())
    sid = str(uuid.uuid4())
    argv = engine._runtime_state.build_command(
        binary="/opt/opencuria/runtimes/claude-agent/2.1.310/claude",
        prompt_path="/workspace/.opencuria/harness/claude/abc/prompt.md",
        model="claude-sonnet-4-5",
        effort="high",
        permission_mode="default",
        native_tools=["Bash", "Read"],
        external_session_id=sid,
        fresh_session_id=str(uuid.uuid4()),
        has_session_store=True,
    )
    assert "--output-format" in argv and "stream-json" in argv
    assert "--input-format" in argv
    assert "--setting-sources=" in argv
    assert "--strict-mcp-config" in argv
    assert "--include-partial-messages" in argv
    assert "--include-hook-events" in argv
    assert f"--resume={sid}" in argv
    assert "--session-mirror" in argv
    assert "--append-system-prompt-file" in argv
    assert "--system-prompt-file" not in argv
    assert "--disallowedTools" in argv
    assert "Skill" not in argv[argv.index("--disallowedTools") + 1]
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "bypassPermissions" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert all(len(value) < 4096 for value in argv)


def test_cli_argv_fresh_run_mirrors_session_without_forcing_resume() -> None:
    engine = ClaudeEngine(tools=ToolRegistry(), accessor=object())
    session_id = str(uuid.uuid4())
    argv = engine._runtime_state.build_command(
        binary="/opt/opencuria/runtimes/claude-agent/2.1.310/claude",
        prompt_path="/workspace/.opencuria/harness/claude/abc/system-prompt.md",
        model="sonnet",
        effort="high",
        permission_mode="plan",
        native_tools=["Bash", "Read", "Skill"],
        external_session_id="",
        fresh_session_id=session_id,
        has_session_store=True,
        plugin_dir="/workspace/.opencuria/harness/claude/abc/opencuria-skills",
    )
    assert f"--session-id={session_id}" in argv
    assert "--resume" not in argv and "--no-session-persistence" not in argv
    assert "--session-mirror" in argv
    assert argv[argv.index("--tools") + 1] == "Bash,Read,Skill"
    assert "--plugin-dir" in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "bypassPermissions" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "Bash" not in argv[argv.index("--disallowedTools") + 1]


@pytest.mark.asyncio
async def test_run_rejects_oversized_skills_before_managed_processes() -> None:
    from apps.harness.engines.base import RunOptions
    from apps.harness.engines.claude.runtime_state import MAX_SKILL_BODY_BYTES

    class RecordingAccessor:
        workspace_id = str(uuid.uuid4())

        def __init__(self) -> None:
            self.open_process_calls: list[list[str]] = []

        async def open_process(self, command: list[str], **_kwargs: Any) -> None:
            self.open_process_calls.append(command)

    accessor = RecordingAccessor()
    engine = ClaudeEngine(tools=default_tool_registry(), accessor=accessor)  # type: ignore[arg-type]
    options = RunOptions(
        session_id=str(uuid.uuid4()),
        workspace_id=accessor.workspace_id,
        skills=["x" * (MAX_SKILL_BODY_BYTES + 1)],
    )

    with pytest.raises(ValueError, match="skill body exceeds"):
        await engine.run("prompt", "build", "sonnet", opts=options)

    assert accessor.open_process_calls == []


def test_unknown_native_and_mcp_tools_fail_closed() -> None:
    tools = default_tool_registry()
    evaluator = PermissionEvaluator(global_rules={"*": "allow"})
    for name in ("UnknownTool", "mcp__unknown__tool"):
        decision, *_ = decide_tool(
            source_name=name,
            arguments={},
            tools=tools,
            agent=get_agent("build"),
            evaluator=evaluator,
            mode="build",
            depth=0,
            max_depth=2,
        )
        assert decision == "deny"


def test_plan_native_write_asks_and_bash_is_exposed() -> None:
    tools = default_tool_registry()
    decision, event_tool, action, _title = decide_tool(
        source_name="Write",
        arguments={"file_path": "/workspace/notes.txt", "content": "fixture"},
        tools=tools,
        agent=get_agent("plan"),
        evaluator=PermissionEvaluator(),
        mode="plan",
        depth=0,
        max_depth=2,
    )
    assert (decision, event_tool, action) == ("ask", "write", "/workspace/notes.txt")
    assert "Bash" in native_tools_for_claude(
        tools=tools,
        agent=get_agent("plan"),
        evaluator=PermissionEvaluator(),
        mode="plan",
        depth=0,
        max_depth=2,
    )


def _sse(event_type: str, payload: dict[str, Any]) -> bytes:
    """Encode one Anthropic SSE event for the local fixture."""
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n".encode()


def _api_message_start(message_id: str) -> bytes:
    return _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 8, "output_tokens": 0},
            },
        },
    )


def _api_tool_message(name: str, tool_id: str, arguments: dict[str, Any]) -> bytes:
    return b"".join(
        [
            _api_message_start(f"msg-{tool_id}"),
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": {},
                    },
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(arguments),
                    },
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 2},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
    )


def _api_final_message(message_id: str = "final") -> bytes:
    return b"".join(
        [
            _api_message_start(message_id),
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "fixture answer"},
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 3},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
    )


async def _run_local_cli_fixture(
    tmp_path: Path,
    *,
    tool_name: str,
    tool_arguments: dict[str, Any],
    tools: ToolRegistry,
    evaluator: PermissionEvaluator | None = None,
    mode: str = "build",
    prompt: str = "Use the available test tool, then answer fixture.",
    store: FixtureSessionStore | None = None,
    session_id: str | None = None,
    external_session_id: str = "",
    cwd: str | None = None,
    sdk_config_dir: str | None = None,
    history: list[LLMMessage] | None = None,
    fixture_files: dict[str, bytes] | None = None,
    permission_response: str = "once",
    steering: Any | None = None,
    question_hook: Any | None = None,
) -> tuple[
    Any,
    list[dict[str, Any]],
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    FixtureSessionStore,
    LocalSseWorkspaceAccessor,
]:
    """Run the actual pinned SDK CLI against an in-process local SSE endpoint."""
    requests: list[dict[str, Any]] = []
    sent_tool = False

    async def messages(request: web.Request) -> web.StreamResponse:
        nonlocal sent_tool
        payload = await request.json()
        requests.append(payload)
        response = web.StreamResponse(
            status=200,
            headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
        )
        await response.prepare(request)
        available = {str(item.get("name") or "") for item in payload.get("tools") or []}
        fixture = tool_name.removeprefix("mcp__opencuria__")
        can_call = tool_name in available or any(
            name == fixture or name.endswith("__" + fixture) for name in available
        )
        if tool_name and not sent_tool and can_call:
            sent_tool = True
            body = _api_tool_message(tool_name, f"call-{len(requests)}", tool_arguments)
        else:
            body = _api_final_message()
        await response.write(body)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    accessor = LocalSseWorkspaceAccessor(
        tmp_path,
        {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CLAUDE_CONFIG_DIR": sdk_config_dir
            or f"/workspace/.opencuria/harness/claude/{uuid.uuid4()}/config",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "MCP_TIMEOUT": "20000",
        },
    )
    for relative, content in (fixture_files or {}).items():
        target = accessor.guest_path(f"/workspace/{relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        accessor._files[f"/workspace/{relative}"] = bytes(content)
        accessor._remote_files[f"/workspace/{relative}"] = bytes(content)
    emitted: list[dict[str, Any]] = []
    questions: list[str] = []
    permissions: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        emitted.append(event)

    async def on_question(**kwargs: Any) -> list[str]:
        questions.append(str(kwargs.get("call_id") or ""))
        if question_hook is not None:
            await question_hook()
        return ["fixture answer"]

    async def on_permission(**kwargs: Any) -> str:
        permissions.append(dict(kwargs))
        return permission_response

    fixture_store = store or FixtureSessionStore()
    engine = ClaudeEngine(
        tools=tools,
        accessor=accessor,
        emit=emit,
        auth_env={"ANTHROPIC_API_KEY": "sk-ant-test-fixture-only"},
        evaluator=evaluator,
        external_session_id=external_session_id,
        session_store=fixture_store,
        runtime_artifact_ensurer=lambda *_args: asyncio.sleep(
            0,
            result={
                "ok": True,
                "path": "/opt/opencuria/runtimes/claude-agent/2.1.310/claude",
            },
        ),
    )
    from apps.harness.engines.base import RunOptions

    options = RunOptions(
        session_id=session_id or str(uuid.uuid4()),
        workspace_id=accessor.workspace_id,
        cwd=cwd or "/workspace",
        history=history or [],
        on_question=on_question,
        on_permission=on_permission,
        steering=steering,
    )
    try:
        result = await asyncio.wait_for(
            engine.run(
                prompt, "plan" if mode == "plan" else "build", "sonnet", mode, options
            ),
            timeout=60,
        )
    finally:
        await server.cleanup()
    return result, emitted, questions, requests, permissions, fixture_store, accessor


@pytest.mark.asyncio
async def test_real_sdk_cli_mcp_hook_question_and_followup_sse(tmp_path: Path) -> None:
    require_pinned_cli()
    (
        result,
        events,
        questions,
        requests,
        _permissions,
        _store,
        _accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="mcp__opencuria__question",
        tool_arguments={
            "questions": [
                {
                    "question": "Continue?",
                    "header": "Fixture",
                    "options": [{"label": "Yes", "description": "Continue"}],
                    "multiple": False,
                }
            ]
        },
        tools=default_tool_registry(),
    )
    assert result.output == "fixture answer"
    assert len(requests) == 3
    assert not requests[0].get("tools")
    assert any(tool["name"].endswith("question") for tool in requests[1]["tools"])
    assert len(questions) == 1 and questions[0]
    finishes = [event for event in events if event["type"] == "step_finish"]
    assert finishes and isinstance(finishes[-1]["tokens"], dict)
    assert len(finishes) == len(
        [event for event in events if event["type"] == "step_start"]
    )
    assert len(finishes) == 2
    assert (
        sum(event["tokens"]["total_tokens"] for event in finishes)
        == result.usage.total_tokens
    )
    assert [event["type"] for event in events].count("tool_queued") == 1
    assert [event["type"] for event in events].count("tool_started") == 1
    assert [event["type"] for event in events].count("tool_completed") == 1


@pytest.mark.asyncio
async def test_real_cli_fresh_session_mirror_is_resumable_without_network(
    tmp_path: Path,
) -> None:
    require_pinned_cli()
    (
        result,
        _events,
        _questions,
        requests,
        _permissions,
        store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="",
        tool_arguments={},
        tools=default_tool_registry(),
        prompt="Answer fixture without calling tools.",
        session_id=str(uuid.uuid4()),
        cwd="/workspace",
    )
    external_id = result.metadata["external_session_id"]
    assert uuid.UUID(external_id)
    assert any(key[1] == external_id and rows for key, rows in store.entries.items())
    command = accessor.opened[0]["argv"]
    assert f"--session-id={external_id}" in command
    assert "--session-mirror" in command and "--no-session-persistence" not in command
    project_key = next(key[0] for key in store.entries if key[1] == external_id)
    from claude_agent_sdk import project_key_for_directory

    assert project_key == project_key_for_directory("/workspace")
    load_count = len(store.load_calls)
    (
        resumed,
        _events,
        _questions,
        resume_requests,
        _permissions,
        _store,
        resume_accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="",
        tool_arguments={},
        tools=default_tool_registry(),
        prompt="Continue the fixture conversation and answer fixture.",
        store=store,
        session_id=str(uuid.uuid4()),
        external_session_id=external_id,
        cwd="/workspace",
    )
    assert resumed.output == "fixture answer"
    assert resumed.metadata["external_session_id"] == external_id
    assert any(call[1] == external_id for call in store.load_calls[load_count:])
    assert f"--resume={external_id}" in resume_accessor.opened[0]["argv"]
    assert any(
        "Answer fixture without calling tools." in json.dumps(req)
        for req in resume_requests
    )


@pytest.mark.asyncio
async def test_real_sdk_cli_local_bash_task_does_not_emit_subtask_events(
    tmp_path: Path,
) -> None:
    """A foreground Bash task has CLI task frames but is not an Agent subtask."""
    require_pinned_cli()

    (
        result,
        events,
        _questions,
        _requests,
        _permissions,
        _store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Bash",
        tool_arguments={"command": "sleep 3; printf 'local-bash-completed\\n'"},
        tools=default_tool_registry(),
        prompt=(
            "Run this foreground Bash command without delegating it: "
            "sleep 3; printf 'local-bash-completed\\n'. "
            "Then report its output."
        ),
        cwd="/workspace",
    )

    assert result.output == "fixture answer"
    completed = [
        event
        for event in events
        if event.get("type") == "tool_completed" and event.get("tool") == "bash"
    ]
    assert len(completed) == 1
    assert "local-bash-completed" in completed[0]["output"]

    # Observe raw pinned-CLI frames: absence of projected child events is only
    # meaningful if the CLI actually reported its local Bash task lifecycle.
    local_bash_starts = [
        frame
        for frame in accessor.opened[0]["stream"].frames
        if frame.get("type") == "system"
        and frame.get("subtype") == "task_started"
        and frame.get("task_type") == "local_bash"
    ]
    assert local_bash_starts
    assert not any(
        event.get("type") in {"subtask_started", "subtask_updated", "subtask_finished"}
        for event in events
    )


@pytest.mark.asyncio
async def test_real_sdk_cli_plan_native_write_uses_open_curia_approval(
    tmp_path: Path,
) -> None:
    require_pinned_cli()
    target = tmp_path / "workspace" / "approved-note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    (
        result,
        events,
        _questions,
        _requests,
        permissions,
        _store,
        _accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Write",
        tool_arguments={"file_path": str(target), "content": "approved"},
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(global_rules={"write": "ask"}),
        prompt="Write the approved note, then answer fixture.",
        mode="plan",
        cwd="/workspace",
    )
    assert result.output == "fixture answer"
    assert target.read_text() == "approved"
    assert len(permissions) == 1 and permissions[0]["tool"] == "write"
    assert any(event["type"] == "tool_completed" for event in events)


@pytest.mark.asyncio
async def test_real_sdk_cli_mcp_ask_is_approved_once_before_dispatch(
    tmp_path: Path,
) -> None:
    require_pinned_cli()
    registry = ToolRegistry()
    note = WriteNoteTool()
    registry.register(note)
    (
        result,
        events,
        _questions,
        requests,
        permissions,
        _store,
        _accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name=f"mcp__opencuria__{note.name}",
        tool_arguments={"text": "approved fixture"},
        tools=registry,
        evaluator=PermissionEvaluator(
            global_rules={"mcp_demo_server_write_note": "ask"}
        ),
        prompt=(
            "Use mcp_demo_server_write_note with text=approved fixture, "
            "then answer fixture."
        ),
    )
    assert result.output == "fixture answer" and note.writes == ["approved fixture"]
    assert len(requests) == 3
    queued = next(event for event in events if event["type"] == "tool_queued")
    assert queued["tool"] == note.name
    assert queued["arguments"] == '{"text": "approved fixture"}'
    assert [event["type"] for event in events].count("tool_started") == 1
    assert [event["type"] for event in events].count("tool_completed") == 1
    assert len(permissions) == 1 and permissions[0]["call_id"] == queued["call_id"]
    assert permissions[0]["action"] == ""


@pytest.mark.asyncio
async def test_real_sdk_cli_folds_steer_into_the_running_turn(tmp_path: Path) -> None:
    """A message queued while a tool runs reaches the model with the tool result."""
    require_pinned_cli()
    from apps.harness.steering import SteeringChannel

    delivered: list[str] = []

    async def deliver(entries: list[Any]) -> None:
        delivered.extend(entry.content for entry in entries)

    channel = SteeringChannel(deliver=deliver)

    async def steer_while_tool_waits() -> None:
        channel.submit("Please call the file steered.txt instead")
        # Give the input feed time to hand the message to the CLI.
        await asyncio.sleep(1)

    (
        result,
        _events,
        questions,
        requests,
        _perm,
        _store,
        _acc,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="mcp__opencuria__question",
        tool_arguments={
            "questions": [
                {
                    "question": "Continue?",
                    "header": "Fixture",
                    "options": [{"label": "Yes", "description": "Continue"}],
                    "multiple": False,
                }
            ]
        },
        tools=default_tool_registry(),
        steering=channel,
        question_hook=steer_while_tool_waits,
    )
    assert len(questions) == 1
    assert delivered == ["Please call the file steered.txt instead"]
    assert result.output == "fixture answer"
    # Title generation also sees the text; only the agent conversation counts.
    conversation = [request for request in requests if request.get("tools")]
    # One request per model turn: the steer joined the tool-result turn instead
    # of starting a separate turn after the answer.
    assert len(conversation) == 2
    folded = [
        message
        for message in conversation[-1]["messages"]
        if message["role"] == "user" and "tool_result" in json.dumps(message["content"])
    ]
    assert len(folded) == 1
    assert "steered.txt" in json.dumps(folded[0]["content"])
    assert channel.pending() == []
