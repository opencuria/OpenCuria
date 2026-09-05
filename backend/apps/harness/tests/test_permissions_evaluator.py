"""Tests for the permission evaluator (OpenCode semantics)."""

from __future__ import annotations

import pytest

from apps.harness.permissions.evaluator import PermissionEvaluator


def test_default_is_allow() -> None:
    """No rules means allow."""
    evaluator = PermissionEvaluator()
    assert evaluator.evaluate("read", "/workspace/a.txt") == "allow"
    assert evaluator.evaluate("bash", "ls") == "allow"


def test_reserved_defaults_are_ask() -> None:
    """external_directory and doom_loop default to ask."""
    evaluator = PermissionEvaluator()
    assert evaluator.evaluate("read", "/tmp/x", external_directory=True) == "ask"
    assert evaluator.evaluate("bash", "ls", doom_loop=True) == "ask"


def test_last_match_wins() -> None:
    """Later rules override earlier ones in the same dict."""
    # Insertion order decides: a later matching tool pattern wins.
    ordered = PermissionEvaluator(global_rules=dict([("*", "ask"), ("bash", "allow")]))
    assert ordered.evaluate("bash", "ls") == "allow"
    assert ordered.evaluate("read", "x") == "ask"


def test_last_match_wins_within_layer() -> None:
    """Insertion order decides when several tool patterns match."""
    evaluator = PermissionEvaluator(
        global_rules={"*": "deny", "bash": "ask", "bas?": "allow"}
    )
    # "bas?" matches "bash" and comes last -> allow wins.
    assert evaluator.evaluate("bash", "ls") == "allow"


def test_wildcards_star_and_question() -> None:
    """* and ? wildcards match tool names."""
    evaluator = PermissionEvaluator(global_rules={"re*": "deny", "???h": "ask"})
    assert evaluator.evaluate("read", "f") == "deny"
    assert evaluator.evaluate("bash", "ls") == "ask"
    assert evaluator.evaluate("glob", "p") == "allow"


def test_granular_patterns_last_match_wins() -> None:
    """Granular per-tool patterns apply last-match-wins on the action."""
    evaluator = PermissionEvaluator(
        global_rules={
            "edit": {"*": "ask", "/workspace/src/**": "allow"},
        }
    )
    assert evaluator.evaluate("edit", "/workspace/src/a.py") == "allow"
    assert evaluator.evaluate("edit", "/workspace/other.py") == "ask"


def test_granular_question_wildcard() -> None:
    """? matches a single character in granular patterns."""
    evaluator = PermissionEvaluator(
        global_rules={"bash": {"*": "deny", "git sta?us": "allow"}}
    )
    assert evaluator.evaluate("bash", "git status") == "allow"
    assert evaluator.evaluate("bash", "rm -rf /") == "deny"


def test_invalid_decision_rejected() -> None:
    """Unknown decision strings raise ValueError."""
    with pytest.raises(ValueError, match="Invalid permission"):
        PermissionEvaluator(global_rules={"bash": "sometimes"})
    with pytest.raises(ValueError, match="Invalid permission"):
        PermissionEvaluator(global_rules={"bash": {"*": "maybe"}})


def test_mode_merge_global_agent_mode() -> None:
    """Layers merge global -> agent -> mode; deny always wins."""
    evaluator = PermissionEvaluator(
        global_rules={"*": "ask"},
        agent_rules={"bash": "allow"},
        mode_rules={"plan": {"bash": "ask", "edit": "deny"}},
    )
    assert evaluator.evaluate("bash", "ls") == "allow"
    assert evaluator.evaluate("bash", "ls", mode="plan") == "ask"
    assert evaluator.evaluate("edit", "f", mode="plan") == "deny"
    assert evaluator.evaluate("read", "f", mode="plan") == "ask"
    assert evaluator.evaluate("read", "f") == "ask"


def test_deny_wins_over_merged_allow() -> None:
    """A deny in any layer beats allow from another layer."""
    evaluator = PermissionEvaluator(
        global_rules={"bash": "deny"},
        agent_rules={"bash": "allow"},
        mode_rules={"build": {"bash": "allow"}},
    )
    assert evaluator.evaluate("bash", "ls") == "deny"
    assert evaluator.evaluate("bash", "ls", mode="build") == "deny"


def test_unknown_mode_ignored() -> None:
    """Modes without rules fall back to global/agent layers."""
    evaluator = PermissionEvaluator(
        global_rules={"*": "ask"},
        mode_rules={"plan": {"bash": "deny"}},
    )
    assert evaluator.evaluate("bash", "ls", mode="build") == "ask"


def test_reserved_keys_overridable() -> None:
    """Explicit rules can override the ask default for reserved keys."""
    evaluator = PermissionEvaluator(
        global_rules={"doom_loop": "deny", "external_directory": "allow"}
    )
    assert evaluator.evaluate("bash", "x", doom_loop=True) == "deny"
    assert evaluator.evaluate("read", "/tmp/x", external_directory=True) == "allow"


@pytest.mark.parametrize("decision", ["allow", "ask", "deny"])
def test_all_decisions_reachable(decision: str) -> None:
    """Each decision can be produced by a matching rule."""
    evaluator = PermissionEvaluator(global_rules={"bash": decision})
    assert evaluator.evaluate("bash", "ls") == decision
