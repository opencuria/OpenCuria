"""Tests for static agent definitions (M4)."""

from __future__ import annotations

import pytest

from apps.harness.agents.definitions import (
    AGENT_DEFINITIONS,
    AgentDefinition,
    get_agent,
    list_agents,
    subagent_descriptions,
)


def test_all_agents_defined() -> None:
    """All expected agents exist with valid modes."""
    assert set(AGENT_DEFINITIONS) == {
        "build",
        "plan",
        "general",
        "explore",
        "computeruse",
        "title",
        "compaction",
    }
    assert get_agent("build").mode == "primary"
    assert get_agent("plan").mode == "primary"
    assert get_agent("general").mode == "subagent"
    assert get_agent("explore").mode == "subagent"
    assert get_agent("computeruse").mode == "subagent"
    assert get_agent("computeruse").steps is None
    assert get_agent("computeruse").permissions == {"*": "allow"}
    assert get_agent("title").mode == "hidden"
    assert get_agent("compaction").mode == "hidden"


def test_build_allows_everything() -> None:
    """Build grants allow on the wildcard (deny-tools are not filtered)."""
    assert get_agent("build").permissions == {"*": "allow"}


def test_plan_edit_asks_and_bash_allows_read_only() -> None:
    """Plan asks for edits; read-only bash is allowed, mutating asks."""
    from apps.harness.permissions.evaluator import PermissionEvaluator

    evaluator = PermissionEvaluator(agent_rules=dict(get_agent("plan").permissions))
    assert evaluator.evaluate("edit", "/workspace/a.py") == "ask"
    assert evaluator.evaluate("write", "/workspace/a.py") == "ask"
    assert evaluator.evaluate("bash", "git status") == "allow"
    assert evaluator.evaluate("bash", "git log --oneline") == "allow"
    assert evaluator.evaluate("bash", "ls -la") == "allow"
    assert evaluator.evaluate("bash", "cat README.md") == "allow"
    assert evaluator.evaluate("bash", "rm -rf /tmp/x") == "ask"
    assert evaluator.evaluate("read", "/workspace/a.py") == "allow"


def test_explore_is_read_only() -> None:
    """Explore denies edits/destructive bash, allows research tools."""
    from apps.harness.permissions.evaluator import PermissionEvaluator

    evaluator = PermissionEvaluator(agent_rules=dict(get_agent("explore").permissions))
    assert evaluator.evaluate("edit", "/workspace/a.py") == "deny"
    assert evaluator.evaluate("write", "/workspace/a.py") == "deny"
    assert evaluator.evaluate("bash", "git diff") == "allow"
    assert evaluator.evaluate("bash", "ls") == "allow"
    assert evaluator.evaluate("bash", "find /workspace -name '*.py'") == "allow"
    assert evaluator.evaluate("bash", "rg --line-number pattern /workspace") == "allow"
    assert evaluator.evaluate("bash", "grep -rn pattern /workspace") == "allow"
    assert evaluator.evaluate("bash", "rm -rf /tmp/x") == "deny"
    assert evaluator.evaluate("bash", "sudo reboot") == "deny"
    assert evaluator.evaluate("read", "/workspace/a.py") == "allow"
    assert evaluator.evaluate("grep", "/workspace") == "allow"
    assert evaluator.evaluate("process", "") == "deny"
    assert get_agent("explore").steps is None


def test_hidden_agents_deny_tools() -> None:
    """Hidden agents deny tools; only title uses the small model."""
    for name in ("title", "compaction"):
        agent = get_agent(name)
        assert agent.permissions == {"*": "deny"}
        assert agent.steps is None
    assert get_agent("title").model_override == "small"
    assert get_agent("compaction").model_override is None


def test_unknown_agent_raises() -> None:
    """Unknown agent names raise KeyError."""
    with pytest.raises(KeyError, match="Unknown agent"):
        get_agent("nonexistent")


def test_list_agents_filters_by_mode() -> None:
    """Mode filtering returns the right subsets."""
    assert {agent.name for agent in list_agents(mode="primary")} == {"build", "plan"}
    assert {agent.name for agent in list_agents(mode="subagent")} == {
        "general",
        "explore",
        "computeruse",
    }
    with pytest.raises(ValueError, match="Invalid agent mode"):
        list_agents(mode="bogus")


def test_subagent_descriptions_only_subagents() -> None:
    """Only subagent-mode agents are advertised to the task tool."""
    assert set(subagent_descriptions()) == {"general", "explore", "computeruse"}


def test_invalid_mode_rejected() -> None:
    """Agent definitions validate their mode."""
    with pytest.raises(ValueError, match="Invalid agent mode"):
        AgentDefinition(
            name="x",
            mode="superuser",
            description="d",
            system_prompt="p",
        )


def test_agent_fields_present() -> None:
    """Every agent carries name, mode, description, prompt, color."""
    for agent in AGENT_DEFINITIONS.values():
        assert agent.name and agent.description and agent.system_prompt
        assert agent.color
        assert agent.steps is None
        assert agent.model_override is None or agent.model_override == "small"
