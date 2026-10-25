"""Tests for the system-prompt composer (M4)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apps.harness.access.file_types import FileContent
from apps.harness.agents.definitions import get_agent
from apps.harness.prompts.composer import (
    MAX_CONTEXT_FILE_BYTES,
    compose_system_prompt,
    load_project_context,
)
from apps.harness.prompts.opencuria import opencuria_context
from apps.harness.providers.base import ToolSchema
from apps.harness.tests.conftest import FakeAccessor


def _tools() -> list[ToolSchema]:
    return [
        ToolSchema(name="read", description="Read a file.", parameters={}),
        ToolSchema(name="bash", description="Run a command.", parameters={}),
    ]


async def test_walk_up_loads_nearest_first() -> None:
    """AGENTS.md files from cwd up to root are all inlined nearest-first."""
    accessor = FakeAccessor(
        files={
            "/workspace/AGENTS.md": b"root rules",
            "/workspace/sub/AGENTS.md": b"sub rules",
        }
    )
    files, truncated = await load_project_context(accessor, "/workspace/sub")
    assert [item.path for item in files] == [
        "/workspace/sub/AGENTS.md",
        "/workspace/AGENTS.md",
    ]
    assert [item.content for item in files] == ["sub rules", "root rules"]
    assert truncated is False


async def test_claude_md_fallback() -> None:
    """CLAUDE.md is used when no AGENTS.md exists at that level."""
    accessor = FakeAccessor(files={"/workspace/CLAUDE.md": b"claude rules"})
    files, _ = await load_project_context(accessor, "/workspace")
    assert [item.path for item in files] == ["/workspace/CLAUDE.md"]


async def test_agents_md_preferred_over_claude_md() -> None:
    """AGENTS.md wins when both exist in the same directory."""
    accessor = FakeAccessor(
        files={
            "/workspace/AGENTS.md": b"agents",
            "/workspace/CLAUDE.md": b"claude",
        }
    )
    files, _ = await load_project_context(accessor, "/workspace/deep/nested")
    assert [item.content for item in files] == ["agents"]


async def test_missing_files_are_silently_skipped() -> None:
    """No context files means empty sources (no error)."""
    files, truncated = await load_project_context(FakeAccessor(files={}), "/workspace")
    assert files == []
    assert truncated is False


async def test_accessor_errors_are_guarded() -> None:
    """Runner failures during walk-up never break composition."""

    class Exploding(FakeAccessor):
        async def read_file(self, path: str, max_size=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("runner down")

    files, _ = await load_project_context(Exploding(files={}), "/workspace")
    assert files == []


async def test_oversized_files_skipped_and_flagged() -> None:
    """Files above the size cap are skipped with truncated=True."""

    class BigFile(FakeAccessor):
        async def read_file(self, path: str, max_size=None):  # type: ignore[no-untyped-def]
            return FileContent(content=b"x", truncated=True)

    files, truncated = await load_project_context(BigFile(files={}), "/workspace")
    assert files == []
    assert truncated is True


async def test_max_files_limit() -> None:
    """Only MAX_CONTEXT_FILES files are inlined (nearest-first)."""
    accessor = FakeAccessor(
        files={
            "/workspace/AGENTS.md": b"root",
            "/workspace/a/AGENTS.md": b"a",
            "/workspace/a/b/AGENTS.md": b"b",
            "/workspace/a/b/c/AGENTS.md": b"c",
        }
    )
    files, truncated = await load_project_context(accessor, "/workspace/a/b/c")
    assert len(files) == 3
    assert files[0].path == "/workspace/a/b/c/AGENTS.md"
    assert truncated is True


async def test_composer_sections_and_metadata() -> None:
    """Composer renders agent prompt, context, env, tools, subagents."""
    accessor = FakeAccessor(files={"/workspace/AGENTS.md": b"follow the guide"})
    composed = await compose_system_prompt(
        agent=get_agent("build"),
        mode="build",
        tools=_tools(),
        subagents={"general": "Delegated subtasks."},
        accessor=accessor,
        cwd="/workspace",
        now=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    assert "senior software engineer" in composed.system
    assert "follow the guide" in composed.system
    assert composed.sources == ["/workspace/AGENTS.md"]
    assert "2026-09-05" in composed.system
    assert "Working directory: /workspace" in composed.system
    assert "Mode: build" in composed.system
    assert "isolated OpenCuria workspace" in composed.system
    assert composed.system.count(opencuria_context(get_agent("build"))) == 1
    assert "- read: Read a file." in composed.system
    assert "- general: Delegated subtasks." in composed.system
    assert (
        "Independent tool calls in one assistant message run in parallel"
        in composed.system
    )
    assert "Launch independent subagents in one message" in composed.system
    assert "![Screenshot](/workspace/screenshots/result.png)" in composed.system
    assert "actual running UI (not a mock)" in composed.system
    assert composed.truncated is False


@pytest.mark.parametrize("native_tool_contracts", [False, True])
@pytest.mark.parametrize(
    ("agent_name", "mode"),
    [("build", "build"), ("plan", "plan"), ("general", "build"), ("general", "plan")],
)
async def test_composer_encourages_local_testing_with_boundaries(
    agent_name: str, mode: str, native_tool_contracts: bool
) -> None:
    """Active agents get initiative guidance without bypassing permissions."""
    composed = await compose_system_prompt(
        agent=get_agent(agent_name),
        mode=mode,
        native_tool_contracts=native_tool_contracts,
    )
    assert composed.system.count(opencuria_context(get_agent(agent_name))) == 1
    assert "starting development servers" in composed.system
    assert "installing dependencies" in composed.system
    assert "running migrations against workspace-local test data" in composed.system
    assert "testing relevant flows in the running app" in composed.system
    assert "Preserve existing user work" in composed.system
    assert "When permitted by the current task, mode, and permissions" in (
        composed.system
    )
    assert "does not extend to external or production services" in composed.system
    if agent_name == "plan":
        assert "Ask before deliberate source/configuration changes" in composed.system


@pytest.mark.parametrize("native_tool_contracts", [False, True])
@pytest.mark.parametrize("agent_name", ["explore", "title", "compaction"])
@pytest.mark.parametrize("mode", ["plan", "build"])
async def test_composer_restricted_agents_do_not_get_local_testing_guidance(
    agent_name: str, mode: str, native_tool_contracts: bool
) -> None:
    """Read-only and hidden agents must not be told to mutate the workspace."""
    composed = await compose_system_prompt(
        agent=get_agent(agent_name),
        mode=mode,
        native_tool_contracts=native_tool_contracts,
    )
    assert "starting development servers" not in composed.system
    assert "installing dependencies" not in composed.system
    assert "running migrations" not in composed.system
    if agent_name == "explore":
        assert "read-only research subagent" in composed.system.lower()
        assert "Never edit or write files" in composed.system
    else:
        assert "not on the user's machine" not in composed.system
        assert "OpenCuria chat renders" not in composed.system


async def test_shared_chat_paths_stay_workspace_relative_from_a_subdirectory() -> None:
    """Shell cwd must not change the paths advertised to the chat renderer."""
    composed = await compose_system_prompt(
        agent=get_agent("build"), mode="build", cwd="/workspace/project/backend"
    )
    assert "Working directory: /workspace/project/backend" in composed.system
    assert "Relative chat paths resolve from /workspace" in composed.system
    assert "![Screenshot](/workspace/screenshots/result.png)" in composed.system
    assert composed.system.count("OpenCuria chat renders") == 1


async def test_composer_skills_references_extension_point() -> None:
    """Skills/references render only when provided (empty by default)."""
    base = await compose_system_prompt(agent=get_agent("plan"), mode="plan")
    assert "Skills" not in base.system
    extended = await compose_system_prompt(
        agent=get_agent("plan"),
        mode="plan",
        skills=["python-expert"],
        references=["@/workspace/a.py"],
    )
    assert "python-expert" in extended.system
    assert "@/workspace/a.py" in extended.system


async def test_composer_rejects_invalid_mode() -> None:
    """Unknown modes raise ValueError."""
    with pytest.raises(ValueError, match="Invalid mode"):
        await compose_system_prompt(agent=get_agent("build"), mode="turbo")


async def test_composer_uses_max_context_byte_cap() -> None:
    """The per-file cap constant matches the read tool guard scale."""
    assert MAX_CONTEXT_FILE_BYTES == 32 * 1024


async def test_plan_general_prompt_follows_its_own_agent_permissions() -> None:
    """Delegated general agents do not acquire the root Plan edit restriction."""
    from apps.harness.engines.claude.runtime_state import ClaudeRuntimeState

    for native_tool_contracts in (False, True):
        composed = await compose_system_prompt(
            agent=get_agent("general"),
            mode="plan",
            native_tool_contracts=native_tool_contracts,
        )
        system = (
            ClaudeRuntimeState.claude_system_prompt(composed.system, "plan")
            if native_tool_contracts
            else composed.system
        )
        assert "follow the configured agent permissions" in system
        assert "Ask before deliberate source" not in system
        assert "before user approval" not in system
        assert "unclassified mutations" not in system


async def test_plan_explore_prompt_keeps_read_only_restrictions() -> None:
    """Plan never grants Explore the shell/process or general edit freedoms."""
    composed = await compose_system_prompt(
        agent=get_agent("explore"), mode="plan", native_tool_contracts=True
    )
    assert (
        "Plan mode does not relax Explore's read-only restrictions" in composed.system
    )
    assert "Do not edit files, run tests or mutating commands, or delegate" in (
        composed.system
    )
