"""Version presentation and captured-image management."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from common.exceptions import ConflictError, NotFoundError

from ..image_lines import ImageLine, ImageLineRepository
from ..models import CapturedImage, ImageInstance
from ..retention_repository import RetentionRepository


@dataclass
class _VersionFacts:
    """Request-local facts shared by version rows and their owning lines."""

    extras: dict[uuid.UUID, dict]
    latest: dict[tuple[str, uuid.UUID], ImageInstance | None]


class ImageVersionService:
    """Answer "which version is this, is it latest, will it be kept?"."""

    def __init__(self) -> None:
        self.lines = ImageLineRepository

    def version_ref(self, image) -> dict | None:
        """Compact description of the version a workspace is based on."""
        if image is None:
            return None
        line = self.lines.line_of(image)
        latest = self.lines.latest(line)
        return {
            "id": image.id,
            "line_kind": line.kind if line else None,
            "line_id": line.id if line else None,
            "name": line.name if line else image.name,
            "version": image.generation,
            "message": image.message,
            "status": image.status,
            "latest_id": latest.id if latest else None,
            "latest_version": latest.generation if latest else None,
            "update_available": bool(latest and latest.id != image.id),
        }

    def version_extras(self, images) -> dict[uuid.UUID, dict]:
        """Latest/retention/usage facts per image, computed once per line."""
        return self._version_facts(images).extras

    def _version_facts(
        self,
        images: list[ImageInstance],
        *,
        requested_lines: list[ImageLine] | None = None,
    ) -> _VersionFacts:
        """Read the requested lines' latest, retention and pin facts in batches."""
        images = list(images)
        if not images:
            latest = self.lines.latest_many(requested_lines or [])
            return _VersionFacts(
                extras={},
                latest={(line.kind, line.id): image for line, image in latest.items()},
            )
        lines: dict[ImageLine, list] = {line: [] for line in requested_lines or []}
        extras: dict[uuid.UUID, dict] = {}
        for image in images:
            line = self.lines.line_of(image)
            if line is None:
                extras[image.id] = self._extra(image, None, {}, set())
            else:
                lines.setdefault(line, []).append(image)
        usage = self.lines.pinning_workspace_refs(i.id for i in images)
        keep_by_org = {
            org_id: RetentionRepository.keep_for(org_id)
            for org_id in {
                members[0].runner.organization_id
                for members in lines.values()
                if members
            }
        }
        latest_by_line = self.lines.latest_many(list(lines))
        labels_by_line = self.lines.retention_labels_many(
            {
                line: keep_by_org[members[0].runner.organization_id]
                for line, members in lines.items()
                if members
            },
            latest_by_line,
        )
        for line, members in lines.items():
            if not members:
                continue
            labels = labels_by_line[line]
            latest = latest_by_line[line]
            for image in members:
                extras[image.id] = self._extra(
                    image, line, labels, usage.get(image.id, [])
                )
                extras[image.id]["is_latest"] = bool(latest and latest.id == image.id)
        return _VersionFacts(
            extras=extras,
            latest={
                (line.kind, line.id): image for line, image in latest_by_line.items()
            },
        )

    @staticmethod
    def _extra(image, line, labels, workspaces) -> dict:
        return {
            "version": image.generation,
            "message": image.message,
            "is_latest": False,
            "retention": labels.get(image.id),
            "workspace_count": len(workspaces),
            "workspaces": list(workspaces),
            "captured_image_id": image.captured_image_id,
            "line_kind": line.kind if line else None,
        }

    def definition_versions(self, build) -> list:
        """All non-deleted versions (generations) of one runner build."""
        return self.lines.version_rows(
            ImageLine("definition", build.id, "", build.runner_id)
        )

    def list_captured_images(self, user, org_id: uuid.UUID) -> list[dict]:
        """A user's captured images with all versions, newest first."""
        lines = list(self.lines.captured_lines(org_id, user.id))
        if not lines:
            return []
        refs = [
            ImageLine("captured", line.id, line.name, line.runner_id) for line in lines
        ]
        versions = self.lines.version_rows_for_lines(refs)
        facts = self._version_facts(versions, requested_lines=refs)
        by_line: dict[uuid.UUID, list[ImageInstance]] = {line.id: [] for line in lines}
        for version in versions:
            by_line[version.captured_image_id].append(version)
        return [
            self._captured_image_view(line, by_line[line.id], facts) for line in lines
        ]

    def captured_image(self, line: CapturedImage) -> dict:
        """Captured image with versions and usage."""
        ref = ImageLine("captured", line.id, line.name, line.runner_id)
        versions = self.lines.version_rows(ref)
        facts = self._version_facts(versions, requested_lines=[ref])
        return self._captured_image_view(line, versions, facts)

    @staticmethod
    def _captured_image_view(
        line: CapturedImage,
        versions: list[ImageInstance],
        facts: _VersionFacts,
    ) -> dict:
        """Assemble one line from the request's batched version facts."""
        extras = {version.id: facts.extras[version.id] for version in versions}
        latest = facts.latest.get(("captured", line.id))
        return {
            "line": line,
            "versions": versions,
            "extras": extras,
            "latest": latest,
            "total_size_bytes": sum(v.size_bytes or 0 for v in versions),
            "workspace_count": sum(e["workspace_count"] for e in extras.values()),
        }

    def owned_captured_image(self, user, org_id: uuid.UUID, line_id: uuid.UUID):
        """A captured image the user owns, or NotFoundError."""
        line = self.lines.captured_line(line_id, org_id)
        if line is None or line.created_by_id != user.id or line.status == "deleted":
            raise NotFoundError("Captured image", str(line_id))
        return line

    def rename_captured_image(self, user, org_id, line_id, name: str) -> dict:
        """Rename an owned captured image."""
        name = (name or "").strip()
        if not name:
            raise ConflictError("Name must not be empty")
        line = self.owned_captured_image(user, org_id, line_id)
        self.lines.rename_captured(line.id, name[:255])
        line.name = name[:255]
        return self.captured_image(line)

    def latest_captured_version(self, user, org_id, line_id):
        """The version a new workspace from this captured image will use."""
        line = self.owned_captured_image(user, org_id, line_id)
        latest = self.lines.latest(
            ImageLine("captured", line.id, line.name, line.runner_id)
        )
        if latest is None:
            raise ConflictError("This image has no ready version")
        return latest
