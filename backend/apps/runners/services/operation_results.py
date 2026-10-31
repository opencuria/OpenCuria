"""Validate and reconcile runner lifecycle results through one service boundary."""

from __future__ import annotations

import inspect
import uuid
from typing import Any

from ..operation_result_repository import OperationResultRepository
from ..operations import LIFECYCLE_TYPES

RESULT_METHODS = {
    "workspace:created": (
        "handle_workspace_created",
        {"create_workspace", "create_workspace_from_image_artifact"},
    ),
    "workspace:stopped": ("handle_workspace_stopped", {"stop_workspace"}),
    "workspace:resumed": ("handle_workspace_resumed", {"resume_workspace"}),
    "workspace:updated": ("handle_workspace_updated", {"update_workspace"}),
    "workspace:removed": ("handle_workspace_removed", {"remove_workspace"}),
    "workspace:error": (
        "handle_workspace_error",
        LIFECYCLE_TYPES - {"build_image", "delete_image", "create_image_artifact"},
    ),
    "image:built": ("handle_image_built", {"build_image"}),
    "image:build_failed": ("handle_image_build_failed", {"build_image"}),
    "image_artifact:created": (
        "handle_image_artifact_created",
        {"create_image_artifact"},
    ),
    "image_artifact:failed": (
        "handle_image_artifact_failed",
        {"create_image_artifact"},
    ),
    "image_artifact:deleted": ("handle_image_artifact_deleted", {"delete_image"}),
    "image_artifact:delete_failed": (
        "handle_image_artifact_delete_failed",
        {"delete_image"},
    ),
}
FAILURE_EVENTS = {
    "workspace:error",
    "image:build_failed",
    "image_artifact:failed",
    "image_artifact:delete_failed",
}
TERMINAL_STATUSES = {"completed", "failed"}


def apply_result(service: Any, runner_id: str, event: str, data: dict) -> bool:
    """Reconcile an authenticated result atomically; return terminal ACK eligibility."""
    if event not in RESULT_METHODS:
        return False
    try:
        uuid.UUID(str(runner_id))
        uuid.UUID(str(data.get("operation_id")))
    except (TypeError, ValueError, AttributeError):
        return False

    with OperationResultRepository.locked(
        runner_id, data.get("operation_id")
    ) as result:
        if result is None:
            return False
        state = result.state()
        method_name, types = RESULT_METHODS[event]
        if (
            str(state.runner_id) != runner_id
            or state.task_type not in types
            or data.get("task_id") != str(state.task_id)
            or data.get("attempt") != state.attempt
            or data.get("target") != state.target
            or data.get("runner_id") != runner_id
        ):
            return False
        if state.workspace_id and data.get(
            "workspace_id", str(state.workspace_id)
        ) != str(state.workspace_id):
            return False
        if (
            event in FAILURE_EVENTS
            and data.get("outcome_known") is False
            and (state.status not in TERMINAL_STATUSES or state.phase == "intervention")
        ):
            reason = (
                "Operation interrupted; automatic recovery will verify "
                "the runner state."
            )
            result.intervene(reason)
            if state.workspace_id:
                service._forward_workspace_operation(str(state.workspace_id), None)
                service._forward_to_frontend(
                    "workspace:error",
                    {
                        "workspace_id": str(state.workspace_id),
                        "task_id": str(state.task_id),
                        "error": reason,
                    },
                    str(state.workspace_id),
                )
            return False
        if state.status in TERMINAL_STATUSES:
            if state.status == "failed" and state.phase == "intervention":
                # Backend delivery deadlines are not runner terminal outcomes.
                # Exact authenticated journal completion can still resolve the fence.
                if event in FAILURE_EVENTS and (
                    not data.get("execution_finished") or not data.get("outcome_known")
                ):
                    return False
                if state.workspace_id and state.current_task_id != state.task_id:
                    return False
                result.reopen(
                    "building" if state.task_type == "build_image" else "capturing"
                )
            else:
                return True
        if state.workspace_id and state.current_task_id != state.task_id:
            return False
        data = {
            **data,
            "artifact_id": data.get("image_artifact_id", ""),
            "runner_ref": data.get("image_artifact_id", ""),
        }
        result.mark_received()
        method = getattr(service, method_name)
        kwargs = {
            k: v
            for k, v in {**data, "runner_id": runner_id}.items()
            if k in inspect.signature(method).parameters
        }
        method(**kwargs)
        status, _ = result.outcome()
        if status not in TERMINAL_STATUSES:
            return False
        result.record_runtime_observation(
            {
                "workspace:created": "running",
                "workspace:resumed": "running",
                "workspace:updated": "running",
                "workspace:stopped": "stopped",
                "workspace:removed": "absent",
            }.get(event, data.get("observed_status") or "unknown")
            if status == "completed"
            else data.get("observed_status") or "unknown"
        )
        result.release_workspace()
        return True
