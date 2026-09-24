"""File explorer passthrough (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.workspaces`` plus
sibling helpers (``_emit_to_runner`` / ``_route_harness_reply`` via
``RunnerTransportMixin``). The ``sio_server`` import stays lazy inside the
method bodies (deeper relative level ``...`` from ``domains/``).
"""

from __future__ import annotations

import logging
import uuid

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)


class FileTransferMixin:
    """Stateless file-explorer relay shared by RunnerService."""

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
        from ...sio_server import emit_to_frontend

        # Map request event → result event for error responses.
        _result_event: dict[str, str] = {
            "files:read": "files:content_result",
            "files:list": "files:list_result",
            "files:find": "files:find_result",
            "files:upload": "files:upload_result",
            "files:download": "files:download_result",
        }

        # Chunk uploads use a start/chunk/finish convention. The single
        # synthesized offline error per transfer fires only for an
        # offline-at-start upload (chunked start or legacy single-shot;
        # no finish will ever arrive). Intermediate chunks and the finish
        # marker never synthesize errors on their own. Untrusted chunk
        # metadata is validated before relay so oversized or malformed
        # transfers fail fast with a small error instead of reaching
        # the runner.

        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if event in ("files:upload_chunk", "files:upload_finish"):
            if not runner.is_online:
                # Terminal error was already synthesized for the start.
                return
            if not self._validate_upload_relay(event, data):
                await emit_to_frontend(
                    "files:upload_result",
                    {
                        "workspace_id": workspace_id,
                        "request_id": data.get("request_id", ""),
                        "path": data.get("path", ""),
                        "status": "error",
                        "error": "Invalid upload chunk",
                    },
                    workspace_id,
                )
                return
        if event == "files:upload" and data.get("chunked"):
            if not runner.is_online:
                await emit_to_frontend(
                    "files:upload_result",
                    {
                        "workspace_id": workspace_id,
                        "request_id": data.get("request_id", ""),
                        "path": data.get("path", ""),
                        "status": "error",
                        "error": "Runner is offline",
                    },
                    workspace_id,
                )
                return
            if not self._validate_upload_start(data):
                await emit_to_frontend(
                    "files:upload_result",
                    {
                        "workspace_id": workspace_id,
                        "request_id": data.get("request_id", ""),
                        "path": data.get("path", ""),
                        "status": "error",
                        "error": "Invalid upload start",
                    },
                    workspace_id,
                )
                return
        if not runner.is_online:
            result_event = _result_event.get(event)
            if result_event:
                error_payload: dict = {
                    "workspace_id": workspace_id,
                    "request_id": data.get("request_id", ""),
                    "path": data.get("path", ""),
                    "error": "Runner is offline",
                }
                if result_event == "files:content_result":
                    error_payload.update({"content": "", "size": 0, "truncated": False})
                elif result_event == "files:list_result":
                    error_payload["entries"] = []
                elif result_event == "files:find_result":
                    error_payload["paths"] = []
                    error_payload["query"] = data.get("query", "")
                    error_payload["truncated"] = False
                elif result_event == "files:upload_result":
                    error_payload["status"] = "error"
                elif result_event == "files:download_result":
                    error_payload.update(
                        {
                            "content": "",
                            "filename": "",
                            "is_archive": False,
                            "size": 0,
                        }
                    )
                await emit_to_frontend(result_event, error_payload, workspace_id)
            return

        await self._emit_to_runner(runner, event, data)
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    @staticmethod
    def _validate_upload_start(data: dict) -> bool:
        """Pre-validate an untrusted chunked upload start before relay."""
        try:
            total = int(data.get("total_chunks", 0))
        except (TypeError, ValueError):
            return False
        if total <= 0 or total > 64:
            return False
        if not str(data.get("request_id", "")):
            return False
        if not str(data.get("path", "")):
            return False
        return True

    @staticmethod
    def _validate_upload_relay(event: str, data: dict) -> bool:
        """Pre-validate an untrusted upload chunk/finish before relay."""
        # AuthZ stays per chunk in the frontend handlers; here we only
        # reject malformed or oversized metadata before it reaches the
        # runner. Chunk bodies carry base64 content only: the clean
        # (whitespace-stripped) length must fit exactly 256 KiB —
        # JSON framing lives outside content, so no slack applies.
        # Whitespace is normalized/accepted here; the runner normalizes.
        try:
            total = int(data.get("total_chunks", 0))
        except (TypeError, ValueError):
            # Finish markers legitimately omit total_chunks.
            total = -1 if event == "files:upload_finish" else 0
        if event == "files:upload_chunk":
            if total <= 0 or total > 64:
                return False
            try:
                index = int(data.get("index", -1))
            except (TypeError, ValueError):
                return False
            if index < 0 or index >= total:
                return False
            content = data.get("content", "")
            if not isinstance(content, str):
                return False
            if len("".join(content.split())) > 256 * 1024:
                return False
        if not str(data.get("request_id", "")):
            return False
        return True

    # Runner→frontend chunk validation: dropped/invalid payloads are
    # logged and never fanned out. Bounds mirror the runner/webapp copies:
    # at most 256 KiB clean base64 per slice (whitespace normalized,
    # JSON framing lives outside content, so no slack applies),
    # 1..560 slices per read/download transfer.
    _FRONTEND_CHUNK_B64_SIZE = 256 * 1024
    _FRONTEND_READ_MAX_CHUNKS = 560

    @classmethod
    def _validate_runner_file_chunk(cls, event: str, data: dict) -> bool:
        """Validate a runner-originated file/harness chunk reply.

        Applies to ``files:content_chunk``, ``files:download_chunk`` and
        ``harness:read_file_chunk`` alike: dict payload, non-empty
        request/path strings, int index/total with total in 1..560 and
        index in range, non-empty string content whose whitespace-cleaned
        form fits exactly 256 KiB.
        """
        if event not in {
            "files:content_chunk",
            "files:download_chunk",
            "harness:read_file_chunk",
        }:
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
        if total < 1 or total > cls._FRONTEND_READ_MAX_CHUNKS:
            return False
        if index < 0 or index >= total:
            return False
        content = data.get("content", "")
        if not isinstance(content, str):
            return False
        clean = "".join(content.split())
        if not clean:
            return False
        if len(clean) > cls._FRONTEND_CHUNK_B64_SIZE:
            return False
        return True

    async def handle_files_result(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> None:
        """Forward a file result event from runner to subscribed frontends.

        Runner→frontend chunk replies are validated before fan-out and
        ownership stays fail-closed: a missing/invalid ``workspace_id``
        never bypasses the owner check, it drops the payload.
        """
        routed = self._route_harness_reply(event, data, runner_id=runner_id)
        if routed is not None:
            return
        from ...sio_server import emit_to_frontend

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
        if not self._validate_runner_file_chunk(event, data):
            logger.warning(
                "%s rejected: invalid chunk payload for workspace %s",
                event,
                workspace_id,
            )
            return

        await emit_to_frontend(event, data, workspace_id)
