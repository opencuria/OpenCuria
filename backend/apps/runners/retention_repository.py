"""Materialized image-retention inputs and the runner serialization boundary."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import transaction

from apps.organizations.models import Organization

from .image_lines import DONE_DELETION_PHASES, ImageLine
from .locking import lock_runner
from .models import CapturedImage, ImageBuildJob, ImageDeletionRequest, ImageInstance

INACTIVE_JOB_STATUSES = ["pending_deletion", "deleting", "deleted", "delete_failed"]


class RetentionRepository:
    """Read retention inputs; workflow and deletion decisions live in the service."""

    @staticmethod
    def lines() -> list[tuple[ImageLine, uuid.UUID]]:
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
    def configured_keep(org_id: uuid.UUID) -> int | None:
        """Read the stored retention setting without applying policy defaults."""
        return (
            Organization.objects.filter(pk=org_id)
            .values_list("image_versions_to_keep", flat=True)
            .first()
        )

    @staticmethod
    @contextmanager
    def lock_line(runner_id: uuid.UUID) -> Iterator[None]:
        """Keep selection and retirement atomic with capture and workspace pins."""
        with transaction.atomic():
            lock_runner(runner_id)
            yield

    @staticmethod
    def pending_lines() -> list[CapturedImage]:
        """Load captured lines pending deletion for orphan recovery."""
        return list(CapturedImage.objects.filter(status="pending_deletion"))

    @staticmethod
    def finalizable_line_ids() -> list[uuid.UUID]:
        """Read line ids whose remaining versions may have disappeared."""
        return list(
            CapturedImage.objects.filter(
                status__in=["active", "pending_deletion"]
            ).values_list("id", flat=True)
        )

    @staticmethod
    def has_line_deletion(line_id: uuid.UUID) -> bool:
        """Read whether a live request already owns deletion of the whole line."""
        return (
            ImageDeletionRequest.objects.filter(
                target_type="captured_image", target_id=line_id
            )
            .exclude(phase__in=DONE_DELETION_PHASES)
            .exists()
        )

    @staticmethod
    def recoverable_versions(line_id: uuid.UUID) -> list[ImageInstance]:
        """Load versions outside deleted, deleting and in-flight capture states."""
        return list(
            ImageInstance.objects.filter(captured_image_id=line_id).exclude(
                status__in=["deleted", "deleting", "capturing"]
            )
        )
