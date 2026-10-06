"""Organization image-version retention for captured and definition lines.

Per line, the latest version plus the ``image_versions_to_keep`` newest ready
versions are kept. Every other ready version is handed to the deletion
coordinator (deferred) as soon as no workspace is based on it. Versions that
are still in use stay ready, so resetting those workspaces keeps working.
"""

from __future__ import annotations

import structlog
from django.db import transaction

from common.exceptions import ConflictError, NotFoundError

from .image_lines import ImageLine, ImageLineRepository
from .locking import lock_runner
from .models import CapturedImage, ImageBuildJob

logger = structlog.get_logger(__name__)

INACTIVE_JOB_STATUSES = ["pending_deletion", "deleting", "deleted", "delete_failed"]


class RetentionRepository:
    """Apply retention and finalize captured lines whose versions are all gone."""

    @staticmethod
    def lines() -> list[tuple[ImageLine, object]]:
        """All live lines with their organization id."""
        result = []
        for line in CapturedImage.objects.filter(status="active").only(
            "id", "name", "runner_id", "organization_id"
        ):
            result.append(
                (
                    ImageLine("captured", line.id, line.name, line.runner_id),
                    line.organization_id,
                )
            )
        for job in (
            ImageBuildJob.objects.exclude(status__in=INACTIVE_JOB_STATUSES)
            .select_related("runner", "image_definition")
            .only(
                "id", "runner_id", "runner__organization_id", "image_definition__name"
            )
        ):
            result.append(
                (
                    ImageLine(
                        "definition", job.id, job.image_definition.name, job.runner_id
                    ),
                    job.runner.organization_id,
                )
            )
        return result

    @staticmethod
    def keep_for(org_id) -> int:
        """Configured retention of an organization (at least one version)."""
        from apps.organizations.models import Organization

        keep = (
            Organization.objects.filter(pk=org_id)
            .values_list("image_versions_to_keep", flat=True)
            .first()
        )
        return max(int(keep or 1), 1)

    @staticmethod
    def apply(line: ImageLine, org_id) -> list[str]:
        """Request deferred deletion of expired, unused versions of one line."""
        from .deletion_repository import DeletionRepository

        requested = []
        keep = RetentionRepository.keep_for(org_id)
        with transaction.atomic():
            # Same lock as creation, recreate and capture: pins cannot appear
            # between selecting a candidate and retiring it.
            lock_runner(line.runner_id)
            for image in ImageLineRepository.retention_candidates(line, keep):
                try:
                    DeletionRepository.request(
                        org_id, None, "image", image.id, mode="deferred"
                    )
                except (ConflictError, NotFoundError):
                    continue
                requested.append(str(image.id))
        if requested:
            logger.info(
                "image_versions_expired",
                line_kind=line.kind,
                line_id=str(line.id),
                image_ids=requested,
            )
        return requested

    @staticmethod
    def tick() -> list[str]:
        """Apply retention to every line; one bad line never blocks the others."""
        requested: list[str] = []
        for line, org_id in RetentionRepository.lines():
            try:
                requested += RetentionRepository.apply(line, org_id)
            except Exception:
                logger.exception(
                    "image_retention_failed", line_kind=line.kind, line_id=str(line.id)
                )
        for line in CapturedImage.objects.filter(status="pending_deletion"):
            try:
                requested += RetentionRepository.heal(line)
            except Exception:
                logger.exception("image_line_heal_failed", line_id=str(line.id))
        for line_id in CapturedImage.objects.filter(
            status__in=["active", "pending_deletion"]
        ).values_list("id", flat=True):
            ImageLineRepository.finalize_captured(line_id)
        return requested

    @staticmethod
    def heal(line: CapturedImage) -> list[str]:
        """A line being deleted always converges: orphaned versions get a request."""
        from .deletion_repository import DONE, DeletionRepository
        from .models import ImageDeletionRequest, ImageInstance

        if (
            ImageDeletionRequest.objects.filter(
                target_type="captured_image", target_id=line.id
            )
            .exclude(phase__in=DONE)
            .exists()
        ):
            return []
        requested = []
        with transaction.atomic():
            lock_runner(line.runner_id)
            reserved = ImageLineRepository.reserved_image_ids()
            for image in ImageInstance.objects.filter(captured_image=line).exclude(
                status__in=["deleted", "deleting", "capturing"]
            ):
                if str(image.id) in reserved:
                    continue
                try:
                    DeletionRepository.request(
                        line.organization_id, None, "image", image.id, mode="deferred"
                    )
                except (ConflictError, NotFoundError):
                    continue
                requested.append(str(image.id))
        return requested
