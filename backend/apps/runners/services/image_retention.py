"""Image-version retention policy and recovery for captured and definition lines.

Keep the selected latest version plus the configured newest ready versions.
Unused versions outside that window retire through the deletion coordinator;
pins and deletion reservations are checked under the shared runner lock.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

from common.exceptions import ConflictError, NotFoundError

from ..deletion_repository import DeletionRepository
from ..image_lines import ImageLine, ImageLineRepository
from ..retention_repository import RetentionRepository

if TYPE_CHECKING:
    from ..models import CapturedImage, ImageInstance

logger = structlog.get_logger(__name__)


def _kept_ids(
    ready_ids: list[uuid.UUID], keep: int, latest: ImageInstance | None
) -> set[uuid.UUID]:
    """Apply the retention window to ready ids ordered newest first."""
    return set(ready_ids[: max(keep, 1)]) | ({latest.id} if latest else set())


class ImageRetentionService:
    """Own retention labels, retirement decisions and per-line recovery."""

    @staticmethod
    def keep_for(org_id: uuid.UUID) -> int:
        """Configured retention of an organization (at least one version)."""
        keep = RetentionRepository.configured_keep(org_id)
        return max(int(keep or 1), 1)

    @staticmethod
    def retention_labels(line: ImageLine, keep: int) -> dict[uuid.UUID, str]:
        """Label ready versions ``latest``, ``kept`` or ``expires_when_unused``."""
        latest = ImageLineRepository.latest_many([line])
        return ImageRetentionService.retention_labels_many({line: keep}, latest)[line]

    @staticmethod
    def retention_labels_many(
        keep_by_line: dict[ImageLine, int],
        latest_by_line: dict[ImageLine, ImageInstance | None],
    ) -> dict[ImageLine, dict[uuid.UUID, str]]:
        """Label all requested lines with one ordered read of ready version ids."""
        ready = ImageLineRepository.ready_ids_many(list(keep_by_line))
        result = {}
        for line, keep in keep_by_line.items():
            ready_ids = ready[(line.kind, line.id)]
            latest = latest_by_line[line]
            kept = _kept_ids(ready_ids, keep, latest)
            result[line] = {
                image_id: (
                    "latest"
                    if latest and image_id == latest.id
                    else "kept"
                    if image_id in kept
                    else "expires_when_unused"
                )
                for image_id in ready_ids
            }
        return result

    @staticmethod
    def _candidates(line: ImageLine, keep: int) -> list[ImageInstance]:
        """Ready versions outside the retained window that nothing depends on."""
        ready = ImageLineRepository.ready_ids(line, max(keep, 1))
        latest = ImageLineRepository.latest(line)
        kept = _kept_ids(ready, keep, latest)
        expired = ImageLineRepository.ready_versions_excluding(line, kept)
        if not expired:
            return []
        pinned = ImageLineRepository.pin_counts(i.id for i in expired)
        reserved = ImageLineRepository.reserved_image_ids()
        return [
            image
            for image in expired
            if not pinned.get(image.id) and str(image.id) not in reserved
        ]

    @staticmethod
    def apply(line: ImageLine, org_id: uuid.UUID) -> list[str]:
        """Request deferred deletion of expired, unused versions of one line."""
        requested = []
        keep = ImageRetentionService.keep_for(org_id)
        with RetentionRepository.lock_line(line.runner_id):
            for image in ImageRetentionService._candidates(line, keep):
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
                requested += ImageRetentionService.apply(line, org_id)
            except Exception:
                logger.exception(
                    "image_retention_failed", line_kind=line.kind, line_id=str(line.id)
                )
        for line in RetentionRepository.pending_lines():
            try:
                requested += ImageRetentionService.heal(line)
            except Exception:
                logger.exception("image_line_heal_failed", line_id=str(line.id))
        for line_id in RetentionRepository.finalizable_line_ids():
            ImageLineRepository.finalize_captured(line_id)
        return requested

    @staticmethod
    def heal(line: CapturedImage) -> list[str]:
        """A line being deleted always converges: orphaned versions get a request."""
        if RetentionRepository.has_line_deletion(line.id):
            return []
        requested = []
        with RetentionRepository.lock_line(line.runner_id):
            reserved = ImageLineRepository.reserved_image_ids()
            for image in RetentionRepository.recoverable_versions(line.id):
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
