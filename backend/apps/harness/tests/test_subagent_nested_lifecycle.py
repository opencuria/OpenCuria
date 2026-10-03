"""Real three-level lifecycle regressions with event-controlled provider waits."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from apps.harness import harness_service as service_module
from apps.harness.providers.base import Delta, ProviderError
from apps.harness.tests.test_harness_service import FakeProvider, _tool_step
from apps.harness.tests.test_subagent_depth import _service, _session
from apps.harness.tests.test_subagent_lifecycle import (
    assert_settled,
    assistant_for,
    checkpoint,
    children,
    finish,
    start,
    task_parts,
)

pytestmark = pytest.mark.django_db(transaction=True)


class NestedProvider(FakeProvider):
    """Root and general child delegate once; only the grandchild can fail."""

    def __init__(self, agent: str = "general", *, failure: bool = False) -> None:
        super().__init__([])
        self.agent = agent
        self.failure = failure
        self.child_entered = asyncio.Event()
        self.spawn = asyncio.Event()
        self.spawn.set()
        self.grandchild_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.histories: dict[str, list[list[Any]]] = {}

    async def chat_stream(self, model, messages, tools, opts=None):
        prompt = next(m.content for m in reversed(messages) if m.role == "user")
        self.histories.setdefault(prompt, []).append(list(messages))
        if prompt == "grandchild":
            self.grandchild_entered.set()
            await self.release.wait()
            if self.failure:
                raise ProviderError("grandchild provider failed")
            yield Delta(text="grandchild finished")
            return
        if any(m.role == "tool" for m in messages):
            yield Delta(text=f"{prompt} continued")
            return
        target = "child" if prompt == "parent" else "grandchild"
        if prompt == "child":
            self.child_entered.set()
            await self.spawn.wait()
        for delta in _tool_step(
            "task",
            {
                "description": target,
                "prompt": target,
                "subagent_type": "general" if target == "child" else self.agent,
            },
            call_id=f"call-{target}",
        ):
            yield delta


def tree(root):
    child = children(root)[0]
    return [root, child, children(child)[0]]


def assert_tree_settled(service, sessions) -> None:
    assert_settled(service, sessions)
    assert not service._admissions
    assert not service._tasks
    assert not service._runs
    assert not service._event_locks
    assert not service._abort_requested


def assert_user_abort(service, sessions) -> None:
    for session in sessions:
        message = assistant_for(session)
        assert (message.finish, message.error) == (
            "aborted",
            "aborted by user",
        ), f"session={session.id}, parent={session.parent_id}"
        for kind in ("tool", "subtask"):
            assert all(p.state == "error" for p in task_parts(session, kind))
    assert_tree_settled(service, sessions)


@pytest.mark.parametrize("agent", ["general", "explore"])
@pytest.mark.parametrize("failure", ["cancel", "provider-error"])
async def test_independent_grandchild_failure_keeps_both_ancestors_running(
    harness_workspace, agent, failure
) -> None:
    """A failed leaf supplies its real resume ID without aborting ancestors."""
    provider = NestedProvider(agent, failure=failure == "provider-error")
    service, runs, _ = _service(provider)
    root = await _session(harness_workspace)
    assistant, root_task = await start(service, root)
    await checkpoint(provider.grandchild_entered)
    sessions = tree(root)
    grandchild = sessions[-1]
    leaf_task = service._tasks[str(grandchild.id)]
    if failure == "cancel":
        leaf_task.cancel()
    else:
        provider.release.set()
    await finish(root_task)
    failed = assistant_for(grandchild)
    expected = (
        "Run interrupted unexpectedly"
        if failure == "cancel"
        else "grandchild provider failed"
    )
    assert (failed.finish, failed.error) == ("error", expected)
    output = next(m for m in provider.histories["child"][-1] if m.role == "tool")
    assert output.tool_call_id == "call-grandchild"
    assert f"task_id: {grandchild.id}" in output.content
    assert expected in output.content
    assert 'state="completed"' not in output.content
    for session, prompt in zip(sessions[:2], ("parent", "child"), strict=True):
        message = assistant_for(session)
        assert (message.finish, message.content) == ("stop", f"{prompt} continued")
        expected_state = "completed" if session.id == root.id else "error"
        for kind in ("tool", "subtask"):
            assert [p.state for p in task_parts(session, kind)] == [expected_state]
    assistant.refresh_from_db()
    assert assistant.finish == "stop"
    assert [options.depth for options in runs] == [0, 1, 2]
    assert [options.max_depth for options in runs] == [2, 2, 2]
    assert grandchild.agent_name == agent
    assert_tree_settled(service, sessions)


async def test_root_abort_joins_already_cancelling_grandchild_persistence(
    harness_workspace, monkeypatch
) -> None:
    """Root stop cannot inject a second cancel before leaf persistence settles."""
    provider = NestedProvider()
    service, _, _ = _service(provider)
    root = await _session(harness_workspace)
    entered = asyncio.Event()
    release = asyncio.Event()
    joined = asyncio.Event()
    original_record = service._record_run_interruption
    original_shield = asyncio.shield

    async def record(session, assistant, *, phase):
        if session.id == sessions[-1].id:
            entered.set()
            await release.wait()
        # Compute provenance after release, not before the root stop marks it.
        await original_record(session, assistant, phase=phase)

    assistant, root_task = await start(service, root)
    await checkpoint(provider.grandchild_entered)
    sessions = tree(root)
    leaf_task = service._tasks[str(sessions[-1].id)]
    monkeypatch.setattr(service, "_record_run_interruption", record)
    leaf_task.cancel()
    await checkpoint(entered)
    assert leaf_task.cancelling() == 1
    assert not service._runs[str(sessions[-1].id)].get("finalizing")

    def shield(owner):
        if owner is leaf_task and root_task.cancelling():
            joined.set()
        return original_shield(owner)

    monkeypatch.setattr(asyncio, "shield", shield)
    abort = asyncio.create_task(service.abort_run(root.id))
    try:
        await checkpoint(joined)
        assert leaf_task.cancelling() == 1
        assert not abort.done() and not root_task.done() and not leaf_task.done()
        assert all(str(s.id) in service._abort_requested for s in sessions)
        assert not service._runs[str(sessions[-1].id)].get("finalizing")
    finally:
        release.set()
        await finish(abort)
    assert root_task.done() and leaf_task.done()
    assistant.refresh_from_db()
    assert (assistant.finish, assistant.error) == ("aborted", "aborted by user")
    assert_user_abort(service, sessions)


async def test_root_abort_covers_grandchild_admitted_after_descendant_snapshot(
    harness_workspace, monkeypatch
) -> None:
    """An outdated traversal cannot lose user provenance on a newly admitted leaf."""
    provider = NestedProvider()
    provider.spawn.clear()
    service, _, _ = _service(provider)
    root = await _session(harness_workspace)
    admitted = asyncio.Event()
    release_snapshot = asyncio.Event()
    original_history = service._build_history
    original_adapter = service_module.sync_to_async
    descendant_query = service.sessions.list_descendant_ids
    snapshots = []

    async def history(session, **kwargs):
        if session.parent_id and session.parent_id != root.id:
            admitted.set()
            await asyncio.Event().wait()
        return await original_history(session, **kwargs)

    def adapter(func, *args, **kwargs):
        wrapped = original_adapter(func, *args, **kwargs)
        if func != descendant_query:
            return wrapped

        async def snapshot_then_admit(session_id):
            snapshot = await wrapped(session_id)
            if session_id == root.id and not snapshots:
                snapshots.append(snapshot)
                provider.spawn.set()
                # Run on the event loop, never block the sync DB worker thread.
                await checkpoint(admitted)
                await checkpoint(release_snapshot)
            return snapshot

        return snapshot_then_admit

    monkeypatch.setattr(service, "_build_history", history)
    assistant, root_task = await start(service, root)
    await checkpoint(provider.child_entered)
    child = children(root)[0]
    assert all(str(s.id) in service._runs for s in (root, child))
    monkeypatch.setattr(service_module, "sync_to_async", adapter)
    abort = asyncio.create_task(service.abort_run(root.id))
    await checkpoint(admitted)
    sessions = tree(root)
    assert set(snapshots[0]) == {root.id, child.id}
    assert sessions[-1].id not in snapshots[0]
    assert str(sessions[-1].id) in service._admissions
    assert not provider.grandchild_entered.is_set()
    release_snapshot.set()
    await finish(abort)
    assert root_task.done()
    assistant.refresh_from_db()
    assert (assistant.finish, assistant.error) == ("aborted", "aborted by user")
    assert_user_abort(service, sessions)
