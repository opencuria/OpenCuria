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
keeps working unchanged. ``get_runner_service()`` is the process-wide
singleton every adapter (REST, Socket.IO, MCP) and other app resolves.

Phase 4: the constructor additionally accepts optional injected ports
(``credential_resolver``, ``harness_gateway``, ``frontend_bus`` — all
keyword-only, default ``None``). An unset ``harness_gateway`` resolves to
the gateway the harness app registered at startup; the other ports keep
their previous lazy-import behaviour when unset. The
pending RPC registries are plain per-instance dictionaries; terminal/desktop
maps remain shared class attributes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..repositories import (
    ImageBuildJobRepository,
    ImageDefinitionRepository,
    ImageInstanceRepository,
    RunnerRepository,
    TaskRepository,
    WorkspaceProcessRepository,
    WorkspaceRepository,
)
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
from .infra.frontend_bus import FrontendBusMixin
from .infra.harness_gateway import get_harness_gateway
from .infra.ownership import OwnershipMixin
from .infra.realtime import get_sio_server
from .infra.rpc_registry import RpcRegistryMixin
from .infra.runner_transport import RunnerTransportMixin
from .infra.session_store import SessionStoreMixin
from .infra.task_dispatch import TaskDispatchMixin
from .ports import HarnessGateway

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
        harness_gateway: HarnessGateway | None = None,
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
            harness_gateway: Optional ``HarnessGateway``. When unset,
                        the gateway registered by the harness app is
                        used (see :meth:`_harness`).
            frontend_bus: Optional async callable
                        ``(event, data, workspace_id)`` or object with an
                        ``emit`` method. When set,
                        ``_forward_to_frontend`` uses it; otherwise
                        ``infra.realtime.emit_to_frontend`` is used.
        """
        self.sio = sio_server
        # Injected ports (all optional; see the constructor docstring).
        self._credential_resolver = credential_resolver
        self._harness_gateway = harness_gateway
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
        self._process_pending: dict[str, asyncio.Future] = {}
        # In-flight git RPCs: request_id -> _PendingGitRequest. Runner
        # git:operation handlers only emit git:operation_result events
        # (no Socket.IO ACK), so correlation uses request_id futures plus
        # fail-closed workspace_id/operation matching (trust boundary).
        self._git_pending: dict[str, _PendingGitRequest] = {}
        # Heartbeat-vanished process rows awaiting async verify against
        # the runner (workspace_id -> rows). Stashed by the sync
        # ``handle_heartbeat`` (which cannot do RPCs) and drained by
        # ``reconcile_vanished_processes`` (async, Socket.IO handler).
        self._pending_process_verify: dict[str, list] = {}
        # Per workspace/repo serialisation guards for git operations.
        self._git_locks: dict[str, asyncio.Lock] = {}
        self._git_locks_guard = asyncio.Lock()

    def _harness(self) -> HarnessGateway:
        """Return the injected or process-registered harness gateway."""
        return self._harness_gateway or get_harness_gateway()

    # In-memory mapping: workspace_id (str) → terminal_id (str)
    _active_terminals: dict[str, str] = {}
    # In-memory mapping: workspace_id (str) → runner_id (str)
    # Populated when a terminal starts so terminal:output can be validated
    # without a DB lookup on every single chunk.
    _terminal_workspace_runner: dict[str, str] = {}

    # In-memory desktop session state
    _active_desktops: dict[str, dict] = {}
    _desktop_workspace_runner: dict[str, str] = {}

# The RunnerService instance — created lazily
_runner_service: RunnerService | None = None


def get_runner_service() -> RunnerService:
    """Get or create the process-wide RunnerService singleton."""
    global _runner_service
    if _runner_service is None:
        _runner_service = RunnerService(sio_server=get_sio_server())
    return _runner_service


__all__ = [
    "RunnerService",
    "_PendingGitRequest",
    "_CREDENTIAL_INJECT_TIMEOUT_SECONDS",
    "get_runner_service",
]
