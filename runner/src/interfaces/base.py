"""Abstract base for runner interfaces (WebSocket, CLI, etc.)."""

from __future__ import annotations

import abc

from ..service import WorkspaceService


class Interface(abc.ABC):
    """Base class for control interfaces.

    An interface receives tasks (create workspace, run prompt, …) from
    an external source and delegates them to the ``WorkspaceService``.
    """

    def __init__(self, service: WorkspaceService) -> None:
        self._service = service

    @abc.abstractmethod
    async def start(self) -> None:
        """Start listening for incoming tasks."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the interface."""
