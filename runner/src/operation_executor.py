"""Crash-safe lifecycle execution and terminal replay around the journal."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import structlog

from .commands import CommandHandler
from .journal import Journal
from .operation_context import active_operation
from .runtime.recovery import RecoveryConflict, disk_helpers_active

logger = structlog.get_logger(__name__)

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


class OperationExecutor:
    """Persist command admission and terminal outcomes around an explicit service port."""

    def __init__(
        self,
        journal: Journal,
        commands: CommandHandler,
        emit: Callable[[str, dict], Awaitable[None]],
        *,
        reconciler: Callable | None = None,
        observer: Any = None,
        is_connected: Callable[[], bool] = lambda: True,
    ) -> None:
        self.observer = observer
        self.reconciler = reconciler
        self._recovered = False
        self.commands = commands
        self.journal = journal
        self.emit = emit
        self.is_connected = is_connected
        self.running: dict[str, asyncio.Task] = {}
        self._bindings: dict[str, dict] = {}
        self._unresolved = dict(journal.unfinished())
        self._admissions: set[asyncio.Task] = set()
        self._admission_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._execution_epoch = 0
        self._closing = False

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
            and resource.get("metadata", {})
            .get("workspace_id", "")
            .startswith("image-build-")
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
        if not self.is_connected():
            return {"status": "unknown", "execution_finished": False}
        response = {
            "instance_id": self.journal.instance_id,
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
        unfinished_publication = (
            row
            and row[2] is None
            and identity.get("failure_event")
            in {"image:build_failed", "image_artifact:failed"}
        )
        needs_recovery = row and (
            unfinished_publication
            or (
                row[2]
                and row[2].get("outcome_known") is False
                and identity.get("failure_event") is not None
            )
        )
        if needs_recovery and await asyncio.to_thread(disk_helpers_active):
            return {
                **response,
                "identity": {k: v for k, v in identity.items() if k != "failure_event"},
                "diagnostic_code": "disk_helpers_active",
            }
        if (
            row
            and (
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
            response["inspected_at"] = datetime.now(timezone.utc).isoformat()
            response["inventory"] = snapshot
            # Orphan conversion/build helper processes are live effects too.
            helpers = await asyncio.to_thread(disk_helpers_active)
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
    def _recovery_blocked(
        response: dict, identity: dict, error: RecoveryConflict
    ) -> dict:
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
                    disk_helpers_active
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
                                and identity.get("failure_event")
                                != "image:build_failed"
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
                                    if self.reconciler
                                    else None
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

    async def _finish(self, binding: dict, event: str, data: dict) -> None:
        """Verify effects, commit the outcome, then publish a replayable envelope."""
        result = {**(data or {}), **binding, "execution_finished": True}
        if event in {"image:built", "image:build_failed"}:
            recover = getattr(self.observer, "recover_interrupted_operation", None)
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
                if observed
                else None
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

    async def execute(self, event: str, data: dict) -> dict | None:
        """Shield durable admission and execution from transport cancellation."""
        if event not in COMMANDS:
            raise ValueError(f"Unsupported lifecycle command: {event}")
        if not isinstance(data, dict) or not all(
            key in data for key in ("operation_id", "attempt", "target", "runner_id")
        ):
            return None
        # Admission includes an uninterruptible fsync. A lost delivery waiter
        # must not strand a committed operation which never started.
        task = asyncio.create_task(self._admit(event, data))
        self._admissions.add(task)
        task.add_done_callback(self._admissions.discard)
        return await asyncio.shield(task)

    async def _admit(self, event: str, data: dict) -> dict | None:
        op_id = data["operation_id"]
        binding = {
            key: data[key]
            for key in (
                "task_id",
                "operation_id",
                "attempt",
                "target",
                "runner_id",
                "workspace_id",
                "build_job_id",
                "image_instance_id",
                "image_path",
            )
            if key in data
        }

        async def run() -> dict | None:
            token = active_operation.set(binding)
            try:
                for execution_attempt in range(3):
                    outcome = await self.commands.execute(event, data)
                    if outcome.event not in TERMINAL:
                        raise ValueError(
                            "Command returned an unsupported terminal event"
                        )
                    if (
                        outcome.event != "workspace:error"
                        or not outcome.retryable
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
                await self._finish(binding, outcome.event, outcome.data)
                return outcome.response
            except Exception:  # noqa: BLE001 - isolate runtime/transport failures
                row = await self.journal.call("lookup", op_id)
                failure_event = row[0]["failure_event"]
                await self._finish(
                    binding,
                    failure_event,
                    {
                        **binding,
                        "error": "Handler failed; resources preserved for retry",
                        "diagnostic_code": "handler_failed",
                        "outcome_known": False,
                    },
                )
            finally:
                active_operation.reset(token)
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
                for running_id, identity in (self._bindings | self._unresolved).items()
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
