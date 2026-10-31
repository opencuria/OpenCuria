"""Process identity and session-scoping rules, independent of live runner I/O."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from ..process_repository import WorkspaceProcessRepository

if TYPE_CHECKING:
    from ..models import WorkspaceProcess


class ProcessLookupService:
    """Resolve workspace processes with shared UUID/name and session precedence."""

    def __init__(
        self, *, repository: type[WorkspaceProcessRepository] | None = None
    ) -> None:
        """Inject process persistence without constructing runner execution."""
        self.repository = repository or WorkspaceProcessRepository

    def resolve(
        self,
        workspace_id: uuid.UUID,
        id_or_name: str | uuid.UUID,
        *,
        kind: str | None = None,
        session_id: uuid.UUID | None = None,
    ) -> WorkspaceProcess | None:
        """Resolve a process by UUID first, then by exact name.

        A UUID match wins when admitted by the supplied kind and session scope.
        Without a session, user controls may address temp rows by UUID. Temp
        names require a session and take precedence over persistent names.

        Never raises on unparsable UUIDs — falls back to name lookup.
        """
        candidate: WorkspaceProcess | None = None
        try:
            parsed = (
                id_or_name
                if isinstance(id_or_name, uuid.UUID)
                else uuid.UUID(str(id_or_name))
            )
        except (ValueError, TypeError, AttributeError):
            parsed = None
        if parsed is not None:
            candidate = self.repository.get_for_workspace(parsed, workspace_id)
            if candidate is not None:
                candidate_kind = str(getattr(candidate, "kind", "") or "")
                if candidate_kind == "temp":
                    if session_id is not None and candidate.session_id != session_id:
                        candidate = None
                    elif kind == "persistent":
                        candidate = None
                elif kind == "temp":
                    candidate = None
                if candidate is not None:
                    return candidate
        if kind == "temp":
            if session_id is None:
                return None
            return self.repository.get_temp_by_name(
                workspace_id, str(id_or_name), session_id
            )
        if kind == "persistent":
            return self.repository.get_by_name(
                workspace_id, str(id_or_name), kind="persistent"
            )
        if session_id is not None:
            temp = self.repository.get_temp_by_name(
                workspace_id, str(id_or_name), session_id
            )
            if temp is not None:
                return temp
        return self.repository.get_by_name(workspace_id, str(id_or_name))
