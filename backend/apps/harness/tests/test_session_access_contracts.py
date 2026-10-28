"""Characterize access rejection before orchestration can mutate a conversation."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.harness.harness_service import HarnessService
from common.exceptions import AuthenticationError


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize(("owner_id", "user_id"), [(1, 2), (None, 1), (1, None)])
@pytest.mark.parametrize("action", ["start", "edit"])
async def test_external_session_rejects_nonowner_before_writing(
    engine: str, owner_id: int | None, user_id: int | None, action: str
) -> None:
    """Direct service callers must obey the same rule as REST and MCP callers."""
    session = SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        parent_id=None,
        harness_id=engine,
        created_by_id=owner_id,
    )
    sessions = Mock(get_by_id=Mock(return_value=session))
    messages = Mock()
    service = HarnessService(
        sessions=sessions, messages=messages, workspace_admission=Mock()
    )
    expected = (
        "Only the session owner may use its external engine connection"
        if action == "start"
        else "Only the session owner may edit an external conversation"
    )

    with pytest.raises(AuthenticationError) as caught:
        if action == "start":
            await service.start_run(session, "hello", user_id=user_id)
        else:
            await service.edit_user_message(
                session.id, message_id=uuid.uuid4(), prompt="edit", user_id=user_id
            )

    assert caught.value.message == expected
    assert caught.value.code == "authentication_error"
    assert messages.mock_calls == []
    assert service._tasks == {}
    assert service._runs == {}
