"""Real pinned Claude CLI contract tests against the local SSE fixture only."""

from __future__ import annotations

import base64
import json
import shlex
import uuid
from pathlib import Path
from typing import Any

import claude_agent_sdk
import pytest

from apps.harness.engines.claude.engine import ClaudeEngine
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.tests.conftest import TINY_PNG
from apps.harness.tests.test_claude_engine import (
    BUNDLED_CLAUDE,
    FixtureSessionStore,
    WriteNoteTool,
    _run_local_cli_fixture,
)
from apps.harness.tools import default_tool_registry


def _require_pinned_cli() -> None:
    """Skip when the repository's pinned SDK/CLI fixture is unavailable."""
    if claude_agent_sdk.__version__ != "0.2.164" or not BUNDLED_CLAUDE.is_file():
        pytest.skip("Pinned SDK CLI is unavailable")


def _system_text(request: dict[str, Any]) -> str:
    """Flatten the system prompt as sent to Anthropic by the real CLI."""
    system = request.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            str(block.get("text") or "") for block in system if isinstance(block, dict)
        )
    return ""


def _tool_schemas(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the provider-facing schemas captured by the local endpoint."""
    tools = request.get("tools")
    return (
        [item for item in tools if isinstance(item, dict)]
        if isinstance(tools, list)
        else []
    )


def _assert_cli_permission_mode(accessor: Any, mode: str) -> None:
    """Ensure the real CLI retains OpenCuria's mode without permission bypasses."""
    argv = accessor.opened[0]["argv"]
    assert argv[argv.index("--permission-mode") + 1] == (
        "plan" if mode == "plan" else "default"
    )
    assert "bypassPermissions" not in argv
    assert "--dangerously-skip-permissions" not in argv
    settings_path = argv[argv.index("--settings") + 1]
    settings = json.loads(accessor.guest_path(settings_path).read_text())
    assert "bypassPermissions" not in json.dumps(settings)
    assert "auto_approve" not in json.dumps(settings)


def _bash_write_command(path: Path, content: str, output: str) -> str:
    """Build a Python-plus-shell command with observable output and a file write."""
    python_script = "print(6)"
    return " ".join(
        [
            " ".join(shlex.quote(item) for item in ("python3", "-c", python_script)),
            "&&",
            "printf",
            shlex.quote(content),
            ">",
            shlex.quote(str(path)),
            "&&",
            "printf",
            shlex.quote(output),
        ]
    )


def _minimal_pdf() -> bytes:
    """Build a tiny one-page PDF with a valid cross-reference table."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1 1] >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode())
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(b"xref\n0 4\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        b"trailer\n<< /Root 1 0 R /Size 4 >>\nstartxref\n"
        + f"{xref_offset}\n".encode()
        + b"%%EOF\n"
    )
    return bytes(payload)


@pytest.mark.asyncio
async def test_real_cli_reuses_binding_with_minimal_extension_in_plan(
    tmp_path: Path,
) -> None:
    """Native history resumes without adding OpenCuria-composed project context."""
    _require_pinned_cli()
    session_id = "c391aac1-8040-46ce-bcac-ff26a7f66324"
    store = FixtureSessionStore()
    context_path = tmp_path / "workspace" / "AGENTS.md"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text("BUILD_CONTEXT_MARKER: original project rules\n")

    (
        built,
        _build_events,
        _build_questions,
        build_requests,
        _build_permissions,
        build_store,
        build_accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="",
        tool_arguments={},
        tools=default_tool_registry(),
        prompt="Remember the build-history marker: ORIGINAL_USER_TURN.",
        store=store,
        session_id=session_id,
        cwd="/workspace",
    )
    external_id = str(built.metadata["external_session_id"])
    assert external_id
    uuid.UUID(external_id)
    _assert_cli_permission_mode(build_accessor, "build")
    assert not any(
        "BUILD_CONTEXT_MARKER" in _system_text(item) for item in build_requests
    )

    context_path.write_text("PLAN_CONTEXT_MARKER: revised project rules\n")
    plan_target = "resumed-plan-bash.txt"
    plan_command = _bash_write_command(
        Path(plan_target), "resumed plan file contents", "resumed plan command output"
    )
    (
        planned,
        plan_events,
        _plan_questions,
        plan_requests,
        plan_permissions,
        plan_store,
        plan_accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Bash",
        tool_arguments={"command": plan_command},
        tools=default_tool_registry(),
        prompt="Continue the build-history marker and execute the scoped check.",
        store=store,
        session_id=session_id,
        external_session_id=external_id,
        cwd="/workspace",
        mode="plan",
    )

    assert planned.metadata["external_session_id"] == external_id
    _assert_cli_permission_mode(plan_accessor, "plan")
    assert plan_permissions == []
    assert plan_accessor.guest_path(f"/workspace/{plan_target}").read_text() == (
        "resumed plan file contents"
    )
    plan_bash = [
        event
        for event in plan_events
        if event.get("type") == "tool_completed" and event.get("tool") == "bash"
    ]
    assert len(plan_bash) == 1
    assert "resumed plan command output" in plan_bash[0]["output"]
    assert build_store is store and plan_store is store
    assert f"--resume={external_id}" in plan_accessor.opened[0]["argv"]
    assert store.load_calls == [("-workspace", external_id, "")]
    assert any(
        key[1] == external_id
        and any("ORIGINAL_USER_TURN" in json.dumps(entry) for entry in rows)
        for key, rows in store.entries.items()
    )
    managed_session_root = f"/harness/claude/{session_id}/"
    assert managed_session_root in " ".join(build_accessor.opened[0]["argv"])
    assert managed_session_root in " ".join(plan_accessor.opened[0]["argv"])
    assert build_accessor.opened[0]["guest_cwd"] == plan_accessor.opened[0]["guest_cwd"]

    system_text = "\n".join(_system_text(item) for item in plan_requests)
    assert "# Available tools" not in system_text
    assert "PLAN_CONTEXT_MARKER" not in system_text
    assert "BUILD_CONTEXT_MARKER" not in system_text
    assert "Remember the build-history marker" not in system_text
    assert "ORIGINAL_USER_TURN" in json.dumps(plan_requests)
    assert "Continue the build-history marker" in json.dumps(plan_requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plan", "build"])
async def test_real_cli_bash_runs_harmless_python_and_shell_without_approval(
    tmp_path: Path, mode: str
) -> None:
    """The OpenCuria Bash bridge executes normal commands in both modes."""
    _require_pinned_cli()
    target_name = f"{mode}-bash-contract.txt"
    command = _bash_write_command(
        Path(target_name), f"{mode} wrote the file", f"{mode} command output"
    )
    (
        result,
        events,
        _questions,
        _requests,
        permissions,
        _store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Bash",
        tool_arguments={"command": command},
        tools=default_tool_registry(),
        prompt=f"Run this harmless {mode} Bash fixture and report its result.",
        cwd="/workspace",
        mode=mode,
    )

    assert result.output == "fixture answer"
    _assert_cli_permission_mode(accessor, mode)
    assert permissions == []
    assert accessor.guest_path(f"/workspace/{target_name}").read_text() == (
        f"{mode} wrote the file"
    )
    starts = [
        event
        for event in events
        if event.get("type") == "tool_started" and event.get("tool") == "bash"
    ]
    completed = [
        event
        for event in events
        if event.get("type") == "tool_completed" and event.get("tool") == "bash"
    ]
    assert len(starts) == len(completed) == 1
    assert "6" in completed[0]["output"]
    assert f"{mode} command output" in completed[0]["output"]


@pytest.mark.asyncio
async def test_real_cli_explicit_bash_deny_blocks_without_approval_or_execution(
    tmp_path: Path,
) -> None:
    """An explicit Bash deny blocks dispatch without asking for approval."""
    _require_pinned_cli()
    target = "explicit-deny-must-not-write.txt"
    command = _bash_write_command(Path(target), "must not exist", "denied output")
    (
        result,
        events,
        _questions,
        _requests,
        permissions,
        _store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Bash",
        tool_arguments={"command": command},
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(global_rules={"bash": {command: "deny"}}),
        prompt="Try the explicitly denied command.",
        cwd="/workspace",
        mode="plan",
    )

    assert result.output == "fixture answer"
    _assert_cli_permission_mode(accessor, "plan")
    assert permissions == []
    assert not accessor.guest_path(f"/workspace/{target}").exists()
    assert not any(
        event.get("type") in {"tool_started", "tool_completed"}
        and event.get("tool") == "bash"
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plan", "build"])
@pytest.mark.parametrize("permission_response", ["once", "reject"])
async def test_real_cli_explicit_bash_ask_calls_back_and_honors_response(
    tmp_path: Path, mode: str, permission_response: str
) -> None:
    """Explicit scoped Bash ask invokes approval and honors approve or reject."""
    _require_pinned_cli()
    target = "explicit-ask-must-not-write.txt"
    command = _bash_write_command(Path(target), "must not exist", "denied output")
    (
        result,
        events,
        _questions,
        _requests,
        permissions,
        _store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Bash",
        tool_arguments={"command": command},
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(global_rules={"bash": {command: "ask"}}),
        prompt="Try the explicitly approval-gated command.",
        cwd="/workspace",
        mode=mode,
        permission_response=permission_response,
    )

    assert result.output == "fixture answer"
    _assert_cli_permission_mode(accessor, mode)
    assert len(permissions) == 1
    assert permissions[0]["tool"] == "bash"
    assert permissions[0]["key"] == "permission"
    assert permissions[0]["action"] == command
    if permission_response == "once":
        assert accessor.guest_path(f"/workspace/{target}").read_text() == (
            "must not exist"
        )
        assert any(
            event.get("type") == "tool_started" and event.get("tool") == "bash"
            for event in events
        )
        assert any(
            event.get("type") == "tool_completed" and event.get("tool") == "bash"
            for event in events
        )
        assert "6" in next(
            event["output"]
            for event in events
            if event.get("type") == "tool_completed" and event.get("tool") == "bash"
        )
        return
    assert not accessor.guest_path(f"/workspace/{target}").exists()
    assert any(
        event.get("type") == "tool_error" and event.get("tool") == "bash"
        for event in events
    )
    assert not any(
        event.get("type") in {"tool_started", "tool_completed"}
        and event.get("tool") == "bash"
        for event in events
    )


@pytest.mark.asyncio
async def test_real_cli_default_allowed_registered_mcp_dispatches_in_plan(
    tmp_path: Path,
) -> None:
    """A default-allowed configured MCP tool dispatches without an approval."""
    _require_pinned_cli()
    registry = default_tool_registry()
    note = WriteNoteTool()
    registry.register(note)
    (
        result,
        events,
        _questions,
        _requests,
        permissions,
        _store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name=f"mcp__opencuria__{note.name}",
        tool_arguments={"text": "plan mode default allowance"},
        tools=registry,
        prompt="Call the configured note tool and report its completion.",
        cwd="/workspace",
        mode="plan",
    )

    assert result.output == "fixture answer"
    _assert_cli_permission_mode(accessor, "plan")
    assert note.writes == ["plan mode default allowance"]
    assert permissions == []
    assert any(
        event.get("type") == "tool_completed" and event.get("tool") == note.name
        for event in events
    )


@pytest.mark.asyncio
async def test_real_cli_root_plan_write_still_requires_explicit_approval(
    tmp_path: Path,
) -> None:
    """Plan-mode native Write keeps its approval gate absent global overrides."""
    _require_pinned_cli()
    target = tmp_path / "workspace" / "root-plan-write.txt"
    (
        result,
        events,
        _questions,
        _requests,
        permissions,
        _store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Write",
        tool_arguments={
            "file_path": str(target),
            "content": "approved plan write",
        },
        tools=default_tool_registry(),
        prompt="Write the requested plan scratch file.",
        cwd="/workspace",
        mode="plan",
    )

    assert result.output == "fixture answer"
    _assert_cli_permission_mode(accessor, "plan")
    assert len(permissions) == 1
    assert permissions[0]["tool"] == "write"
    assert permissions[0]["action"] == str(target)
    assert accessor.guest_path(str(target)).read_text() == "approved plan write"
    assert any(
        event.get("type") == "tool_completed" and event.get("tool") == "write"
        for event in events
    )


@pytest.mark.asyncio
async def test_real_cli_keeps_native_read_and_mcp_schemas_without_prompt_duplicates(
    tmp_path: Path,
) -> None:
    """Native and MCP schemas are available without duplicated prompt tool lists."""
    _require_pinned_cli()
    registry = default_tool_registry()
    note = WriteNoteTool()
    registry.register(note)

    read_path = str(tmp_path / "workspace" / "numbered.txt")
    (
        result,
        events,
        _questions,
        requests,
        _permissions,
        _store,
        _accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Read",
        tool_arguments={"file_path": read_path, "limit": 2},
        tools=registry,
        prompt="Read the first two lines of numbered.txt, then answer fixture.",
        cwd="/workspace",
        fixture_files={"numbered.txt": b"first\nsecond\nthird\n"},
    )

    assert result.output == "fixture answer"
    assert "--max-turns" not in _accessor.opened[0]["argv"]
    read_starts = [
        event
        for event in events
        if event.get("type") == "tool_started" and event.get("tool") == "read"
    ]
    assert len(read_starts) == 1
    assert "offset" not in json.loads(read_starts[0]["arguments"])
    read_results = [
        event
        for event in events
        if event.get("type") == "tool_completed" and event.get("tool") == "read"
    ]
    assert len(read_results) == 1
    native_read = json.loads(read_results[0]["output"])
    assert native_read["file"]["startLine"] == 1
    assert native_read["file"]["content"].startswith("first\n")
    # The first call can be CLI-owned title generation; capture the actual
    # OpenCuria turn that contains native Read and the configured MCP server.
    provider_request = next(
        request
        for request in requests
        if any(item.get("name") == "Read" for item in _tool_schemas(request))
    )
    schemas = _tool_schemas(provider_request)
    names = [str(item.get("name") or "") for item in schemas]
    assert names.count("Read") == 1
    assert len(names) == len(set(names))
    read_schema = next(item for item in schemas if item.get("name") == "Read")
    read_properties = read_schema.get("input_schema", {}).get("properties", {})
    offset = read_properties["offset"]
    # Claude's native schema omits a default key: its Read tool defaults to
    # line 1 (unlike the registry Read tool's 0-based line offset contract).
    assert offset.get("default", 1) == 1
    assert "line number" in offset.get("description", "").lower()
    assert "0-based" not in offset.get("description", "").lower()
    assert "file_path" in read_properties and "path" not in read_properties

    mcp_name = f"mcp__opencuria__{note.name}"
    assert names.count(mcp_name) == 1
    system = _system_text(provider_request)
    assert "# OpenCuria tools" not in system
    assert "# Available tools" not in system
    assert f"- {mcp_name}:" not in system
    assert "- Read:" not in system and "- Bash:" not in system
    assert "Independent tool calls in one assistant message" not in system
    assert not any(name.lower() == "read" for name in names if name != "Read")


@pytest.mark.asyncio
async def test_real_cli_hydrates_png_and_pdf_file_references_once(
    tmp_path: Path,
) -> None:
    """File references arrive as native Anthropic media blocks without tools."""
    _require_pinned_cli()
    pdf = _minimal_pdf()
    prompt = (
        "Inspect @file:/workspace/screenshot.png and "
        "@file:/workspace/report.pdf attached"
    )
    (
        result,
        _events,
        _questions,
        requests,
        _permissions,
        _store,
        _accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="",
        tool_arguments={},
        tools=default_tool_registry(),
        prompt=prompt,
        cwd="/workspace",
        fixture_files={"screenshot.png": TINY_PNG, "report.pdf": pdf},
    )

    assert result.output == "fixture answer"
    user_requests = [
        request
        for request in requests
        if any(
            isinstance(message.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "document"
                for block in message["content"]
            )
            for message in request.get("messages", [])
        )
    ]
    assert len(user_requests) == 1
    media_request = user_requests[0]
    media_schemas = _tool_schemas(media_request)
    media_names = [str(item.get("name") or "") for item in media_schemas]
    assert "Read" in media_names and len(media_names) == len(set(media_names))
    media_blocks = [
        block
        for message in media_request.get("messages", [])
        for block in (message.get("content") or [])
        if isinstance(block, dict)
    ]
    images = [block for block in media_blocks if block.get("type") == "image"]
    documents = [block for block in media_blocks if block.get("type") == "document"]
    assert len(images) == len(documents) == 1
    assert images[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(images[0]["source"]["data"]) == TINY_PNG
    assert documents[0]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(documents[0]["source"]["data"]) == pdf
    flattened_text = "".join(
        str(block.get("text") or "")
        for block in media_blocks
        if block.get("type") == "text"
    )
    assert "/workspace/screenshot.png" in flattened_text
    assert "/workspace/report.pdf" in flattened_text
    assert all(block.get("type") != "tool_use" for block in media_blocks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subagent_type", "system_marker", "mode"),
    [
        ("general", "You are opencuria general", "build"),
        ("general", "You are opencuria general", "plan"),
        (
            "explore",
            "You are opencuria explore",
            "build",
        ),
        (
            "explore",
            "You are opencuria explore",
            "plan",
        ),
    ],
)
async def test_real_cli_task_child_gets_minimal_identity_and_scoped_tools(
    tmp_path: Path,
    subagent_type: str,
    system_marker: str,
    mode: str,
) -> None:
    """Native Task/Agent children retain identity and tool scope without extra prose."""
    _require_pinned_cli()
    task_args = {
        "description": f"Inspect the {subagent_type} fixture scope.",
        "prompt": "Read the fixture context and return a concise summary.",
        "subagent_type": subagent_type,
    }
    selected: tuple[Any, list[dict[str, Any]]] | None = None
    # The pinned CLI may expose the native delegated-call wire name as Task or
    # Agent; try both real schemas while keeping the helper's SSE fixture local.
    for candidate in ("Task", "Agent"):
        (
            result,
            events,
            _questions,
            requests,
            _permissions,
            _store,
            _accessor,
        ) = await _run_local_cli_fixture(
            tmp_path / candidate,
            tool_name=candidate,
            tool_arguments=task_args,
            tools=default_tool_registry(),
            prompt=f"Delegate this fixture request to {subagent_type}.",
            cwd="/workspace",
            mode=mode,
            fixture_files={
                "AGENTS.md": b"CHILD_PROJECT_CONTEXT_MARKER: scoped rules\n"
            },
        )
        child_requests = [
            request for request in requests if system_marker in _system_text(request)
        ]
        if child_requests:
            selected = (result, child_requests)
            break

    assert selected is not None, "The real CLI did not start the native Task child"
    result, child_requests = selected
    assert result.output == "fixture answer"
    child_request = child_requests[0]
    system = _system_text(child_request)
    task_calls = [
        event
        for event in events
        if event.get("type") == "tool_started" and event.get("tool") == "task"
    ]
    assert len(task_calls) == 1
    task_call = json.loads(task_calls[0]["arguments"])
    assert task_call["description"] == task_args["description"]
    assert task_call["prompt"] == task_args["prompt"]
    assert task_call["subagent_type"] == subagent_type
    assert "CHILD_PROJECT_CONTEXT_MARKER" not in system
    assert "working inside an OpenCuria workspace" in system
    assert "# OpenCuria tools" not in system
    assert "# Environment" not in system

    names = [str(item.get("name") or "") for item in _tool_schemas(child_request)]
    assert "Read" in names
    if subagent_type == "explore":
        forbidden = {"Write", "Edit", "NotebookEdit", "Task", "Agent", "TodoWrite"}
        assert forbidden.isdisjoint(names)
        assert not any(name.startswith("mcp__") for name in names)
        assert not any(
            name.lower() in {"question", "askuserquestion", "todowrite"}
            or name.lower().startswith("process_")
            for name in names
        )


@pytest.mark.asyncio
async def test_real_cli_max_steps_uses_turn_limit_and_records_tool_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A max-turn terminal returns a partial result after the tool call."""
    _require_pinned_cli()
    original_run = ClaudeEngine.run
    original_build_command = ClaudeEngine._build_command
    commands: list[list[str]] = []

    def capture_command(self: ClaudeEngine, **kwargs: Any) -> list[str]:
        command = original_build_command(self, **kwargs)
        commands.append(command)
        return command

    monkeypatch.setattr(ClaudeEngine, "_build_command", capture_command)

    async def run_with_one_step(
        self: ClaudeEngine,
        prompt: str,
        agent_name: str,
        model: str,
        mode: str = "build",
        opts: Any = None,
    ) -> Any:
        assert opts is not None
        opts.max_steps = 1
        return await original_run(self, prompt, agent_name, model, mode, opts)

    monkeypatch.setattr(ClaudeEngine, "run", run_with_one_step)
    registry = default_tool_registry()
    note = WriteNoteTool()
    registry.register(note)
    try:
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
            tool_name=f"mcp__opencuria__{note.name}",
            tool_arguments={"text": "one bounded tool call"},
            tools=registry,
            prompt="Call the fixture tool once, then stop at the one-turn limit.",
            cwd="/workspace",
        )
    except Exception as exc:
        assert commands and commands[0][commands[0].index("--max-turns") + 1] == "1"
        assert note.writes == ["one bounded tool call"]
        assert "maximum number of turns (1)" in str(exc).lower()
        pytest.fail(
            "Expected a partial max_steps result, but the pinned CLI raised "
            f"{type(exc).__name__}: {exc}"
        )

    assert commands and commands[0][commands[0].index("--max-turns") + 1] == "1"
    assert result.finish_reason == "max_steps"
    assert isinstance(result.output, str)
    assert note.writes == ["one bounded tool call"]
    assert any(event.get("type") == "tool_completed" for event in events)
    assert (
        accessor.opened[0]["argv"][accessor.opened[0]["argv"].index("--max-turns") + 1]
        == "1"
    )
