"""Map image artifacts and captured-image lines to their public schemas.

Shared by the REST routers (``api.py``) and the MCP tools so both expose
identical image payloads without importing each other.
"""

from __future__ import annotations

from .enums import RunnerStatus
from .schemas import CapturedImageOut, ImageArtifactOut
from .services.image_versions import ImageVersionService


def image_artifact_to_out(artifact, extras: dict | None = None) -> ImageArtifactOut:
    """Map an ImageInstance to ImageArtifactOut.

    Runner and runtime come from the image itself. ``origin_workspace``
    is provenance only and may be unset after the source is deleted.
    ``extras`` carries version facts (latest, retention, usage) computed
    per line by :class:`ImageVersionService`.
    """
    if extras is None:
        extras = ImageVersionService().version_extras([artifact])[artifact.id]
    runner_build = getattr(artifact, "build_job", None)
    image_runner = getattr(artifact, "runner", None)
    runtime_type = getattr(artifact, "runtime_type", None)
    source_runner_id = getattr(image_runner, "id", None)
    source_runner_online = False
    if image_runner is not None:
        source_runner_online = image_runner.status == RunnerStatus.ONLINE
    source_definition_name = None
    is_deactivated = False
    if runner_build is not None:
        source_runner_id = getattr(runner_build, "runner_id", source_runner_id)
        runner = getattr(runner_build, "runner", None)
        if runner is not None:
            source_runner_id = runner.id
            source_runner_online = runner.status == RunnerStatus.ONLINE
        image_definition = getattr(runner_build, "image_definition", None)
        if image_definition is not None:
            source_definition_name = image_definition.name
        is_deactivated = getattr(runner_build, "status", "") == "deactivated"
    if getattr(artifact, "status", "") == "retired":
        is_deactivated = True

    return ImageArtifactOut(
        id=artifact.id,
        revision_id=artifact.revision_id,
        generation=artifact.generation,
        is_legacy=artifact.is_legacy,
        is_current=bool(
            runner_build and runner_build.current_generation_id == artifact.id
        ),
        source_workspace_id=artifact.origin_workspace_id,
        runner_artifact_id=artifact.runner_ref,
        name=artifact.name,
        size_bytes=artifact.size_bytes,
        status=artifact.status,
        artifact_kind=(
            "built"
            if getattr(artifact, "origin_type", "") == "definition_build"
            else "captured"
        ),
        build_job_id=artifact.build_job_id,
        source_definition_name=source_definition_name,
        source_runner_id=source_runner_id,
        runtime_type=runtime_type,
        is_deactivated=is_deactivated,
        source_runner_online=source_runner_online,
        delete_requested_at=getattr(artifact, "delete_requested_at", None),
        delete_confirmed_at=getattr(artifact, "delete_confirmed_at", None),
        delete_last_error=getattr(artifact, "delete_last_error", "") or "",
        created_at=artifact.created_at,
        created_by_id=artifact.created_by_id,
        captured_image_id=extras["captured_image_id"],
        version=extras["version"],
        message=extras["message"],
        is_latest=extras["is_latest"],
        retention=extras["retention"],
        workspace_count=extras["workspace_count"],
        workspaces=extras["workspaces"],
    )


def image_artifacts_to_out(artifacts) -> list[ImageArtifactOut]:
    """Map many versions, computing version facts once per line."""
    artifacts = list(artifacts)
    extras = ImageVersionService().version_extras(artifacts)
    return [image_artifact_to_out(a, extras[a.id]) for a in artifacts]


def captured_image_to_out(view: dict) -> CapturedImageOut:
    """Map an ImageVersionService captured-image view."""
    line = view["line"]
    latest = view["latest"]
    return CapturedImageOut(
        id=line.id,
        name=line.name,
        status=line.status,
        runner_id=line.runner_id,
        runner_online=line.runner.status == RunnerStatus.ONLINE,
        created_by_id=line.created_by_id,
        created_at=line.created_at,
        latest_id=latest.id if latest else None,
        latest_version=latest.generation if latest else None,
        total_size_bytes=view["total_size_bytes"],
        workspace_count=view["workspace_count"],
        versions=[
            image_artifact_to_out(v, view["extras"][v.id]) for v in view["versions"]
        ],
    )
