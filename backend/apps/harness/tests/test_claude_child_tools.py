"""Real local-CLI fixture for approved native child-agent tool calls."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from apps.harness.tests.test_claude_engine import (
    FixtureSessionStore,
    LocalSseWorkspaceAccessor,
    _api_final_message,
    _api_tool_message,
    _sse,
    require_pinned_cli,
)
from apps.harness.tools import default_tool_registry


def _child_read_message(file_path: str) -> bytes:
    """Return a native Read tool call from a delegated API response."""
    return b"".join(
        [
            _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "child-read-message",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-sonnet-4-5",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 2, "output_tokens": 0},
                    },
                },
            ),
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "child-read-call",
                        "name": "Read",
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
                        "partial_json": json.dumps({"file_path": file_path}),
                    },
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("subagent_type", ["general", "explore"])
@pytest.mark.parametrize("mode", ["build", "plan"])
async def test_local_agent_task_type_preserves_hook_identity_for_child_read(
    tmp_path: Path,
    subagent_type: str,
    mode: str,
) -> None:
    require_pinned_cli()

    requests: list[dict[str, Any]] = []
    fixture_path = str(tmp_path / "workspace" / "child-note.txt")
    root_tool_sent = False
    child_read_sent = False
    child_tool_response_seen = asyncio.Event()

    async def messages(request: web.Request) -> web.StreamResponse:
        nonlocal root_tool_sent, child_read_sent
        payload = await request.json()
        requests.append(payload)
        response = web.StreamResponse(
            status=200,
            headers={"content-type": "text/event-stream", "cache-control": "no-cache"},
        )
        await response.prepare(request)
        tools = {str(item.get("name") or "") for item in payload.get("tools") or []}
        if not root_tool_sent and "Agent" in tools:
            root_tool_sent = True
            body = _api_tool_message(
                "Agent",
                "root-task-call",
                {
                    "subagent_type": subagent_type,
                    "description": "Read a fixture file",
                    "prompt": f"Use Read on {fixture_path}.",
                },
            )
        elif not child_read_sent and "Read" in tools:
            child_read_sent = True
            body = _child_read_message(fixture_path)
        else:
            if child_read_sent:
                child_tool_response_seen.set()
            body = _api_final_message(
                "child-final" if child_read_sent else "root-final"
            )
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
    fixture_bytes = b"child-readable fixture\n"
    accessor = LocalSseWorkspaceAccessor(
        tmp_path,
        {
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CLAUDE_CONFIG_DIR": (
                f"/workspace/.opencuria/harness/claude/{uuid.uuid4()}/config"
            ),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "MCP_TIMEOUT": "20000",
        },
    )
    await accessor.write_file(fixture_path, fixture_bytes)
    events: list[dict[str, Any]] = []
    child_events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def on_child(parent: str, event: dict[str, Any]) -> None:
        child_events.append((parent, event))

    async def read_file(path: str, max_size: int | None = None):
        from apps.harness.access.base import FileContent
        from apps.harness.access.runner_accessor import RunnerAccessorError

        if path != fixture_path:
            raise RunnerAccessorError(f"not found: {path}")
        content = fixture_bytes if max_size is None else fixture_bytes[:max_size]
        return FileContent(content=content, size=len(fixture_bytes))

    accessor.read_file = read_file

    from apps.harness.engines.claude.engine import ClaudeEngine
    from apps.harness.runner import RunOptions

    engine = ClaudeEngine(
        tools=default_tool_registry(),
        accessor=accessor,
        emit=emit,
        on_child_event=on_child,
        auth_env={"ANTHROPIC_API_KEY": "sk-ant-local-child-fixture"},
        session_store=FixtureSessionStore(),
        runtime_artifact_ensurer=lambda *_args: asyncio.sleep(
            0,
            result={
                "ok": True,
                "path": "/opt/opencuria/runtimes/claude-agent/2.1.310/claude",
            },
        ),
    )
    try:
        result = await asyncio.wait_for(
            engine.run(
                "Use Task to delegate the file read, then return its contents.",
                mode,
                "sonnet",
                mode,
                opts=RunOptions(
                    session_id=str(uuid.uuid4()),
                    workspace_id=accessor.workspace_id,
                    cwd="/workspace",
                ),
            ),
            timeout=60,
        )
    finally:
        await server.cleanup()

    assert result.output == "fixture answer"
    assert child_read_sent and child_tool_response_seen.is_set()
    assert any(
        event.get("type") == "tool_completed"
        and event.get("call_id") == "child-read-call"
        and "child-readable fixture" in event.get("output", "")
        for parent, event in child_events
    )
    assert not any(
        event.get("type") == "tool_error" and event.get("call_id") == "child-read-call"
        for event in events
    )
    starts = [
        event for event in events if event.get("type") == "subtask_started"
    ]
    assert len(starts) == 1
    assert starts[0].get("subtask_id")
    assert starts[0].get("agent") == subagent_type
    assert len(requests) >= 3
