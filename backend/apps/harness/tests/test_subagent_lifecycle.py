"""Real service/runner/DB regressions for cancellation ownership at child boundaries."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from apps.harness.models import HarnessSession
from apps.harness.providers.base import Delta, ProviderError
from apps.harness.repositories import HarnessMessageRepository, HarnessPartRepository
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tests.test_harness_service import (
    FakeProvider,
    _db_create_session,
    _service,
)
from apps.harness.tests.test_mcp_startup_lifecycle import (
    ScriptedAccessor,
    prepared_servers,
)

pytestmark = pytest.mark.django_db(transaction=True)
TIMEOUT = 10


class RoutingProvider(FakeProvider):
    """Route by user prompt; expose deterministic provider-wait checkpoints."""

    def __init__(self, agent: str = "general", *, sibling: bool = False) -> None:
        super().__init__([])
        self.agent = agent
        self.sibling = sibling
        self.entered = {name: asyncio.Event() for name in ("child", "sibling")}
        self.release = {name: asyncio.Event() for name in ("child", "sibling")}
        self.histories: list[list[Any]] = []
        self.failure: str | None = None
        self.partial = ""
        self.cancel_parent = False
        self.block_child = True

    async def chat_stream(self, model, messages, tools, opts=None):
        prompt = next(m.content for m in reversed(messages) if m.role == "user")
        if prompt == "parent":
            self.histories.append(list(messages))
            if self.cancel_parent:
                raise asyncio.CancelledError()
            if any(m.role == "tool" for m in messages):
                yield Delta(text="parent continued")
                return
            names = ["child", "sibling"] if self.sibling else ["child"]
            yield Delta(
                tool_calls=tuple(
                    {
                        "index": index,
                        "id": f"call-{name}",
                        "name": "task",
                        "arguments": json.dumps(
                            {
                                "description": name,
                                "prompt": name,
                                "subagent_type": self.agent,
                            }
                        ),
                    }
                    for index, name in enumerate(names)
                )
            )
            return
        if prompt in self.entered:
            self.entered[prompt].set()
            if self.block_child:
                await self.release[prompt].wait()
            if self.partial:
                yield Delta(text=self.partial)
            if self.failure:
                raise ProviderError(self.failure)
            yield Delta(text=f"{prompt} finished")
            return
        yield Delta(text="title")


async def start(service, session):
    """Start a real background turn and retain its handle before finalization."""
    assistant = await service.start_run(
        session,
        "parent",
        organization_id=session.organization_id,
        workspace_id=str(session.workspace_id),
    )
    return assistant, service._tasks[str(session.id)]


async def checkpoint(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), TIMEOUT)


async def finish(task: asyncio.Task) -> None:
    await asyncio.wait_for(asyncio.shield(task), TIMEOUT)


def children(session):
    return list(HarnessSession.objects.filter(parent_id=session.id))


def assistant_for(session):
    return HarnessMessageRepository.list_for_session(session.id)[-1]


def assert_settled(service, sessions) -> None:
    for session in sessions:
        session.refresh_from_db()
        assert session.status == "idle"
        key = str(session.id)
        assert key not in service._tasks
        assert key not in service._runs
        assert key not in service._event_locks
        assert key not in service._abort_requested


def task_parts(session, part_type="tool"):
    return [
        p
        for p in HarnessPartRepository.list_for_session(session.id)
        if p.type == part_type
    ]


@pytest.mark.parametrize("agent", ["general", "explore"])
async def test_independent_child_cancel_preserves_parent_and_sibling(
    harness_workspace,
    agent,
) -> None:
    provider = RoutingProvider(agent, sibling=True)
    service, _, events = _service(provider=provider)
    parent = await _db_create_session(harness_workspace)
    assistant, parent_task = await start(service, parent)
    await checkpoint(provider.entered["child"])
    await checkpoint(provider.entered["sibling"])
    rows = children(parent)
    child = next(row for row in rows if row.title == "child")
    sibling = next(row for row in rows if row.title == "sibling")
    child_task = service._tasks[str(child.id)]
    sibling_task = service._tasks[str(sibling.id)]
    # Keep terminal SQLite writes deterministic while both child tasks run.
    # The cancellation boundary must settle before releasing the sibling.
    child_finished = asyncio.Event()
    original_emit = service._emit

    async def emit(event, data):
        await original_emit(event, data)
        if (
            event == "harness.part_updated"
            and data.get("delta", {}).get("call_id") == "call-child"
            and data.get("delta", {}).get("state") == "error"
        ):
            child_finished.set()

    service._emit = emit
    child_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finish(child_task)
    await checkpoint(child_finished)
    assert not sibling_task.done()
    provider.release["sibling"].set()
    await finish(parent_task)
    assistant.refresh_from_db()
    assert (assistant.finish, assistant.content) == ("stop", "parent continued")
    failed = assistant_for(child)
    assert failed.finish == "error"
    assert failed.error == "Run interrupted unexpectedly"
    assert assistant_for(sibling).finish == "stop"
    history = provider.histories[-1]
    outputs = [m for m in history if m.role == "tool"]
    assert [m.tool_call_id for m in outputs] == ["call-child", "call-sibling"]
    assert str(child.id) in outputs[0].content
    assert failed.error in outputs[0].content
    assert 'state="completed"' not in outputs[0].content
    assert str(sibling.id) in outputs[1].content
    calls = next(m.tool_calls for m in history if m.tool_calls)
    assert [call["id"] for call in calls] == ["call-child", "call-sibling"]
    assert sorted(p.state for p in task_parts(parent)) == ["completed", "error"]
    assert sorted(p.state for p in task_parts(parent, "subtask")) == [
        "completed",
        "error",
    ]
    terminal = [e for e in events if e["event"] == "harness.subtask_finished"]
    assert len(terminal) == 2
    assert {e["status"] for e in terminal} == {"aborted", "completed"}
    assert_settled(service, [parent, *rows])


@pytest.mark.parametrize("partial", ["", "useful partial answer"])
async def test_failed_child_returns_error_and_resume_id_not_completed_result(
    harness_workspace,
    partial,
) -> None:
    provider = RoutingProvider()
    provider.block_child = False
    provider.failure = "scripted provider failure"
    provider.partial = partial
    service, _, events = _service(provider=provider)
    parent = await _db_create_session(harness_workspace)
    assistant, task = await start(service, parent)
    await finish(task)
    child = children(parent)[0]
    failed = assistant_for(child)
    assert failed.finish == "error"
    assert failed.error == provider.failure
    assert failed.content == partial
    output = next(m.content for m in provider.histories[-1] if m.role == "tool")
    assert provider.failure in output
    assert str(child.id) in output
    assert 'state="completed"' not in output
    if partial:
        assert "Partial output:" in output
        assert partial in output
    assistant.refresh_from_db()
    assert assistant.finish == "stop"
    assert assistant.content == "parent continued"
    assert task_parts(parent)[0].state == "error"
    assert task_parts(parent, "subtask")[0].state == "error"
    assert any(
        e["event"] == "harness.subtask_finished" and e["status"] == "error"
        for e in events
    )
    assert_settled(service, [parent, child])


@pytest.mark.parametrize("phase", ["startup", "execution"])
async def test_user_abort_during_child_lifecycle_settles_entire_tree(
    harness_workspace,
    monkeypatch,
    phase,
) -> None:
    provider = RoutingProvider()
    service, _, _ = _service(provider=provider)
    parent = await _db_create_session(harness_workspace)
    entered = asyncio.Event()
    original = service._build_history

    async def build_history(session, **kwargs):
        if session.parent_id and phase == "startup":
            entered.set()
            await asyncio.Event().wait()
        return await original(session, **kwargs)

    monkeypatch.setattr(service, "_build_history", build_history)
    assistant, task = await start(service, parent)
    await checkpoint(entered if phase == "startup" else provider.entered["child"])
    rows = children(parent)
    assert len(rows) == 1
    tracked = list(service._tasks.values())
    await asyncio.wait_for(service.abort_run(parent.id), TIMEOUT)
    assert task.done()
    assert all(t.done() for t in tracked)
    assistant.refresh_from_db()
    assert assistant.finish == "aborted"
    assert assistant.error == "aborted by user"
    child_message = assistant_for(rows[0])
    if phase == "execution":
        assert child_message.finish == "aborted"
        assert child_message.error == "aborted by user"
    else:
        assert child_message.finish in {"aborted", "error"}
    assert all(p.state == "error" for p in task_parts(parent))
    assert all(p.state == "error" for p in task_parts(parent, "subtask"))
    assert_settled(service, [parent, *rows])


async def test_unexpected_parent_provider_cancellation_is_not_user_abort(
    harness_workspace,
) -> None:
    provider = RoutingProvider()
    provider.cancel_parent = True
    service, _, _ = _service(provider=provider)
    parent = await _db_create_session(harness_workspace)
    assistant, task = await start(service, parent)
    with pytest.raises(asyncio.CancelledError):
        await finish(task)
    assistant.refresh_from_db()
    assert assistant.finish == "error"
    assert assistant.error == "Run interrupted unexpectedly"
    assert "user" not in assistant.error
    assert_settled(service, [parent])


@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_cleanup_cancellation_ownership_and_finalization(
    harness_workspace,
    cancel_owner,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()
    cleanup_tasks = []

    async def cleanup(workspace_id, session_id, *, reason):
        cleanup_tasks.append(asyncio.current_task())
        entered.set()
        await release.wait()
        cleaned.set()

    service, _, _ = _service(provider=FakeProvider([[Delta(text="finished")]]))
    service._process_cleanup = cleanup
    parent = await _db_create_session(harness_workspace)
    assistant, task = await start(service, parent)
    await checkpoint(entered)
    if cancel_owner:
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await finish(task)
        assert cleaned.is_set()
        assert not cleanup_tasks[0].cancelled()
    else:
        cleanup_tasks[0].cancel()
        await finish(task)
        assert not task.cancelled()
        assert cleanup_tasks[0].cancelled()
    assistant.refresh_from_db()
    assert assistant.finish == "stop"
    assert_settled(service, [parent])


async def test_child_mcp_startup_deadline_does_not_cancel_healthy_parent(
    harness_workspace,
    monkeypatch,
    capsys,
) -> None:
    provider = RoutingProvider()
    provider.block_child = False
    service, _, _ = _service(provider=provider)
    parent = await _db_create_session(harness_workspace)
    child_accessor = ScriptedAccessor({"first": None, "bad": "initialize"})
    parent_accessor = FakeAccessor()
    accessors = iter([parent_accessor, child_accessor])

    async def accessor_factory(workspace_id):
        return next(accessors)

    def snapshot(session, organization_id, accessor):
        return (
            prepared_servers("first", "bad")
            if session.agent_name == "general"
            else prepared_servers()
        )

    service._accessor_factory = accessor_factory
    monkeypatch.setattr(service, "_prepare_mcp_snapshot_for_run", snapshot)
    assistant, task = await start(service, parent)
    await checkpoint(child_accessor.processes["bad"].reached)
    await finish(task)
    child = children(parent)[0]
    assert assistant_for(child).finish == "stop"
    assistant.refresh_from_db()
    assert assistant.finish == "stop"
    assert assistant.content == "parent continued"
    assert "child finished" in next(
        m.content for m in provider.histories[-1] if m.role == "tool"
    )
    assert child_accessor.closed == ["bad", "first"]
    assert all(p.close_count == 1 for p in child_accessor.processes.values())
    logs = capsys.readouterr().out
    assert "mcp_server_setup_skipped" in logs
    assert "startup timed out" in logs
    assert "McpServerHealthError" in logs
    assert task_parts(parent)[0].state == "completed"
    assert task_parts(parent, "subtask")[0].state == "completed"
    assert_settled(service, [parent, child])


@pytest.mark.parametrize("child_boundary", [False, True])
async def test_cancellation_after_admission_preserves_or_cancels_child_by_owner(
    harness_workspace,
    monkeypatch,
    child_boundary,
) -> None:
    """Caller cancellation cannot orphan a child or erase a durable root run."""
    provider = RoutingProvider()
    service, _, _ = _service(provider=provider)
    parent = await _db_create_session(harness_workspace)
    emission_entered = asyncio.Event()
    original = service._emit_frontend

    async def emit(event, data, workspace_id):
        if event == "harness.session_status" and data.get("status") == "busy":
            is_child = data.get("session_id") != str(parent.id)
            if is_child == child_boundary:
                emission_entered.set()
                await asyncio.Event().wait()
        await original(event, data, workspace_id)

    monkeypatch.setattr(service, "_emit_frontend", emit)
    caller = asyncio.create_task(
        service.start_run(
            parent,
            "parent",
            organization_id=parent.organization_id,
            workspace_id=str(parent.workspace_id),
        )
    )
    await checkpoint(emission_entered)
    await checkpoint(provider.entered["child"])
    child = children(parent)[0]
    child_task = service._tasks[str(child.id)]
    parent_task = service._tasks[str(parent.id)]
    if child_boundary:
        await finish(caller)
        await asyncio.wait_for(service.abort_run(parent.id), TIMEOUT)
        assert child_task.done()
        assert parent_task.done()
        assert assistant_for(parent).finish == "aborted"
        assert assistant_for(child).finish == "aborted"
    else:
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finish(caller)
        assert service._tasks[str(parent.id)] is parent_task
        assert not parent_task.done()
        assert not child_task.done()
        provider.release["child"].set()
        await finish(parent_task)
        assert assistant_for(parent).finish == "stop"
        assert assistant_for(child).finish == "stop"
    assert_settled(service, [parent, child])


async def test_direct_root_abort_during_history_admission(
    harness_workspace,
    monkeypatch,
) -> None:
    """Admission ownership is abortable before a background task exists."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    entered = asyncio.Event()

    async def blocked_history(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_build_history", blocked_history)
    caller = asyncio.create_task(
        service.start_run(
            session,
            "parent",
            organization_id=session.organization_id,
            workspace_id=str(session.workspace_id),
        )
    )
    await checkpoint(entered)
    assert str(session.id) not in service._tasks
    assistant = assistant_for(session)
    await asyncio.wait_for(service.abort_run(session.id), TIMEOUT)
    # Bound failure cleanup as well: a regression must not leave admission alive.
    if not caller.done():
        caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finish(caller)
    assistant.refresh_from_db()
    assert assistant.finish == "aborted"
    assert assistant.error == "aborted by user"
    assert_settled(service, [session])
    assert str(session.id) not in service._admissions


@pytest.mark.parametrize("user_abort", [False, True])
async def test_cancelled_background_before_execution_entry_is_finalized(
    harness_workspace,
    monkeypatch,
    recwarn,
    user_abort,
) -> None:
    """A task cancelled before its first instruction still owns durable state."""
    import gc
    import inspect

    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    key = str(session.id)
    idle = asyncio.Event()
    original_emit = service._emit
    original_spawn = service._spawn_background
    executions = []
    cancelled_coroutines = []

    async def emit(event, data):
        await original_emit(event, data)
        if event == "harness.session_status" and data.get("status") == "idle":
            idle.set()

    async def no_title(**kwargs):
        return None

    def spawn(coro):
        task = original_spawn(coro)
        if coro.cr_code.co_name == "_execute_run":
            executions.append(task)
            cancelled_coroutines.append(coro)
            assert len(executions) == 1
            if user_abort:
                service._abort_requested.add(key)
            task.cancel()
        return task

    service._emit = emit
    monkeypatch.setattr(service, "_generate_title", no_title)
    monkeypatch.setattr(service, "_spawn_background", spawn)
    assistant = await asyncio.wait_for(
        service.start_run(
            session,
            "parent",
            organization_id=session.organization_id,
            workspace_id=str(session.workspace_id),
        ),
        TIMEOUT,
    )
    await checkpoint(idle)
    assert len(executions) == 1
    assert executions[0].cancelled()
    assert inspect.getcoroutinestate(cancelled_coroutines[0]) == inspect.CORO_CLOSED
    assistant.refresh_from_db()
    assert assistant.finish == ("aborted" if user_abort else "error")
    assert assistant.error == (
        "aborted by user" if user_abort else "Run interrupted unexpectedly"
    )
    assert_settled(service, [session])
    assert key not in service._admissions
    gc.collect()
    assert not [w for w in recwarn if "was never awaited" in str(w.message)]


async def test_completed_run_not_rewritten_by_cancelled_busy_emission(
    harness_workspace,
    monkeypatch,
) -> None:
    """Admission caller cancellation cannot overwrite an already finished run."""
    service, _, _ = _service(provider=FakeProvider([[Delta(text="completed")]]))
    session = await _db_create_session(harness_workspace)
    busy = asyncio.Event()
    idle = asyncio.Event()
    original = service._emit_frontend

    async def emit(event, data, workspace_id):
        if event == "harness.session_status":
            if data.get("status") == "busy":
                busy.set()
                await asyncio.Event().wait()
            elif data.get("status") == "idle":
                await original(event, data, workspace_id)
                idle.set()
                return
        await original(event, data, workspace_id)

    monkeypatch.setattr(service, "_emit_frontend", emit)
    caller = asyncio.create_task(
        service.start_run(
            session,
            "parent",
            organization_id=session.organization_id,
            workspace_id=str(session.workspace_id),
        )
    )
    await checkpoint(busy)
    await checkpoint(idle)
    assistant = assistant_for(session)
    assert assistant.finish == "stop"
    assert str(session.id) not in service._tasks
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finish(caller)
    assistant.refresh_from_db()
    assert (assistant.finish, assistant.content, assistant.error) == (
        "stop",
        "completed",
        "",
    )
    assert_settled(service, [session])
    assert str(session.id) not in service._admissions


async def test_real_client_session_scope_cancel_persists_before_async_teardown(
    harness_workspace,
    monkeypatch,
) -> None:
    """Level-triggered MCP cancellation must not interrupt DB finalization."""
    import anyio
    from mcp.client.session import ClientSession

    from apps.harness.mcp_client.runtime import McpRuntime

    accessor = ScriptedAccessor({"first": None})
    runtimes = []
    original_setup = McpRuntime.setup
    original_close = accessor.processes["first"].aclose
    teardown = asyncio.Event()
    provider_entered = asyncio.Event()
    cleanup = asyncio.Event()
    observed = []

    async def setup(runtime, **kwargs):
        await original_setup(runtime, **kwargs)
        runtimes.append(runtime)

    async def close_stream():
        # A checkpoint proves teardown works asynchronously in the owning task.
        await anyio.sleep(0)
        await original_close()
        teardown.set()

    class ScopeCancellingProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):
            if not runtimes:  # Independent title generation is irrelevant.
                yield Delta(text="title")
                return
            conn = runtimes[0].connections[0]
            assert isinstance(conn._session, ClientSession)
            provider_entered.set()
            conn._session._task_group.cancel_scope.cancel()
            await anyio.sleep(0)
            pytest.fail("cancelled MCP scope did not cancel provider checkpoint")
            yield Delta(text="unreachable")

    service, _, _ = _service(provider=ScopeCancellingProvider([]))
    session = await _db_create_session(harness_workspace)

    async def accessor_factory(workspace_id):
        return accessor

    async def clean(workspace_id, session_id, *, reason):
        observed.append(assistant_for(session).finish)
        await anyio.sleep(0)
        cleanup.set()

    async def no_title(**kwargs):
        return None

    monkeypatch.setattr(McpRuntime, "setup", setup)
    monkeypatch.setattr(accessor.processes["first"], "aclose", close_stream)
    monkeypatch.setattr(service, "_generate_title", no_title)
    monkeypatch.setattr(
        service,
        "_prepare_mcp_snapshot_for_run",
        lambda *args: prepared_servers("first"),
    )
    service._accessor_factory = accessor_factory
    service._process_cleanup = clean
    assistant, task = await start(service, session)
    await checkpoint(provider_entered)
    try:
        await finish(task)
    except asyncio.CancelledError:
        pass  # Scope teardown may suppress its own CancelledError on SDK versions.
    assistant.refresh_from_db()
    assert assistant.finish == "error"
    assert assistant.error == "Run interrupted unexpectedly"
    assert observed == ["error"]
    assert cleanup.is_set()
    assert teardown.is_set()
    assert accessor.closed == ["first"]
    assert accessor.processes["first"].close_count == 1
    assert runtimes[0].connections == []
    assert_settled(service, [session])
    assert str(session.id) not in service._admissions


@pytest.mark.parametrize("parent_abort", [False, True])
async def test_parent_waits_for_child_preentry_replacement_finalizer(
    harness_workspace,
    monkeypatch,
    parent_abort,
) -> None:
    """A cancelled child shell is not a result until its new owner settles it."""
    provider = RoutingProvider()
    service, _, events = _service(provider=provider)
    parent = await _db_create_session(harness_workspace)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_spawn = service._spawn_background
    original_finalize = service._finalize_unstarted_run
    cancelled = []

    async def finalize(session, assistant):
        assert session.parent_id == parent.id
        entered.set()
        await release.wait()
        await original_finalize(session, assistant)

    def spawn(coro):
        # Inspect only execute-run frames: title and fallback owners stay real.
        session = coro.cr_frame.f_locals.get("session")
        task = original_spawn(coro)
        if coro.cr_code.co_name == "_execute_run" and session.parent_id:
            assert not cancelled
            cancelled.append(task)
            task.cancel()
        return task

    monkeypatch.setattr(service, "_spawn_background", spawn)
    monkeypatch.setattr(service, "_finalize_unstarted_run", finalize)
    assistant, parent_task = await start(service, parent)
    await checkpoint(entered)
    child = children(parent)[0]
    finalizer = service._tasks[str(child.id)]
    assert finalizer is not cancelled[0]
    assert cancelled[0].cancelled()
    assert not finalizer.done()
    assert not parent_task.done()
    assert len(provider.histories) == 1
    assert not any(e["event"] == "harness.subtask_finished" for e in events)
    if parent_abort:
        waiting = asyncio.Event()
        original_shield = asyncio.shield

        def shield(owner):
            if owner is finalizer and parent_task.cancelling():
                waiting.set()
            return original_shield(owner)

        monkeypatch.setattr(asyncio, "shield", shield)
        abort = asyncio.create_task(service.abort_run(parent.id))
        await checkpoint(waiting)
        assert str(child.id) in service._abort_requested
        assert not abort.done()
        assert not parent_task.done()
        assert not finalizer.done()
        assert finalizer.cancelling() == 0
        release.set()
        await finish(abort)
        assert parent_task.done()
        failed = assistant_for(child)
        assert (failed.finish, failed.error) == ("aborted", "aborted by user")
        assistant.refresh_from_db()
        assert (assistant.finish, assistant.error) == ("aborted", "aborted by user")
        assert len(provider.histories) == 1
    else:
        release.set()
        await finish(parent_task)
        failed = assistant_for(child)
        assert failed.finish == "error"
        assert failed.error == "Run interrupted unexpectedly"
        output = next(m.content for m in provider.histories[-1] if m.role == "tool")
        assert failed.error in output
        assert f"task_id: {child.id}" in output
        assistant.refresh_from_db()
        assert (assistant.finish, assistant.content) == ("stop", "parent continued")
    assert finalizer.done() and not finalizer.cancelled()
    assert task_parts(parent)[0].state == "error"
    assert task_parts(parent, "subtask")[0].state == "error"
    assert_settled(service, [parent, child])
    assert not service._admissions


async def test_direct_cancel_during_final_idle_emit_releases_bookkeeping(
    harness_workspace,
    monkeypatch,
) -> None:
    """Direct asyncio cancellation bypasses scope shielding but not release."""
    service, _, _ = _service(provider=FakeProvider([[Delta(text="terminal")]]))
    session = await _db_create_session(harness_workspace)
    entered = asyncio.Event()
    original = service._emit_frontend

    async def emit(event, data, workspace_id):
        if event == "harness.session_status" and data.get("status") == "idle":
            entered.set()
            await asyncio.Event().wait()
        await original(event, data, workspace_id)

    monkeypatch.setattr(service, "_emit_frontend", emit)
    assistant, task = await start(service, session)
    await checkpoint(entered)
    assert service._runs[str(session.id)]["finalizing"]
    assistant.refresh_from_db()
    assert assistant.finish == "stop"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finish(task)
    assistant.refresh_from_db()
    assert (assistant.finish, assistant.content, assistant.error) == (
        "stop",
        "terminal",
        "",
    )
    assert_settled(service, [session])
    assert not service._admissions


async def test_repeated_abort_during_cleanup_joins_without_cancelling_owner(
    harness_workspace,
    monkeypatch,
) -> None:
    """Repeated stop joins terminal cleanup rather than injecting new cancels."""
    provider = RoutingProvider()
    service, _, _ = _service(provider=provider)
    session = await _db_create_session(harness_workspace)
    cleanup_entered = asyncio.Event()
    release = asyncio.Event()
    joined = asyncio.Queue()
    cleanup_tasks = []
    original_shield = asyncio.shield

    async def cleanup(workspace_id, session_id, *, reason):
        cleanup_tasks.append(asyncio.current_task())
        cleanup_entered.set()
        await release.wait()

    # Use a plain provider wait (no child) to isolate terminal root ownership.
    provider = FakeProvider([])

    async def stream(model, messages, tools, opts=None):
        if any(m.role == "user" and m.content == "parent" for m in messages):
            provider_entered.set()
            await asyncio.Event().wait()
        yield Delta(text="title")

    provider_entered = asyncio.Event()
    provider.chat_stream = stream
    service._provider_factory = lambda org: provider
    service._process_cleanup = cleanup
    assistant, task = await start(service, session)
    await checkpoint(provider_entered)
    first_abort = asyncio.create_task(service.abort_run(session.id))
    await checkpoint(cleanup_entered)
    cancelling_before = task.cancelling()

    def shield(owner):
        if owner is task:
            joined.put_nowait(owner)
        return original_shield(owner)

    monkeypatch.setattr(asyncio, "shield", shield)
    second_abort = asyncio.create_task(service.abort_run(session.id))
    third_abort = asyncio.create_task(service.abort_run(session.id))
    await asyncio.wait_for(joined.get(), TIMEOUT)
    await asyncio.wait_for(joined.get(), TIMEOUT)
    assert not second_abort.done() and not third_abort.done()
    assert task.cancelling() == cancelling_before
    assert not cleanup_tasks[0].done()
    assert cleanup_tasks[0].cancelling() == 0
    release.set()
    await asyncio.wait_for(
        asyncio.gather(first_abort, second_abort, third_abort), TIMEOUT
    )
    assert task.done()
    assert cleanup_tasks[0].done() and not cleanup_tasks[0].cancelled()
    assistant.refresh_from_db()
    assert (assistant.finish, assistant.error) == ("aborted", "aborted by user")
    assert_settled(service, [session])
    assert not service._admissions


async def test_concurrent_aborts_join_blocked_interruption_persistence(
    harness_workspace,
    monkeypatch,
) -> None:
    """Repeated stop cannot cancel terminal persistence before finalizing starts."""
    provider_entered = asyncio.Event()
    persistence_entered = asyncio.Event()
    release = asyncio.Event()
    second_joined = asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):
            provider_entered.set()
            await asyncio.Event().wait()
            yield Delta(text="unreachable")

    service, _, _ = _service(provider=WaitingProvider([]))
    session = await _db_create_session(harness_workspace)
    original_record = service._record_run_interruption
    original_shield = asyncio.shield

    async def record(session, assistant, *, phase):
        persistence_entered.set()
        await release.wait()
        await original_record(session, assistant, phase=phase)

    async def no_title(**kwargs):
        return None

    monkeypatch.setattr(service, "_record_run_interruption", record)
    monkeypatch.setattr(service, "_generate_title", no_title)
    assistant, task = await start(service, session)
    await checkpoint(provider_entered)
    first_abort = asyncio.create_task(service.abort_run(session.id))
    await checkpoint(persistence_entered)
    assert task.cancelling() == 1
    assert not service._runs[str(session.id)].get("finalizing")

    def shield(owner):
        if owner is task and asyncio.current_task() is second_abort:
            second_joined.set()
        return original_shield(owner)

    monkeypatch.setattr(asyncio, "shield", shield)
    second_abort = asyncio.create_task(service.abort_run(session.id))
    await checkpoint(second_joined)
    assert task.cancelling() == 1
    assert not task.done()
    assert not first_abort.done() and not second_abort.done()
    assert not service._runs[str(session.id)].get("finalizing")
    assert str(session.id) in service._abort_requested
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first_abort, second_abort), TIMEOUT)
    assert all(result.status == "idle" for result in results)
    assert task.done()
    assistant.refresh_from_db()
    assert (assistant.finish, assistant.error) == ("aborted", "aborted by user")
    assert not children(session)
    assert_settled(service, [session])
    assert not service._admissions
