"""Port protocols for the runners service package.

Only :class:`typing.Protocol` definitions — no implementations and no
imports from ``apps.harness`` / ``apps.credentials``. Concrete adapters
live outside this package; the service depends on these protocols only
via structural typing so tests can inject fakes.

Wiring status (what ``RunnerService`` consumes):

- ``CredentialResolver``: used by ``credential_sync._dispatch_credential_inject``
  and the three ``workspace_lifecycle`` credential paths (``update`` /
  ``resume`` / ``create_from_image_artifact`` uniqueness check).
- ``HarnessGateway``: the only way the runners app reaches the agent
  harness (reply/stream routing, stream failure, viewer desktop actions,
  busy agent checks, desktop take-over). The harness app registers its
  implementation at startup (``infra/harness_gateway.register_harness_gateway``);
  tests may inject a fake via ``RunnerService(harness_gateway=...)``.
  Repositories never resolve the gateway themselves: lifecycle admissions
  receive ``has_busy_session`` from the service as a predicate evaluated
  under their row lock.
- ``FrontendBus``: used by ``infra/frontend_bus._forward_to_frontend``.

Everything else in ``workspace_lifecycle`` / ``interactive_sessions`` /
``file_transfer`` keeps its lazy ``emit_to_frontend`` import (documented
Phase-5 remainder).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Protocol


class RunnerTransport(Protocol):
    """Socket.IO transport towards a connected runner."""

    async def emit_to_runner(self, runner: Any, event: str, data: dict) -> None:
        """Send a fire-and-forget event to *runner*."""
        ...

    async def call_runner(
        self,
        runner: Any,
        event: str,
        data: dict,
        *,
        timeout: int = 15,
    ) -> dict:
        """Send a request/response call to *runner* and return the reply."""
        ...


class FrontendBus(Protocol):
    """Outbound bus towards subscribed frontend clients.

    Phase-4 wiring: ``RunnerService`` accepts either an async callable
    ``(event, data, workspace_id)`` or an object with an ``emit`` method
    (sync or async, same ``(event, data, workspace_id)`` signature). When
    set, ``FrontendBusMixin._forward_to_frontend`` uses it (see
    ``infra/frontend_bus._emit_via_injected_frontend_bus``); otherwise the
    lazy ``emit_to_frontend`` import is kept so existing
    ``monkeypatch``-based tests keep working.

    The protocol surface reflects the injected forms: either the object
    itself is callable, or it exposes ``emit``. The legacy
    ``forward_to_frontend(event, payload)`` name is NOT what the mixin
    calls — do not implement only that name for new fakes.
    """

    def __call__(self, event: str, data: dict, workspace_id: str) -> Any:
        """Deliver *data* as *event* for *workspace_id* (async or sync)."""
        ...

    def emit(self, event: str, data: dict, workspace_id: str) -> Any:
        """Deliver *data* as *event* for *workspace_id* (sync or async)."""
        ...


class CredentialResolver(Protocol):
    """Resolve workspace credentials for injection/reconcile flows.

    Phase-4 wiring: ``RunnerService(credential_resolver=...)`` stores the
    object on ``self._credential_resolver``; the credential call sites use
    ``resolver.resolve_workspace_credentials(workspace)`` (sync method,
    called via ``sync_to_async``) instead of constructing
    ``CredentialSvc()``. ``assert_unique_workspace_credentials`` (used by
    ``create_workspace_from_image_artifact``) is optional: objects that do
    not provide it fall back to ``CredentialSvc()`` for that check only.
    """

    def resolve_workspace_credentials(self, workspace: Any) -> Any:
        """Return the resolved credentials for *workspace*."""
        ...


class HarnessGateway(Protocol):
    """Runner-service view of the agent harness.

    Implemented by ``apps.harness`` and registered at app startup, so the
    runner service never imports harness internals. Routing methods return
    True when the payload reached a live harness waiter.
    """

    def route_harness_result(self, data: dict) -> bool:
        """Route a ``harness:*_result`` payload to its waiting accessor."""
        ...

    def route_harness_file_chunk(self, data: dict) -> bool:
        """Route a ``harness:read_file_chunk`` payload to its accessor."""
        ...

    def route_stream_output(self, data: dict) -> bool:
        """Route a ``workspace:stream_output`` chunk to its byte stream."""
        ...

    def route_stream_closed(self, data: dict) -> bool:
        """Route a ``workspace:stream_closed`` notice to its byte stream."""
        ...

    def fail_streams(
        self, matches_workspace: Callable[[str], bool], error: str
    ) -> None:
        """Fail open byte streams whose workspace id matches the predicate."""
        ...

    async def desktop_action(
        self, runner_service: Any, workspace_id: str, action: str, args: dict
    ) -> dict:
        """Run a correlated desktop action in *workspace_id*.

        Raises:
            HarnessActionError: When the runner reports a failure.
            TimeoutError: When no reply arrives in time.
        """
        ...

    def has_busy_computeruse_session(self, workspace_id: uuid.UUID) -> bool:
        """Return True when a computer-use agent drives the desktop (sync)."""
        ...

    def has_busy_session(self, workspace_id: uuid.UUID) -> bool:
        """Return True when any agent session in the workspace is busy (sync).

        Busy includes sessions waiting for a user answer or approval.
        """
        ...

    async def abort_busy_computeruse(self, workspace_id: uuid.UUID) -> list[uuid.UUID]:
        """Abort busy computer-use sessions and return their session ids."""
        ...
