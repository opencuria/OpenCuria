"""Transaction-scoped persistence for workspace configuration."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.db import transaction

from common.exceptions import NotFoundError

from .locking import lock_runner
from .models import ImageInstance, Workspace
from .process_repository import WorkspaceProcessRepository
from .repositories import ImageGenerationRepository, WorkspaceRepository


class WorkspaceConfigurationRepository:
    """Keep configuration validation and writes within the same runner lock."""

    @staticmethod
    @contextmanager
    def creating(
        runner_id: uuid.UUID, image_id: uuid.UUID | None
    ) -> Iterator[ImageInstance | None]:
        """Lock runner then image selection until creation commits or rolls back."""
        with transaction.atomic():
            lock_runner(runner_id)
            selected = (
                ImageGenerationRepository.validate_selection(image_id)
                if image_id is not None
                else None
            )
            yield selected

    @staticmethod
    @contextmanager
    def updating(workspace_id: uuid.UUID) -> Iterator[Workspace]:
        """Reload under runner → workspace locks and hold them through validation.

        The initial read finds the runner only. Policy must use the second read,
        since ownership, reservations or existence may change before locking.
        """
        with transaction.atomic():
            existing = WorkspaceRepository.get_by_id(workspace_id)
            if existing is None:
                raise NotFoundError("Workspace", str(workspace_id))
            lock_runner(existing.runner_id)
            workspace = WorkspaceRepository.get_by_id(workspace_id, lock=True)
            if workspace is None:
                raise NotFoundError("Workspace", str(workspace_id))
            yield workspace

    @staticmethod
    def initialize(
        *,
        workspace_fields: dict[str, Any],
        user: Any,
        credentials: list,
        credentials_present: bool,
    ) -> Workspace:
        """Persist workspace-owned state inside the creation scope."""
        workspace = WorkspaceRepository.create(**workspace_fields)
        selected = workspace_fields.get("base_image_instance")
        if selected is not None:
            WorkspaceProcessRepository.restore_definitions(
                workspace, selected.process_definitions, created_by=user
            )
        WorkspaceRepository.set_credentials(workspace, credentials)
        if credentials_present:
            WorkspaceRepository.update_credentials_present(workspace, True)
            workspace = WorkspaceRepository.get_by_id(workspace.id)
        return workspace
