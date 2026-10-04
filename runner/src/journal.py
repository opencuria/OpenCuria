"""Crash-safe lifecycle execution and terminal replay without secret persistence."""

from __future__ import annotations

import asyncio
import contextvars
import fcntl
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path

_terminal = contextvars.ContextVar("lifecycle_terminal", default=None)
_active = contextvars.ContextVar("lifecycle_operation", default=None)
TERMINAL = {
    "workspace:created",
    "workspace:stopped",
    "workspace:resumed",
    "workspace:updated",
    "workspace:removed",
    "workspace:error",
    "image:built",
    "image:build_failed",
    "image_artifact:created",
    "image_artifact:failed",
    "image_artifact:deleted",
    "image_artifact:delete_failed",
}
COMMANDS = {
    "task:create_workspace",
    "task:create_workspace_from_image_artifact",
    "task:stop_workspace",
    "task:resume_workspace",
    "task:update_workspace",
    "task:remove_workspace",
    "task:build_image",
    "task:create_image_artifact",
    "task:delete_image_artifact",
}


class Journal:
    """SQLite FULL sync; acknowledged tombstones are retained indefinitely."""

    def __init__(self, state_dir: str, *, defer_recovery: bool = False) -> None:
        path = Path(state_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_file = (path / "operations.lock").open("a")
        fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db = sqlite3.connect(path / "operations.sqlite3")
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
            "CREATE TABLE IF NOT EXISTS publications "
            "(physical_id TEXT PRIMARY KEY, proof TEXT)"
        )
        if not defer_recovery:
            self.interrupt_unproven()

    def publications(self) -> dict:
        """Return durable final Docker IDs, including pre-ledger terminal outcomes."""
        bindings = {}
        for event, result in self.db.execute(
            "SELECT event,result FROM operations WHERE result IS NOT NULL"
        ):
            data = json.loads(result)
            if (
                event == "image:built"
                and data.get("image_id")
                and data.get("image_tag")
            ):
                bindings[data["image_id"]] = {
                    "generation_id": data.get("image_instance_id"),
                    "operation_id": data.get("operation_id"),
                    "image_tag": data["image_tag"],
                }
        for physical, proof in self.db.execute(
            "SELECT physical_id,proof FROM publications"
        ):
            bindings[physical] = json.loads(proof)
        return bindings

    def publish_image(self, physical_id: str, proof: dict) -> None:
        """Checkpoint physical identity after successful final tag verification."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO publications VALUES(?,?)",
                (physical_id, json.dumps(proof)),
            )

    def unfinished(self) -> list[tuple[str, dict]]:
        """Read identities only; no execution recipes or credentials are persisted."""
        return [
            (op, json.loads(identity))
            for op, identity in self.db.execute(
                "SELECT id,identity FROM operations WHERE result IS NULL"
            ).fetchall()
        ]

    def interrupt_unproven(self) -> None:
        """Fence effects with no terminal publication evidence; never rerun them."""
        for op_id, identity in self.unfinished():
            event = identity.pop("failure_event")
            self.finish(
                op_id,
                event,
                {
                    **identity,
                    "error": "Manual intervention required",
                    "diagnostic_code": "interrupted",
                    "outcome_known": False,
                    "execution_finished": True,
                },
            )

    def close(self) -> None:
        """Release storage after executor has drained all handlers."""
        self.db.close()
        self._lock_file.close()

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
                "image_tag",
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
                "dockerfile_content",
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

    def finish(self, op_id: str, event: str, data: dict) -> None:
        """Persist terminal evidence before emitting, with sanitized errors."""
        clean = {
            k: v for k, v in data.items() if k not in {"env_vars", "files", "ssh_keys"}
        }
        if "error" in clean:
            interrupted = (
                clean.get("diagnostic_code") == "interrupted"
                or clean.get("outcome_known") is False
            )
            clean["error"] = (
                "Operation interrupted; manual intervention required"
                if interrupted
                else "Operation failed; execution finished"
            )
            clean.setdefault(
                "diagnostic_code",
                "insufficient_storage"
                if "no space left" in str(data.get("error", "")).lower()
                else "handler_failed",
            )
            clean.setdefault("outcome_known", not interrupted)
        with self.db:
            self.db.execute(
                "UPDATE operations SET event=?,result=? WHERE id=? AND result IS NULL",
                (event, json.dumps(clean), op_id),
            )

    def invalidate_checkpoint(self, workspace_id: str) -> None:
        """Revoke scrub proof before any injection, even when runtime scan fails."""
        with self.db:
            self.db.execute(
                "DELETE FROM checkpoints WHERE workspace_id=?", (workspace_id,)
            )

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

    def observe_workspace(self, workspace_id: str, state: str) -> None:
        """Permanently revoke stopped scrub evidence on an observed boot."""
        row = self.db.execute(
            "SELECT proof FROM checkpoints WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if (
            row
            and state == "running"
            and json.loads(row[0]).get("state") in {"exited", "stopped"}
        ):
            self.invalidate_checkpoint(workspace_id)

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

    def pending(self, op_id: str | None = None) -> list:
        """Read terminal replay records, optionally including acknowledged result."""
        sql = "SELECT event,result FROM operations WHERE result IS NOT NULL"
        sql += " AND id=?" if op_id else " AND ack=0"
        return [
            (event, json.loads(data))
            for event, data in self.db.execute(sql, (op_id,) if op_id else ())
        ]

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


class OperationExecutor:
    """Wrap existing handlers and emit surface, preserving one runtime implementation."""

    def __init__(self, sio, journal: Journal, reconciler=None, observer=None) -> None:
        self.observer = observer
        for runtime in getattr(observer, "_runtimes", {}).values():
            if getattr(runtime, "runtime_type", None) == "docker":
                runtime._publication_journal = journal
        self.reconciler = reconciler
        self._recovered = False
        self.sio = sio
        self.journal = journal
        self.emit = sio.emit
        self.running: dict[str, asyncio.Task] = {}
        sio.emit = self.record_emit
        sio.on("operation:ack", journal.acknowledge)
        sio.on("operation:inspect", self.inspect)
        sio.on("operation:seal", self.seal)
        sio.on("operation:inspect_request", self.inspect_request)
        for event in COMMANDS:
            handler = sio.handlers["/"][event]
            sio.on(event, self.wrap(event, handler))

    async def seal(self, data: dict) -> dict:
        """Fence a never-delivered identity before operator releases its DB fence."""
        evidence = await self.inspect(data)
        if evidence.get(
            "diagnostic_code"
        ) != "identity_not_recorded" or not evidence.get("quiescent"):
            return evidence
        identity = {
            k: data[k] for k in ("operation_id", "runner_id", "attempt", "target")
        }
        # Unique permanent tombstone rejects every future delayed delivery;
        # its fingerprint deliberately cannot match an execution recipe.
        with self.journal.db:
            self.journal.db.execute(
                "INSERT OR IGNORE INTO operations(id,fingerprint,identity,event,result) VALUES(?,?,?,?,?)",
                (
                    data["operation_id"],
                    "operator-sealed",
                    json.dumps(identity),
                    "workspace:error",
                    json.dumps(
                        {
                            **identity,
                            "task_id": data["operation_id"],
                            "diagnostic_code": "interrupted",
                            "outcome_known": False,
                            "execution_finished": True,
                            "error": "Interrupted allocation acknowledged; resources preserved",
                        }
                    ),
                ),
            )
        return await self.inspect(data)

    async def inspect_request(self, data: dict) -> None:
        """Reply-event variant for the independent backend recovery worker."""
        evidence = await self.inspect(data)
        await self.emit("operation:inspection", evidence)

    async def inspect(self, data: dict) -> dict:
        """Inspect exact identity on this exclusive journal instance without secrets."""
        row = self.journal.db.execute(
            "SELECT identity,event,result FROM operations WHERE id=?",
            (data.get("operation_id"),),
        ).fetchone()
        if hasattr(self.sio, "connected") and not self.sio.connected:
            return {"status": "unknown", "execution_finished": False}
        response = {
            "instance_id": self.journal.instance_id,
            "status": "unknown",
            "execution_finished": False,
            "outcome_known": False,
        }
        if row:
            identity = json.loads(row[0])
        else:
            identity = {
                k: data.get(k)
                for k in ("operation_id", "runner_id", "attempt", "target")
            }
        if data.get("operation_id") in self.running:
            return {**response, "status": "running"}

        if any(
            identity.get(k) != data.get(k) for k in ("runner_id", "attempt", "target")
        ):
            return response
        if self.observer is not None and hasattr(self.observer, "inventory_for_runner"):
            snapshot = await self.observer.inventory_for_runner()
            from datetime import datetime, timezone

            response["inspected_at"] = datetime.now(timezone.utc).isoformat()
            response["inventory"] = snapshot
            # Orphan conversion/build helper processes are live effects too.
            helpers = False
            for proc in Path("/proc").glob("[0-9]*/comm"):
                try:
                    if proc.read_text().strip() in {
                        "qemu-img",
                        "virt-customize",
                        "guestfish",
                    }:
                        helpers = True
                except OSError:
                    continue
            foreign_effects = any(
                r.get("kind") == "foreign_reference"
                and r.get("state") in {"running", "unknown"}
                for scan in snapshot.get("runtimes", [])
                for r in scan.get("resources", [])
            )
            response["quiescent"] = (
                snapshot.get("complete") is True
                and not helpers
                and not foreign_effects
                and not self.running
            )
        response["identity"] = {
            k: v for k, v in identity.items() if k != "failure_event"
        }
        if data["operation_id"] in self.running:
            return {**response, "status": "running"}
        if not row:
            # Explicit operator acknowledgment can release an allocation that
            # never reached this journal; no destructive runtime effect is run.
            return {
                **response,
                "execution_finished": response.get("quiescent", False),
                "diagnostic_code": "identity_not_recorded",
            }
        if row[2]:
            result = json.loads(row[2])
            return {
                **response,
                "status": "terminal",
                "execution_finished": result.get("execution_finished", False),
                "outcome_known": result.get("outcome_known", "error" not in result),
                "event": row[1],
                "result": result,
            }
        return response

    async def replay(self, op_id: str | None = None, *, recover: bool = True) -> None:
        """Retry delivery until backend durably acknowledges."""
        if recover and not self._recovered:
            self._recovered = True
            for interrupted_id, identity in self.journal.unfinished():
                if interrupted_id in self.running:
                    continue
                proof = await self.reconciler(identity) if self.reconciler else None
                if proof:
                    event, result = proof
                    result.pop("failure_event", None)
                    result.update(execution_finished=True)
                    result.setdefault("outcome_known", True)
                    self.journal.finish(interrupted_id, event, result)
                else:
                    failure = identity.pop("failure_event")
                    self.journal.finish(
                        interrupted_id,
                        failure,
                        {
                            **identity,
                            "error": "Manual intervention required",
                            "diagnostic_code": "interrupted",
                            "outcome_known": False,
                            "execution_finished": True,
                        },
                    )
        if op_id is None:
            for running_id in list(self.running):
                row = self.journal.db.execute(
                    "SELECT identity FROM operations WHERE id=?", (running_id,)
                ).fetchone()
                identity = json.loads(row[0])
                identity.pop("failure_event", None)
                try:
                    await self.emit("operation:heartbeat", identity)
                except Exception:  # noqa: BLE001 - isolate transport/runtime failures
                    return
        for event, data in self.journal.pending(op_id):
            try:
                await self.emit("operation:result", {"event": event, "data": data})
            except Exception:  # noqa: BLE001 - isolate transport/runtime failures
                return

    async def record_emit(self, event: str, data=None, *args, **kwargs):
        """Capture handler's terminal result even during backend outage."""
        binding = _active.get()
        if binding and event in TERMINAL:
            buffered = _terminal.get()
            if buffered is not None:
                buffered[:] = [(event, data or {})]
                return
            result = {**(data or {}), **binding, "execution_finished": True}
            if (
                self.observer is not None
                and hasattr(self.observer, "workspace_incarnation")
                and binding.get("workspace_id")
            ):
                workspace_id = binding["workspace_id"]
                try:
                    observed = await self.observer.workspace_incarnation(workspace_id)
                except Exception:  # noqa: BLE001 - failed inspection is not proof
                    observed = None
                if observed and event in {
                    "workspace:created",
                    "workspace:stopped",
                    "workspace:resumed",
                }:
                    incarnation, state = observed
                    self.journal.checkpoint(
                        workspace_id,
                        incarnation,
                        {
                            "event": event,
                            "operation_id": binding["operation_id"],
                            "state": state,
                            "credentials_present": result.get("credentials_present"),
                            "initialized": event != "workspace:stopped",
                        },
                    )
            if (
                event == "image_artifact:failed"
                and self.observer is not None
                and hasattr(self.observer, "workspace_incarnation")
            ):
                workspace_id = binding.get("workspace_id", "")
                try:
                    observed = await self.observer.workspace_incarnation(workspace_id)
                except Exception:  # noqa: BLE001 - failed inspection is not proof
                    observed = None
                proof = (
                    self.journal.proof(workspace_id, observed[0]) if observed else None
                )
                if not (
                    proof
                    and proof.get("credentials_present") is False
                    and proof.get("state") in {"exited", "stopped"}
                    and observed[1] in {"exited", "stopped"}
                ):
                    result.update(diagnostic_code="interrupted", outcome_known=False)
            self.journal.finish(binding["operation_id"], event, result)
            await self.replay(binding["operation_id"])
            return
        if binding:
            try:
                return await self.emit(event, data, *args, **kwargs)
            except Exception:  # noqa: BLE001 - isolate transport/runtime failures
                return  # progress transport loss must not abort runtime work
        return await self.emit(event, data, *args, **kwargs)

    async def safe_retry(self, event: str, data: dict) -> bool:
        """Retry only terminated workspace effects with a complete fresh observation."""
        if self.observer is None or not hasattr(self.observer, "workspace_incarnation"):
            return False
        observed = await self.observer.workspace_incarnation(
            data.get("workspace_id", "")
        )
        if event == "task:remove_workspace":
            # Runtime implementation preserves cache until deletion succeeds.
            return observed is not None
        if observed is None:
            return False
        if event == "task:stop_workspace":
            return observed[1] == "running"
        return observed[1] in {"running", "exited", "stopped"}

    def wrap(self, event: str, handler: Callable) -> Callable:
        """Shield lifecycle execution from disconnect/caller cancellation."""

        async def execute(data: dict):
            # Same-version protocol is mandatory; legacy lifecycle work is unsafe.
            if not all(
                k in data for k in ("operation_id", "attempt", "target", "runner_id")
            ):
                return
            op_id = data["operation_id"]
            for running_id in list(self.running):
                if running_id == op_id:
                    continue
                row = self.journal.db.execute(
                    "SELECT identity FROM operations WHERE id=?", (running_id,)
                ).fetchone()
                if row and json.loads(row[0]).get("target") == data["target"]:
                    return
            if not self.journal.begin(event, data):
                await self.replay(op_id)
                return

            async def run():
                binding = {
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
                    )
                    if k in data
                }
                token = _active.set(binding)
                try:
                    terminal = []
                    terminal_token = _terminal.set(terminal)
                    try:
                        for execution_attempt in range(3):
                            terminal.clear()
                            await handler(data)
                            failed = terminal and terminal[0][0] == "workspace:error"
                            if (
                                not failed
                                or execution_attempt == 2
                                or event
                                not in {
                                    "task:resume_workspace",
                                    "task:stop_workspace",
                                    "task:remove_workspace",
                                }
                                or not await self.safe_retry(event, data)
                            ):
                                break
                            await asyncio.sleep(2**execution_attempt)
                    finally:
                        _terminal.reset(terminal_token)
                    if terminal:
                        self.running.pop(op_id, None)
                        await self.record_emit(*terminal[0])
                    else:
                        raise RuntimeError("Handler returned without terminal result")
                except Exception:  # noqa: BLE001 - isolate transport/runtime failures
                    row = self.journal.db.execute(
                        "SELECT identity FROM operations WHERE id=?", (op_id,)
                    ).fetchone()
                    failure_event = json.loads(row[0])["failure_event"]
                    self.running.pop(op_id, None)
                    await self.record_emit(
                        failure_event,
                        {
                            **binding,
                            "error": "Handler failed; manual intervention required",
                            "diagnostic_code": "handler_failed",
                            "outcome_known": False,
                        },
                    )
                finally:
                    _active.reset(token)
                    self.running.pop(op_id, None)

            task = asyncio.create_task(run())
            self.running[op_id] = task
            await asyncio.shield(task)

        return execute
