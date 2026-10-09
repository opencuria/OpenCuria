"""Shared execution contract for harness engines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..runner import RunOptions, RunResult


class HarnessEngine(Protocol):
    """Engine interface consumed by the persistent harness service."""

    async def run(
        self,
        prompt: str,
        agent: str,
        model: str,
        mode: str,
        opts: RunOptions,
    ) -> RunResult:
        """Run one agent turn and return its aggregated result."""
        ...
