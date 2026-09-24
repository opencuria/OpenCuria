"""Port protocols for the runners service package (Phase 4).

Only :class:`typing.Protocol` definitions — no implementations and no
imports from ``apps.harness`` / ``apps.credentials``. Concrete adapters
live outside this package; the service depends on these protocols only
via structural typing so tests can inject fakes.

Phase-4 wiring status (what is actually injected on ``RunnerService``):

- ``CredentialResolver``: used by ``credential_sync._dispatch_credential_inject``
  and the three ``workspace_lifecycle`` credential paths (``update`` /
  ``resume`` / ``create_from_image_artifact`` uniqueness check).
- ``HarnessStreamRouter``: used by ``stream_transport.handle_stream_reply``
  (``route_stream_output(data)`` / ``route_stream_closed(data)``).
- ``HarnessReplyRouter``: used by ``runner_transport._route_harness_reply``.
- ``FrontendBus``: used by ``infra/frontend_bus._forward_to_frontend``.

Everything else in ``workspace_lifecycle`` / ``interactive_sessions`` /
``file_transfer`` keeps its lazy ``emit_to_frontend`` import (documented
Phase-5 remainder). ``fail_streams_for_runner`` /
``_fail_workspace_processes`` keep the lazy ``_ACCESSORS_BY_STREAM``
harness access as fallback (documented harness-internal remainder).
"""

from __future__ import annotations

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


class HarnessStreamRouter(Protocol):
    """Route generic byte-stream transport events to the harness layer.

    Phase-4 wiring: ``StreamTransportMixin.handle_stream_reply`` prefers
    an injected router (``self._harness_stream_router``) over the lazy
    ``apps.harness`` import, so stream routing is unit-testable without
    the harness package. Signature mirrors the real harness functions
    ``route_stream_output(data)`` / ``route_stream_closed(data)``.
    """

    def route_stream_output(self, data: dict) -> bool | None:
        """Route a stream output/chunk event; True when consumed."""
        ...

    def route_stream_closed(self, data: dict) -> bool | None:
        """Route a stream closed/result event; True when consumed."""
        ...


class HarnessReplyRouter(Protocol):
    """Route harness reply events to the owning accessor.

    Phase-4 wiring: ``RunnerTransportMixin._route_harness_reply`` prefers
    an injected router (``self._harness_reply_router``) over the lazy
    ``apps.harness`` import.
    """

    def route_harness_chunk(self, data: dict) -> None:
        """Route an exec-chunk reply payload."""
        ...

    def route_harness_done(self, data: dict) -> None:
        """Route an exec-done reply payload."""
        ...

    def route_harness_result(self, data: dict) -> None:
        """Route a generic harness result payload."""
        ...

    def route_harness_file_chunk(self, data: dict) -> None:
        """Route a file-chunk reply payload."""
        ...
