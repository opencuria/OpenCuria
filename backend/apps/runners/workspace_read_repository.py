"""Load workspace relations before composing user-visible read metadata."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.db.models import prefetch_related_objects

from .models import ImageInstance, Workspace


@dataclass(frozen=True)
class WorkspaceReadRelations:
    """Runner facts and image records needed by the workspace read service."""

    annotated_runner_online: bool | None
    runner_status: str | None
    auto_stop_timeout_minutes: int | None
    base_image: ImageInstance | None
    pending_base_image: ImageInstance | None
    base_image_definition_name: str | None


class WorkspaceReadRepository:
    """Resolve potentially lazy foreign keys within the persistence boundary."""

    @staticmethod
    def load_relations_many(
        workspaces: list[Workspace],
    ) -> dict[uuid.UUID, WorkspaceReadRelations]:
        """Preload read relations while retaining request-local annotations."""
        prefetch_related_objects(
            workspaces,
            "runner__organization",
            "base_image_instance__origin_definition",
            "pending_base_image_instance",
        )
        return {
            workspace.id: WorkspaceReadRepository.load_relations(workspace)
            for workspace in workspaces
        }

    @staticmethod
    def load_relations(
        workspace: Workspace, *, include_pending_image: bool = True
    ) -> WorkspaceReadRelations:
        """Read related facts without replacing request-local workspace annotations."""
        runner = getattr(workspace, "runner", None)
        organization = getattr(runner, "organization", None)
        base_image = getattr(workspace, "base_image_instance", None)
        definition = getattr(base_image, "origin_definition", None)
        return WorkspaceReadRelations(
            annotated_runner_online=(
                bool(workspace.runner_is_online)
                if hasattr(workspace, "runner_is_online")
                else None
            ),
            runner_status=runner.status if runner is not None else None,
            auto_stop_timeout_minutes=(
                organization.workspace_auto_stop_timeout_minutes
                if organization is not None
                else None
            ),
            base_image=base_image,
            pending_base_image=(
                getattr(workspace, "pending_base_image_instance", None)
                if include_pending_image
                else None
            ),
            base_image_definition_name=getattr(definition, "name", None),
        )
