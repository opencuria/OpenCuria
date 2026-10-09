"""Tests for Claude model catalog validation and engine state storage."""

from __future__ import annotations

import pytest

from apps.harness.engines.catalog import (
    CLAUDE_AGENT_SDK_VERSION,
    CLAUDE_CODE_CLI_VERSION,
    ENGINE_IDS,
    list_claude_models,
    normalize_claude_effort,
    normalize_claude_model,
    resolve_claude_sdk_model,
)
from apps.harness.engines.repositories import HarnessEngineRepository
from apps.harness.models import HarnessMessage, HarnessMessageRole, HarnessSession
from apps.harness.repositories import HarnessMessageRepository, HarnessSessionRepository
from common.utils import decrypt_value


def test_claude_catalog_is_separate_and_constrained() -> None:
    models = list_claude_models()
    assert ENGINE_IDS == ("native", "claude")
    assert {model.id for model in models} == {"sonnet", "opus", "haiku"}
    assert all(model.provider == "claude" for model in models)
    assert all(model.supports_tools for model in models)
    assert CLAUDE_AGENT_SDK_VERSION == "0.2.164"
    assert CLAUDE_CODE_CLI_VERSION == "2.1.292"
    assert normalize_claude_model(" Opus ") == "opus"
    assert resolve_claude_sdk_model("sonnet") == "sonnet"
    assert resolve_claude_sdk_model("claude-opus-4-6-20250514") == (
        "claude-opus-4-6-20250514"
    )
    assert normalize_claude_effort(" HIGH ") == "high"
    for bad in ("", "openrouter/anthropic/claude", "some-random-model"):
        with pytest.raises(ValueError):
            normalize_claude_model(bad)
    for bad in ("", "ultra"):
        with pytest.raises(ValueError):
            normalize_claude_effort(bad)


@pytest.mark.django_db
def test_engine_state_and_transcript_are_ordered_encrypted_and_idempotent(
    harness_workspace,
) -> None:
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        model="sonnet",
    )
    session.harness_id = "claude"
    session.save(update_fields=["harness_id"])
    HarnessEngineRepository.set_session_engine_state(
        session.id,
        external_session_id="sdk-session-123",
        engine_state={"cwd": "/workspace", "resume_token": "not-secret"},
    )
    entry = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello"}]},
        "uuid": "entry-1",
    }
    first = HarnessEngineRepository.append_transcript_entry(
        session.id,
        external_session_id="sdk-session-123",
        subpath="main",
        entry=entry,
    )
    duplicate = HarnessEngineRepository.append_transcript_entry(
        session.id,
        external_session_id="sdk-session-123",
        subpath="main",
        entry=entry,
    )
    second = HarnessEngineRepository.append_transcript_entry(
        session.id,
        external_session_id="sdk-session-123",
        subpath="main",
        entry={"type": "result", "uuid": "entry-2"},
    )

    session.refresh_from_db()
    assert session.external_session_id == "sdk-session-123"
    assert session.engine_state == {
        "cwd": "/workspace",
        "resume_token": "not-secret",
    }
    assert first.id == duplicate.id
    assert first.position == 0
    assert second.position == 1
    assert "hello" not in first.payload_encrypted
    assert decrypt_value(first.payload_encrypted).find('"hello"') >= 0
    assert HarnessEngineRepository.list_transcript_entries(
        session.id,
        external_session_id="sdk-session-123",
        subpath="main",
    ) == [entry, {"type": "result", "uuid": "entry-2"}]
    assert (
        HarnessEngineRepository.clear_transcript(
            session.id, external_session_id="sdk-session-123", subpath="main"
        )
        == 2
    )
    assert (
        HarnessEngineRepository.list_transcript_entries(
            session.id,
            external_session_id="sdk-session-123",
            subpath="main",
        )
        == []
    )


@pytest.mark.django_db
def test_session_and_message_engine_fields_are_additive(harness_workspace) -> None:
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="Native session",
    )
    user_message = HarnessMessageRepository.create(
        session_id=session.id,
        role=HarnessMessageRole.USER,
        content="hello",
    )
    assistant = HarnessMessage.objects.create(
        session=session,
        role=HarnessMessageRole.ASSISTANT,
        harness_id="claude",
        engine_meta={"stop_reason": "end_turn"},
        position=100,
    )
    assert session.harness_id == "native"
    assert user_message.harness_id == "native"
    assert user_message.engine_meta == {}
    assert assistant.harness_id == "claude"
    assert assistant.engine_meta == {"stop_reason": "end_turn"}


def test_repo_validates_external_session_id_and_subpath(harness_workspace) -> None:
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
    )
    with pytest.raises(ValueError, match="external_session_id"):
        HarnessEngineRepository.append_transcript_entry(
            session.id,
            external_session_id="",
            subpath="",
            entry={"type": "message"},
        )
    with pytest.raises(ValueError, match="subpath"):
        HarnessEngineRepository.append_transcript_entry(
            session.id,
            external_session_id="external",
            subpath="x" * 256,
            entry={"type": "message"},
        )
