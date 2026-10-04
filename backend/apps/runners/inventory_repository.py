"""Normalized, session-bound runtime observations and conservative identity joins."""

import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    CaptureRequest,
    ImageInstance,
    InventoryAlias,
    InventoryEdge,
    InventoryRefresh,
    InventoryResource,
    InventoryRuntime,
    InventorySnapshot,
    LifecycleCommand,
    Runner,
    Workspace,
)

SIZES = (
    "allocated_bytes",
    "logical_bytes",
    "virtual_bytes",
    "shared_bytes",
    "reclaimable_bytes",
)


class InventoryRepository:
    """Persist scans without converting database intentions into physical facts."""

    @staticmethod
    def owner_label(user) -> str:
        """Safe display name, never an email or authentication identifier."""
        if not user:
            return "Unknown owner"
        name = user.get_full_name().strip()
        return name if name and "@" not in name else f"User {user.pk}"

    @staticmethod
    def record(runner_id: str, session: str, payload: dict) -> bool:
        """Reject obsolete sessions/sequences atomically; malformed scans roll back."""
        if payload.get("schema_version") != 1:
            return False
        try:
            epoch = uuid.UUID(payload["inventory_epoch"])
            sequence = payload["inventory_sequence"]
            if type(sequence) is not int or sequence < 0:
                return False
            with transaction.atomic():
                runner = Runner.objects.select_for_update().get(pk=runner_id)
                if runner.sid != session:
                    return False
                previous = (
                    InventorySnapshot.objects.filter(runner=runner, session=session)
                    .order_by("-id")
                    .first()
                )
                if previous:
                    if previous.epoch == epoch and sequence <= previous.sequence:
                        return False
                    if previous.epoch != epoch:
                        # Epoch changes require a new authenticated connection.
                        return False
                scans = payload["runtimes"]
                runtime_names = [scan["runtime_type"] for scan in scans]
                if len(set(runtime_names)) != len(runtime_names):
                    return False
                snapshot = InventorySnapshot.objects.create(
                    runner=runner,
                    session=session,
                    epoch=epoch,
                    sequence=sequence,
                    complete=bool(payload.get("complete"))
                    and bool(scans)
                    and all(scan.get("complete") is True for scan in scans)
                    and set(runner.available_runtimes).issubset(runtime_names),
                )
                images = list(ImageInstance.objects.filter(runner=runner))
                workspaces = {
                    str(w.id): w for w in Workspace.objects.filter(runner=runner)
                }
                for scan in scans:
                    collected = parse_datetime(scan["collected_at"])
                    if collected is None or timezone.is_naive(collected):
                        raise ValueError("Inventory timestamp requires timezone")
                    runtime = InventoryRuntime.objects.create(
                        snapshot=snapshot,
                        runtime_type=scan["runtime_type"],
                        collected_at=collected,
                        complete=scan.get("complete") is True,
                        errors=scan.get("errors", []),
                        filesystems=scan.get("filesystems", []),
                        foreign_resource_count=scan.get("foreign_resource_count", 0),
                    )
                    resources = {}
                    mapped_images = set()
                    raw = scan["resources"]
                    for item in raw:
                        physical_id = item["resource_id"]
                        aliases = item.get("aliases", [])
                        metadata = item.get("metadata", {})
                        candidates = [
                            i
                            for i in images
                            if i.runtime_type == runtime.runtime_type
                            and (
                                item["kind"] == "image"
                                or (
                                    runtime.runtime_type == "qemu"
                                    and i.is_legacy
                                    and item["kind"] == "disk"
                                )
                            )
                            and (
                                runtime.runtime_type != "qemu"
                                or i.is_legacy
                                or (
                                    str(i.id) == metadata.get("artifact_id")
                                    and i.creating_task_id is not None
                                    and str(i.creating_task_id)
                                    == metadata.get("operation_id")
                                )
                            )
                            and (
                                bool(i.runner_ref)
                                and i.runner_ref in [physical_id, *aliases]
                                or (
                                    runtime.runtime_type == "docker"
                                    and metadata.get("published_image_id")
                                    == physical_id
                                    and metadata.get("expected_image_tag")
                                    == i.runner_ref
                                    and str(i.id)
                                    == metadata.get("opencuria.image-instance-id")
                                )
                                or (
                                    runtime.runtime_type == "qemu"
                                    and str(i.id) == metadata.get("artifact_id")
                                )
                            )
                        ]
                        asserted_id = metadata.get(
                            "opencuria.image-instance-id"
                        ) or metadata.get("artifact_id")
                        image = candidates[0] if len(candidates) == 1 else None
                        if image and asserted_id and str(image.id) != asserted_id:
                            image = None
                        if image and image.is_legacy and runtime.runtime_type == "qemu":
                            # Exact references establish association, not publication.
                            # Shared aliases cannot identify a unique physical object.
                            matches = [
                                other
                                for other in raw
                                if image.runner_ref
                                and image.runner_ref
                                in [other["resource_id"], *other.get("aliases", [])]
                            ]
                            if len(matches) != 1:
                                image = None
                        if image:
                            if image.id in mapped_images:
                                raise ValueError(
                                    "Ambiguous physical generation identity"
                                )
                            mapped_images.add(image.id)
                        ws_id = metadata.get("workspace_id") or metadata.get(
                            "opencuria.workspace-id"
                        )
                        workspace = workspaces.get(str(ws_id))
                        sizes = {key: item.get(key) for key in SIZES}
                        if any(
                            value is not None and (type(value) is not int or value < 0)
                            for value in sizes.values()
                        ):
                            raise ValueError("Invalid resource size")
                        resource = InventoryResource.objects.create(
                            runtime=runtime,
                            physical_id=physical_id,
                            kind=item["kind"],
                            managed=item["managed"],
                            state=(
                                "unknown"
                                if image
                                and image.is_legacy
                                and runtime.runtime_type == "qemu"
                                and item["kind"] == "disk"
                                else item.get("state", "unknown")
                            ),
                            **sizes,
                            provenance=item.get("provenance", ""),
                            metadata=metadata,
                            image=image,
                            workspace=workspace,
                        )
                        resources[physical_id] = resource
                        InventoryAlias.objects.bulk_create(
                            [
                                InventoryAlias(resource=resource, reference=alias)
                                for alias in set(aliases)
                            ]
                        )
                        if (
                            image
                            and resource.managed
                            and runtime.complete
                            and resource.state != "unknown"
                            and timezone.now() - timedelta(minutes=5)
                            < collected
                            <= timezone.now()
                        ):
                            # No invented zero or aggregate Docker layer sum.
                            size = (
                                sizes["allocated_bytes"]
                                if runtime.runtime_type == "qemu"
                                else sizes["logical_bytes"]
                            )
                            ImageInstance.objects.filter(pk=image.id).update(
                                size_bytes=size
                            )
                    for item in raw:
                        for target_id in set(item.get("dependencies", [])):
                            if target_id not in resources:
                                resources[target_id] = InventoryResource.objects.create(
                                    runtime=runtime,
                                    physical_id=target_id,
                                    kind="unknown",
                                    managed=False,
                                )
                            InventoryEdge.objects.create(
                                source=resources[item["resource_id"]],
                                target=resources[target_id],
                            )
                    if (
                        runtime.complete
                        and timezone.now() - timedelta(minutes=5)
                        < collected
                        <= timezone.now()
                    ):
                        for item in raw:
                            source = resources[item["resource_id"]]
                            if (
                                source.kind != "workspace"
                                or not source.workspace_id
                                or not source.managed
                            ):
                                continue
                            dependencies = [
                                resources[ref].image_id
                                for ref in item.get("dependencies", [])
                                if resources[ref].image_id
                            ]
                            if len(set(dependencies)) != 1:
                                continue
                            ws = workspaces.get(str(source.workspace_id))
                            image_id = dependencies[0]
                            image = next(i for i in images if i.id == image_id)
                            if (
                                ws.runtime_type == runtime.runtime_type
                                and ws.base_image_instance_id is None
                                and not ws.current_task_id
                            ):
                                Workspace.objects.filter(
                                    pk=ws.id,
                                    base_image_instance__isnull=True,
                                    current_task__isnull=True,
                                ).update(base_image_instance_id=image_id)
                if snapshot.complete:
                    InventoryRefresh.objects.filter(
                        runner=runner,
                        requested_at__lt=snapshot.received_at,
                        fulfilled_at__isnull=True,
                    ).update(fulfilled_at=timezone.now())
                return True
        except (ValueError, TypeError, KeyError, IntegrityError, Runner.DoesNotExist):
            return False

    @staticmethod
    def request_refresh(runner: Runner) -> dict:
        """Queue scans even offline; a partial report does not satisfy the request."""
        now = timezone.now()
        InventoryRefresh.objects.update_or_create(
            runner=runner,
            defaults={
                "requested_at": now,
                "next_delivery_at": now,
                "fulfilled_at": None,
            },
        )
        return {"requested": True, "runner_id": str(runner.id), "requested_at": now}

    @staticmethod
    def refresh_candidates() -> list:
        """Lease pending requests with bounded polling; reconnect uses current SID."""
        now = timezone.now()
        with transaction.atomic():
            rows = list(
                InventoryRefresh.objects.select_for_update(of=("self",))
                .select_related("runner")
                .filter(
                    fulfilled_at__isnull=True,
                    next_delivery_at__lte=now,
                    runner__status="online",
                )
                .exclude(runner__sid="")[:100]
            )
            for row in rows:
                row.next_delivery_at = now + timedelta(seconds=30)
                row.save(update_fields=["next_delivery_at"])
            return rows

    @staticmethod
    def detail(runner: Runner) -> dict:
        """Cached graph and latest diagnostics; never reconcile on GET."""
        now = timezone.now()
        latest = InventorySnapshot.objects.filter(runner=runner).order_by("-id").first()
        result = {
            "runner_id": str(runner.id),
            "runner_online": runner.is_online,
            "runtimes": [],
            "latest_snapshot_id": latest.id if latest else None,
            "latest_complete": latest.complete if latest else False,
        }
        for runtime_type in runner.available_runtimes:
            scan = (
                InventoryRuntime.objects.filter(
                    snapshot__runner=runner, runtime_type=runtime_type, complete=True
                )
                .select_related("snapshot")
                .order_by("-id")
                .first()
            )
            diagnostic = (
                InventoryRuntime.objects.filter(
                    snapshot__runner=runner, runtime_type=runtime_type
                )
                .order_by("-id")
                .first()
            )
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
                for resource in scan.resources.select_related(
                    "workspace__created_by", "image"
                ):
                    ws = resource.workspace
                    resources.append(
                        {
                            "physical_id": resource.physical_id,
                            "kind": resource.kind,
                            "managed": resource.managed,
                            "state": resource.state,
                            **{key: getattr(resource, key) for key in SIZES},
                            "aliases": list(
                                resource.aliases.values_list("reference", flat=True)
                            ),
                            "dependencies": list(
                                resource.dependencies.values_list(
                                    "target__physical_id", flat=True
                                )
                            ),
                            "image_id": str(resource.image_id)
                            if resource.image_id
                            else None,
                            "workspace": {
                                "id": str(ws.id),
                                "owner_id": str(ws.created_by_id),
                                "owner_label": InventoryRepository.owner_label(
                                    ws.created_by
                                ),
                                "name": ws.name,
                                "status": ws.status,
                                "last_activity_at": ws.last_activity_at,
                            }
                            if ws
                            else None,
                            "provenance": resource.provenance,
                        }
                    )
            result["runtimes"].append(
                {
                    "runtime_type": runtime_type,
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
        workspaces = list(
            Workspace.objects.filter(runner=runner).select_related("created_by")
        )
        # Reverse physical dependencies recursively; provenance is deliberately ignored.
        # Domain/container observations alone describe workspace runtime state.
        # Disks retain dependency ownership, never state authority. Prefer managed
        # observations then stable identity, not known states over unknown ones.
        workspace_states = {}
        for runtime in result["runtimes"]:
            for resource in sorted(
                runtime["resources"],
                key=lambda r: (not r["managed"], r["physical_id"]),
            ):
                if resource["kind"] == "workspace" and resource["workspace"]:
                    key = (runtime["runtime_type"], resource["workspace"]["id"])
                    workspace_states.setdefault(key, resource["state"])
        result["generations"] = []
        for image in ImageInstance.objects.filter(runner=runner).select_related(
            "created_by", "build_job", "origin_definition"
        ):
            image_resources = [
                resource
                for runtime in result["runtimes"]
                if runtime["runtime_type"] == image.runtime_type
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
                    or str(w.id) in physical_ws
                )
                and w.status not in {"deleted", "removed"}
            ]
            runtime = next(
                (
                    r
                    for r in result["runtimes"]
                    if r["runtime_type"] == image.runtime_type
                ),
                None,
            )
            result["generations"].append(
                {
                    "id": str(image.id),
                    "name": image.name,
                    "owner_id": str(image.created_by_id)
                    if image.created_by_id
                    else None,
                    "owner_label": InventoryRepository.owner_label(image.created_by),
                    "runtime_type": image.runtime_type,
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
                    if observed and image.runtime_type == "qemu"
                    else (
                        "observed logical (overlapping)"
                        if observed
                        else "last recorded / unknown"
                    ),
                    "dependencies": [
                        {
                            "id": str(w.id),
                            "name": w.name,
                            "owner_id": str(w.created_by_id),
                            "owner_label": InventoryRepository.owner_label(
                                w.created_by
                            ),
                            "status": w.status,
                            "last_activity_at": w.last_activity_at,
                            "observed_state": workspace_states.get(
                                (w.runtime_type, str(w.id)), "unknown"
                            ),
                        }
                        for w in dependents
                    ],
                }
            )
        result["capture_requests"] = list(
            CaptureRequest.objects.filter(workspace__runner=runner).values(
                "id",
                "workspace_id",
                "image_id",
                "phase",
                "child_id",
                "prior_running",
                "resume_suppressed",
                "diagnostic",
                "created_at",
            )
        )
        result["pin_diagnostics"] = []
        if latest:
            for physical in InventoryResource.objects.filter(
                runtime__snapshot=latest, kind="workspace"
            ).select_related("workspace"):
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
        result["operations"] = list(
            LifecycleCommand.objects.filter(task__runner=runner)
            .exclude(task__status="completed")
            .values(
                "task_id",
                "task__status",
                "task__error",
                "phase",
                "target",
                "heartbeat_at",
                "deadline_at",
                "deliveries",
            )
        )
        return result
