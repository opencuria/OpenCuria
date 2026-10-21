"""Live steering: user messages that flow into an already running turn.

A :class:`SteeringChannel` is the single hand-off point between the service
(which accepts a message while a run is active) and the engine (which decides
*when* the model sees it). Native runs drain it between steps; the Claude
engine streams entries to the CLI as they arrive. Entries still pending when a
run settles are turned into a follow-up run by the service, so nothing a user
typed is silently lost.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from common.exceptions import ConflictError

#: Machine-readable ``ConflictError.code`` when no run can take a steer.
NO_ACTIVE_RUN_CODE = "no_active_run"

#: Upper bound for one steered message (characters).
MAX_STEER_CHARS = 100_000

#: Upper bound for messages waiting at the same time.
MAX_PENDING_STEERS = 20

DeliverFn = Callable[[list["SteerEntry"]], Awaitable[None]]


@dataclass(frozen=True)
class SteerEntry:
    """One queued user message addressed to a running turn."""

    id: str
    content: str
    created_at: datetime
    user_id: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return the secret-free wire shape shared by REST and Socket.IO."""
        return {
            "id": self.id,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class SteeringChannel:
    """FIFO of steers for one run, consumed by exactly one engine.

    ``deliver`` persists and publishes entries the engine has taken; it runs
    before the engine hands the text to the model so the transcript order
    matches what the model saw.
    """

    deliver: DeliverFn
    _pending: list[SteerEntry] = field(default_factory=list)
    _reserved: set[str] = field(default_factory=set)
    _closed: bool = False
    _feed_ended: bool = False
    _wakeup: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def closed(self) -> bool:
        """True once the run settled and no further steer is accepted."""
        return self._closed

    def pending(self) -> list[SteerEntry]:
        """Return a snapshot of entries not yet taken by the engine."""
        return list(self._pending)

    def submit(self, content: str, *, user_id: int | None = None) -> SteerEntry:
        """Queue *content*; raises :class:`ConflictError` when the run is over."""
        text = (content or "").strip()
        if not text:
            raise ValueError("prompt must not be empty")
        if len(text) > MAX_STEER_CHARS:
            raise ValueError(f"prompt exceeds {MAX_STEER_CHARS} characters")
        if self._closed:
            raise ConflictError("The run has already ended", code=NO_ACTIVE_RUN_CODE)
        if len(self._pending) >= MAX_PENDING_STEERS:
            raise ConflictError(
                "Too many queued messages; wait for the agent to pick them up",
                code="steer_queue_full",
            )
        entry = SteerEntry(
            id=str(uuid.uuid4()),
            content=text,
            created_at=datetime.now(timezone.utc),
            user_id=user_id,
        )
        self._pending.append(entry)
        self._wakeup.set()
        return entry

    def withdraw(self, steer_id: str) -> SteerEntry | None:
        """Remove a still-pending entry; ``None`` when already taken."""
        if steer_id in self._reserved:
            raise ConflictError(
                "The message is already being delivered", code="steer_in_flight"
            )
        for index, entry in enumerate(self._pending):
            if entry.id == steer_id:
                return self._pending.pop(index)
        return None

    def reserve(self, steer_id: str) -> SteerEntry | None:
        """Fence withdrawal while an external engine waits for acceptance."""
        if self._closed or self._feed_ended or steer_id in self._reserved:
            return None
        entry = next((row for row in self._pending if row.id == steer_id), None)
        if entry is not None:
            self._reserved.add(steer_id)
        return entry

    def release(self, steer_id: str, *, uncertain: bool = False) -> None:
        """Release a rejected steer; uncertain delivery must never be replayed."""
        self._reserved.discard(steer_id)
        if uncertain:
            self._pending = [row for row in self._pending if row.id != steer_id]

    async def acknowledge(self, steer_id: str) -> None:
        """Persist only an accepted steer; never requeue after an uncertain ACK."""
        if steer_id not in self._reserved:
            return  # Explicit Stop may already have discarded the queue.
        self._reserved.remove(steer_id)
        entry = next((row for row in self._pending if row.id == steer_id), None)
        self._pending = [row for row in self._pending if row.id != steer_id]
        if entry is not None:
            await self.deliver([entry])

    def discard(self) -> list[SteerEntry]:
        """Drop and return every pending entry (user stop)."""
        dropped = [row for row in self._pending if row.id not in self._reserved]
        self._pending = []
        self._reserved.clear()
        return dropped

    def close(self) -> list[SteerEntry]:
        """Stop accepting entries and return those nobody consumed."""
        self._closed = True
        self.end_feed()
        return self.discard()

    def end_feed(self) -> None:
        """Tell streaming consumers to stop waiting; entries stay pending."""
        self._feed_ended = True
        self._wakeup.set()

    async def take(self) -> list[SteerEntry]:
        """Pop and deliver all pending entries without waiting."""
        if self._feed_ended or not self._pending:
            return []
        taken, self._pending = self._pending, []
        try:
            await self.deliver(taken)
        except BaseException:
            self._pending = taken + self._pending
            raise
        return taken

    async def next(self) -> SteerEntry | None:
        """Wait for one entry, deliver it, and return it.

        Returns ``None`` once the feed ended; entries that arrived after that
        remain pending for the follow-up run.
        """
        while True:
            if self._feed_ended:
                return None
            if self._pending:
                entry = self._pending.pop(0)
                try:
                    await self.deliver([entry])
                except BaseException:
                    self._pending.insert(0, entry)
                    raise
                return entry
            self._wakeup.clear()
            await self._wakeup.wait()
