"""Normalized, session-bound runtime observations and conservative identity joins."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    ImageInstance,
    InventoryAlias,
    InventoryEdge,
    InventoryRefresh,
    InventoryResource,
    InventoryRuntime,
    InventorySnapshot,
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


@dataclass(frozen=True)
class InventoryObservation:
    """One loaded physical resource and its materialized graph references."""

    resource: InventoryResource
    aliases: list[str]
    dependencies: list[str]


@dataclass(frozen=True)
class InventoryInspectionData:
    """Persisted inspection inputs with all consumed relations already loaded."""

    latest: InventorySnapshot | None
    scan: InventoryRuntime | None
    diagnostic: InventoryRuntime | None
    resources: list[InventoryObservation]
    workspaces: list[Workspace]
    images: list[ImageInstance]
    pin_resources: list[InventoryResource]


class InventoryRepository:
    """Persist scans without converting database intentions into physical facts."""

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
                if len(scans) != 1:
                    return False
                snapshot = InventorySnapshot.objects.create(
                    runner=runner,
                    session=session,
                    epoch=epoch,
                    sequence=sequence,
                    complete=bool(payload.get("complete"))
                    and bool(scans)
                    and all(scan.get("complete") is True for scan in scans),
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
                            if (
                                item["kind"] == "image"
                                or ((i.is_legacy) and (item["kind"] == "disk"))
                            )
                            and (
                                (i.is_legacy)
                                or (
                                    str(i.id) == metadata.get("artifact_id")
                                    and i.creating_task_id is not None
                                    and str(i.creating_task_id)
                                    == metadata.get("operation_id")
                                )
                            )
                            and (
                                (
                                    bool(i.runner_ref)
                                    and i.runner_ref in [physical_id, *aliases]
                                )
                                or (str(i.id) == metadata.get("artifact_id"))
                            )
                        ]
                        asserted_id = metadata.get("artifact_id")
                        image = candidates[0] if len(candidates) == 1 else None
                        if image and asserted_id and str(image.id) != asserted_id:
                            image = None
                        if (image) and (image.is_legacy):
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
                        ws_id = metadata.get("workspace_id")
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
                                if (image)
                                and (image.is_legacy)
                                and (item["kind"] == "disk")
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
                            size = (
                                sizes["allocated_bytes"]
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
                                ws.base_image_instance_id is None
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
    def load_inspection(runner: Runner) -> InventoryInspectionData:
        """Load cached observations and logical pins without deriving storage state."""
        latest = InventorySnapshot.objects.filter(runner=runner).order_by("-id").first()
        scan = (
            InventoryRuntime.objects.filter(snapshot__runner=runner, complete=True)
            .select_related("snapshot")
            .order_by("-id")
            .first()
        )
        diagnostic = (
            InventoryRuntime.objects.filter(snapshot__runner=runner)
            .order_by("-id")
            .first()
        )
        resources = [
            InventoryObservation(
                resource=resource,
                aliases=list(resource.aliases.values_list("reference", flat=True)),
                dependencies=list(
                    resource.dependencies.values_list("target__physical_id", flat=True)
                ),
            )
            for resource in (
                scan.resources.select_related("workspace__created_by", "image")
                if scan
                else []
            )
        ]
        return InventoryInspectionData(
            latest=latest,
            scan=scan,
            diagnostic=diagnostic,
            resources=resources,
            workspaces=list(
                Workspace.objects.filter(runner=runner).select_related("created_by")
            ),
            images=list(
                ImageInstance.objects.filter(runner=runner).select_related(
                    "created_by",
                    "build_job__image_definition",
                    "origin_definition",
                    "captured_image",
                )
            ),
            pin_resources=(
                list(
                    InventoryResource.objects.filter(
                        runtime__snapshot=latest, kind="workspace"
                    ).select_related("workspace")
                )
                if latest
                else []
            ),
        )
