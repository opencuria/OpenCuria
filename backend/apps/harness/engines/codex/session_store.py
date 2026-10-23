"""One encrypted, owner-fenced, replace-in-place Codex checkpoint per session."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from typing import Any

from asgiref.sync import sync_to_async

from ...access.base import WorkspaceAccessor
from ..repositories import HarnessEngineRepository
from .errors import CodexRecoveryError
from .state_io import STATE_PROGRAM

MAX_SNAPSHOT = 34 * 1024 * 1024
SUBPATH = "codex/checkpoint-v1"


def home_for(session_id: str) -> str:
    """Return the session-isolated managed Codex home."""
    return f"/workspace/.opencuria/harness/codex/{uuid.UUID(str(session_id))}/config"


def validate_snapshot(record: dict[str, Any], home: str) -> None:
    """Reject foreign homes, unsafe artifacts, corruption and oversized state."""
    if not isinstance(record, dict) or set(record) != {"version", "home", "files"}:
        raise ValueError("Invalid Codex checkpoint record")
    if record["version"] != 1 or record["home"] != home:
        raise ValueError("Codex checkpoint belongs to a different managed home")
    files = record["files"]
    if not isinstance(files, list) or not 0 < len(files) <= 256:
        raise ValueError("Invalid Codex checkpoint file count")
    seen = set()
    total = 0

    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "data", "sha256"}:
            raise ValueError("Invalid Codex checkpoint file")
        name = item["path"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(
                r"(?:(?:state|thread_history|queue|goals|memories)_\d+\.sqlite|"
                r"memories/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.md|"
                r"(?:sessions|archived_sessions)/\d{4}/\d{2}/\d{2}/rollout-[A-Za-z0-9T_.:-]+\.jsonl)",
                name,
            )
            or name in seen
        ):
            raise ValueError("Unsafe Codex checkpoint path")
        seen.add(name)
        if not isinstance(item["data"], str) or len(item["data"]) > MAX_SNAPSHOT:
            raise ValueError("Oversized Codex checkpoint file")
        data = base64.b64decode(item["data"], validate=True)
        total += len(data)
        if (
            total > 24 * 1024 * 1024
            or hashlib.sha256(data).hexdigest() != item["sha256"]
        ):
            raise ValueError("Corrupt or oversized Codex checkpoint")
    if not any(name.startswith("sessions/") for name in seen):
        raise ValueError("Codex checkpoint has no rollout")


class CodexSessionStore:
    """Production adapter; execution supplies its current durable ownership fence.

    ``run_id`` and ``owner_token`` are mandatory for writes. Loads remain scoped
    to the explicitly authorized session and external thread. Atomic replacement
    bounds encrypted database growth and cannot erase the last valid checkpoint
    when persistence fails. A failed/dirty generation is never auto-restored.
    """

    def __init__(
        self,
        *,
        session_id: uuid.UUID | str,
        external_session_id: str = "",
        run_id: uuid.UUID | str | None = None,
        owner_token: uuid.UUID | str | None = None,
        repository: type[HarnessEngineRepository] | None = None,
    ) -> None:
        """Bind a store to its session and current durable run ownership."""

        self.session_id = uuid.UUID(str(session_id))
        self.external_session_id = str(external_session_id or "")
        if self.external_session_id:
            uuid.UUID(self.external_session_id)
        self.run_id = run_id
        self.owner_token = owner_token
        self.repository = repository or HarnessEngineRepository

    async def load(self, external_id: str, home: str) -> dict[str, Any]:
        """Load exactly one validated generation for the authorized thread."""
        uuid.UUID(external_id)
        if self.external_session_id and self.external_session_id != external_id:
            raise ValueError("Codex checkpoint thread mismatch")
        rows = await sync_to_async(self.repository.list_transcript_entries)(
            self.session_id, external_session_id=external_id, subpath=SUBPATH
        )
        if len(rows) != 1:
            raise CodexRecoveryError(
                "Codex checkpoint missing; fork or start a new conversation"
            )
        entry = rows[0]
        if set(entry) != {"uuid", "generation", "snapshot"}:
            raise ValueError("Invalid Codex stored checkpoint")
        uuid.UUID(entry["generation"])
        if entry["uuid"] != entry["generation"]:
            raise ValueError("Invalid Codex checkpoint generation")
        validate_snapshot(entry["snapshot"], home)
        return entry

    async def save(
        self, external_id: str, generation: str, snapshot: dict[str, Any]
    ) -> None:
        """Validate then atomically replace a checkpoint through the repository."""
        uuid.UUID(external_id)
        uuid.UUID(generation)
        validate_snapshot(snapshot, home_for(str(self.session_id)))
        if self.external_session_id and self.external_session_id != external_id:
            raise ValueError("Codex checkpoint thread mismatch")
        if not self.run_id or not self.owner_token:
            raise RuntimeError("Codex checkpoint requires durable run ownership")

        await sync_to_async(
            self.repository.replace_owned_checkpoint, thread_sensitive=True
        )(
            self.session_id,
            run_id=self.run_id,
            owner_token=self.owner_token,
            external_session_id=external_id,
            subpath=SUBPATH,
            generation=generation,
            snapshot=snapshot,
        )
        self.external_session_id = external_id


async def state_action(
    accessor: WorkspaceAccessor, action: str, home: str, *args: str
) -> str:
    """Execute one bounded managed-state operation inside the workspace."""
    result = await accessor.exec_wait(
        ["python3", "-I", "-c", STATE_PROGRAM, action, home, *args], timeout=45
    )
    if result.exit_code:
        raise RuntimeError(f"Codex managed state {action} failed")
    return result.stdout.strip()


async def restore_snapshot(
    accessor: WorkspaceAccessor, home: str, snapshot: dict[str, Any]
) -> None:
    """Upload bounded pieces, respecting the shared 10 MiB file-write ceiling."""
    validate_snapshot(snapshot, home)
    payload = json.dumps(snapshot, separators=(",", ":")).encode()
    if len(payload) > MAX_SNAPSHOT:
        raise ValueError("Oversized Codex restore")
    await state_action(accessor, "prepare", home)
    parent = home.rsplit("/", 1)[0]
    chunk_size = 4 * 1024 * 1024
    count = 0
    for index, offset in enumerate(range(0, len(payload), chunk_size)):
        await accessor.write_file(
            f"{parent}/restore-{index}.part",
            payload[offset : offset + chunk_size],
            mode=0o600,
        )
        count += 1
    await state_action(
        accessor, "assemble", home, str(count), hashlib.sha256(payload).hexdigest()
    )
    await state_action(accessor, "restore", home)


async def capture(accessor: WorkspaceAccessor, home: str) -> dict[str, Any]:
    """Read checked no-follow snapshot chunks and validate their total digest."""
    checksum = await state_action(accessor, "snapshot", home)
    digest, count = checksum.split(":")
    if not count.isdigit() or not 1 <= int(count) <= 9:
        raise ValueError("Invalid Codex snapshot chunks")
    payload = bytearray()
    try:
        for index in range(int(count)):
            stored = await accessor.read_file_no_follow(
                home.rsplit("/", 1)[0] + f"/snapshot-{index}.part",
                max_size=4 * 1024 * 1024,
            )
            if stored.truncated or len(payload) + len(stored.content) > MAX_SNAPSHOT:
                raise ValueError("Codex checkpoint read was incomplete")
            payload.extend(stored.content)
    finally:
        await state_action(accessor, "purge", home)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Codex checkpoint checksum mismatch")
    record = json.loads(payload)
    validate_snapshot(record, home)
    return record


async def materialize_skills(
    accessor: WorkspaceAccessor, home: str, skills: list[str]
) -> None:
    """Publish selected skill bodies through native Codex discovery, not prompts."""
    if not isinstance(skills, list) or len(skills) > 128:
        raise ValueError("Codex skills must be a list of at most 128 bodies")
    if any(not isinstance(x, str) or len(x.encode()) > 64 * 1024 for x in skills):
        raise ValueError("Codex skill body exceeds the managed size limit")
    payload = json.dumps(skills, ensure_ascii=False).encode()
    await state_action(accessor, "prepare", home)
    parent = home.rsplit("/", 1)[0]
    chunks = [
        payload[i : i + 4 * 1024 * 1024]
        for i in range(0, len(payload), 4 * 1024 * 1024)
    ]
    for i, chunk in enumerate(chunks):
        await accessor.write_file(f"{parent}/restore-{i}.part", chunk, mode=0o600)
    await state_action(
        accessor,
        "assemble",
        home,
        str(len(chunks)),
        hashlib.sha256(payload).hexdigest(),
    )
    await state_action(accessor, "skills", home)
