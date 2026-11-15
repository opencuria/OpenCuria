"""Session access policy is independent of transport, persistence and live runs."""

from __future__ import annotations

import uuid

import pytest

from apps.harness.session_access_service import SessionAccessService
from common.exceptions import AuthenticationError


@pytest.mark.parametrize("engine", ["native", "claude", "codex", "future-engine"])
@pytest.mark.parametrize(
    ("owner_id", "user_id", "matches"),
    [
        (1, 1, True),
        (1, "1", True),
        (1, 2, False),
        (None, 1, False),
        (1, None, False),
        (None, None, False),
    ],
)
def test_external_engine_requires_a_known_matching_owner(
    engine: str, owner_id: object | None, user_id: object | None, matches: bool
) -> None:
    """Native sessions remain usable without a personal account owner."""
    if engine == "native" or matches:
        SessionAccessService.require_external_owner(
            engine_id=engine, owner_id=owner_id, user_id=user_id
        )
    else:
        with pytest.raises(AuthenticationError) as caught:
            SessionAccessService.require_external_owner(
                engine_id=engine, owner_id=owner_id, user_id=user_id
            )
        assert caught.value.code == "authentication_error"


def test_user_promptability_depends_only_on_parentage() -> None:
    """Root conversations are user-addressable; subagents are internally driven."""
    SessionAccessService.require_user_promptable(None)
    with pytest.raises(
        ValueError, match="^Cannot send messages to a subagent session$"
    ):
        SessionAccessService.require_user_promptable(uuid.uuid4())
