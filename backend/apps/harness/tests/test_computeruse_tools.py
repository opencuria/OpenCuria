"""Regression/backcompat tests for computer-use registries and routing.

Replaces the legacy OpenComputer tool tests (``ViewScreenTool``,
``LeftClickTool``, 1000-grid scaling): the desktop tool module is gone and
Agent-S children intentionally see an empty tool registry. Model
resolution/subagent-child DB coverage from the old file is ported below.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.harness.agents.definitions import get_agent, subagent_descriptions
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessSession
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.repositories import HarnessSessionRepository
from apps.harness.runner import HarnessRunner
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import (
    agent_s_tool_registry,
    computeruse_tool_registry,
    default_tool_registry,
)
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.subagents import (
    ALLOWED_SUBAGENT_TYPES,
    TaskArgs,
    TaskTool,
    _child_registry,
)


class ComputerUseTextProvider(ProviderAdapter):
    """Text-only provider so the computer-use loop finishes quickly."""

    name = "fake-cu"

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Yield one DONE plan with usage (immediate Agent-S DONE)."""
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


def test_agent_s_registry_empty_and_alias_in_sync() -> None:
    """Agent-S registry offers no tools; the legacy alias stays empty too."""
    assert agent_s_tool_registry().names() == []
    assert computeruse_tool_registry().names() == []


def test_default_registry_excludes_legacy_computer_use_tools() -> None:
    """Primary agents never see legacy OpenComputer tool names."""
    names = set(default_tool_registry().names())
    legacy = {
        "view_screen",
        "view_region",
        "move_mouse",
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "drag",
        "scroll",
        "type_text",
        "press_key",
        "open_url",
        "wait",
        "ask_user",
    }
    assert not names.intersection(legacy)


def test_computeruse_agent_definition_pins_agent_s_budget() -> None:
    """Computeruse skips system prompts and pins the Agent-S step budget."""
    agent = get_agent("computeruse")
    assert agent.mode == "subagent"
    assert agent.system_prompt == ""
    assert agent.steps == 15
    assert "Agent-S" in agent.description


def test_filtered_schemas_hide_legacy_names_from_build_agent(
    fake_accessor,
) -> None:
    """Build agent is not offered any legacy computer-use tool name."""
    runner = HarnessRunner(
        provider=ComputerUseTextProvider(),
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
    )
    schemas = runner._filtered_schemas(
        get_agent("build"),
        "build",
        depth=0,
        max_depth=1,
    )
    names = [schema.name for schema in schemas]
    assert "view_screen" not in names
    assert "bash" in names


def test_task_child_registry_computeruse_empty_and_hooks_copied() -> None:
    """Computeruse child gets an empty registry; build child keeps tools."""
    parent = default_tool_registry()
    seen: list[str] = []

    async def _before(tool_name: str, args: Any, ctx: Any) -> None:
        seen.append(tool_name)

    parent.add_before_hook(_before)
    build_child = _child_registry(parent, "general")
    assert "read" in build_child.names()
    assert "view_screen" not in build_child.names()

    cu_child = _child_registry(parent, "computeruse")
    assert cu_child.names() == []
    assert list(cu_child.before_hooks) == [_before]


async def test_task_accepts_computeruse_subagent(fake_accessor) -> None:
    """task(subagent_type=computeruse) passes agent validation."""
    ctx = ToolContext(
        session_id="sess-parent",
        workspace_id="ws-1",
        accessor=fake_accessor,
        model="parent-model",
        provider=object(),
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
        provider=object(),
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
    ("computer_use_model", "model_override", "ctx_model", "expected_model"),
    [
        ("cu-model", None, "parent-model", "cu-model"),
        ("cu-model", "override-model", "parent-model", "override-model"),
        ("", None, "parent-model", "parent-model"),
    ],
)
async def test_computeruse_subagent_model_resolution(
    harness_workspace,
    computer_use_model: str,
    model_override: str | None,
    ctx_model: str,
    expected_model: str,
) -> None:
    """Computer-use child sessions resolve model override > cu model > parent."""
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
        small_model="small-model",
        computer_use_model=computer_use_model,
        default_model="default-model",
    )
    children = list(HarnessSession.objects.filter(parent_id=parent.id))
    assert len(children) == 1
    assert children[0].agent_name == "computeruse"
    assert children[0].model == expected_model
    actions = [call[0] for call in accessor.desktop_calls]
    assert "hold" in actions
    assert "record_start" in actions
    assert "record_stop" in actions
    assert "release" in actions


def test_computeruse_recording_path_prefers_embedded_final_path() -> None:
    """Embedded final record_stop path wins; fallback stays single-rooted."""
    from apps.harness.agent_s.harness import (
        append_video_to_output,
        default_recording_path,
        sanitize_run_id,
        truncate_task_output,
    )
    from apps.harness.harness_service import computeruse_recording_path
    from apps.harness.tools.subagents import TASK_OUTPUT_MAX_CHARS

    child_id = "child-123"
    fallback = default_recording_path(sanitize_run_id(child_id))
    assert fallback.startswith("/workspace/.opencuria/computeruse/")
    assert fallback.endswith("/session.mp4")
    assert fallback.count("/workspace") == 1

    # No embed → sanitized fallback.
    assert computeruse_recording_path("done", child_id) == fallback
    # Empty / unterminated embeds → sanitized fallback (never empty).
    assert computeruse_recording_path("![Computer use]()", child_id) == fallback
    assert computeruse_recording_path("![Computer use](abc", child_id) == fallback

    # Embedded final path wins verbatim (no double /workspace prefix).
    embedded_out = "done\n\n![Computer use](/videos/final.mp4)"
    assert computeruse_recording_path(embedded_out, child_id) == "/videos/final.mp4"

    marker = f"![Computer use]({fallback})"
    once = append_video_to_output("done", fallback)
    assert once.count(marker) == 1

    display, truncated = truncate_task_output(
        embedded_out,
        computeruse_recording_path(embedded_out, child_id),
        TASK_OUTPUT_MAX_CHARS,
    )
    assert truncated is False
    assert display == embedded_out
    assert display.count("![Computer use](/videos/final.mp4)") == 1
    assert display.count("/workspace") == 0


def test_computeruse_recording_path_truncation_keeps_marker_once() -> None:
    """Long child output truncates to one marker, no doubled workspace root."""
    from apps.harness.agent_s.harness import truncate_task_output
    from apps.harness.harness_service import computeruse_recording_path

    child_id = "child-123"
    long_body = "x" * 9000
    path = computeruse_recording_path(long_body, child_id)
    assert path.count("/workspace") == 1
    display, truncated = truncate_task_output(long_body, path, 8000)
    assert truncated is True
    assert display.count(f"![Computer use]({path})") == 1
    assert display.count(path) == 1


@pytest.mark.django_db(transaction=True)
async def test_parent_cancel_propagates_through_subagent_tool(
    harness_workspace,
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
        subagent_type="computeruse",
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
            computer_use_model="cu-model",
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
            computer_use_model="cu-model",
        )
    assert current_task_cancelling() == 0
    assert finished
    assert finished[-1]["type"] == "subtask_finished"
    assert finished[-1]["status"] == "aborted"


@pytest.mark.django_db(transaction=True)
async def test_independent_general_cancel_reraises(
    harness_workspace,
) -> None:
    """Independent non-computeruse child cancel still propagates CancelledError."""
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
        subagent_type="general",
    )

    async def _cancelled_start_run(*_a: object, **_k: object):
        raise asyncio.CancelledError("child gone")

    service.start_run = _cancelled_start_run  # type: ignore[method-assign]
    ctx = _task_ctx(parent, str(harness_workspace.id))
    with pytest.raises(asyncio.CancelledError):
        await service._run_subagent_tool(
            parent=parent,
            args=args,
            ctx=ctx,
            subtask_id="sub-general-cancel",
            organization_id=harness_workspace.runner.organization_id,
            default_model="default-model",
        )


def current_task_cancelling() -> int:
    """Return the current task's pending cancel count (0 outside a task)."""
    import asyncio

    task = asyncio.current_task()
    return int(task.cancelling() if task is not None else 0)
