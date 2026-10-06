"""Image lines: one version model for captured images and definition builds.

A line is what users see as "one image". Captured images are versioned by
``CapturedImage``; definition builds by ``ImageBuildJob`` (definition x runner).
Every version is a standalone ``ImageInstance`` whose ``generation`` is its
version number. All ORM access for versions, pins and retention lives here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.db.models import Count, Q, QuerySet

from .models import (
    CapturedImage,
    ImageBuildJob,
    ImageDeletionRequest,
    ImageInstance,
    Workspace,
)

PIN_TERMINAL_STATUSES = ["deleted", "removed"]
DONE_DELETION_PHASES = ["completed", "cancelled"]
ACTIVE_JOB_STATUSES = {"active"}


@dataclass(frozen=True)
class ImageLine:
    """Identity of a version line; ``kind`` is ``captured`` or ``definition``."""

    kind: str
    id: uuid.UUID
    name: str
    runner_id: uuid.UUID

    @property
    def filter(self) -> Q:
        """Version filter for this line."""
        if self.kind == "captured":
            return Q(captured_image_id=self.id)
        return Q(build_job_id=self.id)


class ImageLineRepository:
    """Resolve lines, latest versions, pins and retention for both line kinds."""

    @staticmethod
    def line_of(image: ImageInstance | None) -> ImageLine | None:
        """Return the line of a version, or None for untracked legacy images."""
        if image is None:
            return None
        if image.captured_image_id:
            line = image.captured_image
            return ImageLine("captured", line.id, line.name, line.runner_id)
        if image.build_job_id:
            job = image.build_job
            return ImageLine(
                "definition", job.id, job.image_definition.name, job.runner_id
            )
        return None

    @staticmethod
    def captured(line_id: uuid.UUID) -> ImageLine | None:
        """Return a captured line by id."""
        line = CapturedImage.objects.filter(pk=line_id).first()
        if line is None:
            return None
        return ImageLine("captured", line.id, line.name, line.runner_id)

    @staticmethod
    def versions(line: ImageLine) -> QuerySet[ImageInstance]:
        """All non-deleted versions of a line, newest first."""
        return (
            ImageInstance.objects.filter(line.filter)
            .exclude(status="deleted")
            .order_by("-generation", "-created_at")
        )

    @staticmethod
    def latest(line: ImageLine | None) -> ImageInstance | None:
        """The version new workspaces and updates use; None when unavailable."""
        if line is None:
            return None
        if line.kind == "captured":
            if not CapturedImage.objects.filter(pk=line.id, status="active").exists():
                return None
            return (
                ImageInstance.objects.filter(line.filter, status="ready")
                .exclude(runner_ref="")
                .order_by("-generation")
                .first()
            )
        job = (
            ImageBuildJob.objects.select_related(
                "current_generation", "image_definition"
            )
            .filter(pk=line.id)
            .first()
        )
        if (
            job is None
            or job.status not in ACTIVE_JOB_STATUSES
            or job.image_definition.status != "active"
            or job.current_generation is None
            or job.current_generation.status != "ready"
        ):
            return None
        return job.current_generation

    @staticmethod
    def latest_for(image: ImageInstance | None) -> ImageInstance | None:
        """Latest version of the line an image belongs to."""
        return ImageLineRepository.latest(ImageLineRepository.line_of(image))

    @staticmethod
    def pinning_workspaces(image_ids) -> QuerySet[Workspace]:
        """Workspaces based on (or being recreated onto) any of the images."""
        ids = list(image_ids)
        return Workspace.objects.filter(
            Q(base_image_instance_id__in=ids)
            | Q(pending_base_image_instance_id__in=ids)
        ).exclude(status__in=PIN_TERMINAL_STATUSES)

    @staticmethod
    def pin_counts(image_ids) -> dict[uuid.UUID, int]:
        """Number of pinning workspaces per image id."""
        ids = list(image_ids)
        counts: dict[uuid.UUID, int] = {}
        live = Workspace.objects.exclude(status__in=PIN_TERMINAL_STATUSES)
        for field in ("base_image_instance_id", "pending_base_image_instance_id"):
            rows = (
                live.filter(**{f"{field}__in": ids})
                .values(field)
                .annotate(total=Count("id"))
            )
            for row in rows:
                counts[row[field]] = counts.get(row[field], 0) + row["total"]
        return counts

    @staticmethod
    def kept_ids(line: ImageLine, keep: int) -> set[uuid.UUID]:
        """Latest plus the ``keep`` newest ready versions are always retained."""
        ready = list(
            ImageInstance.objects.filter(line.filter, status="ready")
            .order_by("-generation")
            .values_list("id", flat=True)[: max(keep, 1)]
        )
        latest = ImageLineRepository.latest(line)
        return set(ready) | ({latest.id} if latest else set())

    @staticmethod
    def retention_labels(line: ImageLine, keep: int) -> dict[uuid.UUID, str]:
        """Label ready versions ``latest``, ``kept`` or ``expires_when_unused``."""
        latest = ImageLineRepository.latest(line)
        kept = ImageLineRepository.kept_ids(line, keep)
        labels = {}
        for image_id in ImageInstance.objects.filter(
            line.filter, status="ready"
        ).values_list("id", flat=True):
            if latest and image_id == latest.id:
                labels[image_id] = "latest"
            elif image_id in kept:
                labels[image_id] = "kept"
            else:
                labels[image_id] = "expires_when_unused"
        return labels

    @staticmethod
    def retention_candidates(line: ImageLine, keep: int) -> list[ImageInstance]:
        """Ready versions outside the retained window that nothing depends on."""
        kept = ImageLineRepository.kept_ids(line, keep)
        expired = list(
            ImageInstance.objects.filter(line.filter, status="ready").exclude(
                pk__in=kept
            )
        )
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
    def reserved_image_ids(
        exclude_target: tuple[str, uuid.UUID] | None = None,
    ) -> set[str]:
        """Image ids already owned by a live deletion request."""
        reserved: set[str] = set()
        requests = ImageDeletionRequest.objects.exclude(phase__in=DONE_DELETION_PHASES)
        if exclude_target is not None:
            requests = requests.exclude(
                target_type=exclude_target[0], target_id=exclude_target[1]
            )
        for request in requests:
            reserved |= {
                key.split(":", 1)[1]
                for key in request.previous
                if key.startswith("image:")
            }
        return reserved

    @staticmethod
    def captured_lines(org_id: uuid.UUID, created_by_id) -> QuerySet[CapturedImage]:
        """A user's visible captured images in an organization."""
        return (
            CapturedImage.objects.filter(
                organization_id=org_id, created_by_id=created_by_id
            )
            .exclude(status="deleted")
            .select_related("runner")
        )

    @staticmethod
    def captured_line(line_id: uuid.UUID, org_id: uuid.UUID) -> CapturedImage | None:
        """One captured image of an organization."""
        return (
            CapturedImage.objects.filter(pk=line_id, organization_id=org_id)
            .select_related("runner")
            .first()
        )

    @staticmethod
    def rename_captured(line_id: uuid.UUID, name: str) -> None:
        """Rename a line; version rows keep their immutable publication name."""
        CapturedImage.objects.filter(pk=line_id).update(name=name)

    @staticmethod
    def version_rows(line: ImageLine) -> list[ImageInstance]:
        """Versions with the relations needed for presentation."""
        return list(
            ImageLineRepository.versions(line).select_related(
                "runner", "build_job__runner", "build_job__image_definition"
            )
        )

    @staticmethod
    def pinning_workspace_refs(image_ids) -> dict[uuid.UUID, list[dict]]:
        """Workspaces (id, name, owner) per pinned image id."""
        ids = set(image_ids)
        refs: dict[uuid.UUID, list[dict]] = {}
        for ws in ImageLineRepository.pinning_workspaces(ids).only(
            "id",
            "name",
            "created_by_id",
            "base_image_instance_id",
            "pending_base_image_instance_id",
        ):
            for image_id in {
                ws.base_image_instance_id,
                ws.pending_base_image_instance_id,
            } & ids:
                refs.setdefault(image_id, []).append(
                    {"id": ws.id, "name": ws.name, "created_by_id": ws.created_by_id}
                )
        return refs

    @staticmethod
    def finalize_captured(line_id: uuid.UUID) -> bool:
        """A line without remaining versions is deleted; returns whether it was."""
        if (
            ImageInstance.objects.filter(captured_image_id=line_id)
            .exclude(status="deleted")
            .exists()
        ):
            return False
        return bool(
            CapturedImage.objects.filter(pk=line_id)
            .exclude(status="deleted")
            .update(status="deleted")
        )

    @staticmethod
    def next_captured_version(line_id: uuid.UUID) -> int:
        """Monotonic version number; numbers of failed versions are never reused."""
        current = (
            ImageInstance.objects.filter(captured_image_id=line_id)
            .order_by("-generation")
            .values_list("generation", flat=True)
            .first()
        )
        return (current or 0) + 1
