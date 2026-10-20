"""Harness implementation of the runners ``HarnessGateway`` port.

Registered from :meth:`HarnessConfig.ready`, so the runner service reaches
harness accessors and sessions without importing ``apps.harness``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from apps.runners.exceptions import HarnessActionError

from .access import runner_accessor
from .repositories import HarnessSessionRepository


class RunnerHarnessGateway:
    """Route runner-service callbacks into the agent harness."""

    def route_harness_result(self, data: dict) -> bool:
        """Deliver a ``harness:*_result`` payload to its accessor."""
        return runner_accessor.route_harness_result(data)

    def route_harness_file_chunk(self, data: dict) -> bool:
        """Deliver a ``harness:read_file_chunk`` payload to its accessor."""
        return runner_accessor.route_harness_file_chunk(data)

    def route_stream_output(self, data: dict) -> bool:
        """Deliver a ``workspace:stream_output`` chunk to its byte stream."""
        return runner_accessor.route_stream_output(data)

    def route_stream_closed(self, data: dict) -> bool:
        """Deliver a ``workspace:stream_closed`` notice to its byte stream."""
        return runner_accessor.route_stream_closed(data)

    def fail_streams(
        self, matches_workspace: Callable[[str], bool], error: str
    ) -> None:
        """Fail open byte streams whose workspace id matches the predicate."""
        runner_accessor.fail_streams(matches_workspace, error)

    async def desktop_action(
        self, runner_service: Any, workspace_id: str, action: str, args: dict
    ) -> dict:
        """Run a correlated desktop action through a harness accessor."""
        accessor = await runner_accessor.create_harness_accessor(
            runner_service, workspace_id
        )
        try:
            return await accessor.desktop_action(action, args)
        except runner_accessor.RunnerAccessorError as exc:
            raise HarnessActionError(str(exc)) from exc

    def has_busy_computeruse_session(self, workspace_id: uuid.UUID) -> bool:
        """Return True when a computer-use session is busy in the workspace."""
        return bool(
            HarnessSessionRepository.list_busy_computeruse_for_workspace(
                workspace_id
            )
        )
