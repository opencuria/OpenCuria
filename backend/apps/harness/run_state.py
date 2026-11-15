"""One authoritative in-memory record for each locally owned run.

Persistence owns admission across processes; this registry owns only tasks and
cancellation inside the execution process. Named transitions update the record;
callers inspect it directly or query the registry without creating ownership.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any


class RunPhase(str, Enum):
    """The three local ownership phases; release removes the record."""

    ADMITTING = "admitting"
    RUNNING = "running"
    FINALIZING = "finalizing"


@dataclass
class ActiveRun:
    """Task ownership, ancestry, stop intent and timeline data travel together."""

    session_id: str
    parent_id: str | None = None
    assistant_message_id: str | None = None
    phase: RunPhase = RunPhase.ADMITTING
    task: asyncio.Task | None = None
    admission: asyncio.Task | None = None
    abort_requested: bool = False
    execution_started: bool = False
    context: dict[str, Any] | None = None
    event_lock: asyncio.Lock | None = None
    indexed: bool = False

    def begin_execution(self, task: asyncio.Task) -> None:
        """Transfer ownership from admission to the dedicated execution task."""
        if self.task is not None and self.task is not task and not self.task.done():
            raise RuntimeError("A run already has a live execution owner")
        if self.phase == RunPhase.FINALIZING:
            # A cancelled pre-entry task is replaced by its cleanup task.
            self.task = task
        else:
            self.phase = RunPhase.RUNNING
            self.task = task
        self.admission = None

    def begin_finalization(self) -> None:
        """Once finalizing, cancellation must not interrupt durable cleanup."""
        self.phase = RunPhase.FINALIZING

    @property
    def is_running(self) -> bool:
        """Count startup and finalization as owned work, not just streaming."""
        # A done task can still owe its pre-entry completion callback or
        # cleanup. Explicit release, not Task.done(), ends local ownership.
        return any(
            (
                self.task is not None,
                self.admission is not None,
                self.context is not None,
            )
        )


class RunRegistry:
    """Own records and release them only after all local obligations end."""

    def __init__(self) -> None:
        self._records: dict[str, ActiveRun] = {}

    def begin_admission(self, key: str, owner: asyncio.Task) -> None:
        """Acquire local startup ownership without replacing another owner."""
        run = self.ensure(key)
        if run.is_running and run.admission is not owner:
            raise RuntimeError("A run already has an admission or execution owner")
        if run.admission is owner:
            return
        run.phase = RunPhase.ADMITTING
        run.execution_started = False
        run.admission = owner

    def start_execution(self, key: str, task: asyncio.Task) -> None:
        """Transfer admission to execution, or install pre-entry finalization."""
        self.ensure(key).begin_execution(task)

    def release_admission(self, key: str, owner: asyncio.Task | None = None) -> None:
        """Release only this admission's ownership after transfer or rollback."""
        run = self.get(key)
        if run is not None and (owner is None or run.admission is owner):
            run.admission = None
            self.prune(key)

    def release_task(self, key: str, owner: asyncio.Task | None = None) -> None:
        """A stale completion callback cannot unregister a replacement task."""
        run = self.get(key)
        if run is not None and (owner is None or run.task is owner):
            run.task = None
            self.prune(key)

    def attach_context(self, key: str, context: dict[str, Any]) -> None:
        """Attach engine/timeline data to the existing typed ownership record."""
        run = self.ensure(key)
        run.context = context
        message_id = context.get("message_id")
        run.assistant_message_id = str(message_id) if message_id is not None else None

    def release_context(self, key: str) -> None:
        """Release execution data after its persistence/teardown consumers finish."""
        run = self.get(key)
        if run is not None:
            run.context = None
            self.prune(key)

    def link_parent(self, key: str, parent: str | None) -> None:
        """Index ancestry before a run can suspend or spawn descendants."""
        run = self.ensure(key)
        run.parent_id, run.indexed = parent, True

    def unlink_parent(self, key: str) -> None:
        """Release ancestry only after the cancellation coordinator permits it."""
        run = self.get(key)
        if run is not None:
            run.parent_id, run.indexed = None, False
            self.prune(key)

    def request_abort(self, key: str) -> None:
        """Record user intent separately from incidental task cancellation."""
        self.ensure(key).abort_requested = True

    def clear_abort(self, key: str) -> None:
        """Release user intent once the affected run tree is settled."""
        run = self.get(key)
        if run is not None:
            run.abort_requested = False
            self.prune(key)

    def event_lock(self, key: str) -> asyncio.Lock:
        """Allocate event serialization within its owning run record."""
        run = self.ensure(key)
        if run.event_lock is None:
            run.event_lock = asyncio.Lock()
        return run.event_lock

    def release_event_lock(self, key: str) -> None:
        """Release event serialization after no more events can be persisted."""
        run = self.get(key)
        if run is not None:
            run.event_lock = None
            self.prune(key)

    @property
    def records(self) -> Mapping[str, ActiveRun]:
        """Expose ownership for inspection without allowing index mutation."""
        return MappingProxyType(self._records)

    def get(self, key: str) -> ActiveRun | None:
        """Read local ownership without implicitly creating a run."""
        return self._records.get(str(key))

    def get_task(self, key: str) -> asyncio.Task | None:
        """Return the execution owner, including a still-settling done task."""
        run = self.get(key)
        return run.task if run is not None else None

    def get_admission(self, key: str) -> asyncio.Task | None:
        """Return startup ownership before transfer to execution."""
        run = self.get(key)
        return run.admission if run is not None else None

    def get_context(self, key: str) -> dict[str, Any]:
        """Read attached timeline data; missing runs yield an untracked empty dict."""
        run = self.get(key)
        return run.context if run is not None and run.context is not None else {}

    def get_parent(self, key: str, default: str | None = None) -> str | None:
        """Return indexed ancestry, preserving an explicitly linked root's None."""
        run = self.get(key)
        return run.parent_id if run is not None and run.indexed else default

    def is_aborted(self, key: str) -> bool:
        """Read explicit user-stop intent without allocating a run."""
        run = self.get(key)
        return bool(run and run.abort_requested)

    def ensure(self, key: str) -> ActiveRun:
        """Allocate a record before the first asynchronous admission boundary."""
        key = str(key)
        if key not in self._records:
            self._records[key] = ActiveRun(key)
        return self._records[key]

    def prune(self, key: str) -> None:
        """Release a record after task, timeline, ancestry and stop cleanup."""
        run = self.get(key)
        if run and not any(
            (
                run.task,
                run.admission,
                run.context is not None,
                run.event_lock,
                run.indexed,
                run.abort_requested,
            )
        ):
            self._records.pop(key, None)

    def finalizing(self, key: str) -> bool:
        """Expose the cancellation fence to lifecycle collaborators."""
        run = self.get(key)
        return run is not None and run.phase == RunPhase.FINALIZING
