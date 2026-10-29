"""Real service -> durable owner -> registry -> pinned CLI -> local Responses.

Only workspace transport, the model catalog and provider HTTP are local fixtures.
No engine, execution, account authorization or checkpoint repository is mocked.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest
from aiohttp import web

from apps.harness.engines.codex.session_store import CodexSessionStore, home_for
from apps.harness.engines.run_repository import HarnessRunRepository
from apps.harness.harness_service import HarnessService
from apps.harness.model_catalog_service import clear_models_cache
from apps.harness.models import (
    HarnessMessage,
    HarnessPart,
    HarnessSession,
    HarnessTranscriptBatch,
    ProviderTurn,
)
from apps.harness.permissions.models import PermissionRequest
from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.providers.models_catalog import (
    normalize_openrouter_catalog,
)
from apps.harness.tests.test_codex_core import PINNED, LocalAccessor, McpWriteTool
from apps.plugins.runtime_snapshot import (
    PreparedPluginRuntime,
    WorkspacePluginSnapshot,
)
from common.exceptions import ConflictError

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


class ServiceAccessor(LocalAccessor):
    """Real local subprocesses, with runner ownership scoped to each lease."""

    def __init__(self, root):
        super().__init__(root)
        self.owners = {}
        self.uncertain_release = False
        self.protocol_items = []

    async def open_process(self, argv, cwd, env, **kwargs):
        stream = await super().open_process(argv, cwd, env, **kwargs)
        self.owners.setdefault(kwargs["owner"]["lease_id"], []).append(stream)
        receive = stream.receive
        stream.terminal_statuses = []
        stream.drop_terminal = False
        pending = bytearray()

        async def observed_receive():
            while b"\n" not in pending:
                chunk = await receive()
                if not chunk:
                    return chunk
                pending.extend(chunk)
            frames = []
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending[:] = rest
                message = json.loads(line)
                if message.get("method") in {"item/started", "item/completed"}:
                    self.protocol_items.append(message)
                if message.get("method") == "turn/completed":
                    stream.terminal_statuses.append(message["params"]["turn"]["status"])
                    if stream.drop_terminal:
                        # ACK without terminal: simulate a dropped transport.
                        return b""
                frames.append(line + b"\n")
            return b"".join(frames)

        stream.receive = observed_receive
        return stream

    async def desktop_action(self, action, args):
        self.actions.append(action)
        if action == "release":
            assert all(
                s.process.returncode is not None
                for s in self.owners.get(args["lease_id"], [])
            ), "runner must not confirm release before its process has exited"
            return {
                "ok": True,
                "lease_state": "closing" if self.uncertain_release else "released",
            }
        return {"ok": True, "epoch": "local-epoch", "lease_state": "reserved"}


class Responses:
    """Actual loopback SSE endpoints for Codex Responses and metadata chat."""

    def __init__(self):
        self.requests = []
        self.auxiliary_requests = []
        self.chat_requests = []
        self.script = []
        self.respond = None
        self.observe = lambda: {}
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.title_blocked = asyncio.Event()
        self.title_release = asyncio.Event()

    @staticmethod
    def text(value="Root done."):
        return {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": value, "annotations": []}],
        }

    @staticmethod
    def tool(name="mcp_test_write", **args):
        return {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(args or {"path": "note.txt", "content": "local"}),
        }

    async def handle_chat_completions(self, request):
        observed = {
            "body": await request.json(),
            "authorization": request.headers.get("Authorization"),
            "owners": self.observe(),
        }
        self.chat_requests.append(observed)
        body = observed["body"]
        user_prompt = " ".join(
            str(message.get("content") or "")
            for message in body.get("messages", [])
            if message.get("role") == "user"
        )
        system_prompt = " ".join(
            str(message.get("content") or "")
            for message in body.get("messages", [])
            if message.get("role") == "system"
        )
        is_title = "short title" in system_prompt
        if is_title and "First Codex root prompt" in user_prompt:
            self.title_blocked.set()
            await self.title_release.wait()
        text = "Codex first prompt title" if is_title else "Codex goal summary"
        chunks = [
            {
                "choices": [
                    {"index": 0, "delta": {"content": text}, "finish_reason": None}
                ]
            },
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "total_tokens": 7,
                },
            },
        ]
        return web.Response(
            text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n",
            content_type="text/event-stream",
        )

    async def handle(self, request):
        observed = {
            "body": await request.json(),
            "authorization": request.headers.get("Authorization"),
            "owners": self.observe(),
        }
        if observed["body"].get("model") != "~acme/local":
            self.auxiliary_requests.append(observed)
            item = self.text("Auxiliary native feature completed.")
        else:
            self.requests.append(observed)
            assert self.script or self.respond, "unexpected provider request"
            item = (
                self.respond(observed["body"]) if self.respond else self.script.pop(0)
            )
        index = len(self.requests) + len(self.auxiliary_requests)
        if item == "block":
            self.blocked.set()
            await self.release.wait()
            item = self.text("Released.")
        if item == "fail":
            return web.Response(status=400, text="local provider failure")
        item = {"id": f"item_{index}", **item}
        if item["type"] == "function_call":
            item["call_id"] = f"call_{index}"
        rid = f"response_{index}"
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


@pytest.fixture
async def runtime(harness_workspace, tmp_path, monkeypatch):
    if not PINNED.exists():
        pytest.skip("Pinned Codex binary unavailable; set OPENCURIA_TEST_CODEX_BINARY")
    provider = Responses()
    app = web.Application()
    app.router.add_post("/v1/responses", provider.handle)
    app.router.add_post("/v1/chat/completions", provider.handle_chat_completions)

    async def models(_):
        return web.json_response({"data": []})

    app.router.add_get("/v1/models", models)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    accessor = ServiceAccessor(tmp_path / "workspace")
    events = []

    async def emit(name, data):
        events.append((name, data))

    async def access(_):
        return accessor

    service = HarnessService(emit=emit, accessor_factory=access)
    provider.observe = lambda: {
        key: {
            "account_ids": dict(ctx.get("account_ids") or {}),
            "provider_turn_id": str(ctx.get("provider_turn_id")),
        }
        for key, ctx in service._runs.items()
    }
    service.conversation_events.emit_sync = lambda *_: None
    original_tools = HarnessService._tools_for_session

    def registered_mcp_tools(self, *args, **kwargs):
        registry = original_tools(self, *args, **kwargs)
        registry.register(McpWriteTool())
        return registry

    monkeypatch.setattr(HarnessService, "_tools_for_session", registered_mcp_tools)
    # Empty, already-resolved plugin snapshot; real McpRuntime still executes.
    service.plugin_runtime.prepare = lambda *_: PreparedPluginRuntime(
        snapshot=WorkspacePluginSnapshot(
            workspace_id=harness_workspace.id,
            organization_id=harness_workspace.runner.organization_id,
            plugins=(),
        ),
        plaintexts={},
    )
    catalog = normalize_openrouter_catalog(
        {
            "data": [
                {
                    "id": "acme/local",
                    "name": "Local",
                    "context_length": 32000,
                    "supported_parameters": ["tools", "reasoning"],
                    "reasoning": {
                        "supported_efforts": ["low", "high"],
                        "default_effort": "low",
                    },
                }
            ]
        }
    )
    monkeypatch.setattr(
        "apps.harness.providers.models_catalog.fetch_openrouter_models",
        lambda **_: catalog,
    )
    clear_models_cache()
    accounts = ProviderAccountService()
    account = accounts.save_account(
        organization_id=harness_workspace.runner.organization_id,
        user=harness_workspace.created_by,
        provider="openrouter",
        payload={
            "label": "Loopback",
            "api_key": "local-secret-key",
            "base_url": f"http://127.0.0.1:{port}/v1",
        },
    )
    data = SimpleNamespace(
        service=service,
        accessor=accessor,
        provider=provider,
        events=events,
        accounts=accounts,
        account=account,
        workspace=harness_workspace,
    )
    try:
        yield data
    finally:
        provider.release.set()
        provider.title_release.set()
        tasks = list(service._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for stream in accessor.streams:
            if stream.process.returncode is None:
                await stream.aclose()
        await server.cleanup()
        clear_models_cache()


def new_session(data, *, prompt="Local test", **kwargs):
    return data.service.create_session(
        workspace_id=data.workspace.id,
        organization_id=data.workspace.runner.organization_id,
        user_id=data.workspace.created_by_id,
        prompt=prompt,
        harness_id="codex",
        model="openrouter/acme/local",
        **kwargs,
    )


async def start(data, session, prompt="Local test"):
    assistant = await data.service.start_run(
        session, prompt, user_id=data.workspace.created_by_id
    )
    task = data.service._tasks[str(session.id)]
    return assistant, task


async def finish(data, session, prompt="Local test"):
    assistant, task = await start(data, session, prompt)
    await asyncio.wait_for(task, 40)
    assistant.refresh_from_db()
    session.refresh_from_db()
    return assistant


def assert_settled(data, session, assistant, status="completed", *, launched=True):
    session.refresh_from_db()
    assistant.refresh_from_db()
    run = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert run.status == status, (run.status, assistant.error)
    if launched:
        assert run.lease_id
        assert run.lease_epoch == "local-epoch"
    assert session.status == "idle"
    assert HarnessRunRepository.get_open_for_session(session.id) is None
    assert str(session.id) not in data.service._tasks
    assert str(session.id) not in data.service._runs
    assert all(s.process.returncode is not None for s in data.accessor.streams)
    return run


def cold_service(data):
    """Replace the service without retaining warm engines or stores."""
    data.service = HarnessService(
        emit=data.service._emit, accessor_factory=data.service._accessor_factory
    )
    data.service.plugin_runtime.prepare = lambda *_: PreparedPluginRuntime(
        snapshot=WorkspacePluginSnapshot(
            workspace_id=data.workspace.id,
            organization_id=data.workspace.runner.organization_id,
            plugins=(),
        ),
        plaintexts={},
    )


async def test_codex_start_run_generates_first_title_with_frozen_account(
    runtime, monkeypatch
):
    """Codex titles use a local OpenRouter call and the root's captured account."""
    from apps.harness.services import ProviderConfigService

    data = runtime
    data.provider.script = ["block", Responses.text("Second Codex answer.")]
    ProviderConfigService().save_config(
        organization_id=data.workspace.runner.organization_id,
        small_model="openrouter/acme/local",
    )
    session = new_session(data, prompt="First Codex root prompt")
    data.events.clear()
    title_entered = asyncio.Event()
    allow_title = asyncio.Event()
    title_kwargs = []
    generate_title = data.service.metadata.generate_title

    async def gated_title(**kwargs):
        title_kwargs.append(dict(kwargs))
        title_entered.set()
        await allow_title.wait()
        await generate_title(**kwargs)

    monkeypatch.setattr(data.service.metadata, "generate_title", gated_title)
    first, task = await start(data, session, "First Codex root prompt")
    await asyncio.wait_for(data.provider.blocked.wait(), 20)
    await asyncio.wait_for(title_entered.wait(), 10)
    turn = ProviderTurn.objects.get(assistant_message=first)
    assert turn.account_ids == {"openrouter": str(data.account.id)}

    replacement = data.accounts.save_account(
        organization_id=session.organization_id,
        user=data.workspace.created_by,
        provider="openrouter",
        payload={
            "label": "Selected after root admission",
            "api_key": "replacement-local-secret",
            "base_url": data.account.config["base_url"],
        },
    )
    data.accounts.select_account(
        session.organization_id,
        session.created_by_id,
        "openrouter",
        replacement.id,
    )
    allow_title.set()
    await asyncio.wait_for(data.provider.title_blocked.wait(), 15)
    title_request = data.provider.chat_requests[0]
    assert title_request["authorization"] == "Bearer local-secret-key"
    assert title_request["body"]["model"] == "acme/local"
    assert "short title" in " ".join(
        str(message.get("content") or "")
        for message in title_request["body"]["messages"]
        if message.get("role") == "system"
    )
    assert title_kwargs[0]["account_ids"] == {"openrouter": str(data.account.id)}
    assert title_kwargs[0]["provider_turn_id"] == turn.id
    assert title_kwargs[0]["initiated_by_id"] == session.created_by_id
    turn.refresh_from_db()
    assert turn.used_account_ids == [str(data.account.id)]
    assert title_request["owners"][str(session.id)]["account_ids"] == {
        "openrouter": str(data.account.id)
    }

    data.provider.title_release.set()
    data.provider.release.set()
    await asyncio.wait_for(task, 40)
    async with asyncio.timeout(10):
        while (
            HarnessSession.objects.get(id=session.id).title
            != "Codex first prompt title"
        ):
            await asyncio.sleep(0.02)
        while (
            sum(name == "harness.conversations_changed" for name, _ in data.events) < 2
        ):
            await asyncio.sleep(0.02)
    assert_settled(data, session, first)
    assert sum(name == "harness.conversations_changed" for name, _ in data.events) == 2
    assert title_request["authorization"] != "Bearer replacement-local-secret"
    async with asyncio.timeout(10):
        while ProviderTurn.objects.get(id=turn.id).active:
            await asyncio.sleep(0.02)

    second = await finish(data, session, "Second Codex root prompt")
    assert_settled(data, session, second)
    async with asyncio.timeout(10):
        while len(data.provider.chat_requests) < 3:
            await asyncio.sleep(0.02)
    session.refresh_from_db()
    assert session.title == "Codex first prompt title"
    title_requests = [
        request
        for request in data.provider.chat_requests
        if "short title"
        in " ".join(
            str(message.get("content") or "")
            for message in request["body"]["messages"]
            if message.get("role") == "system"
        )
    ]
    assert len(title_requests) == 1
    assert "First Codex root prompt" in json.dumps(title_requests[0]["body"])
    assert "Second Codex root prompt" not in json.dumps(title_requests[0]["body"])
    assert sum(name == "harness.conversations_changed" for name, _ in data.events) == 3
    assert title_requests[0]["authorization"] == "Bearer local-secret-key"


async def test_native_file_changes_persist_before_the_final_answer(
    runtime: SimpleNamespace,
) -> None:
    """The persisted and live timelines contain each edit once, then the answer."""
    data = runtime
    data.provider.script = [
        Responses.tool(
            "exec_command",
            cmd=(
                "apply_patch <<'PATCH'\n"
                "*** Begin Patch\n"
                "*** Add File: first.txt\n+first\n"
                "*** Add File: second.txt\n+second\n"
                "*** End Patch\nPATCH"
            ),
        ),
        Responses.tool("exec_command", cmd="cat first.txt second.txt"),
        Responses.text(),
    ]
    session = new_session(data)
    assistant = await finish(data, session)
    assert_settled(data, session, assistant)
    assert assistant.content == "Root done."
    parts = list(assistant.parts.exclude(type__in=["step-start", "step-finish"]))
    assert [part.type for part in parts] == ["tool", "patch", "patch", "tool", "text"]
    patches = [part for part in parts if part.type == "patch"]
    assert {part.meta["path"] for part in patches} == {
        str(data.accessor.path("/workspace/first.txt")),
        str(data.accessor.path("/workspace/second.txt")),
    }
    assert {part.call_id for part in patches} == {parts[0].call_id}
    assert parts[-1].output == "Root done."
    live = [
        payload["delta"]
        for name, payload in data.events
        if name == "harness.part_updated" and payload["message_id"] == str(assistant.id)
    ]
    assert sum("patch" in delta for delta in live) == 2
    last_patch = max(i for i, delta in enumerate(live) if "patch" in delta)
    assert "".join(delta.get("text", "") for delta in live[last_patch + 1 :]) == (
        "Root done."
    )


async def test_managed_write_usage_encrypted_checkpoint_and_cold_followup(runtime):
    data = runtime
    data.provider.script = [
        Responses.tool(),
        Responses.text(),
        Responses.text("Cold resumed."),
    ]
    session = new_session(data)
    assistant = await finish(data, session)
    assert_settled(data, session, assistant)
    assert assistant.content == "Root done."
    assert assistant.finish == "stop"
    assert data.accessor.path("/workspace/note.txt").read_text() == "local"
    assert session.tokens == assistant.tokens
    assert assistant.tokens == {"prompt": 20, "completion": 10, "total": 30}
    context = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "context_length": 258400,
    }
    finished = HarnessPart.objects.get(message=assistant, type="step-finish")
    assert finished.meta["context"] == finished.display["context"] == context
    live_context = [
        payload
        for name, payload in data.events
        if name == "harness.part_updated"
        and payload["message_id"] == str(assistant.id)
        and "context" in payload["delta"]
    ]
    assert len(live_context) == 3  # Two live requests and the final step.
    assert live_context[0]["delta"] == {"context": context}
    assert live_context[-1]["delta"]["context"] == context
    start = HarnessPart.objects.get(message=assistant, type="step-start")
    assert live_context[0]["part_id"] == str(start.id)
    assert start.display["context"] == context
    assert (
        HarnessPart.objects.filter(
            message=assistant, type="tool", state="completed"
        ).count()
        == 1
    )
    assert (
        "".join(
            HarnessPart.objects.filter(message=assistant, type="text").values_list(
                "output", flat=True
            )
        )
        == assistant.content
    )
    batch = HarnessTranscriptBatch.objects.get(session=session)
    assert "Root done." not in batch.payload_encrypted
    assert "local-secret-key" not in batch.payload_encrypted
    old_generation = session.engine_state["codex_generation"]
    old_thread = session.external_session_id
    checkpoint = await CodexSessionStore(
        session_id=session.id, external_session_id=old_thread
    ).load(old_thread, home_for(str(session.id)))
    assert checkpoint["generation"] == old_generation
    assert not session.engine_state["codex_dirty"]
    first_tokens = session.tokens["total"]
    shutil.rmtree(data.accessor.path(home_for(str(session.id))))
    cold_service(data)
    session = HarnessSession.objects.get(id=session.id)
    second = await finish(data, session, "Continue")
    assert_settled(data, session, second)
    assert second.content == "Cold resumed."
    assert session.external_session_id == old_thread
    assert session.engine_state["codex_generation"] != old_generation
    assert session.tokens["total"] == first_tokens + second.tokens["total"]
    assert second.tokens == {"prompt": 10, "completion": 5, "total": 15}
    assert HarnessPart.objects.get(message=second, type="step-finish").display[
        "context"
    ] == context
    assert HarnessTranscriptBatch.objects.filter(session=session).count() == 1
    assert len(data.accessor.opened) == 2
    assert "function_call_output" in json.dumps(data.provider.requests[1])
    assert all(
        r["authorization"] == "Bearer local-secret-key" for r in data.provider.requests
    )
    assert "local-secret-key" not in json.dumps(data.events)


@pytest.mark.parametrize(
    "failure",
    [None, "no-terminal", "save", "binding", "release", "owner"],
    ids=[
        "confirmed",
        "no-terminal",
        "save-failed",
        "binding-failed",
        "unconfirmed-release",
        "owner-superseded",
    ],
)
async def test_user_stop_checkpoint_and_cold_same_thread_followup(
    runtime, monkeypatch, failure
):
    """Normal Stop is resumable only after terminal, release and owner fences."""
    data = runtime
    data.provider.script = [
        Responses.tool("exec_command", cmd="sleep 30", yield_time_ms=1000),
        Responses.text("Continued after Stop."),
    ]
    session = new_session(data)
    assistant, task = await start(data, session)
    async with asyncio.timeout(20):
        while not HarnessPart.objects.filter(
            message=assistant, type="tool", state="running"
        ).exists():
            if task.done():
                await task
                assistant.refresh_from_db()
                pytest.fail(assistant.error)
            await asyncio.sleep(0.02)
    stream = data.accessor.streams[0]
    stream.drop_terminal = failure == "no-terminal"
    if failure == "save":

        def fail_encrypt(value):
            raise RuntimeError("Checkpoint encryption unavailable")

        monkeypatch.setattr(
            "apps.harness.engines.repositories.encrypt_value", fail_encrypt
        )
    if failure == "binding":
        # Fail binding after the real encrypted owner-fenced checkpoint save.
        from apps.harness.engines.repositories import HarnessEngineRepository

        bind = HarnessEngineRepository.bind_external_session_if_owner

        def reject_clean(*args, **kwargs):
            if kwargs["engine_state"].get("codex_dirty") is False:
                return False
            return bind(*args, **kwargs)

        monkeypatch.setattr(
            HarnessEngineRepository, "bind_external_session_if_owner", reject_clean
        )
    if failure == "release":
        data.accessor.uncertain_release = True
    if failure == "owner":
        import uuid

        HarnessRunRepository.model.objects.filter(assistant_message=assistant).update(
            owner_token=uuid.uuid4()
        )
    if failure in {"release", "owner"}:
        with pytest.raises(ConflictError, match="cleanup is pending"):
            await asyncio.wait_for(data.service.abort_run(session.id), 30)
    else:
        await asyncio.wait_for(data.service.abort_run(session.id), 30)
    await asyncio.gather(task, return_exceptions=True)
    session.refresh_from_db()
    assistant.refresh_from_db()
    assert stream.terminal_statuses == ["interrupted"]
    assert all(s.process.returncode is not None for s in data.accessor.streams)
    if failure in {"release", "owner"}:
        run = HarnessRunRepository.model.objects.get(assistant_message=assistant)
        assert run.status == ("closing" if failure == "release" else "running")
        assert session.status == "busy"
    else:
        assert_settled(data, session, assistant, "interrupted")
        assert assistant.finish == "aborted"
    assert session.engine_state["codex_dirty"] is (failure is not None)
    if failure is not None:
        assert HarnessTranscriptBatch.objects.filter(session=session).count() == (
            1 if failure == "binding" else 0
        )
        return
    old_thread = session.external_session_id
    old_generation = session.engine_state["codex_generation"]
    checkpoint = await CodexSessionStore(
        session_id=session.id, external_session_id=old_thread
    ).load(old_thread, home_for(str(session.id)))
    assert checkpoint["generation"] == old_generation
    assert "release" in data.accessor.actions
    shutil.rmtree(data.accessor.path(home_for(str(session.id))))
    cold_service(data)
    session = HarnessSession.objects.get(id=session.id)
    second = await finish(data, session, "Continue after Stop")
    assert_settled(data, session, second)
    assert second.content == "Continued after Stop."
    assert second.finish == "stop"
    assert session.external_session_id == old_thread
    assert session.engine_state["codex_generation"] != old_generation
    assistant.refresh_from_db()
    assert assistant.finish == "aborted"
    assert len(data.accessor.opened) == 2


async def test_plan_permission_then_build_same_thread(runtime):
    data = runtime
    data.provider.script = [
        Responses.tool(),
        Responses.text(),
        Responses.tool(),
        Responses.text(),
    ]
    session = new_session(data, mode="plan", agent_name="plan")
    assistant, task = await start(data, session)
    async with asyncio.timeout(20):
        while not PermissionRequest.objects.filter(
            session_id=session.id, status="pending"
        ).exists():
            if task.done():
                await task
                assistant.refresh_from_db()
                pytest.fail(assistant.error)
            await asyncio.sleep(0.02)
    request = PermissionRequest.objects.get(session_id=session.id, status="pending")
    assert not data.accessor.path("/workspace/note.txt").exists()
    await data.service.interactions.resolve_permission(
        session=session, request_id=request.id, response="once"
    )
    await asyncio.wait_for(task, 35)
    assert_settled(data, session, assistant)
    session.refresh_from_db()
    old_thread = session.external_session_id
    old_fingerprint = session.engine_state["codex_tools"]
    session = data.service.session_settings.set_mode(session.id, "build")
    assert session.mode == session.agent_name == "build"
    assistant = await finish(data, session, "Now build")
    assert_settled(data, session, assistant)
    assert session.external_session_id == old_thread
    assert session.engine_state["codex_tools"] == old_fingerprint
    assert PermissionRequest.objects.filter(session_id=session.id).count() == 1
    assert data.accessor.path("/workspace/note.txt").read_text() == "local"


async def test_unconfirmed_runner_release_stays_closing_and_busy(runtime):
    data = runtime
    data.provider.script = [Responses.text()]
    data.accessor.uncertain_release = True
    session = new_session(data)
    assistant = await finish(data, session)
    run = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert run.status == "closing"
    assert session.status == "busy"
    assert assistant.finish == "error"
    assert session.engine_state["codex_dirty"]
    assert HarnessTranscriptBatch.objects.filter(session=session).count() == 0
    assert HarnessRunRepository.get_open_for_session(session.id).id == run.id
    with pytest.raises(ConflictError):
        await start(data, session, "Must not overlap")
    assert len(data.accessor.opened) == 1


async def test_provider_failure_dirty_recovery_finalizes_cleanup(runtime):
    data = runtime
    data.provider.script = ["fail"]
    session = new_session(data)
    assistant = await finish(data, session)
    assert_settled(data, session, assistant, "error")
    assert assistant.finish == "error"
    assert "fork or start a new conversation" in assistant.error
    assert session.engine_state["codex_dirty"]
    assert HarnessTranscriptBatch.objects.filter(session=session).count() == 0
    assistant = await finish(data, session, "Retry dirty")
    assert_settled(data, session, assistant, "error", launched=False)
    assert "fork or start a new conversation" in assistant.error
    assert len(data.accessor.opened) == 1
    assert len(data.provider.requests) == 1
    assert "local-secret-key" not in json.dumps(data.events)


def native_delegation(provider, *, block_child=False, child_tool=False):
    """Script model responses by native thread input, independent of scheduling."""
    root_calls = 0

    def respond(body):
        nonlocal root_calls
        inputs = body.get("input", [])
        is_child = any(
            x.get("role") == "user" and "NATIVE_CHILD_PROMPT" in json.dumps(x)
            for x in inputs
        )
        if is_child:
            if child_tool and not any(
                x.get("type") == "function_call_output" for x in inputs
            ):
                return Responses.tool(
                    "exec_command",
                    cmd="printf native-child > child-native.txt",
                    yield_time_ms=1000,
                )
            return "block" if block_child else Responses.text("Child-only answer.")
        root_calls += 1
        if root_calls == 1:
            return {
                **Responses.tool("spawn_agent", message="NATIVE_CHILD_PROMPT"),
                "namespace": "multi_agent_v1",
            }
        if root_calls == 2:
            outputs = [
                x for x in body["input"] if x.get("type") == "function_call_output"
            ]
            result = json.loads(outputs[-1]["output"])
            return {
                **Responses.tool(
                    "wait_agent", targets=[result["agent_id"]], timeout_ms=10000
                ),
                "namespace": "multi_agent_v1",
            }
        return Responses.text("Parent-only answer.")

    provider.respond = respond


@pytest.mark.parametrize("child_tool", [False, True])
async def test_native_codex_child_projection_and_shared_checkpoint(runtime, child_tool):
    data = runtime
    native_delegation(data.provider, child_tool=child_tool)
    session = new_session(data)
    assistant = await finish(data, session)
    assert_settled(data, session, assistant)
    child = HarnessSession.objects.get(parent=session)
    child_assistant = HarnessMessage.objects.get(session=child, role="assistant")
    assert child.harness_id == "codex"
    assert child.model == session.model
    assert child.created_by_id == session.created_by_id
    assert child.status == "idle"
    assert str(child.id) not in data.service._runs
    assert child_assistant.content == "Child-only answer."
    if child_tool:
        assert (
            data.accessor.path("/workspace/child-native.txt").read_text()
            == "native-child"
        )
        assert HarnessPart.objects.filter(
            message=child_assistant, type="tool", state="completed"
        ).exists()
    assert assistant.content == "Parent-only answer."
    assert (
        HarnessPart.objects.filter(
            message=assistant, type="subtask", state="completed"
        ).count()
        == 1
    )
    assert len(data.accessor.opened) == 1  # Native children share the app-server.
    assert HarnessTranscriptBatch.objects.filter(session=session).count() == 1
    assert not ProviderTurn.objects.filter(
        assistant_message=assistant, active=True
    ).exists()
    checkpoint = await CodexSessionStore(session_id=session.id).load(
        session.external_session_id, home_for(str(session.id))
    )
    assert (
        len(
            [
                x
                for x in checkpoint["snapshot"]["files"]
                if x["path"].startswith("sessions/")
            ]
        )
        >= 2
    )
    assert "Child-only answer." not in "".join(
        HarnessPart.objects.filter(message=assistant, type="text").values_list(
            "output", flat=True
        )
    )


async def test_parent_stop_cancels_real_child_and_releases_both_owners(runtime):
    data = runtime
    native_delegation(data.provider, block_child=True)
    session = new_session(data)
    assistant, task = await start(data, session)
    await asyncio.wait_for(data.provider.blocked.wait(), 20)
    child = HarnessSession.objects.get(parent=session)
    child_assistant = HarnessMessage.objects.get(session=child, role="assistant")
    await asyncio.wait_for(data.service.abort_run(session.id), 25)
    await asyncio.gather(task, return_exceptions=True)
    assert_settled(data, session, assistant, "interrupted")
    child.refresh_from_db()
    child_assistant.refresh_from_db()
    assert child.status == "idle"
    assert assistant.finish == "aborted"
    assert child_assistant.finish in {"aborted", "error"}
    assert len(data.accessor.streams) == 1
    assert all(s.process.returncode is not None for s in data.accessor.streams)
    assert not ProviderTurn.objects.filter(
        assistant_message=assistant, active=True
    ).exists()


async def test_revoke_captured_account_does_not_fall_back_to_new_selection(runtime):
    from asgiref.sync import sync_to_async

    data = runtime
    data.provider.script = ["block"]
    session = new_session(data)
    assistant, task = await start(data, session)
    await asyncio.wait_for(data.provider.blocked.wait(), 20)
    second = data.accounts.save_account(
        organization_id=session.organization_id,
        user=data.workspace.created_by,
        provider="openrouter",
        payload={
            "label": "Do not use",
            "api_key": "second-secret",
            "base_url": data.account.config["base_url"],
        },
    )
    data.accounts.select_account(
        session.organization_id, session.created_by_id, "openrouter", second.id
    )
    turn = ProviderTurn.objects.get(assistant_message=assistant)
    assert turn.account_ids == {"openrouter": str(data.account.id)}
    await sync_to_async(data.accounts.delete_account)(
        session.organization_id, session.created_by_id, data.account.id
    )
    try:
        await asyncio.wait_for(asyncio.shield(task), 20)
    except asyncio.CancelledError:
        pass
    assert_settled(data, session, assistant, "interrupted")
    assert "no longer shared" in assistant.error
    assert assistant.finish == "aborted"
    assert session.engine_state["codex_dirty"]
    assert not HarnessTranscriptBatch.objects.filter(session=session).exists()
    assert len(data.provider.requests) == 1
    assert data.provider.requests[0]["authorization"] == "Bearer local-secret-key"
    assert "second-secret" not in json.dumps(data.events)
    turn.refresh_from_db()
    assert not turn.active


async def test_malformed_process_protocol_retains_dirty_and_public_recovery(runtime):
    data = runtime
    data.provider.script = []
    open_process = data.accessor.open_process

    async def corrupted_process(*args, **kwargs):
        stream = await open_process(*args, **kwargs)
        receive = stream.receive

        async def broken_receive():
            await receive()
            return b"not-json local-secret-key\n"

        # Real CLI launched normally; corrupt only its byte transport.
        stream.receive = broken_receive
        return stream

    data.accessor.open_process = corrupted_process
    session = new_session(data)
    assistant = await finish(data, session)
    assert_settled(data, session, assistant, "error")
    assert "fork or start a new conversation" in assistant.error
    assert "protocol failed" in assistant.error
    assert session.engine_state["codex_dirty"]
    assert not HarnessTranscriptBatch.objects.filter(session=session).exists()
    assert "local-secret-key" not in json.dumps(data.events)


@pytest.mark.parametrize(
    "unknown", [False, True], ids=["accepted", "acceptance-unknown"]
)
async def test_inflight_steering_is_reserved_and_unknown_delivery_never_replays(
    runtime, unknown
):
    from apps.harness.engines.codex.protocol import CodexProtocolError

    data = runtime
    data.provider.script = ["block", Responses.text("Steered answer.")]
    session = new_session(data)
    assistant, task = await start(data, session)
    await asyncio.wait_for(data.provider.blocked.wait(), 20)
    stream = data.accessor.streams[0]
    send = stream.send
    entered, release = asyncio.Event(), asyncio.Event()

    async def controlled_send(payload):
        if b'"method":"turn/steer"' in payload:
            entered.set()
            await release.wait()
            await send(payload)
            if unknown:
                raise CodexProtocolError(
                    "transport acceptance unknown local-secret-key"
                )
            return
        await send(payload)

    stream.send = controlled_send
    entry = await data.service.steer_run(
        session, "Only once steer", user_id=session.created_by_id
    )
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(ConflictError) as raised:
        await data.service.withdraw_steer(session, entry.id)
    assert raised.value.code == "steer_in_flight"
    assert not HarnessPart.objects.filter(message=assistant, type="steer").exists()
    release.set()
    if not unknown:
        async with asyncio.timeout(5):
            while not HarnessPart.objects.filter(
                message=assistant, type="steer"
            ).exists():
                await asyncio.sleep(0.02)
    data.provider.release.set()
    await asyncio.wait_for(task, 35)
    assert_settled(data, session, assistant, "error" if unknown else "completed")
    assert HarnessPart.objects.filter(
        message=assistant, type="steer", call_id=entry.id
    ).count() == (0 if unknown else 1)
    await asyncio.sleep(0.1)
    assert len(data.accessor.opened) == 1, (
        "unknown acceptance must not become a follow-up"
    )
    assert HarnessMessage.objects.filter(session=session, role="assistant").count() == 1


async def test_scheduled_occurrence_snapshot_uses_owner_current_account(runtime):
    from django.utils import timezone

    from apps.scheduled_tasks.repositories import ScheduledTaskRepository
    from apps.scheduled_tasks.services import ScheduledTaskService

    data = runtime
    data.provider.script = [Responses.text("Scheduled answer.")]
    scheduler = ScheduledTaskService()
    task = scheduler.create(
        organization_id=data.workspace.runner.organization_id,
        owner_id=data.workspace.created_by_id,
        workspace=data.workspace,
        values={
            "name": "Local Codex schedule",
            "prompt": "Scheduled original prompt",
            "harness_id": "codex",
            "model": "openrouter/acme/local",
            "recurrence": "daily",
            "local_time": "09:00",
            "timezone_name": "UTC",
        },
    )
    run = ScheduledTaskRepository.create_manual_run(task, timezone.now())
    second = data.accounts.save_account(
        organization_id=task.organization_id,
        user=data.workspace.created_by,
        provider="openrouter",
        payload={
            "label": "Selected at launch",
            "api_key": "launch-secret",
            "base_url": data.account.config["base_url"],
        },
    )
    data.accounts.select_account(
        task.organization_id, task.owner_id, "openrouter", second.id
    )
    task.prompt = "Changed after occurrence claim"
    task.save(update_fields=["prompt"])
    frozen = scheduler._task_for_run(task, run)
    assert frozen.prompt == "Scheduled original prompt"
    run = await scheduler._create_and_start_run(frozen, run, data.service)
    running = data.service._tasks.get(str(run.session_id))
    if running is not None:
        await asyncio.wait_for(running, 35)
    run.refresh_from_db()
    session = HarnessSession.objects.get(id=run.session_id)
    assistant = HarnessMessage.objects.get(id=run.assistant_message_id)
    assert_settled(data, session, assistant)
    assert session.created_by_id == task.owner_id
    assert session.harness_id == "codex"
    assert ProviderTurn.objects.get(assistant_message=assistant).account_ids == {
        "openrouter": str(second.id)
    }
    assert data.provider.requests[0]["authorization"] == "Bearer launch-secret"
    assert "Scheduled original prompt" in json.dumps(data.provider.requests[0]["body"])
    assert "Changed after occurrence claim" not in json.dumps(
        data.provider.requests[0]["body"]
    )
    assert run.status == "succeeded"
    assert "secret" not in json.dumps(run.configuration_snapshot)


async def test_chatgpt_runtime_preflight_refresh_exact_account_and_protocol_privacy(
    runtime, monkeypatch
):
    """OAuth is a seam test, not fabricated live ChatGPT authentication."""
    from apps.harness.engines.codex.protocol import CodexProtocol
    from apps.harness.engines.external_runtime import prepare_external_runtime
    from apps.harness.tests.test_codex_core import QueueStream

    data = runtime
    account = data.accounts.save_account(
        organization_id=data.workspace.runner.organization_id,
        user=data.workspace.created_by,
        provider="chatgpt",
        payload={
            "label": "Captured",
            "access": "old-access-secret",
            "refresh": "refresh-secret",
            "account_id": "exact-upstream-id",
        },
    )
    session = data.service.create_session(
        workspace_id=data.workspace.id,
        organization_id=account.organization_id,
        user_id=data.workspace.created_by_id,
        prompt="Seam only",
        harness_id="codex",
    )
    calls, uses = [], []

    async def fresh(org, user, connection, **kwargs):
        calls.append((org, user, connection, kwargs))
        return {
            "access": "preflight-secret" if len(calls) == 1 else "refreshed-secret",
            "refresh": "refresh-secret",
            "account_id": "exact-upstream-id",
        }

    async def use(turn, connection):
        uses.append((turn, str(connection)))

    async def current():
        return None

    monkeypatch.setattr(
        "apps.harness.providers.chatgpt_credentials.get_fresh_chatgpt_credentials",
        fresh,
    )
    data.service._register_provider_use = use
    runtime = await prepare_external_runtime(
        service=data.service,
        engine_id="codex",
        session=session,
        organization_id=session.organization_id,
        context={
            "account_ids": {"chatgpt": str(account.id)},
            "initiated_by_id": session.created_by_id,
            "provider_turn_id": "captured-turn",
        },
        ownership=SimpleNamespace(run_id=None, owner_token=None),
        ensure_current=current,
    )
    # Selection changes after capture cannot redirect the callback.
    data.accounts.select_account(
        session.organization_id, session.created_by_id, "chatgpt", None
    )
    engine = data.service._engine_registry_for_run().create(
        "codex",
        tools=data.service._tools_for_session(str(session.id), "build"),
        accessor=data.accessor,
        **runtime.engine_options,
    )
    stream = QueueStream()
    rpc = CodexProtocol(stream, engine._server_request)
    try:
        await stream.input.put(
            (
                json.dumps(
                    {
                        "id": 71,
                        "method": "account/chatgptAuthTokens/refresh",
                        "params": {"previousAccountId": "exact-upstream-id"},
                    }
                )
                + "\n"
            ).encode()
        )
        response = await asyncio.wait_for(stream.output.get(), 3)
        assert response == {
            "id": 71,
            "result": {
                "accessToken": "refreshed-secret",
                "chatgptAccountId": "exact-upstream-id",
            },
        }
        assert calls == [
            (session.organization_id, session.created_by_id, account.id, {}),
            (
                session.organization_id,
                session.created_by_id,
                account.id,
                {"rejected_access_token": "preflight-secret"},
            ),
        ]
        assert uses == [("captured-turn", str(account.id))] * 2
        await stream.input.put(
            b'{"id":72,"method":"account/chatgptAuthTokens/refresh","params":{"previousAccountId":"foreign"}}\n'
        )
        denied = await asyncio.wait_for(stream.output.get(), 3)
        assert denied["id"] == 72 and "error" in denied
        assert len(calls) == 2
        assert "secret" not in json.dumps(denied)
        for secret in ("preflight-secret", "refreshed-secret", "refresh-secret"):
            assert secret not in runtime.redact_error("failure " + secret)
            assert secret not in json.dumps(session.engine_state)
            assert secret not in json.dumps(data.events)
    finally:
        await rpc.close()


async def test_explicit_steer_rejection_releases_reservation_for_withdrawal(runtime):
    data = runtime
    data.provider.script = ["block"]
    session = new_session(data)
    assistant, task = await start(data, session)
    await asyncio.wait_for(data.provider.blocked.wait(), 20)
    stream = data.accessor.streams[0]
    send = stream.send
    rejected = asyncio.Event()

    async def stale_turn_send(payload):
        if b'"method":"turn/steer"' in payload:
            message = json.loads(payload)
            # The real CLI, not a mocked RPC, rejects the stale turn identity.
            message["params"]["expectedTurnId"] = "foreign-turn"
            await send((json.dumps(message) + "\n").encode())
            rejected.set()
            return
        await send(payload)

    stream.send = stale_turn_send
    channel = data.service._steering_channel(session.id)
    entry = await data.service.steer_run(session, "Rejected steer")
    await asyncio.wait_for(rejected.wait(), 5)
    async with asyncio.timeout(5):
        while entry.id in channel._reserved:
            await asyncio.sleep(0.02)
    assert data.service.list_pending_steers(session.id) == [entry]
    assert not HarnessPart.objects.filter(message=assistant, type="steer").exists()
    assert await data.service.withdraw_steer(session, entry.id) == entry
    data.provider.release.set()
    await asyncio.wait_for(task, 35)
    assert_settled(data, session, assistant)
    assert len(data.accessor.opened) == 1
    assert not HarnessPart.objects.filter(message=assistant, type="steer").exists()


async def test_superseded_owner_cannot_emit_bind_checkpoint_or_finalize(runtime):
    import uuid

    data = runtime
    data.provider.script = ["block"]
    session = new_session(data)
    assistant, task = await start(data, session)
    await asyncio.wait_for(data.provider.blocked.wait(), 20)
    run = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    original_state = HarnessSession.objects.get(id=session.id).engine_state
    replacement = uuid.uuid4()
    HarnessRunRepository.model.objects.filter(id=run.id).update(owner_token=replacement)
    before_events = len(data.events)
    data.provider.release.set()
    await asyncio.wait_for(task, 35)
    run.refresh_from_db()
    session.refresh_from_db()
    assistant.refresh_from_db()
    assert run.owner_token == replacement
    assert run.status == "running"
    assert session.status == "busy"
    assert session.engine_state == original_state
    assert session.engine_state["codex_dirty"]
    assert not HarnessTranscriptBatch.objects.filter(session=session).exists()
    assert assistant.content == ""
    assert not assistant.finish
    assert not HarnessPart.objects.filter(message=assistant, type="text").exists()
    assert not any(
        "Released." in json.dumps(event) for event in data.events[before_events:]
    )
    assert all(s.process.returncode is not None for s in data.accessor.streams)
    assert str(session.id) not in data.service._tasks
    assert str(session.id) not in data.service._runs
