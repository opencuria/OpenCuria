"""Present execution, unresolved outcomes and history as distinct activity states."""

from ..storage_schemas import RunnerActivityOut

OPERATION_TITLES = {
    "create_workspace": "Create workspace",
    "create_workspace_from_image_artifact": "Create workspace",
    "stop_workspace": "Stop workspace",
    "resume_workspace": "Start workspace",
    "update_workspace": "Update workspace",
    "remove_workspace": "Delete workspace",
    "build_image": "Build image",
    "create_image_artifact": "Save workspace as image",
    "delete_image_artifact": "Delete image",
}
TERMINAL_PHASES = {"completed", "cancelled", "failed", "disposed", "result"}
ATTENTION_PHASES = {"intervention", "intervention_required", "reconfirmation_required"}


class ActivityService:
    """Project existing workflow facts; never resolve or release an operation."""

    @staticmethod
    def project(records: dict) -> list[RunnerActivityOut]:
        """Group children under their workflow and retain closed failures in history."""
        operations = {str(op["task_id"]): op for op in records["operations"]}
        grouped: set[str] = set()
        activities: list[RunnerActivityOut] = []
        names = {}
        for image in records.get("generations", []):
            for identity in (
                image["id"],
                image.get("definition_id"),
                image.get("captured_image_id"),
            ):
                if identity:
                    names[str(identity)] = image.get("line_name") or image["name"]

        def workflow(
            record: dict,
            kind: str,
            title: str,
            target: str,
            child_ids: list,
        ) -> None:
            ids = {str(identity) for identity in child_ids if identity}
            children = [op for identity, op in operations.items() if identity in ids]
            grouped.update(ids)
            blocked = [op for op in children if ActivityService.unresolved(op)]
            phase = record["phase"]
            state = (
                "attention"
                if blocked
                or phase in ATTENTION_PHASES
                or (kind == "deletion" and phase == "failed")
                else "history"
                if phase in TERMINAL_PHASES
                else "active"
            )
            outcome = (
                "blocked"
                if state == "attention"
                else "resolved"
                if any(op["phase"] == "disposed" for op in children)
                and phase == "failed"
                else phase
                if state == "history"
                else "in_progress"
            )
            diagnostic = record.get("diagnostic", "")
            if kind == "capture" and record.get("resume_suppressed"):
                diagnostic = (
                    diagnostic + "\nAutomatic restart was skipped for this capture."
                ).strip()
            summary_title = (
                "Use saved image"
                if record.get("replacement_started")
                and (state == "attention" or outcome == "failed")
                else title
            )
            activities.append(
                RunnerActivityOut(
                    id=str(record["id"]),
                    kind=kind,
                    title=title,
                    target_name=target or "Unnamed workspace",
                    state=state,
                    outcome=outcome,
                    summary=ActivityService.summary(
                        summary_title, state, outcome, phase
                    ),
                    diagnostic=diagnostic,
                    operation_ids=[str(op["task_id"]) for op in blocked],
                    created_at=record["created_at"],
                )
            )

        # A capture hands off to recreation in the same workspace-locked
        # transaction. Its latest preceding capture is the enclosing workflow.
        recreates_by_capture: dict[str, list[dict]] = {}
        standalone_recreates = []
        for recreate in records["recreate_requests"]:
            captures = [
                capture
                for capture in records["capture_requests"]
                if recreate["reason"] == "capture"
                and capture["workspace_id"] == recreate["workspace_id"]
                and capture["created_at"] <= recreate["created_at"]
            ]
            if captures:
                capture = max(captures, key=lambda item: item["created_at"])
                recreates_by_capture.setdefault(str(capture["id"]), []).append(recreate)
            else:
                standalone_recreates.append(recreate)
        for request in records["capture_requests"]:
            recreates = recreates_by_capture.get(str(request["id"]), [])
            current = (
                max(recreates, key=lambda item: item["created_at"])
                if recreates
                else request
            )
            record = {
                **request,
                "phase": current["phase"],
                "diagnostic": current["diagnostic"],
                "replacement_started": bool(recreates),
            }
            workflow(
                record,
                "capture",
                "Save workspace as image",
                request["workspace__name"],
                [request["child_id"], request["image__creating_task_id"]]
                + [recreate["child_id"] for recreate in recreates],
            )
        for request in standalone_recreates:
            title = {
                "reset": "Reset workspace",
                "update": "Update workspace",
                "capture": "Use saved image",
            }.get(request["reason"], "Recreate workspace")
            workflow(
                request,
                "recreate",
                title,
                request["workspace__name"],
                [request["child_id"]],
            )
        for request in records["deletions"]:
            items = request["approval"].get("images", [])
            target = next(
                (item["name"] for item in items if item["id"] == request["target_id"]),
                names.get(request["target_id"], "Image"),
            )
            workflow(
                request,
                "deletion",
                "Delete image",
                target,
                list(request["children"].values())
                + request["approval"].get("failed_children", []),
            )
        for identity, op in operations.items():
            if identity in grouped:
                continue
            state = (
                "attention"
                if ActivityService.unresolved(op)
                else "history"
                if op["phase"] in TERMINAL_PHASES
                or op["task__status"] in {"failed", "completed"}
                else "active"
            )
            outcome = (
                "blocked"
                if state == "attention"
                else "resolved"
                if op["phase"] == "disposed"
                else op["task__status"]
                if state == "history"
                else "in_progress"
            )
            title = OPERATION_TITLES.get(op["task__type"], "Workspace operation")
            activities.append(
                RunnerActivityOut(
                    id=identity,
                    kind="operation",
                    title=title,
                    target_name=op["task__workspace__name"]
                    or names.get(op["target"], "Image"),
                    state=state,
                    outcome=outcome,
                    summary=ActivityService.summary(title, state, outcome, op["phase"]),
                    diagnostic=op["task__error"],
                    operation_ids=[identity] if state == "attention" else [],
                    created_at=op["created_at"],
                )
            )
        return sorted(
            activities, key=lambda activity: activity.created_at, reverse=True
        )

    @staticmethod
    def unresolved(operation: dict) -> bool:
        """An acknowledged failure is history even though execution stays failed."""
        return operation["phase"] != "disposed" and (
            operation["phase"] in ATTENTION_PHASES
            or (
                operation["task__status"] == "failed"
                and operation["task__workspace__current_task_id"]
                == operation["task_id"]
            )
        )

    @staticmethod
    def summary(title: str, state: str, outcome: str, phase: str) -> str:
        """Explain the next decision without exposing transport or locking terms."""
        if phase == "reconfirmation_required":
            return "Dependencies changed. Review the deletion before continuing."
        if state == "attention":
            return (
                f"{title} could not be confirmed. "
                "Check the current status before making further changes."
            )
        if outcome == "resolved":
            return "Interrupted operation closed. Existing resources were kept."
        if outcome == "failed":
            return f"{title} failed. This operation is no longer blocking changes."
        if outcome == "cancelled":
            return "Cancelled. Existing resources were kept."
        if state == "history":
            return f"{title} completed."
        if phase.startswith("waiting"):
            return "Waiting for the runner or dependent resources."
        return f"{title} is in progress."
