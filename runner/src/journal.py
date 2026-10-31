"""Crash-safe lifecycle execution and terminal replay without secret persistence."""

from __future__ import annotations

import asyncio
import contextvars
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

import structlog

from .runtime.recovery import RecoveryConflict

logger = structlog.get_logger(__name__)

_terminal = contextvars.ContextVar("lifecycle_terminal", default=None)
_active = contextvars.ContextVar("lifecycle_operation", default=None)
TERMINAL = {
    "workspace:created",
    "workspace:stopped",
    "workspace:resumed",
    "workspace:updated",
    "workspace:removed",
    "workspace:credentials_injected",
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
    "task:inject_credentials",
    "task:build_image",
    "task:create_image_artifact",
    "task:delete_image_artifact",
}
DISK_HELPERS = {
    "qemu-img", "virt-customize", "guestfish", "genisoimage", "mkisofs",
}


def _disk_helper_active(comm: Path) -> bool:
    """Recognize disk helpers still capable of effects, excluding zombie exits."""
    if comm.read_text().strip() not in DISK_HELPERS:
        return False
    # stat is an atomic process observation. Parse after the final name delimiter
    # because process names can themselves contain spaces or parentheses.
    _, separator, status = comm.with_name("stat").read_text().rpartition(") ")
    if not separator or not status:
        return True
    return status.split()[0] not in {"Z", "X", "x"}


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
        """Retain newest allocation lineage, revoked by a completed removal."""
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
                "task:create_workspace", "task:create_workspace_from_image_artifact",
            }:
                terminal = json.loads(result) if result else None
                return (
                    terminal is None
                    or event == "workspace:created"
                    or terminal.get("outcome_known") is False
                )
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
        """Persist terminal evidence before emitting, with sanitized errors."""
        clean = {
            k: v
            for k, v in data.items()
            if k not in {"env_vars", "files", "ssh_keys", "init_script"}
        }
        if "error" in clean:
            interrupted = (
                clean.get("diagnostic_code") == "interrupted"
                or clean.get("outcome_known") is False
            )
            clean["error"] = (
                "Operation interrupted; resources preserved for retry"
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


class OperationExecutor:
    """Wrap existing handlers and emit surface, preserving one runtime implementation."""

    def __init__(self, sio, journal: Journal, reconciler=None, observer=None) -> None:
        self.observer = observer
        self.reconciler = reconciler
        self._recovered = False
        self.sio = sio
        self.journal = journal
        self.emit = sio.emit
        self.running: dict[str, asyncio.Task] = {}
        self._bindings: dict[str, dict] = {}
        self._unresolved = dict(journal.unfinished())
        self._admissions: set[asyncio.Task] = set()
        self._admission_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._execution_epoch = 0
        self._closing = False
        sio.emit = self.record_emit
        sio.on("operation:ack", self.acknowledge)
        sio.on("operation:inspect", self.inspect)
        sio.on("operation:seal", self.seal)
        sio.on("operation:inspect_request", self.inspect_request)
        for event in COMMANDS:
            handler = sio.handlers["/"][event]
            sio.on(event, self.wrap(event, handler))

    @staticmethod
    def helpers_active() -> bool:
        """Fail closed while crashed-runner disk helpers may still have effects."""
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return True
        for proc in proc_root.glob("[0-9]*/comm"):
            try:
                if _disk_helper_active(proc):
                    return True
            except FileNotFoundError:
                continue  # The process exited between enumeration and inspection.
            except OSError:
                return True  # An unreadable helper is not proven quiescent.
        return False

    def _live_build_guests(
        self, snapshot: dict, *, inspected_operation: str | None = None
    ) -> bool:
        """Find temporary guests which may still execute orphaned image scripts."""
        owned = {
            f"image-build-{op_id}"
            for op_id in self.running
            if op_id != inspected_operation
        }
        return any(
            resource.get("kind") == "workspace"
            and resource.get("metadata", {}).get("workspace_id", "").startswith(
                "image-build-"
            )
            and resource.get("state") not in {"exited", "stopped", "removed"}
            and resource.get("metadata", {}).get("workspace_id") not in owned
            for runtime in snapshot.get("runtimes", [])
            for resource in runtime.get("resources", [])
        )

    async def _build_guests_quiescent(self, operation_id: str | None = None) -> bool:
        """Require complete observation before sealing an interrupted build."""
        if self.observer is None or not hasattr(self.observer, "inventory_for_runner"):
            return True
        snapshot = await self.observer.inventory_for_runner()
        return snapshot.get("complete") is True and not self._live_build_guests(
            snapshot, inspected_operation=operation_id
        )

    async def acknowledge(self, data: dict) -> None:
        """Commit backend receipt without blocking transport health on fsync."""
        await self.journal.call("acknowledge", data)

    async def drain(self) -> None:
        """Fence new deliveries and join admitted work before runner shutdown."""
        self._closing = True
        if self._admissions:
            await asyncio.gather(*self._admissions, return_exceptions=True)
        if self.running:
            await asyncio.gather(*self.running.values(), return_exceptions=True)

    def _target_running(self, target: str | None) -> bool:
        """Do not make unrelated long-running builds block target recovery."""
        return any(
            operation_id in self.running and binding.get("target") == target
            for operation_id, binding in self._bindings.items()
        )

    async def seal(self, data: dict) -> dict:
        """Fence a never-delivered identity before releasing its backend fence."""
        evidence = await self.inspect(data)
        if evidence.get(
            "diagnostic_code"
        ) != "identity_not_recorded" or not evidence.get("quiescent"):
            return evidence
        identity = {
            k: data[k] for k in ("operation_id", "runner_id", "attempt", "target")
        }
        # Inventory awaited runtime IO. Recheck admission under its mutex before
        # turning that observation into a permanent delayed-delivery fence.
        async with self._admission_lock:
            if (
                self._target_running(data.get("target"))
                or evidence.get("execution_epoch") != self._execution_epoch
            ):
                return {**evidence, "quiescent": False, "execution_finished": False}
            await self.journal.call("seal", identity)
        return await self.inspect(data)

    async def inspect_request(self, data: dict) -> None:
        """Reply-event variant for the independent backend recovery worker."""
        evidence = await self.inspect(data)
        if (
            evidence.get("diagnostic_code") == "identity_not_recorded"
            and evidence.get("quiescent") is True
        ):
            # A delayed delivery must never execute after recovery releases the
            # backend fence. seal() repeats inspection and admission-epoch checks.
            evidence = await self.seal(data)
        await self.emit("operation:inspection", evidence)

    async def inspect(self, data: dict) -> dict:
        """Inspect exact identity on this exclusive journal instance without secrets."""
        row = await self.journal.call("lookup", data.get("operation_id"))
        if hasattr(self.sio, "connected") and not self.sio.connected:
            return {"status": "unknown", "execution_finished": False}
        response = {
            "instance_id": self.journal.instance_id,
            "durable_credentials": True,
            "status": "unknown",
            "execution_finished": False,
            "outcome_known": False,
        }
        if row:
            identity = row[0]
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
        unfinished_publication = row and row[2] is None and identity.get(
            "failure_event"
        ) in {"image:build_failed", "image_artifact:failed"}
        needs_recovery = row and (
            data.get("legacy_credential_sync") is True
            or unfinished_publication
            or (
                row[2] and row[2].get("outcome_known") is False
                and identity.get("failure_event") is not None
            )
        )
        if needs_recovery and await asyncio.to_thread(self.helpers_active):
            return {
                **response,
                "identity": {k: v for k, v in identity.items() if k != "failure_event"},
                "diagnostic_code": "disk_helpers_active",
            }
        if data.get("legacy_credential_sync") is True and row and row[2]:
            # New runners reject legacy credential packets without envelopes.
            # Seal that delayed-delivery identity first, then stop the exact
            # guest so a previous runner's remote credential shell cannot keep
            # writing after the backend releases the old reservation.
            recover = getattr(self.observer, "recover_interrupted_operation", None)
            if recover is not None:
                try:
                    state = await recover({
                        **identity,
                        "workspace_id": identity.get("target"),
                        "failure_event": "workspace:error",
                    })
                except RecoveryConflict as error:
                    return self._recovery_blocked(response, identity, error)
                response["legacy_credential_quiesced"] = state in {
                    "stopped", "absent", "partial",
                }
        if (
            row and (
                unfinished_publication
                or (row[2] and row[2].get("outcome_known") is False)
            )
            and identity.get("failure_event") is not None
        ):
            recover = getattr(self.observer, "recover_interrupted_operation", None)
            if recover is not None:
                try:
                    await recover(identity)
                except RecoveryConflict as error:
                    return self._recovery_blocked(response, identity, error)
        if self.observer is not None and hasattr(self.observer, "inventory_for_runner"):
            epoch = self._execution_epoch
            snapshot = await self.observer.inventory_for_runner()
            from datetime import datetime, timezone

            response["inspected_at"] = datetime.now(timezone.utc).isoformat()
            response["inventory"] = snapshot
            # Orphan conversion/build helper processes are live effects too.
            helpers = await asyncio.to_thread(self.helpers_active)
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
                and not self._live_build_guests(snapshot)
                and not self._target_running(identity.get("target"))
                and epoch == self._execution_epoch
            )
            response["execution_epoch"] = epoch
            workspace_evidence = getattr(
                self.observer, "operation_workspace_evidence", None
            )
            if workspace_evidence is not None:
                response.update(await workspace_evidence(identity, snapshot))
            if epoch != self._execution_epoch or self._target_running(
                identity.get("target")
            ):
                response.update(quiescent=False, workspace_status="unknown")
                response.pop("credentials_present", None)
        response["identity"] = {
            k: v for k, v in identity.items() if k != "failure_event"
        }
        if data["operation_id"] in self.running:
            return {**response, "status": "running"}
        if not row:
            # Automatic recovery seals this identity before releasing a backend
            # allocation which never reached this journal. No recipe is rerun.
            return {
                **response,
                "execution_finished": response.get("quiescent", False),
                "diagnostic_code": "identity_not_recorded",
            }
        if row[2]:
            result = row[2]
            return {
                **response,
                "status": "terminal",
                "execution_finished": result.get("execution_finished", False),
                "outcome_known": result.get("outcome_known", "error" not in result),
                "event": row[1],
                "result": result,
            }
        return response

    @staticmethod
    def _recovery_blocked(response: dict, identity: dict, error: RecoveryConflict) -> dict:
        """Report proven ownership contradiction without asserting quiescence."""
        return {
            **response,
            "identity": {k: v for k, v in identity.items() if k != "failure_event"},
            "status": "unknown",
            "execution_finished": False,
            "quiescent": False,
            "recovery_blocked_code": error.code,
            "diagnostic": str(error),
        }

    async def replay(self, op_id: str | None = None, *, recover: bool = True) -> None:
        """Retry delivery until backend durably acknowledges."""
        if recover and not self._recovered:
            async with self._recovery_lock:
                if not self._recovered and not await asyncio.to_thread(
                    self.helpers_active
                ):
                    complete = True
                    unfinished = await self.journal.call("unfinished")
                    for interrupted_id, identity in unfinished:
                        if interrupted_id in self.running:
                            continue
                        try:
                            proof = (
                                await self.reconciler(identity)
                                if self.reconciler
                                and identity.get("failure_event") != "image:build_failed"
                                else None
                            )
                            recover = getattr(
                                self.observer, "recover_interrupted_operation", None
                            )
                            if recover is not None and not proof:
                                await recover(identity)
                            if (
                                identity.get("failure_event") == "image:build_failed"
                                and not await self._build_guests_quiescent()
                            ):
                                complete = False
                                continue
                            if identity.get("failure_event") == "image:build_failed":
                                proof = (
                                    await self.reconciler(identity)
                                    if self.reconciler else None
                                )
                            if proof:
                                event, result = proof
                                result.pop("failure_event", None)
                                result.update(execution_finished=True)
                                result.setdefault("outcome_known", True)
                            else:
                                event = identity.pop("failure_event")
                                result = {
                                    **identity,
                                    "error": (
                                        "Operation interrupted; resources preserved "
                                        "for retry"
                                    ),
                                    "diagnostic_code": "interrupted",
                                    "outcome_known": False,
                                    "execution_finished": True,
                                }
                            await self.journal.call(
                                "finish", interrupted_id, event, result
                            )
                            self._unresolved.pop(interrupted_id, None)
                        except Exception:
                            complete = False
                            logger.exception(
                                "operation_recovery_failed", operation_id=interrupted_id
                            )
                    self._recovered = complete
        if op_id is None:
            await self.heartbeat()
        for event, data in await self.journal.call("pending", op_id):
            try:
                await self.emit("operation:result", {"event": event, "data": data})
            except Exception:  # noqa: BLE001 - isolate transport/runtime failures
                return

    async def heartbeat(self) -> None:
        """Renew executing operations from memory even when journal IO is delayed."""
        for identity in list(self._bindings.values()):
            try:
                await self.emit("operation:heartbeat", identity)
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
            if event in {"image:built", "image:build_failed"}:
                recover = getattr(
                    self.observer, "recover_interrupted_operation", None
                )
                if recover is not None:
                    try:
                        await recover({**binding, "failure_event": "image:build_failed"})
                    except Exception:
                        logger.exception(
                            "image_build_cleanup_unverified",
                            operation_id=binding["operation_id"],
                        )
                        return
            if event in {"image:built", "image:build_failed"} and not (
                await self._build_guests_quiescent(binding["operation_id"])
            ):
                # Cleanup may fail while the script still runs inside its guest.
                # Keep the original unfinished identity and target fence; recovery
                # can seal it only after those untracked effects have ended.
                logger.warning(
                    "image_build_effects_unverified", operation_id=binding["operation_id"]
                )
                return
            lifecycle = getattr(self.observer, "lifecycle", None)
            if (
                self.observer is not None
                and hasattr(self.observer, "workspace_incarnation")
                and binding.get("workspace_id")
                # Production lifecycle checkpoints are written while holding its
                # workspace mutex. Re-observing after it returns could attach an
                # older result to a later credential injection/incarnation.
                and getattr(lifecycle, "checkpoint_hook", None) is None
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
                    await self.journal.call(
                        "checkpoint",
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
                    await self.journal.call("proof", workspace_id, observed[0])
                    if observed else None
                )
                if not (
                    proof
                    and proof.get("credentials_present") is False
                    and proof.get("state") in {"exited", "stopped"}
                    and observed[1] in {"exited", "stopped"}
                ):
                    result.update(diagnostic_code="interrupted", outcome_known=False)
            await self.journal.call("finish", binding["operation_id"], event, result)
            self._unresolved.pop(binding["operation_id"], None)
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
        """Shield durable admission and execution from caller cancellation."""

        async def admit(data: dict) -> dict | None:
            op_id = data["operation_id"]
            binding = {
                key: data[key]
                for key in (
                    "task_id", "operation_id", "attempt", "target", "runner_id",
                    "workspace_id", "build_job_id", "image_instance_id",
                    "image_path",
                )
                if key in data
            }

            async def run() -> dict | None:
                token = _active.set(binding)
                try:
                    terminal = []
                    terminal_token = _terminal.set(terminal)
                    try:
                        for execution_attempt in range(3):
                            terminal.clear()
                            response = await handler(data)
                            failed = terminal and terminal[0][0] == "workspace:error"
                            if (
                                not failed
                                or execution_attempt == 2
                                or event not in {
                                    "task:resume_workspace", "task:stop_workspace",
                                    "task:remove_workspace",
                                }
                                or not await self.safe_retry(event, data)
                            ):
                                break
                            await asyncio.sleep(2**execution_attempt)
                    finally:
                        _terminal.reset(terminal_token)
                    if not terminal:
                        raise RuntimeError("Handler returned without terminal result")
                    await self.record_emit(*terminal[0])
                    return response
                except Exception:  # noqa: BLE001 - isolate runtime/transport failures
                    row = await self.journal.call("lookup", op_id)
                    failure_event = row[0]["failure_event"]
                    await self.record_emit(
                        failure_event,
                        {
                            **binding,
                            "error": "Handler failed; resources preserved for retry",
                            "diagnostic_code": "handler_failed",
                            "outcome_known": False,
                        },
                    )
                finally:
                    _active.reset(token)
                    self.running.pop(op_id, None)
                    self._bindings.pop(op_id, None)
                    self._execution_epoch += 1
                    if op_id in self._unresolved:
                        self._recovered = False

            async with self._admission_lock:
                if self._closing:
                    return
                if any(
                    identity.get("target") == data["target"] and running_id != op_id
                    for running_id, identity in (
                        self._bindings | self._unresolved
                    ).items()
                ):
                    return
                fresh = await self.journal.call("begin", event, data)
                if fresh:
                    task = asyncio.create_task(run())
                    self.running[op_id] = task
                    self._bindings[op_id] = binding
                    self._unresolved[op_id] = binding
                    self._execution_epoch += 1
            if fresh:
                return await asyncio.shield(task)
            else:
                await self.replay(op_id)
                row = await self.journal.call("lookup", op_id)
                if event == "task:inject_credentials" and row and row[2]:
                    return {
                        **row[2],
                        "ok": row[1] == "workspace:credentials_injected",
                    }

        async def execute(data: dict) -> dict | None:
            # Same-version protocol is mandatory; legacy lifecycle work is unsafe.
            if not all(
                key in data
                for key in ("operation_id", "attempt", "target", "runner_id")
            ):
                return
            # Admission includes an uninterruptible fsync. Shield it too: losing
            # the delivery waiter must not strand a committed, unexecuted recipe.
            task = asyncio.create_task(admit(data))
            self._admissions.add(task)
            task.add_done_callback(self._admissions.discard)
            return await asyncio.shield(task)

        return execute
