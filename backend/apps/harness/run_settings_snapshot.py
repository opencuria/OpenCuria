"""Immutable execution settings captured when a durable run is submitted."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from common.exceptions import ConflictError


@dataclass(frozen=True)
class RunSettingsSnapshot:
    """Fence delayed commands and keep skill hydration from changing their engine."""

    mode: str
    agent_name: str
    model: str
    reasoning_effort: str
    harness_id: str
    skill_ids: tuple[str, ...]

    @classmethod
    def capture(cls, session: Any) -> RunSettingsSnapshot:
        """Copy configuration identities only, never credentials or engine secrets."""
        return cls(
            mode=getattr(session, "mode", None) or "",
            agent_name=getattr(session, "agent_name", None) or "",
            model=getattr(session, "model", None) or "",
            reasoning_effort=getattr(session, "reasoning_effort", None) or "",
            harness_id=getattr(session, "harness_id", None) or "native",
            skill_ids=tuple(getattr(session, "skill_ids", None) or []),
        )

    def to_payload(self) -> dict[str, Any]:
        """Serialize a stable version fence alongside encrypted command arguments."""
        payload = asdict(self)
        payload["skill_ids"] = list(self.skill_ids)
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RunSettingsSnapshot:
        """Validate the stored DTO before comparing it with current configuration."""
        fields = {"mode", "agent_name", "model", "reasoning_effort", "harness_id"}
        if (
            not isinstance(payload, dict)
            or set(payload) != fields | {"skill_ids"}
            or any(not isinstance(payload[field], str) for field in fields)
            or not isinstance(payload["skill_ids"], list)
            or any(not isinstance(value, str) for value in payload["skill_ids"])
        ):
            raise ValueError("Invalid run settings snapshot")
        return cls(
            **{field: payload[field] for field in fields},
            skill_ids=tuple(payload["skill_ids"]),
        )

    def require_current(self, session: Any) -> None:
        """A queued prompt must never silently run with subsequently changed options."""
        if self != self.capture(session):
            raise ConflictError(
                "Chat settings changed while this run was queued; send it again "
                "with the current settings",
                code="run_configuration_changed",
            )

    def retain_execution_settings(self, session: Any) -> None:
        """Keep skill-persistence rereads from replacing the captured run options.

        Skills retain their separately validated selection. Provider defaults are
        resolved later against this captured model/effort rather than a reread.
        """
        session.mode = self.mode
        session.agent_name = self.agent_name
        session.model = self.model
        session.reasoning_effort = self.reasoning_effort
        session.harness_id = self.harness_id
