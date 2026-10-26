"""Compose cached storage evidence, version usage and workflow diagnostics.

Inventory persistence supplies fully loaded records. Freshness, physical
reachability and user-visible storage state belong to this read service;
inspection never reconciles or mutates the underlying workflows.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from common.user_labels import owner_label

from ..activity_repository import ActivityRepository
from ..image_lines import ImageLineRepository
from ..inventory_repository import SIZES, InventoryRepository
from ..models import Runner
from ..retention_repository import RetentionRepository


class InventoryInspectionService:
    """Project persisted observations into a sanitized storage inspection."""

    @staticmethod
    def inspect(runner: Runner) -> dict:
        """Cached graph and latest diagnostics; never reconcile on GET."""
        now = timezone.now()
        data = InventoryRepository.load_inspection(runner)
        latest = data.latest
        result = {
            "runner_id": str(runner.id),
            "runner_online": runner.is_online,
            "runtimes": [],
            "latest_snapshot_id": latest.id if latest else None,
            "latest_complete": latest.complete if latest else False,
        }
        scan = data.scan
        diagnostic = data.diagnostic
        fresh = bool(
            scan
            and runner.is_online
            and latest
            and latest.complete
            and scan.snapshot_id == latest.id
            and scan.snapshot.session == runner.sid
            and scan.snapshot.received_at > now - timedelta(minutes=5)
            and now - timedelta(minutes=5) < scan.collected_at <= now
        )
        resources = []
        if scan:
            for observation in data.resources:
                resource = observation.resource
                ws = resource.workspace
                resources.append(
                    {
                        "physical_id": resource.physical_id,
                        "kind": resource.kind,
                        "managed": resource.managed,
                        "state": resource.state,
                        **{key: getattr(resource, key) for key in SIZES},
                        "aliases": observation.aliases,
                        "dependencies": observation.dependencies,
                        "image_id": str(resource.image_id)
                        if resource.image_id
                        else None,
                        "workspace": {
                            "id": str(ws.id),
                            "base_image_instance_id": str(ws.base_image_instance_id)
                            if ws.base_image_instance_id
                            else None,
                            "owner_id": str(ws.created_by_id),
                            "owner_label": owner_label(ws.created_by),
                            "name": ws.name,
                            "status": ws.status,
                            "last_activity_at": ws.last_activity_at,
                        }
                        if ws
                        else None,
                        "filesystem_id": resource.metadata.get("filesystem_id")
                        if isinstance(resource.metadata.get("filesystem_id"), str)
                        else None,
                        "file_identity": resource.metadata.get("file_identity")
                        if isinstance(resource.metadata.get("file_identity"), str)
                        else None,
                        "provenance": resource.provenance,
                    }
                )
        result["runtimes"].append(
            {
                "fresh": fresh,
                "snapshot_id": scan.snapshot_id if scan else None,
                "epoch": str(scan.snapshot.epoch) if scan else None,
                "sequence": scan.snapshot.sequence if scan else None,
                "collected_at": scan.collected_at if scan else None,
                "received_at": scan.snapshot.received_at if scan else None,
                "filesystems": scan.filesystems if scan else [],
                "resources": resources,
                "diagnostics": {
                    "complete": diagnostic.complete,
                    "errors": diagnostic.errors,
                    "collected_at": diagnostic.collected_at,
                    "foreign_resource_count": diagnostic.foreign_resource_count,
                }
                if diagnostic
                else None,
            }
        )
        workspaces = data.workspaces
        # Reverse physical dependencies recursively; provenance is deliberately ignored.
        # Domain observations alone describe workspace runtime state.
        # Disks retain dependency ownership, never state authority. Prefer managed
        # observations then stable identity, not known states over unknown ones.
        workspace_states = {}
        for runtime in result["runtimes"]:
            for resource in sorted(
                runtime["resources"],
                key=lambda r: (not r["managed"], r["physical_id"]),
            ):
                if resource["kind"] == "workspace" and resource["workspace"]:
                    key = resource["workspace"]["id"]
                    workspace_states.setdefault(key, resource["state"])
        # Allocated bytes of each workspace's own storage (disks and ISOs).
        workspace_bytes: dict[str, int] = {}
        for runtime in result["runtimes"]:
            for resource in runtime["resources"]:
                if resource["workspace"] and resource["kind"] != "workspace":
                    ws_key = resource["workspace"]["id"]
                    workspace_bytes[ws_key] = workspace_bytes.get(ws_key, 0) + (
                        resource["allocated_bytes"] or 0
                    )
        keep = RetentionRepository.keep_for(runner.organization_id)
        version_facts: dict[str, dict] = {}
        result["generations"] = []
        for image in data.images:
            line = ImageLineRepository.line_of(image)
            if line is not None and str(line) not in version_facts:
                latest_version = ImageLineRepository.latest(line)
                version_facts[str(line)] = {
                    "latest": latest_version.id if latest_version else None,
                    "labels": ImageLineRepository.retention_labels(line, keep),
                }
            facts = version_facts.get(str(line), {}) if line else {}
            image_resources = [
                resource
                for runtime in result["runtimes"]
                for resource in runtime["resources"]
            ]
            observed = [r for r in image_resources if r["image_id"] == str(image.id)]
            exact_references = [
                r
                for r in image_resources
                if image.runner_ref
                and image.runner_ref in [r["physical_id"], *r["aliases"]]
            ]
            consumers = set(r["physical_id"] for r in observed)
            while True:
                expanded = consumers | {
                    r["physical_id"]
                    for r in image_resources
                    if any(dep in consumers for dep in r["dependencies"])
                }
                if expanded == consumers:
                    break
                consumers = expanded
            physical_ws = {
                r["workspace"]["id"]
                for r in image_resources
                if r["physical_id"] in consumers and r["workspace"]
            }
            dependent_images = {
                r["image_id"] for r in image_resources if r["physical_id"] in consumers
            }
            dependent_images.add(str(image.id))
            dependents = [
                w
                for w in workspaces
                if (
                    str(w.base_image_instance_id) in dependent_images
                    or str(w.pending_base_image_instance_id) in dependent_images
                    or str(w.id) in physical_ws
                )
                and w.status not in {"deleted", "removed"}
            ]
            runtime = next(iter(result["runtimes"]), None)
            result["generations"].append(
                {
                    "id": str(image.id),
                    "name": image.name,
                    "owner_id": str(image.created_by_id)
                    if image.created_by_id
                    else None,
                    "owner_label": owner_label(image.created_by),
                    "build_job_id": str(image.build_job_id)
                    if image.build_job_id
                    else None,
                    "definition_id": str(image.origin_definition_id)
                    if image.origin_definition_id
                    else None,
                    "definition_name": image.origin_definition.name
                    if image.origin_definition
                    else None,
                    "generation": image.generation,
                    "captured_image_id": str(image.captured_image_id)
                    if image.captured_image_id
                    else None,
                    "line_name": line.name if line else image.name,
                    "message": image.message,
                    "is_latest": bool(facts and facts["latest"] == image.id),
                    "retention": facts.get("labels", {}).get(image.id)
                    if facts
                    else None,
                    "status": image.status,
                    "runner_ref": image.runner_ref,
                    "size_bytes": image.size_bytes,
                    "origin_type": image.origin_type,
                    "revision_id": str(image.revision_id)
                    if image.revision_id
                    else None,
                    "is_legacy": image.is_legacy,
                    "build_job__current_generation_id": str(
                        image.build_job.current_generation_id
                    )
                    if image.build_job and image.build_job.current_generation_id
                    else None,
                    "is_current": bool(
                        image.build_job
                        and image.status != "deleted"
                        and image.build_job.status
                        not in {"deleted", "retired", "deactivated"}
                        and image.build_job.current_generation_id == image.id
                    ),
                    "is_pending": bool(
                        image.build_job
                        and image.build_job.pending_generation_id == image.id
                    ),
                    "assignment_status": image.build_job.status
                    if image.build_job
                    else None,
                    "observed_state": observed[0]["state"]
                    if observed
                    else (
                        "unknown"
                        if exact_references
                        else (
                            ("missing" if image.runner_ref else "not_published")
                            if runtime and runtime["fresh"]
                            else "unknown"
                        )
                    ),
                    "size_source": "observed allocated"
                    if observed
                    else "last recorded / unknown",
                    "dependencies": [
                        {
                            "id": str(w.id),
                            "base_image_instance_id": str(w.base_image_instance_id)
                            if w.base_image_instance_id
                            else None,
                            "pending_base_image_instance_id": str(
                                w.pending_base_image_instance_id
                            )
                            if w.pending_base_image_instance_id
                            else None,
                            "allocated_bytes": workspace_bytes.get(str(w.id)),
                            "name": w.name,
                            "owner_id": str(w.created_by_id),
                            "owner_label": owner_label(w.created_by),
                            "status": w.status,
                            "last_activity_at": w.last_activity_at,
                            "observed_state": workspace_states.get(
                                str(w.id), "unknown"
                            ),
                        }
                        for w in dependents
                    ],
                }
            )
        result["pin_diagnostics"] = []
        if latest:
            for physical in data.pin_resources:
                if (
                    physical.workspace
                    and physical.workspace.base_image_instance_id is None
                ):
                    result["pin_diagnostics"].append(
                        {
                            "workspace_id": str(physical.workspace_id),
                            "diagnostic": (
                                "Legacy dependency unproven or ambiguous; "
                                "no pin guessed"
                            ),
                        }
                    )
        result.update(ActivityRepository.records(runner))
        return result
