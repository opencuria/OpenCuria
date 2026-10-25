"""Agent-S subagent routing, model resolution, cancellation and recording tests."""

from __future__ import annotations

from typing import Any

import pytest

from apps.harness.agents.definitions import subagent_descriptions
from apps.harness.engines.base import RunOptions
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessSession
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.repositories import HarnessSessionRepository
from apps.harness.runner import HarnessRunner
from apps.harness.tests.conftest import FakeAccessor as BaseFakeAccessor
from apps.harness.tools import (
    agent_s_tool_registry,
    default_tool_registry,
)
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.subagents import (
    ALLOWED_SUBAGENT_TYPES,
    TaskArgs,
    TaskTool,
)


class FakeAccessor(BaseFakeAccessor):
    """Computer-use fixture implementing epoch-bound desktop ownership."""

    async def desktop_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        result = await super().desktop_action(action, args, timeout)
        if action in {"binding", "reserve", "hold", "renew", "release"}:
            result.update(
                ok=True,
                epoch="computeruse-fixture-epoch",
                display=":1",
                xauthority="/root/.Xauthority",
                protocol_version=1,
                lease_ttl_seconds=180,
                renew_interval_seconds=45,
            )
            if action != "binding":
                assert args is not None
                assert args["epoch"] == result["epoch"]
                assert args["revision"] == 1
                from uuid import UUID

                UUID(args["lease_id"])
                if action != "renew":
                    UUID(args["owner_id"])
                    assert args["kind"] == "computeruse"
                result["lease_state"] = {
                    "reserve": "reserved",
                    "hold": "held",
                    "renew": "held",
                    "release": "released",
                }[action]
        return result


def assert_desktop_lifecycle(accessor: FakeAccessor, *, recording: bool) -> None:
    """Pin attempt identity and ordered opt-in resource cleanup."""
    calls = accessor.desktop_calls
    expected = ["binding", "reserve", "hold"]
    if recording:
        expected += ["record_start", "record_stop"]
    expected += ["release"]
    actions = [call[0] for call in calls]
    assert [
        action for action in actions if action not in {"display_info", "screenshot"}
    ] == expected
    for index, action in enumerate(actions):
        if action in {"display_info", "screenshot"}:
            assert actions.index("hold") < index < actions.index("release")
    ownership = [
        args for action, args, _ in calls if action in {"reserve", "hold", "release"}
    ]
    assert ownership[0] == ownership[1] == ownership[2]
    if recording:
        for action, args, _ in calls:
            if action in {"record_start", "record_stop"}:
                assert args["run_id"] == ownership[0]["owner_id"]


class ComputerUseTextProvider(ProviderAdapter):
    """Text-only provider so the computer-use loop finishes quickly."""

    name = "fake-cu"

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Yield one DONE plan with usage (immediate Agent-S DONE)."""
        assert tools == []
        yield Delta(
            text="finished\n```python\nagent.done()\n```",
            usage=Usage(1, 1, 2),
        )


def _computeruse_harness_service(accessor: FakeAccessor) -> HarnessService:
    """HarnessService wired for computer-use subagent runs."""

    async def accessor_factory(_workspace_id: str) -> FakeAccessor:
        return accessor

    async def _emit(_event: str, _data: dict) -> None:
        return None

    return HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_emit,
        provider_factory=lambda _org: ComputerUseTextProvider(),
        accessor_factory=accessor_factory,
    )


def _parent_run_context(service: HarnessService, parent) -> None:
    """Seed parent assistant message tracking for subagent runs."""
    parent_assistant = service.messages.create(
        session_id=parent.id, role="assistant", content=""
    )
    service._runs[str(parent.id)] = {
        "session_id": str(parent.id),
        "message_id": str(parent_assistant.id),
        "tool_parts": {},
        "subtask_parts": {},
    }


def _task_ctx(parent, workspace_id: str, *, model: str = "parent-model") -> Any:
    """Minimal ToolContext stand-in for _run_subagent_tool."""
    return type(
        "Ctx",
        (),
        {
            "session_id": str(parent.id),
            "workspace_id": workspace_id,
            "model": model,
            "depth": 0,
            "max_depth": 1,
        },
    )()


def test_agent_s_registry_is_empty() -> None:
    """Agent-S plans use their own action syntax without function tools."""
    assert agent_s_tool_registry().names() == []


async def test_runner_routes_computeruse_through_agent_s() -> None:
    """The runner executes an Agent-S plan and releases its desktop lease."""
    accessor = FakeAccessor()
    runner = HarnessRunner(
        provider=ComputerUseTextProvider(),
        tools=agent_s_tool_registry(),
        accessor=accessor,
    )
    result = await runner.run(
        "finish",
        "computeruse",
        "fake/main",
        "build",
        RunOptions(auto_approve=True, session_id="agent-s-routing"),
    )
    assert result.finish_reason == "stop"
    assert_desktop_lifecycle(accessor, recording=False)
    assert "![Computer use](" not in result.output
    assert "recording_path" not in result.metadata


async def test_task_accepts_computeruse_subagent(fake_accessor) -> None:
    """task(subagent_type=computeruse) passes agent validation."""
    ctx = ToolContext(
        session_id="sess-parent",
        workspace_id="ws-1",
        accessor=fake_accessor,
        model="parent-model",
        registry=default_tool_registry(),
        depth=1,
        max_depth=1,
    )
    with pytest.raises(ToolError, match="depth limit"):
        await TaskTool().execute(
            {
                "description": "desktop",
                "prompt": "click save",
                "subagent_type": "computeruse",
            },
            ctx,
        )


async def test_task_rejects_unknown_subagent(fake_accessor) -> None:
    """Unknown subagent types are still rejected."""
    ctx = ToolContext(
        session_id="sess-parent",
        workspace_id="ws-1",
        accessor=fake_accessor,
        model="parent-model",
        registry=default_tool_registry(),
    )
    with pytest.raises(ToolError, match="Unknown subagent"):
        await TaskTool().execute(
            {
                "description": "bad",
                "prompt": "nope",
                "subagent_type": "nope",
            },
            ctx,
        )


def test_allowed_subagent_types_includes_computeruse() -> None:
    """Task tool advertises computeruse as a valid subagent."""
    assert "computeruse" in ALLOWED_SUBAGENT_TYPES
    assert "computeruse" in subagent_descriptions()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("configured_model", "model_override", "ctx_model", "expected_model"),
    [
        ("cu-model", None, "parent-model", "cu-model"),
        ("cu-model", "override-model", "parent-model", "override-model"),
        ("", None, "parent-model", "parent-model"),
    ],
)
async def test_computeruse_subagent_model_resolution(
    harness_workspace,
    configured_model: str,
    model_override: str | None,
    ctx_model: str,
    expected_model: str,
) -> None:
    """Resolve model override, fixed AgentConfig model, then inherited parent."""
    accessor = FakeAccessor()
    service = _computeruse_harness_service(accessor)
    parent = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
        agent_name="build",
        mode="build",
        model="parent-model",
    )
    _parent_run_context(service, parent)
    args = TaskArgs(
        description="desktop task",
        prompt="click save",
        subagent_type="computeruse",
        model_override=model_override,
    )
    await service._run_subagent_tool(
        parent=parent,
        args=args,
        ctx=_task_ctx(parent, str(harness_workspace.id), model=ctx_model),
        subtask_id="sub-cu-model",
        organization_id=harness_workspace.runner.organization_id,
        agent_configs={
            "computeruse": {
                "model": configured_model,
                "inherit_model": not bool(configured_model),
            }
        },
    )
    children = list(HarnessSession.objects.filter(parent_id=parent.id))
    assert len(children) == 1
    assert children[0].agent_name == "computeruse"
    assert children[0].model == expected_model
    actions = [call[0] for call in accessor.desktop_calls]
    assert_desktop_lifecycle(accessor, recording=False)
    # Recording defaults to off: reserve/hold/release ownership runs.
    assert "hold" in actions
    assert "record_start" not in actions
    assert "record_stop" not in actions
    assert "release" in actions


@pytest.mark.parametrize("agent", ALLOWED_SUBAGENT_TYPES)
@pytest.mark.django_db(transaction=True)
async def test_parent_cancel_propagates_through_subagent_tool(
    harness_workspace,
    agent,
) -> None:
    """Cancelling the waiting parent re-raises; no ToolError conversion."""
    import asyncio

    accessor = FakeAccessor()
    service = _computeruse_harness_service(accessor)
    parent = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
        agent_name="build",
        mode="build",
        model="parent-model",
    )
    _parent_run_context(service, parent)
    args = TaskArgs(
        description="desktop task",
        prompt="click save",
        subagent_type=agent,
    )
    gate = asyncio.Event()

    async def _blocking_start_run(*_a: object, **_k: object):
        await gate.wait()
        raise AssertionError("unreachable")

    service.start_run = _blocking_start_run  # type: ignore[method-assign]
    ctx = _task_ctx(parent, str(harness_workspace.id))
    tool_task = asyncio.ensure_future(
        service._run_subagent_tool(
            parent=parent,
            args=args,
            ctx=ctx,
            subtask_id="sub-parent-cancel",
            organization_id=harness_workspace.runner.organization_id,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    tool_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tool_task
    gate.set()


@pytest.mark.django_db(transaction=True)
async def test_independent_computeruse_cancel_converts_to_tool_error(
    harness_workspace,
) -> None:
    """Independent computeruse child cancel → aborted + ToolError (no raise)."""
    import asyncio

    accessor = FakeAccessor()
    service = _computeruse_harness_service(accessor)
    parent = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
        agent_name="build",
        mode="build",
        model="parent-model",
    )
    _parent_run_context(service, parent)
    args = TaskArgs(
        description="desktop task",
        prompt="click save",
        subagent_type="computeruse",
    )
    finished: list[dict[str, object]] = []

    async def _fake_on_runner_event(session, assistant, event):
        finished.append(dict(event))

    service._on_runner_event = _fake_on_runner_event  # type: ignore[method-assign]

    async def _cancelled_start_run(*_a: object, **_k: object):
        raise asyncio.CancelledError("take-control")

    service.start_run = _cancelled_start_run  # type: ignore[method-assign]
    ctx = _task_ctx(parent, str(harness_workspace.id))
    with pytest.raises(ToolError, match="aborted"):
        await service._run_subagent_tool(
            parent=parent,
            args=args,
            ctx=ctx,
            subtask_id="sub-cu-abort",
            organization_id=harness_workspace.runner.organization_id,
        )
    assert current_task_cancelling() == 0
    assert finished
    assert finished[-1]["type"] == "subtask_finished"
    assert finished[-1]["status"] == "aborted"


@pytest.mark.parametrize("agent", ["general", "explore"])
@pytest.mark.django_db(transaction=True)
async def test_independent_child_start_cancel_converts_to_tool_error(
    harness_workspace,
    agent,
) -> None:
    """A child interrupted before admission cannot abort the waiting parent."""
    import asyncio

    accessor = FakeAccessor()
    service = _computeruse_harness_service(accessor)
    parent = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
        agent_name="build",
        mode="build",
        model="parent-model",
    )
    _parent_run_context(service, parent)
    args = TaskArgs(
        description="search task",
        prompt="find things",
        subagent_type=agent,
    )

    async def _cancelled_start_run(*_a: object, **_k: object):
        raise asyncio.CancelledError("child gone")

    service.start_run = _cancelled_start_run  # type: ignore[method-assign]
    ctx = _task_ctx(parent, str(harness_workspace.id))
    with pytest.raises(ToolError, match="task_id:"):
        await service._run_subagent_tool(
            parent=parent,
            args=args,
            ctx=ctx,
            subtask_id="sub-general-cancel",
            organization_id=harness_workspace.runner.organization_id,
        )

    assert current_task_cancelling() == 0


def current_task_cancelling() -> int:
    """Return the current task's pending cancel count (0 outside a task)."""
    import asyncio

    task = asyncio.current_task()
    return int(task.cancelling() if task is not None else 0)


@pytest.mark.django_db(transaction=True)
async def test_computeruse_subagent_run_recording_opt_in(harness_workspace) -> None:
    """enable_recording=True restores the record_start/record_stop lifecycle."""
    from apps.harness.services import AgentSConfigService

    accessor = FakeAccessor()
    service = _computeruse_harness_service(accessor)
    AgentSConfigService().save_config(
        harness_workspace.runner.organization_id, {"enable_recording": True}
    )
    parent = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
        agent_name="build",
        mode="build",
        model="parent-model",
    )
    _parent_run_context(service, parent)
    args = TaskArgs(
        description="desktop task",
        prompt="click save",
        subagent_type="computeruse",
    )
    await service._run_subagent_tool(
        parent=parent,
        args=args,
        ctx=_task_ctx(parent, str(harness_workspace.id)),
        subtask_id="sub-cu-rec",
        organization_id=harness_workspace.runner.organization_id,
    )
    actions = [call[0] for call in accessor.desktop_calls]
    assert_desktop_lifecycle(accessor, recording=True)
    assert actions.count("hold") == 1
    assert "record_start" in actions
    assert "record_stop" in actions
    assert "release" in actions
