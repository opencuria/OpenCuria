"""
App-specific exceptions for the runners app.
"""

from __future__ import annotations

from common.exceptions import ConflictError, NotFoundError


class RunnerNotFoundError(NotFoundError):
    """Raised when a runner is not found."""

    def __init__(self, identifier: str) -> None:
        super().__init__(resource="Runner", identifier=identifier)


class RunnerOfflineError(ConflictError):
    """Raised when trying to dispatch a task to an offline runner."""

    def __init__(self, runner_id: str) -> None:
        super().__init__(message=f"Runner '{runner_id}' is offline")


class WorkspaceNotFoundError(NotFoundError):
    """Raised when a workspace is not found."""

    def __init__(self, identifier: str) -> None:
        super().__init__(resource="Workspace", identifier=identifier)


class WorkspaceStateError(ConflictError):
    """Raised when a workspace operation conflicts with its current state."""

    pass


class SessionNotFoundError(NotFoundError):
    """Raised when a session is not found."""

    def __init__(self, identifier: str) -> None:
        super().__init__(resource="Session", identifier=identifier)


class TaskNotFoundError(NotFoundError):
    """Raised when a task is not found."""

    def __init__(self, identifier: str) -> None:
        super().__init__(resource="Task", identifier=identifier)


class NoAvailableRunnerError(ConflictError):
    """Raised when no online runner supports the requested agent type."""

    def __init__(self, agent_type: str) -> None:
        super().__init__(
            message=f"No online runner available for agent type '{agent_type}'"
        )
