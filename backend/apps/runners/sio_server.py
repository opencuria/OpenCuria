"""
Socket.IO server for runner and frontend communication.

This module creates and configures the python-socketio AsyncServer that
handles WebSocket connections from runners and frontend clients. It serves
as the thin adapter between the Socket.IO protocol and the RunnerService
business logic.

The server is mounted as an ASGI app in config/asgi.py.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict

import socketio
from asgiref.sync import sync_to_async

from common.utils import hash_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton instances
# ---------------------------------------------------------------------------

# The Socket.IO server — created once, shared across the app
_sio: socketio.AsyncServer | None = None

# The RunnerService instance — created lazily
_runner_service = None

# Frontend subscription tracking: workspace_id -> set of frontend sids
_frontend_subscriptions: dict[str, set[str]] = defaultdict(set)
# Reverse mapping: frontend sid -> set of workspace_ids
_frontend_sid_workspaces: dict[str, set[str]] = defaultdict(set)


@sync_to_async
def _frontend_user_can_access_workspace(user_id: int, workspace_id: str) -> bool:
    """Return whether a frontend user may access a workspace.

    Access rules mirror REST API behavior:
    - user must be member of the workspace runner's organization
    - every user may only access their own workspaces
    """
    from apps.organizations.models import Membership
    from .repositories import WorkspaceRepository

    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except (TypeError, ValueError):
        return False

    workspace = WorkspaceRepository.get_by_id(workspace_uuid)
    if workspace is None:
        return False

    membership = Membership.objects.filter(
        user_id=user_id,
        organization_id=workspace.runner.organization_id,
    ).first()
    if membership is None:
        return False

    return workspace.created_by_id == user_id


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


def get_sio_server() -> socketio.AsyncServer:
    """Get or create the Socket.IO server singleton."""
    global _sio
    if _sio is None:
        from django.conf import settings as django_settings

        cors_origins = getattr(django_settings, "SIO_CORS_ALLOWED_ORIGINS", "*")
        _sio = socketio.AsyncServer(
            async_mode="asgi",
            cors_allowed_origins=cors_origins,
            logger=False,
            engineio_logger=False,
            # Allow large file transfers (up to 200 MB) so that video files
            # can be read from workspace containers and forwarded to the
            # frontend.  The engine.io default of 1 MB is too small for any
            # binary file payload sent as base64 over Socket.IO.
            max_http_buffer_size=200 * 1024 * 1024,
        )
        _register_event_handlers(_sio)
        _register_frontend_handlers(_sio)
    return _sio


def get_runner_service():
    """Get or create the RunnerService singleton."""
    global _runner_service
    if _runner_service is None:
        from .services import RunnerService

        _runner_service = RunnerService(sio_server=get_sio_server())
    return _runner_service


def create_sio_app() -> socketio.ASGIApp:
    """
    Create the Socket.IO ASGI application.

    Called from config/asgi.py to mount the Socket.IO server.
    """
    sio = get_sio_server()
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
        logger.warning(
            "%s from unauthenticated session (sid=%s)", event, sid
        )
    return runner_id


def _register_event_handlers(sio: socketio.AsyncServer) -> None:
    """Register all Socket.IO event handlers."""

    @sio.event
    async def connect(sid: str, environ: dict, auth: dict | None = None):
        """
        Handle runner connection.

        Authenticate using the Bearer token from the Authorization header.
        """
        service = get_runner_service()

        # Extract token from Authorization header
        token = _extract_bearer_token(environ)
        if not token:
            logger.warning("Connection rejected: no Bearer token (sid=%s)", sid)
            raise socketio.exceptions.ConnectionRefusedError(
                "Missing Authorization header"
            )

        try:
            runner = await sync_to_async(service.authenticate_runner)(token)
        except Exception:
            logger.warning("Connection rejected: invalid token (sid=%s)", sid)
            raise socketio.exceptions.ConnectionRefusedError(
                "Invalid API token"
            )

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
        session = await sio.get_session(sid)
        runner_id = session.get("runner_id")

        if not runner_id:
            logger.warning("runner:register from unknown session (sid=%s)", sid)
            return

        from .repositories import RunnerRepository
        import uuid

        runner = await sync_to_async(RunnerRepository.get_by_id)(uuid.UUID(runner_id))
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
            available_runtimes=data.get("supported_runtimes", ["docker"]),
        )

        # Dispatch any image builds that were created while the runner
        # was offline (e.g. during bootstrap).
        runner = await sync_to_async(RunnerRepository.get_by_id)(uuid.UUID(runner_id))
        if runner is not None:
            await service.dispatch_pending_image_builds(runner)

    @sio.on("workspace:created")
    async def on_workspace_created(sid: str, data: dict):
        """Handle workspace creation confirmation from runner."""
        runner_id = await _require_runner_id(sio, sid, "workspace:created")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_workspace_created)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            status=data.get("status", "created"),
            runner_id=runner_id,
        )

    @sio.on("workspace:stopped")
    async def on_workspace_stopped(sid: str, data: dict):
        """Handle workspace stop confirmation from runner."""
        runner_id = await _require_runner_id(sio, sid, "workspace:stopped")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_workspace_stopped)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            runner_id=runner_id,
        )

    @sio.on("workspace:resumed")
    async def on_workspace_resumed(sid: str, data: dict):
        """Handle workspace resume confirmation from runner."""
        runner_id = await _require_runner_id(sio, sid, "workspace:resumed")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_workspace_resumed)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            runner_id=runner_id,
        )

    @sio.on("workspace:updated")
    async def on_workspace_updated(sid: str, data: dict):
        """Handle workspace resource update confirmation from runner."""
        runner_id = await _require_runner_id(sio, sid, "workspace:updated")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_workspace_updated)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            runner_id=runner_id,
        )

    @sio.on("workspace:error")
    async def on_workspace_error(sid: str, data: dict):
        """Handle workspace error from runner."""
        runner_id = await _require_runner_id(sio, sid, "workspace:error")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_workspace_error)(
            task_id=data["task_id"],
            error=data["error"],
            runner_id=runner_id,
        )

    @sio.on("workspace:removed")
    async def on_workspace_removed(sid: str, data: dict):
        """Handle workspace removal confirmation from runner."""
        runner_id = await _require_runner_id(sio, sid, "workspace:removed")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_workspace_removed)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            runner_id=runner_id,
        )

    @sio.on("workspace:cleanup_unknown_done")
    async def on_workspace_cleanup_unknown_done(sid: str, data: dict):
        """Handle successful unknown-workspace cleanup reported by runner."""
        service = get_runner_service()
        session = await sio.get_session(sid)
        runner_id = session.get("runner_id")
        if not runner_id:
            logger.warning(
                "workspace:cleanup_unknown_done from unknown session (sid=%s)",
                sid,
            )
            return

        from .repositories import RunnerRepository
        import uuid as _uuid

        runner = await sync_to_async(RunnerRepository.get_by_id)(
            _uuid.UUID(runner_id)
        )
        if runner is None:
            logger.warning(
                "workspace:cleanup_unknown_done for deleted runner %s (sid=%s)",
                runner_id,
                sid,
            )
            return

        await sync_to_async(service.handle_unknown_workspace_cleanup_result)(
            runner=runner,
            workspace_id=data.get("workspace_id", ""),
            cleaned=bool(data.get("cleaned", False)),
        )

    @sio.on("workspace:cleanup_unknown_failed")
    async def on_workspace_cleanup_unknown_failed(sid: str, data: dict):
        """Handle failed unknown-workspace cleanup reported by runner."""
        service = get_runner_service()
        session = await sio.get_session(sid)
        runner_id = session.get("runner_id")
        if not runner_id:
            logger.warning(
                "workspace:cleanup_unknown_failed from unknown session (sid=%s)",
                sid,
            )
            return

        from .repositories import RunnerRepository
        import uuid as _uuid

        runner = await sync_to_async(RunnerRepository.get_by_id)(
            _uuid.UUID(runner_id)
        )
        if runner is None:
            logger.warning(
                "workspace:cleanup_unknown_failed for deleted runner %s (sid=%s)",
                runner_id,
                sid,
            )
            return

        await sync_to_async(service.handle_unknown_workspace_cleanup_result)(
            runner=runner,
            workspace_id=data.get("workspace_id", ""),
            cleaned=False,
            error=data.get("error", "Unknown cleanup error"),
        )

    @sio.on("output:chunk")
    async def on_output_chunk(sid: str, data: dict):
        """Handle streaming output chunk from runner."""
        runner_id = await _require_runner_id(sio, sid, "output:chunk")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_output_chunk)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            line=data["line"],
            runner_id=runner_id,
        )

    @sio.on("output:status")
    async def on_output_status(sid: str, data: dict):
        """Handle runner execution status updates for the active session."""
        runner_id = await _require_runner_id(sio, sid, "output:status")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_output_status)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            status=data["status"],
            detail=data["detail"],
            runner_id=runner_id,
        )

    @sio.on("output:complete")
    async def on_output_complete(sid: str, data: dict):
        """Handle prompt completion from runner."""
        runner_id = await _require_runner_id(sio, sid, "output:complete")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_output_complete)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            runner_id=runner_id,
        )

    @sio.on("output:error")
    async def on_output_error(sid: str, data: dict):
        """Handle prompt error from runner."""
        runner_id = await _require_runner_id(sio, sid, "output:error")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_output_error)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            error=data["error"],
            runner_id=runner_id,
        )

    @sio.on("prompt:cancelled")
    async def on_prompt_cancelled(sid: str, data: dict):
        """Handle cancellation confirmation for a prompt task."""
        runner_id = await _require_runner_id(sio, sid, "prompt:cancelled")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_prompt_cancelled)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            target_task_id=data.get("target_task_id", ""),
            runner_id=runner_id,
        )

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

    # --- File explorer events from runner ---

    @sio.on("files:list_result")
    async def on_files_list_result(sid: str, data: dict):
        """Forward files:list_result from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:list_result")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result("files:list_result", data, runner_id=runner_id)

    @sio.on("files:content_result")
    async def on_files_content_result(sid: str, data: dict):
        """Forward files:content_result from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:content_result")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result("files:content_result", data, runner_id=runner_id)

    @sio.on("files:upload_result")
    async def on_files_upload_result(sid: str, data: dict):
        """Forward files:upload_result from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:upload_result")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result("files:upload_result", data, runner_id=runner_id)

    @sio.on("files:download_result")
    async def on_files_download_result(sid: str, data: dict):
        """Forward files:download_result from runner to frontend."""
        runner_id = await _require_runner_id(sio, sid, "files:download_result")
        if not runner_id:
            return
        service = get_runner_service()
        await service.handle_files_result("files:download_result", data, runner_id=runner_id)

    # --- Image artifact events from runner ---

    @sio.on("image_artifact:created")
    async def on_image_artifact_created(sid: str, data: dict):
        """Handle image_artifact:created event from runner."""
        runner_id = await _require_runner_id(sio, sid, "image_artifact:created")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_image_artifact_created)(
            task_id=data["task_id"],
            workspace_id=data["workspace_id"],
            artifact_id=data["image_artifact_id"],
            name=data.get("name", ""),
            size_bytes=data.get("size_bytes", 0),
            runner_id=runner_id,
        )

    @sio.on("image_artifact:deleted")
    async def on_image_artifact_deleted(sid: str, data: dict):
        """Handle image_artifact:deleted confirmation from runner."""
        runner_id = await _require_runner_id(sio, sid, "image_artifact:deleted")
        if not runner_id:
            return
        logger.info(
            "Image artifact deleted on runner %s: workspace=%s, artifact=%s",
            runner_id,
            data.get("workspace_id"),
            data.get("image_artifact_id"),
        )

    @sio.on("image_artifact:failed")
    async def on_image_artifact_failed(sid: str, data: dict):
        """Handle image_artifact:failed by marking the pending artifact as failed."""
        runner_id = await _require_runner_id(sio, sid, "image_artifact:failed")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_image_artifact_failed)(
            task_id=data.get("task_id", ""),
            workspace_id=data.get("workspace_id", ""),
            error=data.get("error", ""),
            runner_id=runner_id,
        )

    @sio.on("image:build_progress")
    async def on_image_build_progress(sid: str, data: dict):
        """Handle streamed image build logs from runner."""
        runner_id = await _require_runner_id(sio, sid, "image:build_progress")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_image_build_progress)(
            runner_image_build_id=data.get("runner_image_build_id", ""),
            line=data.get("line", ""),
            runner_id=runner_id,
        )

    @sio.on("image:built")
    async def on_image_built(sid: str, data: dict):
        """Handle successful image build event."""
        runner_id = await _require_runner_id(sio, sid, "image:built")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_image_built)(
            task_id=data.get("task_id", ""),
            runner_image_build_id=data.get("runner_image_build_id", ""),
            image_tag=data.get("image_tag", ""),
            image_path=data.get("image_path", ""),
            runner_id=runner_id,
        )

    @sio.on("image:build_failed")
    async def on_image_build_failed(sid: str, data: dict):
        """Handle failed image build event."""
        runner_id = await _require_runner_id(sio, sid, "image:build_failed")
        if not runner_id:
            return
        service = get_runner_service()
        await sync_to_async(service.handle_image_build_failed)(
            task_id=data.get("task_id", ""),
            runner_image_build_id=data.get("runner_image_build_id", ""),
            error=data.get("error", ""),
            runner_id=runner_id,
        )

    @sio.on("runner:system_metrics")
    async def on_runner_system_metrics(sid: str, data: dict):
        """Persist host system metrics reported by a runner once per minute."""
        session = await sio.get_session(sid)
        runner_id = session.get("runner_id")

        if not runner_id:
            logger.warning("runner:system_metrics from unknown session (sid=%s)", sid)
            return

        from .repositories import RunnerRepository, RunnerSystemMetricsRepository
        from django.utils import timezone
        import uuid as _uuid

        runner = await sync_to_async(RunnerRepository.get_by_id)(
            _uuid.UUID(runner_id)
        )
        if runner is None:
            logger.warning(
                "runner:system_metrics for deleted runner %s (sid=%s)",
                runner_id,
                sid,
            )
            return

        try:
            ts_raw = data.get("timestamp")
            if ts_raw:
                from datetime import datetime, timezone as dt_tz
                timestamp = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            else:
                timestamp = timezone.now()

            await sync_to_async(RunnerSystemMetricsRepository.create)(
                runner=runner,
                timestamp=timestamp,
                cpu_usage_percent=float(data.get("cpu_usage_percent", 0.0)),
                ram_used_bytes=int(data.get("ram_used_bytes", 0)),
                ram_total_bytes=int(data.get("ram_total_bytes", 0)),
                disk_used_bytes=int(data.get("disk_used_bytes", 0)),
                disk_total_bytes=int(data.get("disk_total_bytes", 0)),
                vm_metrics=data.get("vm_metrics")
                if isinstance(data.get("vm_metrics"), dict)
                else None,
            )
            # Prune entries older than 24 h in the background
            await sync_to_async(RunnerSystemMetricsRepository.purge_old)(
                runner_id=runner.id
            )
            logger.debug(
                "System metrics stored for runner %s: cpu=%.1f%%",
                runner_id,
                data.get("cpu_usage_percent", 0.0),
            )
        except Exception:
            logger.exception(
                "Failed to store system metrics for runner %s", runner_id
            )

    @sio.on("runner:heartbeat")
    async def on_runner_heartbeat(sid: str, data: dict):
        """Handle periodic heartbeat from a runner.

        Reconciles workspace states between the runner's actual
        container states and the backend's records.
        """
        service = get_runner_service()
        session = await sio.get_session(sid)
        runner_id = session.get("runner_id")

        if not runner_id:
            logger.warning("runner:heartbeat from unknown session (sid=%s)", sid)
            return

        from .repositories import RunnerRepository
        import uuid as _uuid

        runner = await sync_to_async(RunnerRepository.get_by_id)(
            _uuid.UUID(runner_id)
        )
        if runner is None:
            logger.warning(
                "runner:heartbeat for deleted runner %s (sid=%s)",
                runner_id,
                sid,
            )
            return

        await sync_to_async(service.handle_heartbeat)(
            runner=runner,
            workspaces=data.get("workspaces", []),
        )


def _extract_bearer_token(environ: dict) -> str | None:
    """
    Extract the Bearer token from the ASGI/WSGI environ.

    Only the standard ``Authorization: Bearer <token>`` HTTP header is
    accepted.  Query-string tokens are intentionally NOT supported because
    they would appear in web-server access logs, browser history, and HTTP
    Referer headers, exposing a long-lived secret in cleartext.
    """
    auth_header = environ.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


# ---------------------------------------------------------------------------
# Frontend namespace (/frontend)
# ---------------------------------------------------------------------------


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
            await sio.save_session(sid, {"user_id": str(user.id), "email": user.email}, namespace="/frontend")
            logger.info("Frontend client connected (sid=%s, user=%s)", sid, user.email)
        except Exception as exc:
            logger.warning("Frontend connection rejected: invalid token (sid=%s, error=%s)", sid, exc)
            raise ConnectionRefusedError("Invalid or expired token")

    @sio.on("disconnect", namespace="/frontend")
    async def frontend_disconnect(sid: str):
        """Clean up subscriptions when a frontend client disconnects."""
        workspace_ids = _frontend_sid_workspaces.pop(sid, set())
        for ws_id in workspace_ids:
            _frontend_subscriptions[ws_id].discard(sid)
            if not _frontend_subscriptions[ws_id]:
                del _frontend_subscriptions[ws_id]
        logger.info("Frontend client disconnected (sid=%s)", sid)

    @sio.on("frontend:subscribe_workspace", namespace="/frontend")
    async def on_subscribe_workspace(sid: str, data: dict):
        """Subscribe a frontend client to events for a specific workspace."""
        workspace_id = data.get("workspace_id")
        if not workspace_id or not await _ensure_frontend_workspace_access(
            sio, sid, workspace_id
        ):
            return

        _frontend_subscriptions[workspace_id].add(sid)
        _frontend_sid_workspaces[sid].add(workspace_id)
        logger.debug(
            "Frontend %s subscribed to workspace %s", sid, workspace_id
        )

    @sio.on("frontend:unsubscribe_workspace", namespace="/frontend")
    async def on_unsubscribe_workspace(sid: str, data: dict):
        """Unsubscribe a frontend client from workspace events."""
        workspace_id = data.get("workspace_id")
        if not workspace_id:
            return

        if not await _ensure_frontend_workspace_access(sio, sid, workspace_id):
            return

        _frontend_subscriptions[workspace_id].discard(sid)
        if not _frontend_subscriptions[workspace_id]:
            del _frontend_subscriptions[workspace_id]
        _frontend_sid_workspaces[sid].discard(workspace_id)
        logger.debug(
            "Frontend %s unsubscribed from workspace %s", sid, workspace_id
        )

    # --- Terminal events from frontend ---

    @sio.on("frontend:terminal_input", namespace="/frontend")
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

    @sio.on("frontend:files_read", namespace="/frontend")
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

    @sio.on("frontend:files_upload", namespace="/frontend")
    async def on_frontend_files_upload(sid: str, data: dict):
        """Forward file upload from frontend to the runner."""
        if not await _ensure_frontend_workspace_access(
            sio, sid, data.get("workspace_id")
        ):
            return

        service = get_runner_service()
        await service.forward_files_event(
            workspace_id=data["workspace_id"],
            event="files:upload",
            data=data,
        )

    @sio.on("frontend:files_download", namespace="/frontend")
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


async def emit_to_frontend(
    event: str,
    data: dict,
    workspace_id: str,
) -> None:
    """
    Emit an event to all frontend clients subscribed to a workspace.

    This is called by the RunnerService when processing runner events
    to forward real-time updates to the frontend.
    """
    sio = get_sio_server()
    sids = _frontend_subscriptions.get(workspace_id, set())
    if not sids:
        return

    for sid in sids.copy():
        try:
            await sio.emit(event, data, to=sid, namespace="/frontend")
        except Exception:
            logger.warning(
                "Failed to emit %s to frontend sid %s", event, sid
            )
