"""Tests for computer-use tools, registries, and model resolution."""

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
from apps.harness.tests.conftest import (
    DEFAULT_DESKTOP_HEIGHT,
    DEFAULT_DESKTOP_WIDTH,
    FakeAccessor,
)
from apps.harness.tools import (
    COMPUTER_USE_TOOL_NAMES,
    computeruse_tool_registry,
    default_tool_registry,
)
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.computeruse import (
    LeftClickTool,
    OpenUrlTool,
    ViewRegionTool,
    ViewScreenTool,
    pixel_coordinates,
    scale_coordinate,
)
from apps.harness.tools.subagents import ALLOWED_SUBAGENT_TYPES, TaskArgs, TaskTool, _child_registry


class ComputerUseTextProvider(ProviderAdapter):
    """Text-only provider so the computer-use loop finishes quickly."""

    name = "fake-cu"

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Yield one text delta with usage."""
        yield Delta(text="desktop done", usage=Usage(1, 1, 2))


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


def _ctx(accessor: FakeAccessor) -> ToolContext:
    return ToolContext(
        session_id="sess-cu",
        workspace_id="ws-1",
        accessor=accessor,
        agent_name="computeruse",
    )


async def test_view_region_rejects_small_span(fake_accessor) -> None:
    """Regions must span at least 20 normalized units."""
    ctx = _ctx(fake_accessor)
    await ViewScreenTool().execute({}, ctx)
    with pytest.raises(ToolError, match="20 normalized units"):
        await ViewRegionTool().execute(
            {"left": 0, "top": 0, "right": 10, "bottom": 10},
            ctx,
        )


async def test_view_screen_sets_image_jpeg(fake_accessor) -> None:
    """Screenshots populate ToolResult.image_jpeg."""
    ctx = _ctx(fake_accessor)
    result = await ViewScreenTool().execute({}, ctx)
    assert result.image_jpeg is not None
    assert result.image_jpeg.startswith(b"\xff\xd8")


def test_scale_coordinate_maps_grid_edges() -> None:
    """0 maps to 0 and 1000 maps near the far edge on the 1001 scale."""
    width = DEFAULT_DESKTOP_WIDTH
    assert scale_coordinate(0, 1001, width) == 0
    assert scale_coordinate(1000, 1001, width) == round(1000 * width / 1001)


def test_pixel_coordinates_use_last_capture() -> None:
    """Normalized coordinates map within the current capture frame."""
    from apps.harness.tools.computeruse import LastCapture

    capture = LastCapture(
        target_left=10,
        target_top=20,
        target_width=100,
        target_height=200,
    )
    x, y = pixel_coordinates(capture, x=0, y=1000)
    assert x == 10
    assert y == 20 + scale_coordinate(1000, 1001, 200)


async def test_view_region_without_prior_view_screen_raises() -> None:
    """view_region requires an existing coordinate frame."""
    accessor = FakeAccessor()
    ctx = _ctx(accessor)
    with pytest.raises(ToolError, match="view_screen"):
        await ViewRegionTool().execute(
            {"left": 0, "top": 0, "right": 100, "bottom": 100},
            ctx,
        )


async def test_left_click_without_coordinates_skips_move(fake_accessor) -> None:
    """Cursor clicks do not invoke move when x/y are omitted."""
    ctx = _ctx(fake_accessor)
    await ViewScreenTool().execute({}, ctx)
    fake_accessor.desktop_calls.clear()
    await LeftClickTool().execute({}, ctx)
    actions = [call[0] for call in fake_accessor.desktop_calls]
    assert "move" not in actions
    assert "click" in actions


async def test_open_url_rejects_non_http_schemes(fake_accessor) -> None:
    """Only http/https URLs are accepted."""
    ctx = _ctx(fake_accessor)
    tool = OpenUrlTool()
    for url in ("file:///etc/passwd", "javascript:alert(1)"):
        with pytest.raises(ToolError, match="http"):
            await tool.execute({"url": url}, ctx)


def test_default_registry_excludes_computer_use_tools() -> None:
    """Primary agents never see computer-use tool names."""
    names = set(default_tool_registry().names())
    assert not names.intersection(COMPUTER_USE_TOOL_NAMES)


def test_computeruse_registry_excludes_bash_and_task() -> None:
    """Computer-use registry contains only the OpenComputer tool set."""
    names = set(computeruse_tool_registry().names())
    assert names == set(COMPUTER_USE_TOOL_NAMES)
    for excluded in ("bash", "read", "write", "task", "todowrite"):
        assert excluded not in names


def test_filtered_schemas_hide_computer_use_from_build_agent(fake_accessor) -> None:
    """Build agent is not offered view_screen."""
    runner = HarnessRunner(
        provider=None,
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
    assert "view_screen" not in [schema.name for schema in schemas]
    assert "bash" in [schema.name for schema in schemas]


def test_filtered_schemas_offer_computer_use_to_child(fake_accessor) -> None:
    """Computer-use child gets view_screen but not bash."""
    runner = HarnessRunner(
        provider=None,
        tools=computeruse_tool_registry(),
        evaluator=PermissionEvaluator(),
        accessor=fake_accessor,
    )
    schemas = runner._filtered_schemas(
        get_agent("computeruse"),
        "build",
        depth=1,
        max_depth=1,
    )
    names = [schema.name for schema in schemas]
    assert "view_screen" in names
    assert "bash" not in names


def test_task_child_registry_filters_by_agent() -> None:
    """Parent build child drops computer-use tools; computeruse child swaps registries."""
    parent = default_tool_registry()
    build_child = _child_registry(parent, "general")
    assert "read" in build_child.names()
    assert "view_screen" not in build_child.names()

    cu_child = _child_registry(parent, "computeruse")
    assert "view_screen" in cu_child.names()
    assert "bash" not in cu_child.names()


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
    assert "ensure" in actions
    assert "record_start" in actions
    assert "record_stop" in actions
