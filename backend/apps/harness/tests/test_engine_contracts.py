"""Characterize the execution values shared by every harness engine."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

from apps.harness.engines.base import RunOptions, RunResult
from apps.harness.providers.base import LLMMessage, Usage
from apps.harness.steering import SteeringChannel


def test_run_options_keep_mutable_state_separate_between_turns() -> None:
    """A run must not inherit another run's history, skills or once grants."""
    first = RunOptions()
    second = RunOptions()

    first.history.append(LLMMessage(role="user", content="first turn"))
    first.skills.append("research")
    first.once_approved.add(("bash", "pwd"))

    assert second.history == []
    assert second.skills == []
    assert second.once_approved == set()


def test_run_options_replacement_preserves_execution_ports() -> None:
    """Engine-specific option copies keep callbacks and the steering channel."""
    permission = AsyncMock(return_value="once")
    question = AsyncMock(return_value=["yes"])
    authorize_read = AsyncMock(return_value=True)
    subagent = AsyncMock()
    steering = SteeringChannel(deliver=AsyncMock())
    original = RunOptions(
        on_permission=permission,
        on_question=question,
        authorize_file_read=authorize_read,
        run_subagent=subagent,
        steering=steering,
    )

    copied = replace(original, session_id="child", depth=1)

    assert original.session_id == ""
    assert original.depth == 0
    assert copied.session_id == "child"
    assert copied.depth == 1
    assert copied.on_permission is permission
    assert copied.on_question is question
    assert copied.authorize_file_read is authorize_read
    assert copied.run_subagent is subagent
    assert copied.steering is steering


def test_run_results_keep_metadata_separate_between_turns() -> None:
    """Engine recovery metadata belongs only to the result that produced it."""
    usage = Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    first = RunResult("answer", 1, usage, 0.25, "stop")
    second = RunResult("next answer", 2, usage, 0.5, "length")

    first.metadata["external_session_id"] = "engine-session"

    assert first.usage is usage
    assert first.cost == 0.25
    assert first.finish_reason == "stop"
    assert second.metadata == {}
