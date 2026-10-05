"""Durable managed stream associations, excluding argv and environment secrets."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class StreamIntentStore:
    """Single-writer SQLite intents and owner tombstones survive runner crashes."""

    def __init__(self, state_dir: str | Path) -> None:
        Path(state_dir).mkdir(parents=True, mode=0o700, exist_ok=True)
        Path(state_dir).chmod(0o700)
        self.path = Path(state_dir) / "stream_intents.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.lock = threading.Lock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS intents (
                    connection_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL, runtime_type TEXT NOT NULL,
                    lease_id TEXT NOT NULL, epoch TEXT NOT NULL,
                    control_path TEXT NOT NULL, state TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS intents_owner ON intents(lease_id);
                CREATE INDEX IF NOT EXISTS intents_workspace
                    ON intents(workspace_id, instance_id, state);
                CREATE TABLE IF NOT EXISTS ended_owners (
                    lease_id TEXT PRIMARY KEY);
            """)

        with self._db() as db:
            columns = {r["name"] for r in db.execute("PRAGMA table_info(intents)")}
            if "guest_token" not in columns:
                db.execute("ALTER TABLE intents ADD COLUMN guest_token TEXT")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    async def reserve(self, row: dict[str, str]) -> None:
        """Commit an association before any guest-side launch effect."""

        def run() -> None:
            with self.lock, self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT 1 FROM ended_owners WHERE lease_id=?", (row["lease_id"],)
                ).fetchone():
                    raise ValueError("Stream owner has ended")
                db.execute(
                    "INSERT INTO intents (connection_id,workspace_id,instance_id,"
                    "runtime_type,lease_id,epoch,control_path,state,guest_token) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    tuple(
                        row[k]
                        for k in (
                            "connection_id",
                            "workspace_id",
                            "instance_id",
                            "runtime_type",
                            "lease_id",
                            "epoch",
                            "control_path",
                            "state",
                        )
                    )
                    + (
                        (
                            json.dumps(row["guest_token"])
                            if row.get("guest_token")
                            else None
                        ),
                    ),
                )

        await asyncio.to_thread(run)

    async def get(self, connection_id: str) -> dict | None:
        """Read even a closed tombstone (required for workspace authorization)."""

        def run() -> dict | None:
            with self.lock, self._db() as db:
                row = db.execute(
                    "SELECT * FROM intents WHERE connection_id=?", (connection_id,)
                ).fetchone()
                return dict(row) if row else None

        return await asyncio.to_thread(run)

    async def owner_rows(self, lease_id: str) -> list[dict]:
        """Atomically fence new reservations and list every owner's intent."""

        def run() -> list[dict]:
            with self.lock, self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT OR IGNORE INTO ended_owners VALUES (?)", (lease_id,))
                return [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM intents WHERE lease_id=?", (lease_id,)
                    )
                ]

        return await asyncio.to_thread(run)

    async def set_state(self, connection_id: str, state: str) -> None:
        """Persist closing/closed without deleting the durable authorization."""

        def run() -> None:
            with self.lock, self._db() as db:
                db.execute(
                    "UPDATE intents SET state=? WHERE connection_id=? "
                    "AND state != 'closed'",
                    (state, connection_id),
                )

        await asyncio.to_thread(run)

    async def list_unfinished(
        self, workspace_id: str | None = None, instance_id: str | None = None
    ) -> list[dict]:
        """List recovery intents, retaining terminal rows indefinitely."""

        def run() -> list[dict]:
            clauses = ["state != 'closed'"]
            values = []
            for key, value in (
                ("workspace_id", workspace_id),
                ("instance_id", instance_id),
            ):
                if value is not None:
                    clauses.append(key + "=?")
                    values.append(str(value))
            with self.lock, self._db() as db:
                return [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM intents WHERE " + " AND ".join(clauses), values
                    )
                ]

        return await asyncio.to_thread(run)

    async def finish_workspace(self, workspace_id: str, instance_id: str) -> None:
        """End proven-dead bindings and owners atomically; never age-prune.

        Only StreamManager's trusted lifecycle confirmation may call this.
        """

        def run() -> None:
            with self.lock, self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO ended_owners SELECT lease_id FROM intents "
                    "WHERE workspace_id=? AND instance_id=?",
                    (str(workspace_id), instance_id),
                )
                db.execute(
                    "UPDATE intents SET state='closed' "
                    "WHERE workspace_id=? AND instance_id=?",
                    (str(workspace_id), instance_id),
                )

        await asyncio.to_thread(run)
