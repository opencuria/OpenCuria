"""Durable desktop ownership intents, independent of guest process caches."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any


class DesktopLeaseStore:
    """Single-writer SQLite ownership ledger. Only metadata is persisted."""

    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir).expanduser() / "desktop_leases.sqlite3"
        self._lock = threading.RLock()

    def _run(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Precreate privately: SQLite inherits this file's mode for WAL/SHM.
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("""CREATE TABLE IF NOT EXISTS desktop_leases (
                    lease_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL, kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL, epoch TEXT NOT NULL,
                    revision INTEGER NOT NULL, state TEXT NOT NULL,
                    activated INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS desktop_recordings (
                    recording_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL, instance_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, epoch TEXT NOT NULL,
                    control_path TEXT NOT NULL, path TEXT NOT NULL,
                    state TEXT NOT NULL)""")
                # Upgrade existing recording ledgers without inventing old tokens.
                if "guest_token" not in {
                    r[1] for r in db.execute("PRAGMA table_info(desktop_recordings)")
                }:
                    db.execute(
                        "ALTER TABLE desktop_recordings ADD COLUMN guest_token TEXT"
                    )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS lease_workspace_state "
                    "ON desktop_leases(workspace_id,state)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS lease_state_expiry "
                    "ON desktop_leases(state,expires_at)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS recording_owner_state "
                    "ON desktop_recordings(lease_id,state)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS recording_workspace_state "
                    "ON desktop_recordings(workspace_id,state)"
                )
                db.execute("BEGIN IMMEDIATE")
                return operation(db)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["activated"] = bool(result["activated"])
        return result

    async def get(self, lease_id: str) -> dict | None:
        """Read one intent, including durable ended tombstones."""
        return await asyncio.to_thread(
            self._run,
            lambda db: self._row(
                db.execute(
                    "SELECT * FROM desktop_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
            ),
        )

    async def list_workspace(self, workspace_id: str) -> list[dict]:
        """Read all workspace ownership metadata."""
        return await asyncio.to_thread(
            self._run,
            lambda db: [
                self._row(r)
                for r in db.execute(
                    "SELECT * FROM desktop_leases WHERE workspace_id=?",
                    (str(workspace_id),),
                ).fetchall()
            ],
        )

    async def list_all(self) -> list[dict]:
        """Enumerate metadata for startup epoch reconciliation."""
        return await asyncio.to_thread(
            self._run,
            lambda db: [
                self._row(r)
                for r in db.execute("SELECT * FROM desktop_leases").fetchall()
            ],
        )

    async def list_expired(self, workspace_id: str | None = None) -> list[dict]:
        """Return overdue/closing owners, optionally scoped to one workspace."""

        def operation(db: sqlite3.Connection) -> list[dict]:
            query = (
                "SELECT * FROM desktop_leases WHERE (state='closing' OR "
                "(state IN ('reserved','held') AND expires_at<=?))"
            )
            args = [time.time()]
            if workspace_id is not None:
                query += " AND workspace_id=?"
                args.append(str(workspace_id))
            return [self._row(r) for r in db.execute(query, args)]

        return await asyncio.to_thread(self._run, operation)

    async def unfinished_workspace_ids(self) -> list[str]:
        """Enumerate maintenance targets without reading terminal history."""
        return await asyncio.to_thread(
            self._run,
            lambda db: [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT workspace_id FROM desktop_leases "
                    "WHERE state IN ('reserved','held','closing') UNION "
                    "SELECT DISTINCT workspace_id FROM desktop_recordings "
                    "WHERE state IN ('starting','closing')"
                )
            ],
        )

    async def reserve(
        self,
        workspace_id: str,
        instance_id: str,
        lease_id: str,
        kind: str,
        owner_id: str,
        epoch: str,
        revision: int,
        ttl: float,
    ) -> dict:
        """Reserve without activation; enforce identity/revision tombstones."""
        if kind not in {"viewer", "computeruse", "mcp"}:
            raise ValueError("Invalid desktop lease kind")
        if not lease_id or not owner_id or not epoch:
            raise ValueError("Desktop lease identity is required")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("revision must be a positive integer")
        if ttl <= 0:
            raise ValueError("ttl must be positive")

        def operation(db: sqlite3.Connection) -> dict:
            old = self._row(
                db.execute(
                    "SELECT * FROM desktop_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
            )
            if old:
                identity = (str(workspace_id), kind, owner_id)
                if identity != tuple(
                    old[k] for k in ("workspace_id", "kind", "owner_id")
                ):
                    raise ValueError("Desktop lease identity mismatch")
                if old["instance_id"] != str(instance_id) and not (
                    kind == "viewer"
                    and old["state"] in {"released", "expired"}
                    and not old["activated"]
                    and revision > old["revision"]
                ):
                    raise ValueError(
                        "Desktop lease instance transition requires ended viewer"
                    )
                if revision < old["revision"]:
                    raise ValueError("Stale desktop lease revision")
                if revision == old["revision"]:
                    if old["epoch"] != epoch or old["state"] not in {
                        "reserved",
                        "held",
                    }:
                        raise ValueError("Desktop lease ended or stale epoch")
                    if old["expires_at"] <= time.time():
                        raise ValueError("Desktop lease expired")
                    return old
                if kind != "viewer" or old["state"] == "closing":
                    raise ValueError("Desktop lease cannot advance revision")
                if old["activated"] and old["epoch"] != epoch:
                    raise ValueError("Previous epoch requires cleanup")
            db.execute(
                "INSERT OR REPLACE INTO desktop_leases VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    lease_id,
                    str(workspace_id),
                    str(instance_id),
                    kind,
                    owner_id,
                    epoch,
                    revision,
                    "reserved",
                    int(bool(old and old["activated"])),
                    time.time() + ttl,
                ),
            )
            return self._row(
                db.execute(
                    "SELECT * FROM desktop_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
            )

        return await asyncio.to_thread(self._run, operation)

    async def renew(self, lease_id: str, epoch: str, revision: int, ttl: float) -> dict:
        """Extend only live matching ownership, never resurrect or activate."""

        def operation(db: sqlite3.Connection) -> dict:
            row = self._row(
                db.execute(
                    "SELECT * FROM desktop_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
            )
            if not row or row["epoch"] != epoch or row["revision"] != revision:
                raise ValueError("Unknown or stale desktop lease")
            if (
                row["state"] not in {"reserved", "held"}
                or row["expires_at"] <= time.time()
            ):
                raise ValueError("Desktop lease ended")
            db.execute(
                "UPDATE desktop_leases SET expires_at=? WHERE lease_id=?",
                (time.time() + ttl, lease_id),
            )
            row["expires_at"] = time.time() + ttl
            return row

        return await asyncio.to_thread(self._run, operation)

    async def activate(self, lease_id: str, epoch: str, revision: int) -> dict:
        """Record protective membership before starting guest side effects."""

        def operation(db: sqlite3.Connection) -> dict:
            row = self._row(
                db.execute(
                    "SELECT * FROM desktop_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
            )
            if (
                not row
                or row["epoch"] != epoch
                or row["revision"] != revision
                or row["state"] not in {"reserved", "held"}
                or row["expires_at"] <= time.time()
            ):
                raise ValueError("Desktop lease ended or stale")
            db.execute(
                "UPDATE desktop_leases SET activated=1,state='held' WHERE lease_id=?",
                (lease_id,),
            )
            row.update(activated=True, state="held")
            return row

        return await asyncio.to_thread(self._run, operation)

    async def mark_closing(self, lease_id: str) -> dict:
        """Fence new work while preserving protective activated membership."""

        def operation(db: sqlite3.Connection) -> dict:
            row = self._row(
                db.execute(
                    "SELECT * FROM desktop_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
            )
            if row is None:
                raise ValueError("Unknown desktop lease")
            if row["state"] in {"reserved", "held"}:
                db.execute(
                    "UPDATE desktop_leases SET state='closing' WHERE lease_id=?",
                    (lease_id,),
                )
                row["state"] = "closing"
            return row

        return await asyncio.to_thread(self._run, operation)

    async def finish(self, lease_id: str, state: str = "released") -> None:
        """Persist an ended tombstone only after owner cleanup is confirmed."""
        if state not in {"released", "expired"}:
            raise ValueError("Invalid terminal lease state")

        def operation(db: sqlite3.Connection) -> None:
            if db.execute(
                "SELECT 1 FROM desktop_recordings WHERE lease_id=? "
                "AND state!='closed'",
                (lease_id,),
            ).fetchone():
                raise ValueError("Recording closure is unverified")
            db.execute(
                "UPDATE desktop_leases SET state=?,activated=0 "
                "WHERE lease_id=? AND state='closing'",
                (state, lease_id),
            )

        await asyncio.to_thread(self._run, operation)

    async def reserve_recording(self, row: dict) -> dict:
        """Publish recovery identity atomically before a guest recording launch."""

        def operation(db: sqlite3.Connection) -> dict:
            lease = self._row(
                db.execute(
                    "SELECT * FROM desktop_leases WHERE lease_id=?", (row["lease_id"],)
                ).fetchone()
            )
            if (
                not lease
                or lease["kind"] != "computeruse"
                or lease["state"] not in {"reserved", "held"}
                or lease["expires_at"] <= time.time()
                or any(
                    lease[k] != row[k]
                    for k in ("workspace_id", "instance_id", "owner_id", "epoch")
                )
            ):
                raise ValueError("Recording owner is ended or mismatched")
            existing = db.execute(
                "SELECT * FROM desktop_recordings WHERE lease_id=?", (row["lease_id"],)
            ).fetchone()
            if existing:
                raise ValueError("Recording attempt already exists for lease")
            keys = (
                "recording_id",
                "lease_id",
                "workspace_id",
                "instance_id",
                "owner_id",
                "epoch",
                "control_path",
                "path",
                "state",
            )
            db.execute(
                "INSERT INTO desktop_recordings VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    *tuple(row[k] for k in keys),
                    json.dumps(row["guest_token"]) if row.get("guest_token") else None,
                ),
            )
            return row

        return await asyncio.to_thread(self._run, operation)

    async def recordings(self, lease_id: str | None = None) -> list[dict]:
        """Return durable recording metadata, including closed tombstones."""

        def operation(db: sqlite3.Connection) -> list[dict]:
            query = "SELECT * FROM desktop_recordings"
            args = ()
            if lease_id is not None:
                query += " WHERE lease_id=?"
                args = (lease_id,)
            return [dict(r) for r in db.execute(query, args)]

        return await asyncio.to_thread(self._run, operation)

    async def recording_state(self, recording_id: str, state: str) -> None:
        """Move an attempt towards verified closure, never resurrect it."""
        if state not in {"closing", "closed"}:
            raise ValueError("Invalid recording state")
        await asyncio.to_thread(
            self._run,
            lambda db: db.execute(
                "UPDATE desktop_recordings SET state=? WHERE recording_id=? "
                "AND state!='closed'",
                (state, recording_id),
            ),
        )

    async def clear_workspace(self, workspace_id: str) -> None:
        """Forget tombstones only after caller proves guest stop/removal.

        Administrative use only: requires a permanent logical identity/replay
        boundary, not ordinary stop/removal. Production retains terminal records
        indefinitely because workspace/viewer UUIDs can be reused. Live or
        unverified owners refuse deletion; caller must prove the replay boundary.
        """

        def operation(db: sqlite3.Connection) -> None:
            if (
                db.execute(
                    "SELECT 1 FROM desktop_leases WHERE workspace_id=? "
                    "AND state NOT IN ('released','expired')",
                    (str(workspace_id),),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM desktop_recordings WHERE workspace_id=? "
                    "AND state!='closed'",
                    (str(workspace_id),),
                ).fetchone()
            ):
                raise ValueError("Workspace still has unverified desktop owners")
            db.execute(
                "DELETE FROM desktop_recordings WHERE workspace_id=?",
                (str(workspace_id),),
            )
            db.execute(
                "DELETE FROM desktop_leases WHERE workspace_id=?", (str(workspace_id),)
            )

        await asyncio.to_thread(self._run, operation)

    async def list_unfinished(self, workspace_id: str | None = None) -> list[dict]:
        """Read only owners requiring maintenance; terminal replay fences remain."""

        def operation(db: sqlite3.Connection) -> list[dict]:
            query = (
                "SELECT * FROM desktop_leases "
                "WHERE state IN ('reserved','held','closing')"
            )
            args = ()
            if workspace_id is not None:
                query += " AND workspace_id=?"
                args = (str(workspace_id),)
            return [self._row(r) for r in db.execute(query, args)]

        return await asyncio.to_thread(self._run, operation)

    async def unfinished_recordings(
        self, workspace_id: str | None = None
    ) -> list[dict]:
        """Read recovery attempts without scanning closed recording history."""

        def operation(db: sqlite3.Connection) -> list[dict]:
            query = (
                "SELECT * FROM desktop_recordings WHERE state IN ('starting','closing')"
            )
            args = ()
            if workspace_id is not None:
                query += " AND workspace_id=?"
                args = (str(workspace_id),)
            return [dict(r) for r in db.execute(query, args)]

        return await asyncio.to_thread(self._run, operation)
