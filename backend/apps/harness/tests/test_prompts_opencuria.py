"""Shared product context must not redefine a native harness's behavior."""

from apps.harness.agents.definitions import get_agent
from apps.harness.prompts.opencuria import opencuria_context


def test_context_describes_workspace_and_supported_chat_paths() -> None:
    """All coding engines can expose media without depending on their cwd."""
    context = opencuria_context(get_agent("build"))
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
    context = opencuria_context(get_agent("build"))
    assert context == opencuria_context(get_agent("plan"))
    assert context == opencuria_context(get_agent("general"))
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
    context = opencuria_context(get_agent("explore"))
    assert "rooted at /workspace" in context
    assert "![Screenshot](/workspace/screenshots/result.png)" in context
    assert "development servers" not in context
    assert "installing dependencies" not in context
    assert "running migrations" not in context


def test_hidden_helpers_do_not_get_product_context() -> None:
    """Text-only internal helpers need no workspace or chat instructions."""
    for name in ("title", "summary", "compaction"):
        assert opencuria_context(get_agent(name)) == ""
