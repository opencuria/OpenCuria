"""Shared product context must not redefine a native harness's behavior."""

from apps.harness.access.guest import LINUX_GUEST, WINDOWS_GUEST
from apps.harness.agents.definitions import get_agent
from apps.harness.prompts.opencuria import opencuria_context


def test_context_describes_workspace_and_supported_chat_paths() -> None:
    """All coding engines can expose media without depending on their cwd."""
    context = opencuria_context(get_agent("build"), LINUX_GUEST)
    assert "rooted at /workspace" in context
    assert "Preserve existing user work" in context
    assert "does not extend to external or production services" in context
    assert "![Screenshot](/workspace/screenshots/result.png)" in context
    assert "![Recording](/workspace/recordings/demo.mp4)" in context
    assert "[Report](/workspace/reports/report.pdf)" in context
    assert "Relative chat paths resolve from /workspace" in context
    assert "regardless of the shell's working directory" in context


def test_context_defers_coding_behavior_to_the_owning_harness() -> None:
    """Changing an OpenCuria role must not change the external base prompt."""
    context = opencuria_context(get_agent("build"), LINUX_GUEST)
    assert context == opencuria_context(get_agent("plan"), LINUX_GUEST)
    assert context == opencuria_context(get_agent("general"), LINUX_GUEST)
    assert "When permitted by the current task, mode, and permissions" in context
    assert "starting development servers" in context
    assert "installing dependencies" in context
    assert "running migrations against workspace-local test data" in context
    assert "You are opencuria" not in context
    assert "full shell" not in context
    assert "without unnecessary confirmation" not in context
    for name in ("build", "plan", "general", "explore"):
        assert get_agent(name).system_prompt not in context
    assert len(context.split()) < 200


def test_explore_gets_chat_context_without_workspace_setup_guidance() -> None:
    """Read-only agents can reference existing artifacts without setup work."""
    context = opencuria_context(get_agent("explore"), LINUX_GUEST)
    assert "rooted at /workspace" in context
    assert "![Screenshot](/workspace/screenshots/result.png)" in context
    assert "development servers" not in context
    assert "installing dependencies" not in context
    assert "running migrations" not in context


def test_hidden_helpers_do_not_get_product_context() -> None:
    """Text-only internal helpers need no workspace or chat instructions."""
    for name in ("title", "summary", "compaction"):
        assert opencuria_context(get_agent(name), LINUX_GUEST) == ""


def test_linux_context_keeps_the_existing_environment_text() -> None:
    """Linux workspaces get exactly the established environment paragraph."""
    context = opencuria_context(get_agent("build"), LINUX_GUEST)
    assert context.split("\n\n")[0] == (
        "You are working inside an isolated OpenCuria workspace "
        "(Linux container or VM) rooted at /workspace, not on the user's "
        "machine. Preserve existing user work. Workspace isolation does not "
        "extend to external or production services."
    )


def test_windows_context_states_only_the_guest_constraints() -> None:
    """Windows workspaces learn the shell, paths, encoding and GUI limits."""
    context = opencuria_context(get_agent("build"), WINDOWS_GUEST)
    environment = context.split("\n\n")[0]
    assert "(Windows 11 VM) rooted at /workspace" in environment
    assert "C:\\workspace for native Windows programs" in environment
    assert "Shell commands run in bash (MSYS2) with GNU tools" in environment
    assert "PowerShell is available as powershell.exe" in environment
    assert "switches with a double slash (cmd //c ver" in environment
    assert "Write files as UTF-8 without a byte order mark" in environment
    assert "GUI programs started from the shell are not visible" in environment
    assert "Linux" not in context
    assert "Preserve existing user work" in environment
    assert (
        context.split("\n\n")[1:]
        == opencuria_context(get_agent("build"), LINUX_GUEST).split("\n\n")[1:]
    )
    assert opencuria_context(get_agent("title"), WINDOWS_GUEST) == ""
