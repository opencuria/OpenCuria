"""Additional real pinned Claude CLI contracts for managed upgrades."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from apps.harness.engines.claude.engine import ClaudeEngine
from apps.harness.tests.test_claude_engine import (
    LocalSseWorkspaceAccessor,
    _run_local_cli_fixture,
    require_pinned_cli,
)
from apps.harness.todos.service import TodoService
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import ToolRegistry
from apps.harness.tools.todos import TodoWriteTool


def _provider_schemas(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return all real CLI tool schemas captured by the local endpoint."""
    return [
        schema
        for request in requests
        for schema in request.get("tools", [])
        if isinstance(schema, dict)
    ]


@pytest.mark.asyncio
async def test_real_cli_exposes_no_unmanaged_upgrade_tools_and_disables_forks(
    tmp_path: Path,
) -> None:
    """The pinned CLI schema/environment cannot enable unmanaged workflows."""
    require_pinned_cli()
    (
        result,
        _events,
        _questions,
        requests,
        _permissions,
        _store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="",
        tool_arguments={},
        tools=default_tool_registry(),
        prompt="Do not call a tool; answer fixture.",
    )

    assert result.output == "fixture answer"
    schemas = _provider_schemas(requests)
    assert schemas, "The real CLI did not send a provider-facing tool schema"
    names = {str(schema.get("name") or "").lower() for schema in schemas}
    forbidden = {
        "monitor",
        "workflow",
        "sendmessage",
        "readmessage",
        "taskcreate",
        "taskupdate",
        "taskget",
        "tasklist",
        "taskoutput",
        "teamcreate",
        "teamdelete",
    }
    assert names.isdisjoint(forbidden)
    task_schemas = [
        schema
        for schema in schemas
        if str(schema.get("name") or "").lower() in {"task", "agent"}
    ]
    assert task_schemas, "The native delegated-call schema was not exposed"
    fork_fields = {"fork", "fork_session", "forksession", "forked_session"}
    for schema in task_schemas:
        input_schema = schema.get("input_schema")
        properties = (
            input_schema.get("properties", {})
            if isinstance(input_schema, dict)
            else {}
        )
        assert fork_fields.isdisjoint(str(field).lower() for field in properties)
    assert accessor.opened[0]["env"]["CLAUDE_CODE_FORK_SUBAGENT"] == "0"


@pytest.mark.asyncio
async def test_real_cli_loads_only_selected_managed_skill_and_invokes_native_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selected managed skill is loaded through native Skill, not MCP."""
    require_pinned_cli()
    skill_body = (
        "---\n"
        "name: opencuria-skill-001\n"
        "description: Selected OpenCuria fixture skill\n"
        "---\n\n"
        "SELECTED_SKILL_BODY_SENTINEL: selected skill was loaded.\n"
        "When invoked, use the exact phrase selected-skill-was-loaded.\n"
    )
    original_run = ClaudeEngine.run

    async def run_with_selected_skill(
        self: ClaudeEngine,
        prompt: str,
        agent_name: str,
        model: str,
        mode: str = "build",
        opts: Any = None,
    ) -> Any:
        assert opts is not None
        opts.skills = [skill_body]
        return await original_run(self, prompt, agent_name, model, mode, opts)

    monkeypatch.setattr(ClaudeEngine, "run", run_with_selected_skill)
    selected_name = "opencuria-managed-skills:opencuria-skill-001"
    (
        result,
        events,
        _questions,
        requests,
        _permissions,
        _store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Skill",
        tool_arguments={"skill": selected_name},
        tools=default_tool_registry(),
        prompt="Invoke the selected managed skill, then answer fixture.",
    )

    assert result.output == "fixture answer"
    schemas = _provider_schemas(requests)
    assert any(schema.get("name") == "Skill" for schema in schemas)
    assert not any(
        str(schema.get("name") or "").startswith("mcp__")
        and "skill" in str(schema.get("name") or "").lower()
        for schema in schemas
    )
    plugin_root = accessor.guest_path(
        accessor.opened[0]["argv"][accessor.opened[0]["argv"].index("--plugin-dir") + 1]
    )
    skill_dirs = sorted(path.name for path in (plugin_root / "skills").iterdir())
    assert skill_dirs == ["opencuria-skill-001"]
    assert (plugin_root / "skills/opencuria-skill-001/SKILL.md").read_text() == (
        skill_body.strip()
    )
    assert "SELECTED_SKILL_BODY_SENTINEL" in json.dumps(requests)
    assert "selected-skill-was-loaded" in json.dumps(requests)
    queued = [
        event
        for event in events
        if event.get("type") == "tool_queued"
        and str(event.get("tool") or "").lower() == "skill"
    ]
    assert len(queued) == 1
    calls = [
        block
        for request in requests
        for message in request.get("messages", [])
        if message.get("role") == "assistant"
        for block in (message.get("content") or [])
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == "Skill"
    ]
    assert len(calls) == 1
    assert calls[0]["input"]["skill"] == selected_name
    assert selected_name in json.dumps(queued)


@pytest.mark.asyncio
async def test_real_cli_native_edit_completes_file_change(
    tmp_path: Path,
) -> None:
    """The real CLI invokes native Edit and changes the requested workspace file."""
    require_pinned_cli()
    target = tmp_path / "workspace" / "edit-contract.txt"
    (
        result,
        events,
        _questions,
        requests,
        permissions,
        _store,
        accessor,
    ) = await _run_local_cli_fixture(
        tmp_path,
        tool_name="Edit",
        tool_arguments={
            "file_path": str(target),
            "old_string": "before upgrade",
            "new_string": "after upgrade",
        },
        tools=default_tool_registry(),
        prompt="Replace 'before upgrade' with 'after upgrade' in edit-contract.txt.",
        fixture_files={"edit-contract.txt": b"before upgrade\nkeep this line\n"},
    )

    assert result.output == "fixture answer"
    assert target.read_text() == "after upgrade\nkeep this line\n"
    assert permissions == []
    assert any(
        event.get("type") == "tool_completed" and event.get("tool") == "edit"
        for event in events
    )
    assert any(
        block.get("name") == "Edit"
        for request in requests
        for message in request.get("messages", [])
        if message.get("role") == "assistant"
        for block in (message.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    )
    assert accessor.opened[0]["env"]["CLAUDE_CODE_FORK_SUBAGENT"] == "0"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_real_cli_mcp_todowrite_updates_open_curia_session_state(
    tmp_path: Path, harness_session,
) -> None:
    """The real CLI can dispatch the managed todo tool through its MCP bridge."""
    require_pinned_cli()
    registry = ToolRegistry()
    registry.register(TodoWriteTool())
    session_id = str(harness_session.id)
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
        tool_name="mcp__opencuria__todowrite",
        tool_arguments={
            "todos": [
                {
                    "content": "Verify upgraded CLI contract",
                    "status": "in_progress",
                    "priority": "high",
                }
            ]
        },
        tools=registry,
        prompt="Update the OpenCuria todo list with the fixture item.",
        session_id=session_id,
    )

    assert result.output == "fixture answer"
    assert permissions == []
    schemas = _provider_schemas(requests)
    assert any(
        str(schema.get("name") or "").endswith("__todowrite") for schema in schemas
    )
    todos = TodoService().list(session_id).items
    assert [(item.content, item.status, item.priority) for item in todos] == [
        ("Verify upgraded CLI contract", "in_progress", "high")
    ]
    assert any(
        event.get("type") == "tool_completed" and event.get("tool") == "todowrite"
        for event in events
    )


@pytest.mark.asyncio
async def test_real_cli_cancellation_cleans_up_pending_question_and_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a waiting real CLI run closes the pending MCP call and child."""
    require_pinned_cli()
    question_started = asyncio.Event()
    question_stopped = asyncio.Event()
    cli_streams: list[Any] = []
    emitted: list[dict[str, Any]] = []

    async def hold_question() -> None:
        question_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            question_stopped.set()

    original_open_process = LocalSseWorkspaceAccessor.open_process

    async def record_cli_process(
        self: LocalSseWorkspaceAccessor,
        command: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        *,
        owner: dict[str, str] | None = None,
    ) -> Any:
        stream = await original_open_process(
            self, command, workdir, env, timeout, owner=owner
        )
        if command and command[0].endswith("/claude"):
            cli_streams.append(stream)
        return stream

    original_send = ClaudeEngine._send

    async def record_engine_event(
        self: ClaudeEngine, event: dict[str, Any]
    ) -> None:
        emitted.append(dict(event))
        await original_send(self, event)

    monkeypatch.setattr(
        LocalSseWorkspaceAccessor, "open_process", record_cli_process
    )
    monkeypatch.setattr(ClaudeEngine, "_send", record_engine_event)

    run_task = asyncio.create_task(
        _run_local_cli_fixture(
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
            question_hook=hold_question,
        )
    )
    try:
        await asyncio.wait_for(question_started.wait(), timeout=30)
        assert len(cli_streams) == 1, "The pinned CLI process was not captured"
        process = cli_streams[0].process
        assert process.returncode is None, "The CLI exited before cancellation"
        assert not question_stopped.is_set(), "The MCP question did not remain pending"
        assert any(
            event.get("type") == "tool_queued"
            and event.get("tool") == "question"
            for event in emitted
        ), "The real CLI never queued the MCP question"
        assert not any(
            event.get("type") == "tool_completed"
            and event.get("tool") == "question"
            for event in emitted
        ), "The MCP question completed before cancellation"

        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=10)

        assert question_stopped.is_set(), "Pending question callback was not cancelled"
        await asyncio.wait_for(process.wait(), timeout=5)
        assert process.returncode is not None, "The real CLI child survived cancellation"
        assert any(
            event.get("type") == "aborted" and event.get("reason") == "cancelled"
            for event in emitted
        ), "Cancellation did not emit the run abort event"
    finally:
        if not run_task.done():
            run_task.cancel()
            try:
                await asyncio.wait_for(run_task, timeout=15)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        for stream in cli_streams:
            if stream.process.returncode is None:
                stream.process.kill()
                await stream.process.wait()
