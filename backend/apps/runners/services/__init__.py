"""
Service layer for the runners app.

All business logic lives here. API views and WebSocket consumers delegate
to these functions — they never contain business logic themselves.

The service layer uses repositories for data access and the Socket.IO
server instance for sending events to runners.

Phase 3: this module is the thin ``RunnerService`` facade. All domain
logic lives in the :class:`Mixin <apps.runners.services.domains>` modules
(``services/domains/*.py``); infrastructure helpers live in
``services/infra/*.py``. This module keeps the service constructor, the
shared ``_PendingGitRequest`` record and the public re-exports so that
``from apps.runners.services import RunnerService, _PendingGitRequest``
keeps working unchanged.

Phase 4: the constructor additionally accepts optional injected ports
(``credential_resolver``, ``harness_stream_router``,
``harness_reply_router``, ``frontend_bus`` — all keyword-only, default
``None``). When unset, every call site keeps its previous lazy-import
behaviour, so all existing callers and tests run unchanged. The
in-memory ``PendingMap`` dict-subclasses live in
``services/infra/state.py`` (ownership documentation + dict-compatible
stores); the shared terminal/desktop maps intentionally stay class
attributes (tests read/write them directly on the class facade).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..repositories import (
    ImageDefinitionRepository,
    ImageInstanceRepository,
    RunnerRepository,
    ImageBuildJobRepository,
    TaskRepository,
    WorkspaceProcessRepository,
    WorkspaceRepository,
)

from .infra.ownership import OwnershipMixin
from .infra.task_dispatch import TaskDispatchMixin
from .infra.rpc_registry import RpcRegistryMixin
from .infra.runner_transport import RunnerTransportMixin
from .infra.frontend_bus import FrontendBusMixin
from .infra.session_store import SessionStoreMixin
from .infra.state import PendingMap
from .domains.credential_sync import (
    _CREDENTIAL_INJECT_TIMEOUT_SECONDS,
    CredentialSyncMixin,
)
from .domains.file_transfer import FileTransferMixin
from .domains.git_operations import GitOperationsMixin
from .domains.heartbeat_reconciler import HeartbeatReconcilerMixin
from .domains.image_lifecycle import ImageLifecycleMixin
from .domains.interactive_sessions import InteractiveSessionsMixin
from .domains.process_manager import ProcessManagerMixin
from .domains.runner_lifecycle import RunnerLifecycleMixin
from .domains.stream_transport import StreamTransportMixin
from .domains.workspace_lifecycle import WorkspaceLifecycleMixin

logger = logging.getLogger(__name__)


@dataclass
class _PendingGitRequest:
    """In-flight git:operation RPC with its expected trust-boundary fields.

    ``request_id`` correlation alone is not fail-closed: a reply on the
    same runner connection with a wrong ``workspace_id``/``operation``
    must never resolve another request's waiter. The pending entry
    therefore pins the expected workspace UUID string and operation name.
    """

    future: asyncio.Future
    workspace_id: str
    operation: str


class RunnerService(
    OwnershipMixin,
    TaskDispatchMixin,
    RpcRegistryMixin,
    RunnerTransportMixin,
    FrontendBusMixin,
    SessionStoreMixin,
    StreamTransportMixin,
    InteractiveSessionsMixin,
    ProcessManagerMixin,
    GitOperationsMixin,
    FileTransferMixin,
    CredentialSyncMixin,
    WorkspaceLifecycleMixin,
    HeartbeatReconcilerMixin,
    ImageLifecycleMixin,
    RunnerLifecycleMixin,
):
    """
    Central business logic for runner management and task dispatching.

    This is the only place where domain rules are enforced. All interfaces
    (REST API, Socket.IO consumers) delegate to this service.
    """

    def __init__(
        self,
        sio_server=None,
        *,
        credential_resolver=None,
        harness_stream_router=None,
        harness_reply_router=None,
        frontend_bus=None,
    ):
        """
        Initialize the service.

        Args:
            sio_server: The python-socketio AsyncServer instance for sending
                        events to connected runners. Injected to keep the
                        service testable.
            credential_resolver: Optional object with
                        ``resolve_workspace_credentials(workspace)`` (sync).
                        When set, credential call sites use it instead of
                        constructing ``CredentialSvc()``. Default ``None``
                        keeps the previous lazy behaviour.
            harness_stream_router: Optional object with
                        ``route_stream_output(data)`` /
                        ``route_stream_closed(data)``. When set,
                        ``handle_stream_reply`` uses it instead of the lazy
                        ``apps.harness`` import.
            harness_reply_router: Optional object with
                        ``route_harness_chunk`` / ``route_harness_done`` /
                        ``route_harness_result`` /
                        ``route_harness_file_chunk``. When set,
                        ``_route_harness_reply`` uses it instead of the lazy
                        ``apps.harness`` import.
            frontend_bus: Optional async callable
                        ``(event, data, workspace_id)`` or object with an
                        ``emit`` method. When set,
                        ``_forward_to_frontend`` uses it; otherwise the lazy
                        ``emit_to_frontend`` import is kept (so
                        monkeypatch-based tests keep working).
        """
        self.sio = sio_server
        # Phase-4 injected ports (all optional; None = legacy lazy path).
        self._credential_resolver = credential_resolver
        self._harness_stream_router = harness_stream_router
        self._harness_reply_router = harness_reply_router
        self._frontend_bus = frontend_bus
        self.runners = RunnerRepository
        self.workspaces = WorkspaceRepository
        self.tasks = TaskRepository
        self.image_instances = ImageInstanceRepository
        self.image_definitions = ImageDefinitionRepository
        self.build_jobs = ImageBuildJobRepository
        self.processes = WorkspaceProcessRepository
        # Tracks unknown runtime workspaces for which a cleanup request has
        # already been sent, to avoid emitting duplicate cleanup tasks on every
        # heartbeat while one is still in flight.
        self._pending_unknown_workspace_cleanup: set[tuple[str, str]] = set()
        self._pending_credential_inject: set[tuple[str, str]] = set()
        # In-flight background-process RPCs: request_id -> Future[dict].
        # Runner harness:process_* handlers only emit *_result events (no
        # Socket.IO ACK), so correlation uses request_id futures.
        # Phase 4: PendingMap is a dict subclass (ownership documentation
        # only — direct dict syntax used by tests keeps working).
        self._process_pending: dict[str, asyncio.Future] = PendingMap()
        # In-flight reply-awaited Socket.IO calls: event ->
        # request_id/connection_id -> Future. Runner handlers never
        # return Socket.IO ACK payloads (they emit ``*_result`` events),
        # so ``sio.call`` would always time out; correlation uses the
        # reply events instead (see ``_call_reply_waiter``).
        self._call_pending: dict[str, dict[str, asyncio.Future]] = {}
        # In-flight git RPCs: request_id -> _PendingGitRequest. Runner
        # git:operation handlers only emit git:operation_result events
        # (no Socket.IO ACK), so correlation uses request_id futures plus
        # fail-closed workspace_id/operation matching (trust boundary).
        # Phase 4: PendingMap is a dict subclass (see above).
        self._git_pending: dict[str, _PendingGitRequest] = PendingMap()
        # Heartbeat-vanished process rows awaiting async verify against
        # the runner (workspace_id -> rows). Stashed by the sync
        # ``handle_heartbeat`` (which cannot do RPCs) and drained by
        # ``reconcile_vanished_processes`` (async, Socket.IO handler).
        # Phase 4: PendingMap is a dict subclass (see above).
        self._pending_process_verify: dict[str, list] = PendingMap()
        # Per workspace/repo serialisation guards for git operations.
        self._git_locks: dict[str, asyncio.Lock] = {}
        self._git_locks_guard = asyncio.Lock()

    # In-memory mapping: workspace_id (str) → terminal_id (str)
    _active_terminals: dict[str, str] = {}
    # In-memory mapping: workspace_id (str) → runner_id (str)
    # Populated when a terminal starts so terminal:output can be validated
    # without a DB lookup on every single chunk.
    _terminal_workspace_runner: dict[str, str] = {}

    # In-memory desktop session state
    _active_desktops: dict[str, dict] = {}
    _desktop_workspace_runner: dict[str, str] = {}

__all__ = ["RunnerService", "_PendingGitRequest", "_CREDENTIAL_INJECT_TIMEOUT_SECONDS"]
