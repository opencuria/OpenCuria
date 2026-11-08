"""Crash-safe operation and checkpoint records without secret persistence."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from .runtime.diagnostics import CODES, INTERRUPTED, failure_code


def serialized(method: Callable[..., Any]) -> Callable[..., Any]:
    """Keep synchronous callers and the journal worker on one transaction owner."""

    @wraps(method)
    def wrapped(self: Journal, *args: Any, **kwargs: Any) -> Any:
        with self._mutex:
            return method(self, *args, **kwargs)

    return wrapped


class Journal:
    """SQLite FULL sync; acknowledged tombstones are retained indefinitely."""

    def __init__(self, state_dir: str, *, defer_recovery: bool = False) -> None:
        self._mutex = threading.RLock()
        self._io_lock = asyncio.Lock()
        path = Path(state_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_file = (path / "operations.lock").open("a")
        fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db = sqlite3.connect(path / "operations.sqlite3", check_same_thread=False)
        os.chmod(path / "operations.sqlite3", 0o600)
        self.instance_id = str(uuid.uuid4())
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS operations "
            "(id TEXT PRIMARY KEY, fingerprint TEXT, identity TEXT, "
            "event TEXT, result TEXT, ack INTEGER DEFAULT 0)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS checkpoints "
            "(workspace_id TEXT PRIMARY KEY, incarnation TEXT, proof TEXT)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS operations_pending "
            "ON operations(ack) WHERE result IS NOT NULL"
        )
        if not defer_recovery:
            self.interrupt_unproven()

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Run serialized disk IO off the event loop, joining it on cancellation.

        Queue before starting a thread, so slow storage consumes at most one
        worker. A cancelled caller must not release a lifecycle fence while its
        uninterruptible SQLite transaction can still commit.
        """
        async with self._io_lock:
            work = asyncio.create_task(
                asyncio.to_thread(getattr(self, method), *args, **kwargs)
            )
            cancelled = False
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException:  # noqa: BLE001 - drain worker before unlock
                    break
            if cancelled:
                # Consume a worker exception but preserve cancellation authority.
                if not work.cancelled():
                    work.exception()
                raise asyncio.CancelledError
            return work.result()

    @serialized
    def lookup(self, op_id: str | None) -> tuple[dict, str | None, dict | None] | None:
        """Read a complete operation record under the transaction mutex."""
        row = self.db.execute(
            "SELECT identity,event,result FROM operations WHERE id=?", (op_id,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0]), row[1], json.loads(row[2]) if row[2] else None

    @serialized
    def seal(self, identity: dict) -> None:
        """Permanently reject delayed execution of an inspected missing identity."""
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO operations"
                "(id,fingerprint,identity,event,result) VALUES(?,?,?,?,?)",
                (
                    identity["operation_id"],
                    "recovery-sealed",
                    json.dumps(identity),
                    "workspace:error",
                    json.dumps(
                        {
                            **identity,
                            "task_id": identity["operation_id"],
                            "diagnostic_code": "interrupted",
                            "outcome_known": False,
                            "execution_finished": True,
                            "error": "Operation interrupted; resources preserved for retry",
                        }
                    ),
                ),
            )

    @serialized
    def unfinished(self) -> list[tuple[str, dict]]:
        """Read identities only; no execution recipes or credentials are persisted."""
        return [
            (op, json.loads(identity))
            for op, identity in self.db.execute(
                "SELECT id,identity FROM operations WHERE result IS NULL"
            ).fetchall()
        ]

    @serialized
    def owns_workspace_allocation(self, workspace_id: str) -> bool:
        """Retain newest allocation lineage, revoked by a completed removal.

        A create refused before any effect allocated nothing and is skipped.
        """
        for encoded, event, result in self.db.execute(
            "SELECT identity,event,result FROM operations ORDER BY rowid DESC"
        ):
            identity = json.loads(encoded)
            if (
                identity.get("workspace_id") != workspace_id
                or identity.get("target") != workspace_id
            ):
                continue
            if event == "workspace:removed":
                return False
            if identity.get("command") in {
                "task:create_workspace",
                "task:create_workspace_from_image_artifact",
            }:
                terminal = json.loads(result) if result else None
                if (
                    terminal is None
                    or event == "workspace:created"
                    or terminal.get("outcome_known") is False
                ):
                    return True
        return False

    @serialized
    def interrupt_unproven(self) -> None:
        """Fence effects with no terminal publication evidence; never rerun them."""
        for op_id, identity in self.unfinished():
            event = identity.pop("failure_event")
            self.finish(
                op_id,
                event,
                {
                    **identity,
                    "error": "Operation interrupted; resources preserved for retry",
                    "diagnostic_code": "interrupted",
                    "outcome_known": False,
                    "execution_finished": True,
                },
            )

    @serialized
    def close(self) -> None:
        """Release storage after executor has drained all handlers."""
        self.db.close()
        self._lock_file.close()

    @serialized
    def begin(self, event: str, data: dict) -> bool:
        """Reserve identity before any runtime effect; reject payload collisions."""
        identity = {
            k: data[k]
            for k in (
                "task_id",
                "operation_id",
                "attempt",
                "target",
                "runner_id",
                "workspace_id",
                "build_job_id",
                "image_instance_id",
                "image_artifact_id",
                "image_path",
            )
            if k in data
        }
        failure = (
            "image:build_failed"
            if event == "task:build_image"
            else "image_artifact:delete_failed"
            if event == "task:delete_image_artifact"
            else "image_artifact:failed"
            if event == "task:create_image_artifact"
            else "workspace:error"
        )
        identity["failure_event"] = failure
        identity["command"] = event
        # Credentials are resolved anew at delivery. Exclude all secret-bearing
        # fields and recipe contents from persistent hash/material.
        safe = {
            k: v
            for k, v in data.items()
            if k
            not in {
                "env_vars",
                "files",
                "ssh_keys",
                "init_script",
            }
        }
        fingerprint = hashlib.sha256(
            json.dumps([event, safe], sort_keys=True).encode()
        ).hexdigest()
        op_id = data["operation_id"]
        row = self.db.execute(
            "SELECT fingerprint FROM operations WHERE id=?", (op_id,)
        ).fetchone()
        if row:
            if row[0] != fingerprint:
                raise ValueError("Operation identity collision")
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO operations(id,fingerprint,identity) VALUES(?,?,?)",
                (op_id, fingerprint, json.dumps(identity)),
            )
        return True

    @serialized
    def finish(self, op_id: str, event: str, data: dict) -> None:
        """Persist terminal evidence before emitting, with sanitized errors.

        Raw messages may contain paths or secrets: only a generic text and a
        stable ``diagnostic_code`` leave the runner; the backend words it.
        """
        clean = {
            k: v
            for k, v in data.items()
            if k not in {"env_vars", "files", "ssh_keys", "init_script"}
        }
        if "error" in clean:
            interrupted = (
                clean.get("diagnostic_code") == INTERRUPTED
                or clean.get("outcome_known") is False
            )
            clean["error"] = (
                "Operation interrupted; resources preserved for retry"
                if interrupted
                else "Operation failed; execution finished"
            )
            if clean.get("diagnostic_code") not in CODES:
                clean["diagnostic_code"] = failure_code(str(data.get("error", "")))
            clean.setdefault("outcome_known", not interrupted)
        with self.db:
            self.db.execute(
                "UPDATE operations SET event=?,result=? WHERE id=? AND result IS NULL",
                (event, json.dumps(clean), op_id),
            )

    @serialized
    def invalidate_checkpoint(self, workspace_id: str) -> None:
        """Revoke scrub proof before any injection, even when runtime scan fails."""
        with self.db:
            self.db.execute(
                "DELETE FROM checkpoints WHERE workspace_id=?", (workspace_id,)
            )

    @serialized
    def checkpoint(self, workspace_id: str, incarnation: dict, proof: dict) -> None:
        """Persist only runtime identity and scrub/readiness evidence, never secrets."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?)",
                (
                    workspace_id,
                    json.dumps(incarnation, sort_keys=True),
                    json.dumps(proof),
                ),
            )

    @serialized
    def observe_workspace(self, workspace_id: str, state: str) -> None:
        """Permanently revoke stopped scrub evidence on an observed boot."""
        row = self.db.execute(
            "SELECT proof FROM checkpoints WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if (
            row
            and state not in {"exited", "stopped"}
            and json.loads(row[0]).get("state") in {"exited", "stopped"}
        ):
            self.invalidate_checkpoint(workspace_id)

    @serialized
    def proof(self, workspace_id: str, incarnation: dict) -> dict | None:
        """Trust a checkpoint only for an exact unchanged runtime incarnation."""
        row = self.db.execute(
            "SELECT incarnation,proof FROM checkpoints WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if row:
            saved, proof = json.loads(row[0]), json.loads(row[1])
            # Running readiness is identity-bound: normal writes are expected.
            if proof.get("state") == "running" and proof.get("initialized"):

                def identity(value):
                    value = json.loads(json.dumps(value))
                    for disk in value.get("metadata", {}).get("disks", []):
                        for key in ("size", "mtime_ns", "ctime_ns"):
                            disk.pop(key, None)
                    return value

                if identity(saved) == identity(incarnation):
                    return proof
            elif saved == incarnation:
                return proof
        return None

    @serialized
    def pending(self, op_id: str | None = None) -> list:
        """Read terminal replay records, optionally including acknowledged result."""
        sql = "SELECT event,result FROM operations WHERE result IS NOT NULL"
        sql += " AND id=?" if op_id else " AND ack=0"
        return [
            (event, json.loads(data))
            for event, data in self.db.execute(sql, (op_id,) if op_id else ())
        ]

    @serialized
    def acknowledge(self, data: dict) -> None:
        """ACK requires the same attempt/target, not merely an operation UUID."""
        row = self.db.execute(
            "SELECT identity FROM operations WHERE id=?", (data.get("operation_id"),)
        ).fetchone()
        if not row:
            return
        identity = json.loads(row[0])
        if any(
            identity.get(k) != data.get(k) for k in ("attempt", "target", "runner_id")
        ):
            return
        with self.db:
            self.db.execute(
                "UPDATE operations SET ack=1 WHERE id=? AND result IS NOT NULL",
                (data["operation_id"],),
            )
