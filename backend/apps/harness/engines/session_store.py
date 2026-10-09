"""Claude Agent SDK transcript mirror backed by encrypted harness rows."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from asgiref.sync import sync_to_async

from .repositories import HarnessEngineRepository

_PROJECT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,255}$")
_SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class HarnessClaudeSessionStore:
    """Async SDK ``SessionStore`` adapter isolated to one harness session.

    The adapter accepts the SDK's normalized project key but verifies it
    against the fixed harness workspace key. Session IDs and subpaths are
    never used as filesystem paths by this store.
    """

    def __init__(
        self,
        *,
        session_id: uuid.UUID,
        external_session_id: str | None = None,
        project_key: str = "",
        repository: type[HarnessEngineRepository] | None = None,
    ) -> None:
        self.session_id = uuid.UUID(str(session_id))
        self.external_session_id = (external_session_id or "").strip()
        self.project_key = (project_key or "").strip()
        self.repository = repository or HarnessEngineRepository

    def _validate_key(self, key: Mapping[str, Any]) -> tuple[str, str, str]:
        """Validate and normalize an SDK transcript store key."""
        project_key = key.get("project_key")
        external_id = key.get("session_id")
        subpath = key.get("subpath", "")
        if not isinstance(project_key, str) or not _PROJECT_KEY_PATTERN.fullmatch(
            project_key
        ):
            raise ValueError("Claude session store project_key is invalid")
        if self.project_key and project_key != self.project_key:
            raise ValueError(
                "Claude session store project_key does not match workspace"
            )
        if not isinstance(external_id, str) or not _SESSION_ID_PATTERN.fullmatch(
            external_id
        ):
            raise ValueError("Claude session store session_id must be a UUID")
        if self.external_session_id and external_id != self.external_session_id:
            raise ValueError("Claude session store session_id does not match harness")
        if not isinstance(subpath, str) or subpath and not self._safe_subpath(subpath):
            raise ValueError("Claude session store subpath is invalid")
        return project_key, external_id.lower(), subpath

    @staticmethod
    def _safe_subpath(value: str) -> bool:
        """Accept the SDK's bounded, relative subagent transcript names."""
        if not value or len(value) > 255 or "\x00" in value:
            return False
        if value.startswith(("/", "\\")) or ":" in value:
            return False
        parts = re.split(r"[\\/]", value)
        return bool(
            parts[0] == "subagents"
            and all(part not in {"", ".", ".."} for part in parts)
            and all(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", part) for part in parts)
        )

    async def append(
        self, key: Mapping[str, Any], entries: list[dict[str, Any]]
    ) -> None:
        """Encrypt and append one SDK batch without blocking the event loop."""
        _, external_id, subpath = self._validate_key(key)
        if not isinstance(entries, list):
            raise ValueError("Claude session store entries must be a list")
        self.external_session_id = external_id
        await sync_to_async(
            self.repository.append_transcript_entries, thread_sensitive=True
        )(
            self.session_id,
            external_session_id=external_id,
            subpath=subpath,
            entries=entries,
        )

    async def load(self, key: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        """Load ordered, decrypted entries or None for a never-written key."""
        _, external_id, subpath = self._validate_key(key)
        entries = await sync_to_async(
            self.repository.list_transcript_entries, thread_sensitive=True
        )(
            self.session_id,
            external_session_id=external_id,
            subpath=subpath,
        )
        return entries or None

    async def list_subkeys(self, key: Mapping[str, Any]) -> list[str]:
        """List the session's stored agent transcript paths."""
        _, external_id, subpath = self._validate_key(key)
        if subpath:
            raise ValueError("Claude list_subkeys requires a main-session key")
        return await sync_to_async(
            self.repository.list_transcript_subpaths, thread_sensitive=True
        )(self.session_id, external_session_id=external_id)

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        """Return this harness's one external session for SDK continue APIs."""
        if not isinstance(project_key, str) or not _PROJECT_KEY_PATTERN.fullmatch(
            project_key
        ):
            raise ValueError("Claude session store project_key is invalid")
        if self.project_key and project_key != self.project_key:
            return []
        record = await sync_to_async(
            self.repository.get_session_engine_record, thread_sensitive=True
        )(self.session_id)
        external_id = str(record.get("external_session_id") or "")
        if not external_id:
            return []
        if not _SESSION_ID_PATTERN.fullmatch(external_id):
            return []
        mtime = int(record["updated_at"].timestamp() * 1000)
        return [{"session_id": external_id, "mtime": mtime}]

    async def delete(self, key: Mapping[str, Any]) -> None:
        """Delete just this bound session transcript, cascading main entries."""
        _, external_id, subpath = self._validate_key(key)
        await sync_to_async(self.repository.clear_transcript, thread_sensitive=True)(
            self.session_id,
            external_session_id=external_id,
            subpath=subpath or None,
        )

    async def delete_session(self) -> int:
        """Delete all transcript subpaths belonging to this harness session."""
        return await sync_to_async(
            self.repository.clear_transcript, thread_sensitive=True
        )(self.session_id)
