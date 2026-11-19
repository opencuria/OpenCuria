"""Relay editor operations and file-explorer listings.

File contents move over HTTP (``apps.workspace_files``); this service only
relays listing/search requests and the revision-checked editor RPCs.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from common import realtime

if TYPE_CHECKING:
    from ...repositories import WorkspaceRepository
    from ..domains.workspace_lifecycle import WorkspaceLifecycleService
    from ..infra.ownership import OwnershipService
    from ..ports import RunnerTransport

logger = logging.getLogger(__name__)


class FileTransferService:
    """Stateless editor and file-listing relay with explicit collaborators."""

    def __init__(
        self,
        *,
        workspaces: type[WorkspaceRepository],
        ownership: OwnershipService,
        runner_transport: RunnerTransport,
        workspace_lifecycle: WorkspaceLifecycleService,
    ) -> None:
        """Compose this service without inheriting sibling behavior or state."""
        self.workspaces = workspaces
        self.ownership = ownership
        self.runner_transport = runner_transport
        self.workspace_lifecycle = workspace_lifecycle

    async def editor_file_operation(
        self,
        workspace_id: uuid.UUID,
        operation: str,
        payload: dict,
    ) -> dict:
        """Relay an owned editor read/save through the existing runner channel."""
        if operation not in {"read", "write"}:
            raise ValueError("Invalid editor operation")
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            from ...exceptions import WorkspaceNotFoundError

            raise WorkspaceNotFoundError(str(workspace_id))
        runner = self.ownership.ensure_git_dispatchable(workspace)
        result = await self.runner_transport.call_runner(
            runner,
            f"files:editor_{operation}",
            {**payload, "workspace_id": str(workspace_id)},
            timeout=60,
        )
        await self.workspace_lifecycle.touch_workspace_activity(workspace_id)
        return result

    async def forward_files_event(
        self,
        workspace_id: str,
        event: str,
        data: dict,
    ) -> None:
        """Forward a file explorer event from frontend to the runner.

        Looks up the workspace's runner and emits the event directly.
        When the runner is offline, a synthetic error result is sent back
        to the frontend so callers do not get stuck waiting indefinitely.
        """
        # Map request event → result event for error responses.
        _result_event: dict[str, str] = {
            "files:list": "files:list_result",
            "files:find": "files:find_result",
        }
        if event not in _result_event:
            raise ValueError(f"Unsupported file event: {event}")

        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        self.ownership.ensure_workspace_available(workspace)
        runner = workspace.runner
        if not runner.is_online:
            result_event = _result_event.get(event)
            if result_event:
                error_payload: dict = {
                    "workspace_id": workspace_id,
                    "request_id": data.get("request_id", ""),
                    "path": data.get("path", ""),
                    "error": "Runner is offline",
                }
                if result_event == "files:list_result":
                    error_payload["entries"] = []
                else:
                    error_payload["paths"] = []
                    error_payload["query"] = data.get("query", "")
                    error_payload["truncated"] = False
                await realtime.emit_to_frontend(
                    result_event, error_payload, workspace_id
                )
            return

        await self.runner_transport.emit_to_runner(runner, event, data)
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    # Runner→backend harness read chunks: dropped/invalid payloads are
    # logged and never routed. Bounds mirror the runner copy: at most
    # 256 KiB clean base64 per slice (whitespace normalized, JSON framing
    # lives outside content, so no slack applies), 1..560 slices per read.
    _CHUNK_B64_SIZE = 256 * 1024
    _READ_MAX_CHUNKS = 560

    @classmethod
    def validate_runner_file_chunk(cls, event: str, data: dict) -> bool:
        """Validate a runner-originated ``harness:read_file_chunk`` reply.

        Requires a dict payload, non-empty
        request/path strings, int index/total with total in 1..560 and
        index in range, non-empty string content whose whitespace-cleaned
        form fits exactly 256 KiB.
        """
        if event != "harness:read_file_chunk":
            return True
        if not isinstance(data, dict):
            return False
        request_id = data.get("request_id", "")
        path = data.get("path", "")
        if not isinstance(request_id, str) or not request_id.strip():
            return False
        if not isinstance(path, str) or not path.strip():
            return False
        try:
            index = int(data.get("index", -1))
            total = int(data.get("total_chunks", 0))
        except (TypeError, ValueError):
            return False
        # bool is an int subclass; reject it explicitly so True/False
        # can never pass as chunk counters.
        if isinstance(data.get("index"), bool) or isinstance(
            data.get("total_chunks"), bool
        ):
            return False
        if total < 1 or total > cls._READ_MAX_CHUNKS:
            return False
        if index < 0 or index >= total:
            return False
        content = data.get("content", "")
        if not isinstance(content, str):
            return False
        clean = "".join(content.split())
        if not clean:
            return False
        if len(clean) > cls._CHUNK_B64_SIZE:
            return False
        return True

    async def handle_files_result(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> None:
        """Forward a listing result from runner to subscribed frontends.

        Ownership stays fail-closed: a missing/invalid ``workspace_id``
        never bypasses the owner check, it drops the payload.
        """
        routed = self.runner_transport.route_harness_reply(
            event, data, runner_id=runner_id
        )
        if routed is not None:
            return
        workspace_id = data.get("workspace_id", "") if isinstance(data, dict) else ""
        # Ownership is fail-closed: with a runner claim present, only a
        # valid workspace_id owned by that runner may fan out. Missing or
        # malformed ids drop (they must never bypass ownership).
        if runner_id:
            try:
                workspace_uuid = uuid.UUID(workspace_id)
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "%s rejected: invalid workspace_id %s", event, workspace_id
                )
                return
            try:
                workspace = await sync_to_async(self.workspaces.get_by_id)(
                    workspace_uuid
                )
                if workspace is None or str(workspace.runner_id) != runner_id:
                    logger.warning(
                        "%s rejected: workspace %s does not belong to runner %s",
                        event,
                        workspace_id,
                        runner_id,
                    )
                    return
            except (ValueError, TypeError):
                logger.warning(
                    "%s rejected: invalid workspace_id %s", event, workspace_id
                )
                return
        if not self.validate_runner_file_chunk(event, data):
            logger.warning(
                "%s rejected: invalid chunk payload for workspace %s",
                event,
                workspace_id,
            )
            return

        await realtime.emit_to_frontend(event, data, workspace_id)
