"""Atomic capture phase allocation with exact runner publication identity."""

import uuid

from django.db import transaction

from common.exceptions import ConflictError, NotFoundError

from .image_lines import ImageLineRepository
from .locking import lock_runner
from .models import (
    CapturedImage,
    CaptureRequest,
    ImageInstance,
    LifecycleCommand,
    Task,
    Workspace,
    WorkspaceRecreateRequest,
)
from .operations import OperationRepository
from .phase_children import fence_unknown_child
from .repositories import TaskRepository

MAX_MESSAGE_LENGTH = 500


class CaptureRepository:
    """Own the complete capture reservation without inferring scrub safety."""

    @staticmethod
    def allocate(
        workspace_id: uuid.UUID,
        name: str = "",
        *,
        captured_image_id: uuid.UUID | None = None,
        message: str = "",
    ) -> tuple[Workspace, Task]:
        """Atomically reserve an automatic stop/capture/replace operation."""
        with transaction.atomic():
            runner_id = Workspace.objects.values_list("runner_id", flat=True).get(
                pk=workspace_id
            )
            lock_runner(runner_id)
            ws = (
                Workspace.objects.select_for_update(of=("self",))
                .select_related("runner")
                .get(pk=workspace_id)
            )
            from .models import ImageDeletionRequest

            for deletion in ImageDeletionRequest.objects.filter(
                organization_id=ws.runner.organization_id, mode="force"
            ).exclude(phase__in=["completed", "cancelled"]):
                if str(ws.id) in [w["id"] for w in deletion.approval["workspaces"]]:
                    raise ConflictError(
                        "Workspace is reserved by approved image deletion"
                    )
            if (
                ws.active_operation
                or ws.current_task_id
                or CaptureRequest.objects.filter(workspace=ws)
                .exclude(phase__in=["completed", "failed"])
                .exists()
            ):
                raise ConflictError(
                    "Workspace has an unresolved capture/lifecycle operation"
                )
            if Task.objects.filter(
                workspace=ws,
                type="inject_credentials",
                status__in=["pending", "in_progress"],
            ).exists():
                raise ConflictError(
                    "Workspace credentials are synchronizing. "
                    "Wait for synchronization before capturing."
                )
            if ws.runtime_type != "qemu":
                raise ConflictError("Only QEMU supports capture")
            if ws.status not in ["running", "stopped"]:
                raise ConflictError("Capture requires a running or stopped workspace")
            from apps.harness.models import HarnessSession

            if HarnessSession.objects.filter(workspace=ws, status="busy").exists():
                raise ConflictError(
                    "An agent is active or waiting for an answer or approval. "
                    "Finish or stop it before capturing this workspace."
                )
            if ws.status == "stopped" and ws.credentials_present:
                raise ConflictError(
                    "Stopped guest still has credentials on disk or no scrub proof; "
                    "controlled resume and stop required"
                )
            if (
                ws.base_image_instance_id
                and ImageInstance.objects.filter(pk=ws.base_image_instance_id)
                .exclude(status="ready")
                .exists()
            ):
                raise ConflictError("Source image is retired or unavailable")
            line = CaptureRepository._reserve_line(ws, name, captured_image_id)
            phase = "stop" if ws.status == "running" else "capture"
            # The image and first prepared command commit together, closing the old
            # allocation/transport preparation crash window for capture.
            image = ImageInstance.objects.create(
                runner=ws.runner,
                runtime_type="qemu",
                origin_type="workspace_capture",
                origin_workspace=ws,
                created_by=ws.created_by,
                name=line.name,
                captured_image=line,
                generation=ImageLineRepository.next_captured_version(line.id),
                message=(message or "").strip()[:MAX_MESSAGE_LENGTH],
                min_disk_size_gb=ws.qemu_disk_size_gb,
                status="capturing",
            )
            request = CaptureRequest.objects.create(
                workspace=ws,
                image=image,
                phase=phase,
                prior_running=ws.status == "running",
            )
            task = CaptureRepository._child(request, ws, phase)
            ws.refresh_from_db()
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
            return CapturedImage.objects.create(
                organization_id=ws.runner.organization_id,
                runner=ws.runner,
                created_by=ws.created_by,
                name=name[:255],
            )
        line = (
            CapturedImage.objects.select_for_update()
            .filter(pk=captured_image_id, organization_id=ws.runner.organization_id)
            .first()
        )
        if line is None or line.created_by_id != ws.created_by_id:
            raise NotFoundError("Captured image", str(captured_image_id))
        if line.status != CapturedImage.Status.ACTIVE:
            raise ConflictError("Image is being deleted; capture a new image instead")
        if line.runner_id != ws.runner_id:
            raise ConflictError(
                "New versions must be captured on the runner that stores the image"
            )
        if ImageInstance.objects.filter(
            captured_image=line, status="capturing"
        ).exists():
            raise ConflictError("Another version of this image is being captured")
        return line

    @staticmethod
    def _child(request: CaptureRequest, ws: Workspace, phase: str) -> Task:
        kind = {
            "stop": "stop_workspace",
            "capture": "create_image_artifact",
            "resume": "resume_workspace",
        }[phase]
        task = TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=ws.runner,
            task_type=kind,
            workspace=ws,
            capture_request_id=request.id,
        )
        payload = {"task_id": str(task.id), "workspace_id": str(ws.id)}
        if phase == "capture":
            payload.update(
                image_instance_id=str(request.image_id), name=request.image.name
            )
            ImageInstance.objects.filter(pk=request.image_id).update(creating_task=task)
        if phase == "resume":
            payload.update(runtime_type=ws.runtime_type)
        OperationRepository.prepare(str(task.id), "task:" + kind, payload)
        request.child = task
        request.phase = phase
        request.save(update_fields=["child", "phase"])
        return task

    @staticmethod
    def tick() -> list[dict]:
        """Allocate a child after terminal evidence, never during old execution."""
        ids = list(
            CaptureRequest.objects.exclude(
                phase__in=["completed", "failed"]
            ).values_list("id", flat=True)
        )
        finished = []
        for request_id in ids:
            try:
                result = CaptureRepository.advance(request_id)
                if result:
                    finished.append(result)
            except Exception:
                import structlog

                structlog.get_logger(__name__).exception(
                    "capture_advance_failed", request_id=str(request_id)
                )

        return finished

    @staticmethod
    def active(workspace_id: uuid.UUID | str) -> bool:
        """Whether a durable capture still owns the workspace between children."""
        return (
            CaptureRequest.objects.filter(workspace_id=workspace_id)
            .exclude(phase__in=["completed", "failed"])
            .exists()
        )

    @staticmethod
    def operation(workspace_id: uuid.UUID | str, fallback: str | None) -> str | None:
        """Project the whole operation, retaining explicit intervention semantics."""
        if not CaptureRepository.active(workspace_id):
            return fallback
        if LifecycleCommand.objects.filter(
            task__workspace_id=workspace_id,
            task_id=Workspace.objects.filter(pk=workspace_id).values("current_task_id")[
                :1
            ],
            phase="intervention",
        ).exists():
            return None
        return "capturing_image"

    @staticmethod
    def advance(request_id: uuid.UUID) -> dict | None:
        """Progress one child and return a final notification after commit."""
        with transaction.atomic():
            runner_id = CaptureRequest.objects.values_list(
                "workspace__runner_id", flat=True
            ).get(pk=request_id)
            lock_runner(runner_id)
            request = (
                CaptureRequest.objects.select_for_update(of=("self",))
                .select_related("image", "workspace__runner")
                .get(pk=request_id)
            )
            if request.phase in {"completed", "failed"}:
                return None
            # Phase coordinator uses workspace -> child lock order, allocation
            # and result handling must use the same workspace-first order.
            ws = Workspace.objects.select_for_update().get(pk=request.workspace_id)
            child = Task.objects.select_for_update().get(pk=request.child_id)
            command = LifecycleCommand.objects.get(task=child)
            if child.status not in ["completed", "failed"]:
                return
            fenced, notification = fence_unknown_child(request, ws, child, command)
            if fenced:
                return notification
            if request.phase == "stop":
                if child.status != "completed" or ws.credentials_present:
                    request.phase = "failed"
                    request.diagnostic = (
                        "Controlled stop did not confirm injected credential scrub"
                    )
                    ImageInstance.objects.filter(pk=request.image_id).update(
                        status="failed"
                    )
                elif (
                    ws.base_image_instance_id
                    and ImageInstance.objects.filter(pk=ws.base_image_instance_id)
                    .exclude(status="ready")
                    .exists()
                ):
                    request.phase = "failed"
                    request.diagnostic = "Source retired before capture command release"
                    ImageInstance.objects.filter(pk=request.image_id).update(
                        status="failed"
                    )
                    if (
                        request.prior_running
                        and not request.resume_suppressed
                        and ws.status == "stopped"
                    ):
                        CaptureRepository._child(request, ws, "resume")
                        return
                else:
                    CaptureRepository._child(request, ws, "capture")
                    return
            elif request.phase == "capture":
                # Known safe capture failures finish their runner execution and
                # release the fence. Unknown/interrupted outcomes do not resume.
                if child.status == "failed":
                    request.diagnostic = "Image capture failed: " + child.error
                    request.save(update_fields=["diagnostic"])
                else:
                    blocker = CaptureRepository._replacement_blocker(request, ws)
                    if blocker is None:
                        CaptureRepository._hand_off(request, ws)
                        return None
                    request.diagnostic = blocker
                    request.save(update_fields=["diagnostic"])
                if (
                    request.prior_running
                    and not request.resume_suppressed
                    and ws.status == "stopped"
                ):
                    CaptureRepository._child(request, ws, "resume")
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
            request.save(update_fields=["phase", "diagnostic"])
            ws.active_operation = None
            ws.save(update_fields=["active_operation", "updated_at"])
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
        image = ImageInstance.objects.select_for_update().get(pk=request.image_id)
        if image.status != "ready" or not image.runner_ref:
            return "Image version was deleted during capture; workspace kept as is"
        if ws.status != "stopped":
            return "Workspace changed during capture; it stays on its previous version"
        try:
            from apps.credentials.services import CredentialSvc

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
        from .recreate_repository import RecreateRepository

        request.phase = "completed"
        request.save(update_fields=["phase", "diagnostic"])
        # Cloned repositories are part of the captured image from now on.
        ws.repos = []
        ws.save(update_fields=["repos", "updated_at"])
        RecreateRepository.begin(
            ws,
            ImageInstance.objects.get(pk=request.image_id),
            reason=WorkspaceRecreateRequest.Reason.CAPTURE,
            final_running=request.prior_running and not request.resume_suppressed,
        )

    @staticmethod
    def suppress_resume(workspace_id) -> None:
        """Later explicit user stop/delete suppresses earlier restart approval."""
        CaptureRequest.objects.filter(workspace_id=workspace_id).exclude(
            phase__in=["completed", "failed"]
        ).update(resume_suppressed=True)
