"""Version presentation and captured-image management shared by REST and MCP."""

from __future__ import annotations

import uuid

from common.exceptions import ConflictError, NotFoundError

from ..image_lines import ImageLine, ImageLineRepository
from ..retention_repository import RetentionRepository


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
        images = list(images)
        lines: dict[ImageLine, list] = {}
        extras: dict[uuid.UUID, dict] = {}
        for image in images:
            line = self.lines.line_of(image)
            if line is None:
                extras[image.id] = self._extra(image, None, {}, set())
            else:
                lines.setdefault(line, []).append(image)
        usage = self.lines.pinning_workspace_refs(i.id for i in images)
        for line, members in lines.items():
            org_id = members[0].runner.organization_id
            labels = self.lines.retention_labels(
                line, RetentionRepository.keep_for(org_id)
            )
            latest = self.lines.latest(line)
            for image in members:
                extras[image.id] = self._extra(
                    image, line, labels, usage.get(image.id, [])
                )
                extras[image.id]["is_latest"] = bool(latest and latest.id == image.id)
        return extras

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
        return [
            self.captured_image(line)
            for line in self.lines.captured_lines(org_id, user.id)
        ]

    def captured_image(self, line) -> dict:
        """Captured image with versions and usage."""
        ref = ImageLine("captured", line.id, line.name, line.runner_id)
        versions = self.lines.version_rows(ref)
        extras = self.version_extras(versions)
        latest = self.lines.latest(ref)
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
