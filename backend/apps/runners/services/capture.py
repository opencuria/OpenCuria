"""Workspace image capture admission, phase progression and replacement policy.

All policy checks run inside repository-owned transactions. Runner -> workspace
-> child locking, the initial command and replacement handoff remain atomic.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import structlog

from apps.credentials.services import CredentialSvc
from common.exceptions import ConflictError, NotFoundError

from ..capture_repository import CaptureRepository
from ..models import (
    CapturedImage,
    CaptureRequest,
    Task,
    Workspace,
    WorkspaceRecreateRequest,
)
from ..recreate_repository import RecreateRepository
from ..workspace_operation_repository import WorkspaceOperationRepository

logger = structlog.get_logger("apps.runners.capture_repository")

MAX_MESSAGE_LENGTH = 500


class CaptureService:
    """Own capture admission, phase progression and replacement eligibility."""

    @staticmethod
    def allocate(
        workspace_id: uuid.UUID,
        name: str = "",
        *,
        has_busy_agent: Callable[[uuid.UUID], bool],
        captured_image_id: uuid.UUID | None = None,
        message: str = "",
    ) -> tuple[Workspace, Task]:
        """Atomically reserve an automatic stop/capture/replace operation.

        ``has_busy_agent`` is the harness busy check, evaluated under the
        runner lock that also serializes agent run admission.
        """
        with CaptureRepository.lock_workspace(workspace_id) as ws:
            approvals = CaptureRepository.forced_deletion_approvals(
                ws.runner.organization_id
            )
            for approval in approvals:
                if str(ws.id) in [w["id"] for w in approval["workspaces"]]:
                    raise ConflictError(
                        "Workspace is reserved by approved image deletion"
                    )
            if (
                ws.active_operation
                or ws.current_task_id
                or WorkspaceOperationRepository.capture_active(ws.id)
            ):
                raise ConflictError(
                    "Workspace has an unresolved capture/lifecycle operation"
                )
            if CaptureRepository.credentials_synchronizing(ws.id):
                raise ConflictError(
                    "Workspace credentials are synchronizing. "
                    "Wait for synchronization before capturing."
                )
            if ws.status not in ["running", "stopped"]:
                raise ConflictError("Capture requires a running or stopped workspace")
            if has_busy_agent(ws.id):
                raise ConflictError(
                    "An agent is active or waiting for an answer or approval. "
                    "Finish or stop it before capturing this workspace."
                )
            if ws.status == "stopped" and ws.credentials_present:
                raise ConflictError(
                    "Stopped guest still has credentials on disk or no scrub proof; "
                    "controlled resume and stop required"
                )
            if ws.base_image_instance_id and CaptureRepository.image_unavailable(
                ws.base_image_instance_id
            ):
                raise ConflictError("Source image is retired or unavailable")
            line = CaptureService._reserve_line(ws, name, captured_image_id)
            phase = "stop" if ws.status == "running" else "capture"
            # The image and first prepared command commit together, closing the old
            # allocation/transport preparation crash window for capture.
            request = CaptureRepository.create_request(
                ws,
                line,
                phase=phase,
                prior_running=ws.status == "running",
                message=(message or "").strip()[:MAX_MESSAGE_LENGTH],
            )
            task = CaptureService._child(request, ws, phase)
            CaptureRepository.refresh_workspace(ws)
            return ws, task

    @staticmethod
    def _reserve_line(
        ws: Workspace, name: str, captured_image_id: uuid.UUID | None
    ) -> CapturedImage:
        """Create a new image line or lock an existing one for the next version."""
        if captured_image_id is None:
            name = (name or "").strip()
            if not name:
                raise ConflictError("A name is required for a new image")
            return CaptureRepository.create_line(ws, name[:255])
        line = CaptureRepository.lock_line(captured_image_id, ws.runner.organization_id)
        if line is None or line.created_by_id != ws.created_by_id:
            raise NotFoundError("Captured image", str(captured_image_id))
        if line.status != CapturedImage.Status.ACTIVE:
            raise ConflictError("Image is being deleted; capture a new image instead")
        if line.runner_id != ws.runner_id:
            raise ConflictError(
                "New versions must be captured on the runner that stores the image"
            )
        if CaptureRepository.version_capturing(line):
            raise ConflictError("Another version of this image is being captured")
        return line

    @staticmethod
    def _child(request: CaptureRequest, ws: Workspace, phase: str) -> Task:
        """Allocate the next workflow-selected child inside the current lock."""
        kind = {
            "stop": "stop_workspace",
            "capture": "create_image_artifact",
            "resume": "resume_workspace",
        }[phase]
        return CaptureRepository.create_child(request, ws, phase, kind)

    @staticmethod
    def tick() -> list[dict]:
        """Allocate a child after terminal evidence, never during old execution."""
        ids = CaptureRepository.pending_ids()
        finished = []
        for request_id in ids:
            try:
                result = CaptureService.advance(request_id)
                if result:
                    finished.append(result)
            except Exception:
                logger.exception("capture_advance_failed", request_id=str(request_id))

        return finished

    @staticmethod
    def advance(request_id: uuid.UUID) -> dict | None:
        """Progress one child and return a final notification after commit."""
        with CaptureRepository.lock_request(request_id) as request:
            if request.phase in {"completed", "failed"}:
                return None
            # Phase coordinator uses workspace -> child lock order, allocation
            # and result handling must use the same workspace-first order.
            ws, child, command = CaptureRepository.lock_child(request)
            if child.status not in ["completed", "failed"]:
                return
            fenced, notification = CaptureRepository.fence_child(
                request, ws, child, command
            )
            if fenced:
                return notification
            if request.phase == "stop":
                if child.status != "completed" or ws.credentials_present:
                    request.phase = "failed"
                    request.diagnostic = (
                        "Controlled stop did not confirm injected credential scrub"
                    )
                    CaptureRepository.fail_image(request.image_id)
                elif ws.base_image_instance_id and CaptureRepository.image_unavailable(
                    ws.base_image_instance_id
                ):
                    request.phase = "failed"
                    request.diagnostic = "Source retired before capture command release"
                    CaptureRepository.fail_image(request.image_id)
                    if (
                        request.prior_running
                        and not request.resume_suppressed
                        and ws.status == "stopped"
                    ):
                        CaptureService._child(request, ws, "resume")
                        return
                else:
                    CaptureService._child(request, ws, "capture")
                    return
            elif request.phase == "capture":
                # Known safe capture failures finish their runner execution and
                # release the fence. Unknown/interrupted outcomes do not resume.
                if child.status == "failed":
                    request.diagnostic = "Image capture failed: " + child.error
                    CaptureRepository.save_request(request, ["diagnostic"])
                else:
                    blocker = CaptureService._replacement_blocker(request, ws)
                    if blocker is None:
                        CaptureService._hand_off(request, ws)
                        return None
                    request.diagnostic = blocker
                    CaptureRepository.save_request(request, ["diagnostic"])
                if (
                    request.prior_running
                    and not request.resume_suppressed
                    and ws.status == "stopped"
                ):
                    CaptureService._child(request, ws, "resume")
                    return
                request.phase = "completed" if child.status == "completed" else "failed"
            elif request.phase == "resume":
                request.phase = (
                    "completed" if request.image.status == "ready" else "failed"
                )
                if child.status == "failed":
                    image_result = (
                        "Image retained; restart failed: "
                        if request.image.status == "ready"
                        else "Image capture and restart failed: "
                    )
                    request.diagnostic = image_result + child.error
            CaptureRepository.save_request(request, ["phase", "diagnostic"])
            CaptureRepository.clear_operation(ws)
            return {
                "workspace_id": str(ws.id),
                "phase": request.phase,
                "diagnostic": request.diagnostic,
            }

    @staticmethod
    def _replacement_blocker(request: CaptureRequest, ws: Workspace) -> str | None:
        """Why the source cannot switch to the new version; None when it can.

        Nothing has been removed yet, so every blocker safely keeps the
        workspace on its previous version.
        """
        image = CaptureRepository.lock_image(request.image_id)
        if image.status != "ready" or not image.runner_ref:
            return "Image version was deleted during capture; workspace kept as is"
        if ws.status != "stopped":
            return "Workspace changed during capture; it stays on its previous version"
        try:
            CredentialSvc().resolve_workspace_credentials(ws)
        except Exception:  # noqa: BLE001 - any failure keeps the workspace
            return (
                f"Image saved as v{image.generation}, but workspace credentials "
                "cannot be resolved; the workspace stays on its previous version"
            )
        runner = ws.runner
        if (
            image.min_disk_size_gb
            and image.min_disk_size_gb > runner.qemu_max_disk_size_gb
        ):
            return (
                f"Image saved as v{image.generation}, but it exceeds the runner disk "
                "limit; the workspace stays on its previous version"
            )
        return None

    @staticmethod
    def _hand_off(request: CaptureRequest, ws: Workspace) -> None:
        """Atomically finish the capture and recreate the source on the new version."""
        request.phase = "completed"
        CaptureRepository.save_request(request, ["phase", "diagnostic"])
        # Cloned repositories are part of the captured image from now on.
        CaptureRepository.clear_repos(ws)
        RecreateRepository.begin(
            ws,
            CaptureRepository.image(request.image_id),
            reason=WorkspaceRecreateRequest.Reason.CAPTURE,
            final_running=request.prior_running and not request.resume_suppressed,
        )
