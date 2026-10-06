"""Single projection of a workspace's user-visible blocking operation."""

from __future__ import annotations

import uuid


def project_workspace_operation(
    workspace_id: uuid.UUID | str, fallback: str | None
) -> str | None:
    """Multi-step capture/recreate requests own the operation between children."""
    from .capture_repository import CaptureRepository
    from .recreate_repository import RecreateRepository

    return CaptureRepository.operation(
        workspace_id, RecreateRepository.operation(workspace_id, fallback)
    )
