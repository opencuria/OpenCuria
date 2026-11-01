"""One authoritative in-memory record for each locally owned run.

Persistence owns admission across processes; this registry owns only tasks and
cancellation inside the execution process. Mapping projections preserve the
existing inspection API without storing a second copy of any run state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, MutableMapping, MutableSet
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


class _FieldView(MutableMapping):
    """A projection of record fields, never an independent state map."""

    def __init__(self, registry: RunRegistry, field_name: str) -> None:
        self.registry = registry
        self.field_name = field_name

    def __getitem__(self, key: str) -> Any:
        record = self.registry.get(key)
        if record is None:
            raise KeyError(key)
        if self.field_name == "parent_id":
            if not record.indexed:
                raise KeyError(key)
        elif getattr(record, self.field_name) is None:
            raise KeyError(key)
        return getattr(record, self.field_name)

    def __setitem__(self, key: str, value: Any) -> None:
        record = self.registry.ensure(key)
        if self.field_name == "task":
            record.begin_execution(value)
        else:
            setattr(record, self.field_name, value)
            if self.field_name == "admission":
                record.phase = RunPhase.ADMITTING
                record.execution_started = False
            if self.field_name == "parent_id":
                record.indexed = True

    def __delitem__(self, key: str) -> None:
        self[key]
        record = self.registry.ensure(key)
        setattr(record, self.field_name, None)
        if self.field_name == "parent_id":
            record.indexed = False
        self.registry.prune(key)

    def __iter__(self) -> Iterator[str]:
        return iter([key for key in self.registry.records if key in self])

    def __len__(self) -> int:
        return sum(1 for _ in self)


class _AbortView(MutableSet):
    def __init__(self, registry: RunRegistry) -> None:
        self.registry = registry

    def __contains__(self, key: object) -> bool:
        run = self.registry.get(str(key))
        return bool(run and run.abort_requested)

    def __iter__(self) -> Iterator[str]:
        return iter(
            [key for key, run in self.registry.records.items() if run.abort_requested]
        )

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def add(self, key: str) -> None:
        self.registry.ensure(key).abort_requested = True

    def discard(self, key: str) -> None:
        run = self.registry.get(key)
        if run:
            run.abort_requested = False
            self.registry.prune(key)

    def update(self, keys: set[str] | list[str]) -> None:
        for key in keys:
            self.add(key)


class RunRegistry:
    """Own records and release them only after all local obligations end."""

    def __init__(self) -> None:
        self._records: dict[str, ActiveRun] = {}
        self.tasks = _FieldView(self, "task")
        self.admissions = _FieldView(self, "admission")
        self.contexts = _FieldView(self, "context")
        self.parents = _FieldView(self, "parent_id")
        self.event_locks = _FieldView(self, "event_lock")
        self.aborts = _AbortView(self)

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
        self.ensure(key).context = context

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
