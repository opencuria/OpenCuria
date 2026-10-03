"""Atomic capture phase allocation with exact runner publication identity."""

import uuid

from django.db import transaction

from common.exceptions import ConflictError
from .models import (
    CaptureRequest,
    ImageInstance,
    LifecycleCommand,
    Task,
    Workspace,
    Runner,
)
from .operations import OperationRepository
from .repositories import TaskRepository


from .locking import lock_runner


class CaptureRepository:
    """Never infer scrub safety or resume approval from mere runtime status."""

    @staticmethod
    def allocate(workspace_id, name: str, stop_and_restart: bool):
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
                ws.current_task_id
                or CaptureRequest.objects.filter(workspace=ws)
                .exclude(phase__in=["completed", "failed"])
                .exists()
            ):
                raise ConflictError(
                    "Workspace has an unresolved capture/lifecycle operation"
                )
            if ws.runtime_type != "qemu":
                raise ConflictError("Only QEMU supports capture")
            if ws.status not in ["running", "stopped"]:
                raise ConflictError("Capture requires a running or stopped workspace")
            if ws.status == "running" and not stop_and_restart:
                raise ConflictError(
                    "Running capture requires explicit stop_and_restart approval"
                )
            if ws.status == "stopped" and ws.credentials_present:
                raise ConflictError(
                    "Stopped guest still has credentials on disk or no scrub proof; controlled resume and stop required"
                )
            if (
                ws.base_image_instance_id
                and ImageInstance.objects.filter(pk=ws.base_image_instance_id)
                .exclude(status="ready")
                .exists()
            ):
                raise ConflictError("Source image is retired or unavailable")
            phase = "stop" if ws.status == "running" else "capture"
            # The image and first prepared command commit together, closing the old
            # allocation/transport preparation crash window for capture.
            image = ImageInstance.objects.create(
                runner=ws.runner,
                runtime_type="qemu",
                origin_type="workspace_capture",
                origin_workspace=ws,
                created_by=ws.created_by,
                name=name,
                status="capturing",
            )
            request = CaptureRequest.objects.create(
                workspace=ws,
                image=image,
                phase=phase,
                prior_running=ws.status == "running" and stop_and_restart,
            )
            task = CaptureRepository._child(request, ws, phase)
            return ws, task

    @staticmethod
    def _child(request, ws, phase):
        kind = {
            "stop": "stop_workspace",
            "capture": "create_image_artifact",
            "resume": "resume_workspace",
        }[phase]
        task = TaskRepository.create(
            task_id=uuid.uuid4(), runner=ws.runner, task_type=kind, workspace=ws
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
    def tick() -> None:
        """Allocate one child after terminal evidence, never while old execution lives."""
        ids = list(
            CaptureRequest.objects.exclude(
                phase__in=["completed", "failed"]
            ).values_list("id", flat=True)
        )
        for request_id in ids:
            try:
                CaptureRepository.advance(request_id)
            except Exception:
                import structlog

                structlog.get_logger(__name__).exception(
                    "capture_advance_failed", request_id=str(request_id)
                )

    @staticmethod
    def advance(request_id) -> None:
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
            # Phase coordinator uses workspace -> child lock order, allocation
            # and result handling must use the same workspace-first order.
            ws = Workspace.objects.select_for_update().get(pk=request.workspace_id)
            child = Task.objects.select_for_update().get(pk=request.child_id)
            command = LifecycleCommand.objects.get(task=child)
            if child.status not in ["completed", "failed"]:
                return
            if (
                command.phase == "intervention"
                or ws.current_task_id
                or (child.status == "failed" and "intervention" in child.error.lower())
            ):
                if not ws.current_task_id:
                    ws.current_task = child
                    ws.save(update_fields=["current_task"])
                request.diagnostic = (
                    "Unknown child outcome; exact journal reconciliation required"
                )
                request.save(update_fields=["diagnostic"])
                return
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
                if (
                    request.prior_running
                    and not request.resume_suppressed
                    and ws.status == "stopped"
                ):
                    CaptureRepository._child(request, ws, "resume")
                    return
                request.phase = "completed" if child.status == "completed" else "failed"
            elif request.phase == "resume":
                request.phase = "completed"
                if child.status == "failed":
                    request.diagnostic = (
                        "Capture retained; approved restart failed: " + child.error
                    )
            request.save(update_fields=["phase", "diagnostic"])

    @staticmethod
    def suppress_resume(workspace_id) -> None:
        """Later explicit user stop/delete suppresses earlier restart approval."""
        CaptureRequest.objects.filter(workspace_id=workspace_id).exclude(
            phase__in=["completed", "failed"]
        ).update(resume_suppressed=True)
