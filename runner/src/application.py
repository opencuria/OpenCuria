"""Runner composition root; transport does not own persistence or effect wiring."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from .commands import LifecycleCommands
from .config import RunnerSettings
from .interfaces.websocket import (
    SOCKETIO_MAX_HTTP_BUFFER_SIZE,
    WebSocketInterface,
    _ReconnectingAsyncClient,
)
from .journal import Journal
from .operation_executor import OperationExecutor
from .service import WorkspaceService
from .workspace_evidence import WorkspaceEvidence


class RunnerApplication:
    """Own persistent execution and adapter lifetimes independently of transport."""

    def __init__(
        self,
        service: WorkspaceService,
        settings: RunnerSettings,
        *,
        publish: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> None:
        sio = _ReconnectingAsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=2,
            reconnection_delay_max=30,
            logger=False,
            websocket_extra_options={"max_msg_size": SOCKETIO_MAX_HTTP_BUFFER_SIZE},
        )
        journal = Journal(settings.state_dir, defer_recovery=True)
        try:
            evidence = WorkspaceEvidence(service, journal)
            configure = getattr(service, "configure_operation_persistence", None)
            if configure is not None:
                configure(
                    journal,
                    checkpoint=evidence.checkpoint,
                    scrub_proof=evidence.scrub_proof,
                )
            publisher = publish or sio.emit
            commands = LifecycleCommands(service, evidence, publisher)
            operations = OperationExecutor(
                journal,
                commands,
                publisher,
                reconciler=getattr(service, "publication_evidence", None),
                observer=service,
                is_connected=lambda: sio.connected,
            )
            epoch = str(uuid.uuid4())
            configure_desktop = getattr(service, "configure_desktop_leases", None)
            if configure_desktop is not None:
                configure_desktop(settings.state_dir, epoch)
            interface = WebSocketInterface(
                service, settings, operations, sio=sio, inventory_epoch=epoch
            )
            self.evidence = evidence
            self.journal = journal
            self.operations = operations
            self.interface = interface
        except BaseException:
            journal.close()
            raise

    async def start(self) -> None:
        """Start the runner transport after all execution ports are composed."""
        await self.interface.start()

    async def stop(self) -> None:
        """Drain effects and adapter tasks before releasing the journal writer lock."""
        try:
            await self.interface.stop()
        finally:
            await self.journal.call("close")
