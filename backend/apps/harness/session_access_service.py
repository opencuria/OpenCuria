"""Session access rules shared by adapters and run orchestration.

Workspace membership and visibility belong to the runners context. Once a
caller can see a workspace, these rules decide whether it may address a child
conversation or use a session's personal external-engine authorization.
Only identity values cross this boundary; no persistence or live run is needed.
"""

from __future__ import annotations

import uuid

from common.exceptions import AuthenticationError


class SessionAccessService:
    """Authorize session operations independently of transport and execution."""

    @staticmethod
    def require_external_owner(
        *,
        engine_id: str,
        owner_id: object | None,
        user_id: object | None,
        message: str = (
            "Only the session owner may use its external engine connection"
        ),
    ) -> None:
        """Require a known, matching owner when using a personal engine account."""
        if engine_id == "native":
            return
        if user_id is None or owner_id is None or str(user_id) != str(owner_id):
            raise AuthenticationError(message)

    @staticmethod
    def require_user_promptable(parent_id: uuid.UUID | None) -> None:
        """Reject user prompts and forks of internally managed subagent chats.

        Internal child execution deliberately does not call this rule.
        """
        if parent_id is not None:
            raise ValueError("Cannot send messages to a subagent session")
