"""Depth regression tests for persistent sessions and in-memory child runs."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.sync import sync_to_async

from apps.harness.constants import DEFAULT_MAX_DEPTH
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessSession, Todo
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, LLMMessage, ToolSchema
from apps.harness.repositories import HarnessSessionRepository
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.services import SubagentConfigService
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tests.test_tools_task import FakeProvider, _text_step, _tool_step
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.subagents import TaskArgs
from apps.organizations.models import Organization


class RecursiveProvider(FakeProvider):
    """Attempt one task at every depth, even when the schema withholds it."""

    def __init__(self, tool: str = "task") -> None:
        super().__init__([])
        self.tool = tool

    async def chat_stream(
        self, model: str, messages: list[LLMMessage], tools: list[ToolSchema], opts=None
    ) -> AsyncIterator[Delta]:
        """Record offered tools and deliberately try a hidden tool call."""
        if messages[-1].role == "user":
            self.calls.append({"tools": [tool.name for tool in tools]})
            args = (
                {"description": "nested", "prompt": "spawn", "subagent_type": "general"}
                if self.tool == "task"
                else {"todos": [{"content": "child todo", "status": "pending"}]}
            )
            step = _tool_step(self.tool, args)
        else:
            step = _text_step("done")
        for delta in step:
            yield delta


def _service(provider: FakeProvider):
    """Build the real service with only network/workspace I/O faked."""
    runs: list[RunOptions] = []
    events: list[dict[str, Any]] = []

    class RecordingRunner(HarnessRunner):
        async def _send(self, event: dict[str, Any]) -> None:
            events.append(event)
            await super()._send(event)

        async def run(self, prompt, agent_name, model, mode="build", opts=None):
            runs.append(opts)
            return await super().run(prompt, agent_name, model, mode, opts)

    async def emit(event: str, data: dict[str, Any]) -> None:
        pass

    async def accessor(workspace_id: str) -> FakeAccessor:
        return FakeAccessor(workspace_id)

    async def no_title(**kwargs: Any) -> None:
        pass

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=emit,
        provider_factory=lambda org: provider,
        runner_factory=lambda **kwargs: RecordingRunner(**kwargs),
        accessor_factory=accessor,
    )
    service._generate_title = no_title
    return service, runs, events


async def _session(workspace, *, parent_id=None) -> HarnessSession:
    """Create a persisted root or child session."""
    return await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
        title="depth regression",
        agent_name="general" if parent_id else "build",
        mode="build",
        model="fake-model",
        parent_id=parent_id,
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("override", [None, 1, 3])
async def test_persistent_tree_enforces_configured_depth(harness_workspace, override):
    """The real session path increments depth and denies calls at the limit."""
    expected = DEFAULT_MAX_DEPTH if override is None else override
    org_id = harness_workspace.runner.organization_id
    if override is not None:
        await sync_to_async(SubagentConfigService().save_config)(org_id, override)
    provider = RecursiveProvider()
    service, runs, events = _service(provider)
    root = await _session(harness_workspace)
    assistant = await service.start_run(root, "spawn")
    await service._tasks[str(root.id)]
    await sync_to_async(assistant.refresh_from_db)()

    assert assistant.finish == "stop"
    assert [run.depth for run in runs] == list(range(expected + 1))
    assert all(run.max_depth == expected for run in runs)
    assert len(provider.calls) == expected + 1
    for depth, call in enumerate(provider.calls):
        assert ("task" in call["tools"]) is (depth < expected)
        assert ("todowrite" in call["tools"]) is (depth == 0)
    errors = [e for e in events if e.get("error") and "depth limit" in e["error"]]
    assert len(errors) == 1
    assert f"depth={expected}" in errors[0]["error"]
    assert await sync_to_async(HarnessSession.objects.count)() == expected + 1


@pytest.mark.django_db(transaction=True)
async def test_active_tree_keeps_root_limit_when_setting_changes(harness_workspace):
    """Changing settings mid-run affects the next root run, not its children."""
    org_id = harness_workspace.runner.organization_id

    class UpdatingProvider(RecursiveProvider):
        async def chat_stream(self, model, messages, tools, opts=None):
            if not self.calls:
                await sync_to_async(SubagentConfigService().save_config)(org_id, 1)
            async for delta in super().chat_stream(model, messages, tools, opts):
                yield delta

    service, runs, _ = _service(UpdatingProvider())
    root = await _session(harness_workspace)
    await service.start_run(root, "spawn")
    await service._tasks[str(root.id)]
    assert [run.depth for run in runs] == [0, 1, 2]
    assert [run.max_depth for run in runs] == [2, 2, 2]

    other = await _session(harness_workspace)
    await service.start_run(other, "spawn")
    await service._tasks[str(other.id)]
    assert [run.depth for run in runs[3:]] == [0, 1]
    assert [run.max_depth for run in runs[3:]] == [1, 1]


@pytest.mark.django_db(transaction=True)
async def test_task_resume_preserves_child_depth_and_history(harness_workspace):
    """task_id resume uses persisted ancestry instead of becoming a root run."""
    provider = RecursiveProvider()
    service, runs, _ = _service(provider)
    root = await _session(harness_workspace)
    parent_assistant = await sync_to_async(service.messages.create)(
        session_id=root.id, role="assistant", content=""
    )
    service._runs[str(root.id)] = {
        "message_id": str(parent_assistant.id),
        "tool_parts": {},
        "subtask_parts": {},
    }
    ctx = ToolContext(
        session_id=str(root.id),
        workspace_id=str(harness_workspace.id),
        accessor=FakeAccessor(),
        model="fake-model",
        depth=0,
        max_depth=2,
    )
    first = await service._run_subagent_tool(
        parent=root,
        args=TaskArgs(description="child", prompt="spawn"),
        ctx=ctx,
        subtask_id="first",
        organization_id=root.organization_id,
    )
    resumed = await service._run_subagent_tool(
        parent=root,
        args=TaskArgs(
            description="resume", prompt="spawn", task_id=first.metadata["task_id"]
        ),
        ctx=ctx,
        subtask_id="resume",
        organization_id=root.organization_id,
    )
    assert resumed.metadata["resumed"] is True
    assert resumed.metadata["task_id"] == first.metadata["task_id"]
    assert [run.depth for run in runs] == [1, 2, 1, 2]
    assert all(run.max_depth == 2 for run in runs)
    assert runs[0].history == []
    assert runs[2].history
    assert len(await sync_to_async(service.sessions.list_children)(root.id)) == 1


@pytest.mark.django_db(transaction=True)
async def test_direct_child_start_hides_and_denies_todowrite(harness_workspace):
    """Even internally started children cannot write a child todo store."""
    service, runs, events = _service(RecursiveProvider(tool="todowrite"))
    root = await _session(harness_workspace)
    child = await _session(harness_workspace, parent_id=root.id)
    await service.start_run(child, "todo")
    await service._tasks[str(child.id)]
    assert runs[0].depth == 1
    assert any("todowrite is not available" in e.get("error", "") for e in events)
    assert await sync_to_async(Todo.objects.count)() == 0


@pytest.mark.django_db(transaction=True)
async def test_service_refuses_direct_task_at_limit(harness_workspace):
    """The persistent callback also refuses a direct over-limit invocation."""
    service, runs, _ = _service(RecursiveProvider())
    root = await _session(harness_workspace)
    ctx = ToolContext(
        session_id=str(root.id),
        workspace_id=str(harness_workspace.id),
        accessor=FakeAccessor(),
        depth=2,
        max_depth=2,
    )
    with pytest.raises(ToolError, match="depth limit"):
        await service._run_subagent_tool(
            parent=root,
            args=TaskArgs(description="blocked", prompt="spawn"),
            ctx=ctx,
            subtask_id="blocked",
            organization_id=root.organization_id,
        )
    assert not runs
    assert await sync_to_async(HarnessSession.objects.count)() == 1


@pytest.mark.django_db(transaction=True)
async def test_over_limit_existing_child_rejected_before_run(harness_workspace):
    """Old deeper sessions cannot start when today's configured limit is lower."""
    service, runs, _ = _service(RecursiveProvider())
    root = await _session(harness_workspace)
    child = await _session(harness_workspace, parent_id=root.id)
    grandchild = await _session(harness_workspace, parent_id=child.id)
    await sync_to_async(SubagentConfigService().save_config)(root.organization_id, 1)
    with pytest.raises(ValueError, match="depth 2 exceeds max_depth 1"):
        await service.start_run(grandchild, "spawn")
    assert not runs
    assert not await sync_to_async(service.messages.list_for_session)(grandchild.id)


@pytest.mark.parametrize("max_depth", [1, 2, 3])
async def test_in_memory_child_tree_uses_same_depth_limit(fake_accessor, max_depth):
    """The nonpersistent path keeps task below the limit, never above it."""
    provider = RecursiveProvider()
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
        emit=emit,
    )
    await runner.run(
        "spawn", "build", "fake-model", "build", RunOptions(max_depth=max_depth)
    )
    assert len(provider.calls) == max_depth + 1
    assert all("task" in c["tools"] for c in provider.calls[:-1])
    assert "task" not in provider.calls[-1]["tools"]
    assert all("todowrite" not in c["tools"] for c in provider.calls[1:])
    assert any("depth limit" in e.get("error", "") for e in events)


@pytest.mark.django_db
@pytest.mark.parametrize("invalid", ["cycle", "cross_org"])
def test_invalid_ancestry_fails_closed(harness_workspace, invalid):
    """Broken ancestry cannot reset depth or loop forever."""
    root = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        model="fake-model",
    )
    child = HarnessSessionRepository.create(
        workspace_id=root.workspace_id,
        organization_id=root.organization_id,
        model="fake-model",
        parent_id=root.id,
    )
    if invalid == "cycle":
        HarnessSession.objects.filter(id=root.id).update(parent_id=child.id)
    else:
        org = Organization.objects.create(name="other", slug=f"other-{uuid.uuid4()}")
        HarnessSession.objects.filter(id=root.id).update(organization_id=org.id)
    with pytest.raises(ValueError, match="ancestry"):
        HarnessSessionRepository.get_depth(child.id)


@pytest.mark.parametrize("target", ["general", "computeruse"])
async def test_read_only_child_cannot_delegate_to_writable_agent(fake_accessor, target):
    """Retaining task must not allow explore to bypass its read-only contract."""
    from apps.harness.tools.subagents import TaskTool

    ctx = ToolContext(
        session_id="explore-child",
        workspace_id="ws-1",
        accessor=fake_accessor,
        agent_name="explore",
        depth=1,
        max_depth=2,
    )
    with pytest.raises(ToolError, match="Read-only explore"):
        await TaskTool().execute(
            TaskArgs(description="writable", prompt="edit", subagent_type=target), ctx
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("resume", [False, True])
async def test_persistent_explore_cannot_launch_or_resume_writable_child(
    harness_workspace, resume
):
    """Validate the stored agent on resume as well as the requested target."""
    service, runs, _ = _service(RecursiveProvider())
    root = await _session(harness_workspace)
    parent = await _session(harness_workspace, parent_id=root.id)
    parent.agent_name = "explore"
    await sync_to_async(parent.save)(update_fields=["agent_name"])
    assistant = await sync_to_async(service.messages.create)(
        session_id=parent.id, role="assistant", content=""
    )
    service._runs[str(parent.id)] = {"message_id": str(assistant.id)}
    task_id = None
    if resume:
        writable = await _session(harness_workspace, parent_id=parent.id)
        task_id = str(writable.id)
    ctx = ToolContext(
        session_id=str(parent.id),
        workspace_id=str(harness_workspace.id),
        accessor=FakeAccessor(),
        agent_name="explore",
        model="fake-model",
        depth=1,
        max_depth=2,
    )
    with pytest.raises(ToolError, match="Read-only explore"):
        await service._run_subagent_tool(
            parent=parent,
            args=TaskArgs(
                description="writable",
                prompt="edit",
                subagent_type="explore" if resume else "general",
                task_id=task_id,
            ),
            ctx=ctx,
            subtask_id="blocked",
            organization_id=parent.organization_id,
        )
    assert not runs
    assert await sync_to_async(HarnessSession.objects.count)() == (3 if resume else 2)
