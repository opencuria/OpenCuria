"""
Socket.IO server for runner and frontend communication.

This module registers the event handlers that serve WebSocket connections
from runners and frontend clients on the shared AsyncServer (owned by
``apps.runners.services.infra.realtime``). It is the thin adapter between
the Socket.IO protocol and the RunnerService business logic; no other
module may import it except the ASGI entry point.

The server is mounted as an ASGI app in config/asgi.py.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps

import socketio
from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from .services import get_runner_service
from .services.infra.desktop_tunnels import push_runner_ws_closed, push_runner_ws_frame
from .services.infra.realtime import (
    drop_frontend,
    get_sio_server,
    subscribe_frontend,
    unsubscribe_frontend,
)
from .services.storage import StorageService

logger = logging.getLogger(__name__)


@sync_to_async
def _frontend_user_can_access_workspace(user_id: int, workspace_id: str) -> bool:
    """Return whether a frontend user may access a workspace.

    Same rule as the REST API: members may only access their own workspaces.
    """
    return get_runner_service().can_user_access_workspace(user_id, workspace_id)


async def _ensure_frontend_workspace_access(
    sio: socketio.AsyncServer,
    sid: str,
    workspace_id: str | None,
) -> bool:
    """Validate that the connected frontend user can access *workspace_id*."""
    if not workspace_id:
        return False

    session = await sio.get_session(sid, namespace="/frontend")
    user_id = session.get("user_id") if session else None
    if not user_id:
        logger.warning(
            "Frontend event rejected: no user in session (sid=%s)",
            sid,
        )
        return False

    if not await _frontend_user_can_access_workspace(int(user_id), workspace_id):
        logger.warning(
            "Frontend event rejected: unauthorized workspace access "
            "(sid=%s, user_id=%s, workspace_id=%s)",
            sid,
            user_id,
            workspace_id,
        )
        return False

    return True


def create_sio_app() -> socketio.ASGIApp:
    """
    Create the Socket.IO ASGI application.

    Called from config/asgi.py to mount the Socket.IO server. Attaches the
    runner and frontend event handlers to the shared server singleton.
    """
    sio = get_sio_server()
    _register_event_handlers(sio)
    _register_frontend_handlers(sio)
    return socketio.ASGIApp(
        sio,
        socketio_path="/ws/runner",
    )


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


async def _require_runner_id(
    sio: socketio.AsyncServer, sid: str, event: str
) -> str | None:
    """Return the runner_id from *sid*'s session, or warn and return None.

    All runner→backend event handlers call this so that events from
    sessions that were never properly authenticated are silently dropped.
    """
    session = await sio.get_session(sid)
    runner_id = session.get("runner_id") if session else None
    if not runner_id:
        logger.warning("%s from unauthenticated session (sid=%s)", event, sid)
    if runner_id:
        current = await sync_to_async(get_runner_service().is_current_runner_session)(
            runner_id, sid, require_online=event != "runner:heartbeat"
        )
        if not current:
            return None
    return runner_id


def _register_event_handlers(sio: socketio.AsyncServer) -> None:
    """Register all Socket.IO event handlers."""

    @sio.on("runner:inventory")
    async def runner_inventory(sid: str, data: dict):
        runner_id = await _require_runner_id(sio, sid, "runner:inventory")
        if runner_id:
            await sync_to_async(StorageService.record_inventory)(runner_id, sid, data)

    @sio.on("operation:heartbeat")
    async def operation_heartbeat(sid: str, data: dict):
        runner_id = await _require_runner_id(sio, sid, "operation:heartbeat")
        if not runner_id:
            return
        await sync_to_async(get_runner_service().record_operation_heartbeat)(
            runner_id, data
        )

    @sio.on("operation:inspection")
    async def operation_inspection(sid: str, evidence: dict):
        """Reconcile terminal outcomes and safely close verified interruptions."""
        from .services.disposition import DispositionService

        runner_id = await _require_runner_id(sio, sid, "operation:inspection")
        if not runner_id:
            return
        await sync_to_async(DispositionService.recover_inspection)(
            get_runner_service(), runner_id, sid, evidence
        )

    @sio.on("operation:result")
    async def operation_result(sid: str, packet: dict):
        runner_id = await _require_runner_id(sio, sid, "operation:result")
        if not runner_id:
            return
        data = packet.get("data", {})
        accepted = await sync_to_async(get_runner_service().apply_operation_result)(
            runner_id, packet.get("event", ""), data
        )
        if accepted:
            await sio.emit(
                "operation:ack",
                {
                    k: data[k]
                    for k in ("operation_id", "attempt", "target", "runner_id")
                },
                to=sid,
            )

    @sio.event
    async def connect(sid: str, environ: dict, auth: dict | None = None):
        """
        Handle runner connection.

        Authenticate using the Bearer token from the Authorization header.
        """
        service = get_runner_service()

        # Accept the Authorization header or Socket.IO auth payload.
        token = _extract_bearer_token(environ)
        if not token and isinstance(auth, dict):
            candidate = str(auth.get("token") or "").strip()
            if candidate:
                token = candidate
        if not token:
            logger.warning("Connection rejected: no Bearer token (sid=%s)", sid)
            raise socketio.exceptions.ConnectionRefusedError(
                "Missing Authorization header"
            )

        try:
            runner = await sync_to_async(service.authenticate_runner)(token)
        except Exception:
            logger.warning("Connection rejected: invalid token (sid=%s)", sid)
            raise socketio.exceptions.ConnectionRefusedError("Invalid API token")

        await sync_to_async(service.register_runner)(runner, sid=sid)
        # Store runner_id in the session for later lookups
        await sio.save_session(sid, {"runner_id": str(runner.id)})
        logger.info("Runner connected: %s (sid=%s)", runner.id, sid)

    @sio.event
    async def disconnect(sid: str):
        """Handle runner disconnection."""
        service = get_runner_service()
        await sync_to_async(service.unregister_runner)(sid)
        logger.info("Runner disconnected (sid=%s)", sid)

    # --- Runner → Backend events ---

    @sio.on("runner:register")
    async def on_runner_register(sid: str, data: dict):
        """Handle runner registration with runtime capabilities."""
        service = get_runner_service()
        if not await _require_runner_id(sio, sid, "runner:register"):
            return
        session = await sio.get_session(sid)
        runner_id = session.get("runner_id")

        if not runner_id:
            logger.warning("runner:register from unknown session (sid=%s)", sid)
            return

        runner = await sync_to_async(service.get_runner_or_none)(uuid.UUID(runner_id))
        if runner is None:
            logger.warning(
                "runner:register for deleted runner %s (sid=%s)",
                runner_id,
                sid,
            )
            return

        await sync_to_async(service.register_runner)(
            runner,
            sid=sid,
            desktop_epoch=(data.get("desktop_leases") or {}).get("epoch"),
        )

        # DB worker owns lifecycle delivery. Result replay from runner runs
        # before its first heartbeat; reconnect never authorizes cleanup.

    @sio.on("workspace:error")
    async def on_workspace_error(sid: str, data: dict):
        """Handle direct RPC errors; lifecycle failures require operation:result."""
        runner_id = await _require_runner_id(sio, sid, "workspace:error")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_workspace_rpc_error)(
            task_id=data["task_id"],
            error=data["error"],
            runner_id=runner_id,
        )

    # RPC replies stay inside the backend; stream replies also ACK acceptance.
    def register_reply(event: str, method: str, *, ack: bool = False) -> None:
        @sio.on(event)
        async def on_reply(sid: str, data: dict) -> dict | None:
            runner_id = await _require_runner_id(sio, sid, event)
            if not runner_id:
                return {"ok": False, "error": "unauthenticated"} if ack else None
            accepted = await sync_to_async(getattr(get_runner_service(), method))(
                event, data, runner_id=runner_id
            )
            return {"ok": bool(accepted)} if ack else None

    for method, events in (
        (
            "handle_harness_reply",
            (
                "harness:exec_wait_result",
                "harness:read_file_result",
                "harness:read_file_chunk",
                "harness:write_file_result",
                "harness:list_result",
                "harness:desktop_action_result",
                "workspace:artifact_ensure_result",
            ),
        ),
        ("handle_git_reply", ("git:operation_result",)),
        (
            "handle_process_reply",
            (
                "harness:process_start_result",
                "harness:process_list_result",
                "harness:process_get_result",
                "harness:process_stop_result",
                "harness:process_verify_result",
            ),
        ),
    ):
        for event in events:
            register_reply(event, method)
    for event in ("workspace:stream_output", "workspace:stream_closed"):
        register_reply(event, "handle_stream_reply", ack=True)

    # --- Terminal events from runner ---

    @sio.on("terminal:started")
    async def on_terminal_started(sid: str, data: dict):
        """Handle terminal:started event from runner."""
        runner_id = await _require_runner_id(sio, sid, "terminal:started")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_terminal_started(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            terminal_id=data["terminal_id"],
            runner_id=runner_id,
        )

    @sio.on("terminal:output")
    async def on_terminal_output(sid: str, data: dict):
        """Handle terminal:output event from runner — forward to frontend."""
        runner_id = await _require_runner_id(sio, sid, "terminal:output")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_terminal_output(
            workspace_id=data["workspace_id"],
            terminal_id=data["terminal_id"],
            data=data["data"],
            runner_id=runner_id,
        )

    @sio.on("terminal:closed")
    async def on_terminal_closed(sid: str, data: dict):
        """Handle terminal:closed event from runner."""
        runner_id = await _require_runner_id(sio, sid, "terminal:closed")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_terminal_closed(
            workspace_id=data["workspace_id"],
            terminal_id=data["terminal_id"],
            runner_id=runner_id,
        )

    # --- Desktop session events from runner ---

    @sio.on("desktop:started")
    async def on_desktop_started(sid: str, data: dict):
        """Handle desktop:started event from runner."""
        runner_id = await _require_runner_id(sio, sid, "desktop:started")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_desktop_started(
            task_id=data.get("task_id"),
            workspace_id=data["workspace_id"],
            port=data["port"],
            guest_ip=data["guest_ip"],
            runner_id=runner_id,
            viewer=data["viewer"],
            computer_use=data["computer_use"],
            mcp=data["mcp"],
            holder_count=data["holder_count"],
            generation=data["generation"],
            epoch=data["epoch"],
        )

    @sio.on("desktop:process")
    async def on_desktop_process(sid: str, data: dict):
        """Handle desktop process announcement used for proxy routing."""
        runner_id = await _require_runner_id(sio, sid, "desktop:process")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_desktop_process(
            workspace_id=data["workspace_id"],
            port=data["port"],
            guest_ip=data["guest_ip"],
            runner_id=runner_id,
            viewer=data["viewer"],
            computer_use=data["computer_use"],
            mcp=data["mcp"],
            holder_count=data["holder_count"],
            generation=data["generation"],
            epoch=data["epoch"],
        )

    @sio.on("desktop:stopped")
    async def on_desktop_stopped(sid: str, data: dict):
        """Handle desktop:stopped event from runner."""
        runner_id = await _require_runner_id(sio, sid, "desktop:stopped")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_desktop_stopped(
            task_id=data.get("task_id"),
            workspace_id=data["workspace_id"],
            runner_id=runner_id,
            generation=data["generation"],
            epoch=data["epoch"],
        )

    @sio.on("desktop:viewer_released")
    async def on_desktop_viewer_released(sid: str, data: dict):
        """Handle viewer lease release while computer-use still holds the display."""
        runner_id = await _require_runner_id(sio, sid, "desktop:viewer_released")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_desktop_viewer_released(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            runner_id=runner_id,
            computer_use_active=data["computer_use"],
            viewer_held=data["viewer"],
            mcp=data["mcp"],
            holder_count=data["holder_count"],
            generation=data["generation"],
            epoch=data["epoch"],
        )

    @sio.on("desktop:proxy_ws_frame")
    async def on_desktop_proxy_ws_frame(sid: str, data: dict):
        """Forward runner-originated desktop WebSocket frames to the proxy app."""
        runner_id = await _require_runner_id(sio, sid, "desktop:proxy_ws_frame")
        if not runner_id:
            return

        await push_runner_ws_frame(
            data["tunnel_id"],
            runner_id,
            text=data.get("text"),
            data=data.get("data"),
            encoding=data.get("encoding"),
        )

    @sio.on("desktop:proxy_ws_closed")
    async def on_desktop_proxy_ws_closed(sid: str, data: dict):
        """Forward runner-originated desktop tunnel close notifications."""
        runner_id = await _require_runner_id(sio, sid, "desktop:proxy_ws_closed")
        if not runner_id:
            return

        await push_runner_ws_closed(
            data["tunnel_id"],
            runner_id,
            code=int(data.get("code", 1000)),
        )

    # --- File explorer events from runner ---

    @sio.on("files:list_result")
    async def on_files_list_result(sid: str, data: dict):
        """Forward files:list_result from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:list_result")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result(
            "files:list_result", data, runner_id=runner_id
        )

    @sio.on("files:find_result")
    async def on_files_find_result(sid: str, data: dict):
        """Forward files:find_result from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:find_result")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result(
            "files:find_result", data, runner_id=runner_id
        )

    @sio.on("files:content_result")
    async def on_files_content_result(sid: str, data: dict):
        """Forward files:content_result from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:content_result")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result(
            "files:content_result", data, runner_id=runner_id
        )

    @sio.on("files:content_chunk")
    async def on_files_content_chunk(sid: str, data: dict):
        """Forward one files:content_chunk from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:content_chunk")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result(
            "files:content_chunk", data, runner_id=runner_id
        )

    @sio.on("files:upload_result")
    async def on_files_upload_result(sid: str, data: dict):
        """Forward files:upload_result from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:upload_result")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result(
            "files:upload_result", data, runner_id=runner_id
        )

    @sio.on("files:download_result")
    async def on_files_download_result(sid: str, data: dict):
        """Forward files:download_result from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:download_result")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result(
            "files:download_result", data, runner_id=runner_id
        )

    @sio.on("files:download_chunk")
    async def on_files_download_chunk(sid: str, data: dict):
        """Forward one files:download_chunk from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:download_chunk")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result(
            "files:download_chunk", data, runner_id=runner_id
        )

    # --- Image build progress from runner ---

    @sio.on("image:build_progress")
    async def on_image_build_progress(sid: str, data: dict):
        """Handle streamed image build logs from runner."""
        runner_id = await _require_runner_id(sio, sid, "image:build_progress")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_image_build_progress)(
            build_job_id=data.get("build_job_id", ""),
            line=data.get("line", ""),
            task_id=data.get("task_id", ""),
            runner_id=runner_id,
        )

    @sio.on("runner:system_metrics")
    async def on_runner_system_metrics(sid: str, data: dict):
        """Persist host system metrics reported by a runner once per minute."""
        service = get_runner_service()
        if not await _require_runner_id(sio, sid, "runner:system_metrics"):
            return
        session = await sio.get_session(sid)
        runner_id = session.get("runner_id")

        if not runner_id:
            logger.warning("runner:system_metrics from unknown session (sid=%s)", sid)
            return

        runner = await sync_to_async(service.get_runner_or_none)(uuid.UUID(runner_id))
        if runner is None:
            logger.warning(
                "runner:system_metrics for deleted runner %s (sid=%s)",
                runner_id,
                sid,
            )
            return

        try:
            await sync_to_async(service.record_runner_system_metrics)(runner, data)
            logger.debug(
                "System metrics stored for runner %s: cpu=%.1f%%",
                runner_id,
                data.get("cpu_usage_percent", 0.0),
            )
        except Exception:
            logger.exception("Failed to store system metrics for runner %s", runner_id)

    @sio.on("runner:heartbeat")
    async def on_runner_heartbeat(sid: str, data: dict):
        """Handle periodic heartbeat from a runner.

        Reconciles workspace states between the runner's actual
        container states and the backend's records.
        """
        service = get_runner_service()
        if not await _require_runner_id(sio, sid, "runner:heartbeat"):
            return
        session = await sio.get_session(sid)
        runner_id = session.get("runner_id")

        if not runner_id:
            logger.warning("runner:heartbeat from unknown session (sid=%s)", sid)
            return

        runner = await sync_to_async(service.get_runner_or_none)(uuid.UUID(runner_id))
        if runner is None:
            logger.warning(
                "runner:heartbeat for deleted runner %s (sid=%s)",
                runner_id,
                sid,
            )
            return

        # Refresh transport liveness before potentially slow reconciliation.
        await sync_to_async(service.record_runner_heartbeat)(runner)
        if data.get("workspace_states_observed") is False:
            return

        credential_sync_ids = await sync_to_async(service.handle_heartbeat)(
            runner=runner,
            workspaces=data.get("workspaces", []),
        )
        # Async verify pass for heartbeat-vanished process rows: the
        # sync heartbeat only stashes candidates (no RPCs allowed
        # there); this resolves them against the runner — live rows
        # reattach as RUNNING, confirmed-gone rows become EXITED,
        # unverifiable rows stay RUNNING.
        try:
            await service.reconcile_vanished_processes(runner)
        except Exception:
            logger.exception(
                "reconcile_vanished_processes failed for runner %s",
                runner_id,
            )
        if credential_sync_ids:
            await service.dispatch_credential_reconcile(credential_sync_ids)


def _extract_bearer_token(environ: dict) -> str | None:
    """Extract the Bearer token from the Authorization header."""
    auth_header = environ.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


# ---------------------------------------------------------------------------
# Frontend namespace (/frontend)
# ---------------------------------------------------------------------------


def _frontend_conflict_response(
    sio: socketio.AsyncServer, event: str, **defaults: object
) -> Callable:
    """Translate conflicts into one requester-scoped protocol completion.

    The handler retains its authorization check before service dispatch. Read and
    chunked upload errors use the runner's content_result and upload_result events
    to settle existing client waiters, without duplicate completion callbacks.
    """

    def decorate(handler: Callable[..., Awaitable]) -> Callable:
        @wraps(handler)
        async def wrapped(sid: str, data: dict) -> object:
            try:
                return await handler(sid, data)
            except ConflictError as exc:
                payload = {
                    **defaults,
                    **{
                        key: data[key]
                        for key in (
                            "workspace_id",
                            "request_id",
                            "upload_id",
                            "terminal_id",
                            "path",
                            "query",
                        )
                        if key in data
                    },
                    "error": exc.message,
                    "code": exc.code,
                }
                await sio.emit(event, payload, to=sid, namespace="/frontend")
                return None

        return wrapped

    return decorate


def _register_frontend_handlers(sio: socketio.AsyncServer) -> None:
    """Register Socket.IO event handlers for the /frontend namespace."""

    @sio.on("connect", namespace="/frontend")
    async def frontend_connect(sid: str, environ: dict, auth: dict | None = None):
        """
        Handle frontend client connection with JWT authentication.

        The frontend sends { token: "<JWT>" } in the auth dict.
        The token is validated against the configured auth backend.
        """
        token = None
        if auth and isinstance(auth, dict):
            token = auth.get("token")

        if not token:
            logger.warning("Frontend connection rejected: no token (sid=%s)", sid)
            raise ConnectionRefusedError("Authentication required")

        try:
            from apps.accounts.auth_backends import get_auth_backend

            backend = get_auth_backend()
            user = await sync_to_async(backend.validate_access_token)(token)
            await sio.save_session(
                sid,
                {"user_id": str(user.id), "email": user.email},
                namespace="/frontend",
            )
            logger.info("Frontend client connected (sid=%s, user=%s)", sid, user.email)
        except Exception as exc:
            logger.warning(
                "Frontend connection rejected: invalid token (sid=%s, error=%s)",
                sid,
                exc,
            )
            raise ConnectionRefusedError("Invalid or expired token")

    @sio.on("disconnect", namespace="/frontend")
    async def frontend_disconnect(sid: str):
        """Clean up subscriptions when a frontend client disconnects."""
        drop_frontend(sid)
        logger.info("Frontend client disconnected (sid=%s)", sid)

    @sio.on("frontend:subscribe_workspace", namespace="/frontend")
    async def on_subscribe_workspace(sid: str, data: dict):
        """Subscribe a frontend client to events for a specific workspace."""
        workspace_id = data.get("workspace_id")
        if not workspace_id or not await _ensure_frontend_workspace_access(
            sio, sid, workspace_id
        ):
            return

        subscribe_frontend(sid, workspace_id)
        logger.debug("Frontend %s subscribed to workspace %s", sid, workspace_id)

    @sio.on("frontend:unsubscribe_workspace", namespace="/frontend")
    async def on_unsubscribe_workspace(sid: str, data: dict):
        """Unsubscribe a frontend client from workspace events."""
        workspace_id = data.get("workspace_id")
        if not workspace_id:
            return

        if not await _ensure_frontend_workspace_access(sio, sid, workspace_id):
            return

        unsubscribe_frontend(sid, workspace_id)
        logger.debug("Frontend %s unsubscribed from workspace %s", sid, workspace_id)

    # --- Terminal events from frontend ---

    @sio.on("frontend:terminal_input", namespace="/frontend")
    @_frontend_conflict_response(sio, "terminal:error")
    async def on_frontend_terminal_input(sid: str, data: dict):
        """Forward terminal input from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_terminal_input(
            workspace_id=data["workspace_id"],
            terminal_id=data["terminal_id"],
            data=data["data"],
        )

    @sio.on("frontend:terminal_resize", namespace="/frontend")
    @_frontend_conflict_response(sio, "terminal:error")
    async def on_frontend_terminal_resize(sid: str, data: dict):
        """Forward terminal resize from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_terminal_resize(
            workspace_id=data["workspace_id"],
            terminal_id=data["terminal_id"],
            cols=data.get("cols", 80),
            rows=data.get("rows", 24),
        )

    @sio.on("frontend:terminal_close", namespace="/frontend")
    @_frontend_conflict_response(sio, "terminal:error")
    async def on_frontend_terminal_close(sid: str, data: dict):
        """Forward terminal close from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_terminal_close(
            workspace_id=data["workspace_id"],
            terminal_id=data["terminal_id"],
        )

    # --- File explorer events from frontend ---

    @sio.on("frontend:files_list", namespace="/frontend")
    @_frontend_conflict_response(
        sio, "files:list_result", entries=[], path="/workspace", request_id=""
    )
    async def on_frontend_files_list(sid: str, data: dict):
        """Forward file list request from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_files_event(
            workspace_id=data["workspace_id"],
            event="files:list",
            data=data,
        )

    @sio.on("frontend:files_find", namespace="/frontend")
    @_frontend_conflict_response(
        sio, "files:find_result", paths=[], truncated=False, query="", request_id=""
    )
    async def on_frontend_files_find(sid: str, data: dict):
        """Forward workspace file search from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_files_event(
            workspace_id=data["workspace_id"],
            event="files:find",
            data=data,
        )

    @sio.on("frontend:files_read", namespace="/frontend")
    @_frontend_conflict_response(
        sio,
        "files:content_result",
        content="",
        size=0,
        truncated=False,
        path="/workspace",
        request_id="",
    )
    async def on_frontend_files_read(sid: str, data: dict):
        """Forward file read request from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_files_event(
            workspace_id=data["workspace_id"],
            event="files:read",
            data=data,
        )

    @sio.on("frontend:files_upload_start", namespace="/frontend")
    @_frontend_conflict_response(
        sio, "files:upload_result", status="error", path="/workspace", request_id=""
    )
    async def on_frontend_files_upload_start(sid: str, data: dict):
        """Forward a chunked upload start from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_files_event(
            workspace_id=data["workspace_id"],
            event="files:upload_start",
            data=data,
        )

    @sio.on("frontend:files_upload_chunk", namespace="/frontend")
    @_frontend_conflict_response(
        sio, "files:upload_result", status="error", path="/workspace", request_id=""
    )
    async def on_frontend_files_upload_chunk(sid: str, data: dict):
        """Forward one upload chunk from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_files_event(
            workspace_id=data["workspace_id"],
            event="files:upload_chunk",
            data=data,
        )

    @sio.on("frontend:files_upload_finish", namespace="/frontend")
    @_frontend_conflict_response(
        sio, "files:upload_result", status="error", path="/workspace", request_id=""
    )
    async def on_frontend_files_upload_finish(sid: str, data: dict):
        """Forward an upload finish marker from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_files_event(
            workspace_id=data["workspace_id"],
            event="files:upload_finish",
            data=data,
        )

    @sio.on("frontend:files_download", namespace="/frontend")
    @_frontend_conflict_response(
        sio,
        "files:download_result",
        content="",
        filename="",
        is_archive=False,
        size=0,
        path="/workspace",
        request_id="",
    )
    async def on_frontend_files_download(sid: str, data: dict):
        """Forward file download request from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_files_event(
            workspace_id=data["workspace_id"],
            event="files:download",
            data=data,
        )
