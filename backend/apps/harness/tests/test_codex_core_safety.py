"""Fail-closed auth, ownership, cancellation, policy and state contracts."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import uuid

import pytest
from asgiref.sync import async_to_sync

from apps.harness.agents.definitions import get_agent
from apps.harness.engines.codex.auth import CodexAuthConfiguration
from apps.harness.engines.codex.engine import CodexEngine
from apps.harness.engines.codex.protocol import CodexProtocol, CodexProtocolError
from apps.harness.engines.codex.session_store import (
    CodexSessionStore,
    home_for,
    validate_snapshot,
)
from apps.harness.engines.codex.tools import ManagedToolInvoker
from apps.harness.providers.base import Usage
from apps.harness.runner import RunOptions
from apps.harness.tests.test_codex_core import QueueStream
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import ToolRegistry


def auth(provider="chatgpt", connection=None):
    return CodexAuthConfiguration(
        provider,
        connection or uuid.uuid4(),
        {
            "access": "fake-private-access",
            "account_id": "fake-account",
            "api_key": "local-fake-only",
        },
    )


def snapshot(session):
    data = b'{"type":"session_meta"}\n'
    return {
        "version": 1,
        "home": home_for(str(session)),
        "files": [
            {
                "path": "sessions/2026/10/20/rollout-example.jsonl",
                "data": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    }


@pytest.mark.asyncio
async def test_refresh_is_exact_account_bounded_and_private():
    original = auth()
    refreshed = CodexAuthConfiguration(
        "chatgpt",
        original.connection_id,
        {"access": "fresh-private-access", "account_id": "fake-account"},
    )
    events = []

    async def refresh():
        return refreshed

    async def emit(event):
        events.append(event)

    engine = CodexEngine(
        tools=ToolRegistry(),
        accessor=None,
        auth=original,
        refresh_auth=refresh,
        emit=emit,
    )
    response = await engine._server_request("account/chatgptAuthTokens/refresh", {})
    assert response == {
        "accessToken": "fresh-private-access",
        "chatgptAccountId": "fake-account",
    }
    await engine._send(
        {"type": "part_updated", "text": "fresh-private-access fake-private-access"}
    )
    assert "private-access" not in str(events)
    assert engine.engine_state == {}

    async def wrong_account():
        return auth()

    engine.refresh_auth = wrong_account
    with pytest.raises(RuntimeError, match="identity changed"):
        await engine._server_request("account/chatgptAuthTokens/refresh", {})


@pytest.mark.asyncio
async def test_refresh_and_stale_tool_requests_deny_without_callback():
    engine = CodexEngine(tools=ToolRegistry(), accessor=None, auth=auth())
    with pytest.raises(RuntimeError, match="refresh unavailable"):
        await engine._server_request("account/chatgptAuthTokens/refresh", {})
    engine.turn_ready.set()
    engine.thread_id, engine.turn_id = "thread-current", "turn-current"
    with pytest.raises(RuntimeError, match="Stale"):
        await engine._server_request(
            "item/tool/call", {"threadId": "thread-old", "turnId": "turn-current"}
        )


@pytest.mark.asyncio
async def test_rpc_cancellation_fails_other_requests_and_drains_tasks():
    stream = QueueStream()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(method, params):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    rpc = CodexProtocol(stream, handler)
    await stream.input.put(
        b'{"id":"managed-task","method":"item/tool/call","params":{}}\n'
    )
    await asyncio.wait_for(started.wait(), 1)
    first = asyncio.create_task(rpc.request("turn/start", {}))
    second = asyncio.create_task(rpc.request("account/read", {}))
    await stream.output.get()
    await stream.output.get()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(CodexProtocolError, match="acceptance unknown"):
        await second
    await asyncio.wait_for(cancelled.wait(), 1)
    await rpc.close()
    assert rpc.reader.done()
    assert not rpc.pending
    assert not rpc.handlers


@pytest.mark.asyncio
async def test_dirty_checkpoint_never_launches_or_restores():
    engine = CodexEngine(
        tools=ToolRegistry(),
        accessor=None,
        auth=auth("openrouter"),
        engine_state={"provider_id": "openrouter", "codex_dirty": True},
    )
    with pytest.raises(RuntimeError, match="fork or start"):
        await engine.run(
            "Continue",
            "build",
            "openrouter/test/model",
            "build",
            RunOptions(session_id=str(uuid.uuid4())),
        )


@pytest.mark.asyncio
async def test_explore_denies_delegation_and_unsafe_shell_even_if_forced():
    events = []

    async def emit(event):
        events.append(event)

    invoker = ManagedToolInvoker(default_tool_registry(), None, emit)
    opts = RunOptions(session_id=str(uuid.uuid4()))
    for index, (name, args) in enumerate(
        [
            (
                "task",
                {"description": "write", "prompt": "write", "subagent_type": "general"},
            ),
            ("bash", {"command": "printf destructive > /workspace/source.py"}),
            ("process_start", {"command": "touch source.py", "name": "bad"}),
        ]
    ):
        result = await invoker.invoke(
            {"tool": name, "callId": str(index), "arguments": args},
            agent=get_agent("explore"),
            mode="build",
            opts=opts,
            model="openrouter/test/model",
            allowed={name},
        )
        assert result["success"] is False
    assert all(e["type"] == "tool_error" for e in events)


@pytest.mark.asyncio
async def test_snapshot_authoritative_text_ignores_stale_and_duplicate_completion():
    events = []

    async def emit(event):
        events.append(event)

    engine = CodexEngine(tools=ToolRegistry(), accessor=None, auth=auth(), emit=emit)
    engine.thread_id, engine.turn_id = "thread", "turn"
    engine.items, engine.streamed, engine.redactors = {}, {}, {}
    engine.native, engine.finished_native = set(), set()
    engine.total_text_bytes = 0
    engine.usage, engine.usage_baseline = Usage(), Usage()
    engine.opts = RunOptions()
    item = {"type": "agentMessage", "id": "message", "text": "Hello world"}
    await engine._notification(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "old",
                "turnId": "turn",
                "itemId": "message",
                "delta": "WRONG",
            },
        }
    )
    await engine._notification(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "itemId": "message",
                "delta": "Hello ",
            },
        }
    )
    await engine._item(item, completed=True)
    await engine._item(item, completed=True)
    assert "".join(e.get("delta", {}).get("text", "") for e in events) == "Hello world"
    assert engine.items["message"]["text"] == "Hello world"


@pytest.mark.parametrize(
    "path",
    [
        "../auth.json",
        "/tmp/state_5.sqlite",
        "sessions/../../auth.json",
        "config.toml",
        "logs_2.sqlite",
        "auth.json",
    ],
)
def test_checkpoint_rejects_traversal_and_unrelated_artifacts(path):
    session = uuid.uuid4()
    record = snapshot(session)
    record["files"][0]["path"] = path
    with pytest.raises(ValueError):
        validate_snapshot(record, home_for(str(session)))


def test_checkpoint_rejects_corruption_duplicate_and_wrong_home():
    session = uuid.uuid4()
    record = snapshot(session)
    bad = copy.deepcopy(record)
    bad["files"][0]["sha256"] = "wrong"
    with pytest.raises(ValueError):
        validate_snapshot(bad, home_for(str(session)))
    bad = copy.deepcopy(record)
    bad["files"] *= 2
    with pytest.raises(ValueError):
        validate_snapshot(bad, home_for(str(session)))
    with pytest.raises(ValueError):
        validate_snapshot(record, home_for(str(uuid.uuid4())))


@pytest.mark.django_db(transaction=True)
def test_encrypted_checkpoint_replacement_and_ownership_fence(harness_workspace):
    from apps.harness.models import HarnessMessage, HarnessRun, HarnessTranscriptBatch
    from apps.harness.repositories import HarnessSessionRepository

    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        model="openrouter/test/model",
    )
    session.harness_id = "codex"
    session.save(update_fields=["harness_id"])
    message = HarnessMessage.objects.create(session=session, role="assistant")
    run = HarnessRun.objects.create(
        session=session,
        assistant_message=message,
        organization_id=session.organization_id,
        harness_id="codex",
    )
    external = str(uuid.uuid4())
    store = CodexSessionStore(
        session_id=session.id, run_id=run.id, owner_token=run.owner_token
    )
    record = snapshot(session.id)
    for _ in range(3):
        generation = str(uuid.uuid4())
        async_to_sync(store.save)(external, generation, record)
    assert HarnessTranscriptBatch.objects.filter(session=session).count() == 1
    row = HarnessTranscriptBatch.objects.get(session=session)
    assert "session_meta" not in row.payload_encrypted
    assert (
        async_to_sync(store.load)(external, home_for(str(session.id)))["generation"]
        == generation
    )
    run.owner_token = uuid.uuid4()
    run.save(update_fields=["owner_token"])
    with pytest.raises(RuntimeError, match="ownership superseded"):
        async_to_sync(store.save)(external, str(uuid.uuid4()), record)
    assert HarnessTranscriptBatch.objects.filter(session=session).count() == 1


@pytest.mark.asyncio
async def test_steering_delivery_waits_for_upstream_ack_and_keeps_rejection_pending():
    from apps.harness.engines.codex.protocol import CodexRPCError
    from apps.harness.steering import SteeringChannel

    delivered = []

    async def deliver(entries):
        delivered.extend(entries)

    channel = SteeringChannel(deliver)
    first = channel.submit("accepted message")
    engine = CodexEngine(tools=ToolRegistry(), accessor=None, auth=auth())
    engine.opts = RunOptions(steering=channel)
    engine.thread_id, engine.turn_id = "thread", "turn"
    started, allow = asyncio.Event(), asyncio.Event()

    class RPC:
        async def request(self, method, params, **kwargs):
            assert method == "turn/steer"
            assert params["expectedTurnId"] == "turn"
            started.set()
            await allow.wait()
            engine.terminal.set()
            return {"turnId": "turn"}

    task = asyncio.create_task(engine._steer(RPC()))
    await started.wait()
    assert not delivered
    assert channel.pending() == [first]
    allow.set()
    await task
    assert delivered == [first]
    assert not channel.pending()

    second = channel.submit("must remain queued")
    engine.terminal.clear()

    class RejectRPC:
        async def request(self, *args, **kwargs):
            raise CodexRPCError("turn/steer", -32600)

    await engine._steer(RejectRPC())
    assert channel.pending() == [second]
    assert delivered == [first]


@pytest.mark.asyncio
async def test_tool_images_use_checked_inline_dynamic_content():
    from apps.harness.engines.codex.media import dynamic_content
    from apps.harness.tests.conftest import TINY_PNG

    encoded = base64.b64encode(TINY_PNG).decode()
    result = await dynamic_content(
        [
            {"type": "text", "text": "Screenshot captured"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + encoded},
            },
        ]
    )
    assert result[0] == {"type": "inputText", "text": "Screenshot captured"}
    assert result[1] == {
        "type": "inputImage",
        "imageUrl": "data:image/png;base64," + encoded,
    }
    with pytest.raises(ValueError):
        await dynamic_content(
            [{"type": "image_url", "image_url": {"url": "https://example.test/image"}}]
        )
