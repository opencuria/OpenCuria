"""Provider history filtering after session compaction."""

from __future__ import annotations

import os
import uuid

import pytest

from apps.harness.compaction import CHECKPOINT_PREFIX
from apps.harness.harness_service import HarnessService
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)


@pytest.mark.django_db(transaction=True)
async def test_build_history_filters_compacted_messages(harness_workspace) -> None:
    """_build_history keeps checkpoint + tail; drops pre-compaction user turns."""
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    service = HarnessService()
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="compaction",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    user1 = HarnessMessageRepository.create(
        session_id=session.id,
        role="user",
        content="first user turn",
    )
    asst1 = HarnessMessageRepository.create(
        session_id=session.id,
        role="assistant",
        content="first answer",
    )
    user2 = HarnessMessageRepository.create(
        session_id=session.id,
        role="user",
        content="second user turn",
    )
    asst2 = HarnessMessageRepository.create(
        session_id=session.id,
        role="assistant",
        content="## Objective\n- compacted summary",
    )
    part = HarnessPartRepository.create(
        message_id=asst2.id,
        type="compaction",
        state="completed",
        title="Session compacted",
        meta={"tail_start_id": str(user2.id), "auto": True, "overflow": True},
    )
    HarnessPartRepository.mark_state(
        part, "completed", output="## Objective\n- compacted summary"
    )
    history = await service._build_history(session)

    user_contents = [
        message.content
        for message in history
        if message.role == "user" and isinstance(message.content, str)
    ]
    assert "first user turn" not in user_contents
    assert any(content.startswith(CHECKPOINT_PREFIX) for content in user_contents)
    assert any(
        "## Objective" in content and "compacted summary" in content
        for content in user_contents
    )
    assert "second user turn" in user_contents
    assert not any(
        message.role == "assistant"
        and message.content == "## Objective\n- compacted summary"
        for message in history
    )
    # Full DB history is untouched.
    stored = HarnessMessageRepository.list_for_session(session.id)
    assert len(stored) == 4
    assert stored[0].id == user1.id


@pytest.mark.django_db(transaction=True)
async def test_build_history_compaction_fallback_without_tail_start_id(
    harness_workspace,
) -> None:
    """Missing tail_start_id keeps compaction assistant onward plus checkpoint."""
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    service = HarnessService()
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="compaction-fallback",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    HarnessMessageRepository.create(
        session_id=session.id,
        role="user",
        content="old user",
    )
    HarnessMessageRepository.create(
        session_id=session.id,
        role="assistant",
        content="old answer",
    )
    user2 = HarnessMessageRepository.create(
        session_id=session.id,
        role="user",
        content="recent user",
    )
    asst2 = HarnessMessageRepository.create(
        session_id=session.id,
        role="assistant",
        content="summary text",
    )
    part = HarnessPartRepository.create(
        message_id=asst2.id,
        type="compaction",
        state="completed",
        title="Session compacted",
        meta={"tail_start_id": "", "auto": True},
    )
    HarnessPartRepository.mark_state(
        part, "completed", output="## Objective\n- fallback summary"
    )
    history = await service._build_history(session)

    user_contents = [
        message.content
        for message in history
        if message.role == "user" and isinstance(message.content, str)
    ]
    assert "old user" not in user_contents
    assert "recent user" not in user_contents
    assert any(content.startswith(CHECKPOINT_PREFIX) for content in user_contents)
    assistant_ids = [
        message.message_id
        for message in history
        if message.role == "assistant" and message.message_id
    ]
    assert str(asst2.id) in assistant_ids


@pytest.mark.django_db(transaction=True)
async def test_build_history_ignores_invalid_tail_start_id(
    harness_workspace,
) -> None:
    """Invalid tail_start_id falls back to the compaction assistant message."""
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    service = HarnessService()
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="compaction-invalid-tail",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    HarnessMessageRepository.create(
        session_id=session.id,
        role="user",
        content="old user",
    )
    user2 = HarnessMessageRepository.create(
        session_id=session.id,
        role="user",
        content="tail user",
    )
    asst2 = HarnessMessageRepository.create(
        session_id=session.id,
        role="assistant",
        content="",
    )
    part = HarnessPartRepository.create(
        message_id=asst2.id,
        type="compaction",
        state="completed",
        title="Session compacted",
        meta={"tail_start_id": str(uuid.uuid4()), "auto": True},
    )
    HarnessPartRepository.mark_state(part, "completed", output="## Objective\n- summary")
    history = await service._build_history(session)

    user_contents = [
        message.content
        for message in history
        if message.role == "user" and isinstance(message.content, str)
    ]
    assert "old user" not in user_contents
    assert "tail user" not in user_contents
    assert any(content.startswith(CHECKPOINT_PREFIX) for content in user_contents)
