"""Execution identity exposed to runner transport adapters.

The runner transport must attach these values to every mutating RPC and have
its receiver fence requests against the workspace/run ownership state. A UUID
alone is not side-effect fencing; this context only carries the authority,
it does not make unmodified transports safe.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class RunAuthority:
    """Identity of the durable worker attempt issuing runner operations."""

    run_id: uuid.UUID
    owner_token: uuid.UUID
    worker_id: str
    deployment_version: str
    root_run_id: uuid.UUID


_current_run_authority: contextvars.ContextVar[RunAuthority | None] = (
    contextvars.ContextVar("opencuria_harness_run_authority", default=None)
)


def current_run_authority() -> RunAuthority | None:
    """Return the authority for the current execution task, if any."""
    return _current_run_authority.get()


@contextmanager
def use_run_authority(authority: RunAuthority) -> Iterator[None]:
    """Set and reliably restore the current worker/run identity."""
    token = _current_run_authority.set(authority)
    try:
        yield
    finally:
        _current_run_authority.reset(token)


_uncertain_runs: set[uuid.UUID] = set()


def mark_current_run_uncertain() -> None:
    """Retain uncertainty after a transport deadline; never authorize blind replay."""
    authority = current_run_authority()
    if authority is not None:
        _uncertain_runs.add(authority.run_id)


def run_outcome_uncertain(run_id: uuid.UUID) -> bool:
    """Check whether this process observed an unconfirmed runner effect."""
    return run_id in _uncertain_runs


def forget_run_uncertainty(run_id: uuid.UUID) -> None:
    """Drop only process-local tracking; durable CLOSING remains protective."""
    _uncertain_runs.discard(run_id)
