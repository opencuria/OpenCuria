"""Deletion approval, graph reservations and evidence-gated child allocation.

Edges point from a consumer to its dependency. Only reverse reachability from
image resources cascades: neither provenance nor shared dependency layers do.
"""

import hashlib
import json
import uuid
from datetime import timedelta

import structlog
from django.db import transaction
from django.utils import timezone

from common.exceptions import ConflictError, NotFoundError

from .inventory_repository import InventoryRepository
from .models import (
    ImageBuildJob,
    ImageDefinition,
    ImageDeletionRequest,
    ImageInstance,
    InventoryEdge,
    InventoryRuntime,
    InventorySnapshot,
    LifecycleCommand,
    Runner,
    Task,
    Workspace,
)
from .operations import OperationRepository
from .repositories import ImageGenerationRepository, TaskRepository

TERMINAL = ["deleted", "removed"]
DONE = ["completed", "cancelled"]


class DeletionRepository:
    """All ORM and atomic deletion policy live at this boundary."""

    @staticmethod
    def target(org_id, kind, target_id):
        """Resolve only same-organization targets (global recipes are read-only)."""
        if kind == "image":
            target = (
                ImageInstance.objects.select_related("runner")
                .filter(pk=target_id, runner__organization_id=org_id)
                .first()
            )
            images = ImageInstance.objects.filter(pk=target_id)
            jobs = ImageBuildJob.objects.none()
        elif kind == "assignment":
            target = (
                ImageBuildJob.objects.select_related("runner")
                .filter(pk=target_id, runner__organization_id=org_id)
                .first()
            )
            images = ImageInstance.objects.filter(build_job_id=target_id)
            jobs = ImageBuildJob.objects.filter(pk=target_id)
        elif kind == "definition":
            target = ImageDefinition.objects.filter(
                pk=target_id, organization_id=org_id
            ).first()
            jobs = ImageBuildJob.objects.filter(
                image_definition_id=target_id, runner__organization_id=org_id
            )
            images = ImageInstance.objects.filter(build_job__in=jobs)
        else:
            raise ConflictError("Unknown deletion target type")
        if target is None:
            raise NotFoundError("Deletion target", str(target_id))
        return target, list(images.exclude(status="deleted")), list(jobs)

    @staticmethod
    def graph(org_id, kind, target_id):
        """Return structural approval only; scans/timestamps are diagnostics, not digest."""
        target, roots, jobs = DeletionRepository.target(org_id, kind, target_id)
        result = {
            "roots": sorted(str(i.id) for i in roots),
            "images": [],
            "workspaces": [],
            "resources": [],
            "edges": [],
            "pins": [],
            "blockers": [],
            "snapshots": [],
        }
        runners = {i.runner_id for i in roots} | {j.runner_id for j in jobs}
        if kind in ["image", "assignment"]:
            runners.add(target.runner_id)
        selected_images = {str(i.id): i for i in roots}
        selected_ws = {}
        for runner_id in sorted(runners, key=str):
            runner = Runner.objects.get(pk=runner_id)
            latest = (
                InventorySnapshot.objects.filter(runner=runner).order_by("-id").first()
            )
            now = timezone.now()
            valid = bool(
                runner.is_online
                and latest
                and latest.complete
                and latest.session == runner.sid
                and latest.received_at > now - timedelta(minutes=5)
            )
            scans = (
                list(InventoryRuntime.objects.filter(snapshot=latest)) if latest else []
            )
            valid = (
                valid
                and bool(scans)
                and all(
                    s.complete and now - timedelta(minutes=5) < s.collected_at <= now
                    for s in scans
                )
            )
            if not valid:
                result["blockers"].append(
                    "offline" if not runner.is_online else "inventory"
                )
                continue
            result["snapshots"].append(
                {"runner_id": str(runner_id), "snapshot_id": latest.id}
            )
            for scan in scans:
                resources = list(
                    scan.resources.select_related("image", "workspace__runner")
                )
                by_id = {r.id: r for r in resources}
                edges = list(
                    InventoryEdge.objects.filter(source__runtime=scan).values_list(
                        "source_id", "target_id"
                    )
                )
                root_ids = {
                    i.id
                    for i in roots
                    if i.runner_id == runner_id and i.runtime_type == scan.runtime_type
                }
                chosen = {r.id for r in resources if r.image_id in root_ids}
                for image in roots:
                    if image.id in root_ids and not any(
                        r.image_id == image.id for r in resources
                    ):
                        # A missing ref is not proof of no artifact. Only never-issued
                        # failed placeholders can be logically removed without bytes.
                        if not (
                            not image.runner_ref
                            and not image.creating_task_id
                            and (
                                image.status == "failed"
                                or any(
                                    p.previous.get("image:" + str(image.id)) == "failed"
                                    for p in ImageDeletionRequest.objects.exclude(
                                        phase__in=DONE
                                    )
                                )
                            )
                        ):
                            result["blockers"].append("missing_image:" + str(image.id))
                while True:
                    extra = {source for source, target in edges if target in chosen}
                    # Logical pins are consumers even when physical identity is missing.
                    image_ids = {
                        by_id[x].image_id for x in chosen if by_id[x].image_id
                    } | root_ids
                    pinned = list(
                        Workspace.objects.filter(
                            base_image_instance_id__in=image_ids
                        ).exclude(status__in=TERMINAL)
                    )
                    ws_ids = {w.id for w in pinned} | {
                        by_id[x].workspace_id for x in chosen if by_id[x].workspace_id
                    }
                    extra |= {r.id for r in resources if r.workspace_id in ws_ids}
                    if extra.issubset(chosen):
                        break
                    chosen |= extra
                for w in pinned:
                    selected_ws[str(w.id)] = w
                    result["pins"].append([str(w.id), str(w.base_image_instance_id)])
                    if not any(r.workspace_id == w.id for r in resources):
                        result["blockers"].append("missing_workspace:" + str(w.id))
                for resource_id in chosen:
                    r = by_id[resource_id]
                    key = f"{runner_id}:{scan.runtime_type}:{r.physical_id}"
                    result["resources"].append(
                        {
                            "key": key,
                            "image_id": str(r.image_id) if r.image_id else None,
                            "workspace_id": str(r.workspace_id)
                            if r.workspace_id
                            else None,
                            "kind": r.kind,
                            "managed": r.managed,
                            "aliases": sorted(
                                r.aliases.values_list("reference", flat=True)
                            ),
                        }
                    )
                    if (
                        not r.managed
                        or r.state == "unknown"
                        or not (r.image_id or r.workspace_id)
                    ):
                        result["blockers"].append("unknown_or_foreign:" + key)
                    aliases = list(r.aliases.values_list("reference", flat=True))
                    if (
                        scan.runtime_type == "docker"
                        and r.image_id
                        and any(
                            not alias.startswith(
                                ("opencuria/", "opencuria-", "opencuria:")
                            )
                            for alias in aliases
                        )
                    ):
                        result["blockers"].append("foreign_alias:" + key)
                    if (
                        r.image_id
                        and ImageInstance.objects.filter(
                            runner_id=runner_id,
                            runner_ref__in=[r.physical_id, *aliases],
                        )
                        .exclude(pk=r.image_id)
                        .exclude(status="deleted")
                        .exists()
                    ):
                        result["blockers"].append("ambiguous_shared_identity:" + key)
                    if r.image_id:
                        if r.image.runner.organization_id != org_id:
                            result["blockers"].append("outside_org:" + key)
                        selected_images[str(r.image_id)] = r.image
                    if r.workspace_id:
                        if r.workspace.runner.organization_id != org_id:
                            result["blockers"].append("outside_org:" + key)
                        selected_ws[str(r.workspace_id)] = r.workspace
                    # A dependency endpoint outside the managed graph is not
                    # deleted, but unknown backing identity still fences cleanup.
                    for source, target in edges:
                        if source != resource_id:
                            continue
                        dependency = by_id[target]
                        if (
                            not dependency.managed
                            or dependency.kind == "unknown"
                            or dependency.state == "unknown"
                        ):
                            result["blockers"].append(
                                "unknown_dependency:" + dependency.physical_id
                            )
                        if target in chosen:
                            result["edges"].append(
                                [
                                    key,
                                    f"{runner_id}:{scan.runtime_type}:{dependency.physical_id}",
                                ]
                            )
        for i in selected_images.values():
            if (
                i.creating_task_id
                and Task.objects.filter(
                    pk=i.creating_task_id, status__in=["pending", "in_progress"]
                ).exists()
            ):
                result["blockers"].append("publication_pending:" + str(i.id))
        result["images"] = sorted(
            [
                {
                    "id": k,
                    "runner_id": str(i.runner_id),
                    "owner_id": str(i.created_by_id) if i.created_by_id else None,
                    "owner_label": InventoryRepository.owner_label(i.created_by),
                    "name": i.name,
                }
                for k, i in selected_images.items()
            ],
            key=lambda x: x["id"],
        )
        result["workspaces"] = sorted(
            [
                {
                    "id": k,
                    "runner_id": str(w.runner_id),
                    "owner_id": str(w.created_by_id) if w.created_by_id else None,
                    "owner_label": InventoryRepository.owner_label(w.created_by),
                    "name": w.name,
                }
                for k, w in selected_ws.items()
            ],
            key=lambda x: x["id"],
        )
        for key in ["resources", "edges", "pins", "blockers"]:
            result[key] = sorted(
                result[key], key=lambda x: json.dumps(x, sort_keys=True)
            )
        result["counts"] = {
            "images": len(result["images"]),
            "workspaces": len(result["workspaces"]),
        }
        structural = {
            k: v for k, v in result.items() if k not in ["snapshots", "counts"]
        }
        for group in ["images", "workspaces"]:
            structural[group] = [
                {k: v for k, v in item.items() if k not in {"name", "owner_label"}}
                for item in structural[group]
            ]
        result["fingerprint"] = hashlib.sha256(
            json.dumps(structural, sort_keys=True).encode()
        ).hexdigest()
        return result

    @staticmethod
    def preview(org_id, kind, target_id):
        """Read-only accurate graph; force is refused whenever blockers exist."""
        return DeletionRepository.graph(org_id, kind, target_id)

    @staticmethod
    def _lock_runners(org_id, kind, target_id):
        target, images, jobs = DeletionRepository.target(org_id, kind, target_id)
        ids = {i.runner_id for i in images} | {j.runner_id for j in jobs}
        if kind in ["image", "assignment"]:
            ids.add(target.runner_id)
        from .locking import lock_runner

        for runner_id in sorted(ids):
            lock_runner(runner_id)

    @staticmethod
    def request(org_id, user, kind, target_id, mode="deferred", fingerprint=""):
        """Retire immediately; approval and reservations commit together."""
        if mode not in ["deferred", "force"]:
            raise ConflictError("Mode must be deferred or force")
        with transaction.atomic():
            DeletionRepository._lock_runners(org_id, kind, target_id)
            old = (
                ImageDeletionRequest.objects.filter(
                    organization_id=org_id, target_type=kind, target_id=target_id
                )
                .exclude(phase__in=DONE)
                .first()
            )
            graph = DeletionRepository.graph(org_id, kind, target_id)
            if mode == "force" and (
                graph["blockers"] or fingerprint != graph["fingerprint"]
            ):
                raise ConflictError(
                    "Fresh unblocked preview and matching fingerprint required"
                )
            if old:
                if mode == "force" and (
                    old.phase == "reconfirmation_required"
                    or (old.mode == "deferred" and not old.released_at)
                ):
                    old.mode = "force"
                    old.approval = graph
                    old.fingerprint = fingerprint
                    old.phase = "waiting_dependency"
                    old.save()
                    DeletionRepository._retire(old, graph)
                return DeletionRepository.output(old)
            plan = ImageDeletionRequest.objects.create(
                organization_id=org_id,
                requested_by=user,
                target_type=kind,
                target_id=target_id,
                mode=mode,
                approval=graph,
                fingerprint=fingerprint,
            )
            DeletionRepository._retire(plan, graph)
            return DeletionRepository.output(plan)

    @staticmethod
    def _retire(plan, graph):
        _, roots, jobs = DeletionRepository.target(
            plan.organization_id, plan.target_type, plan.target_id
        )
        image_ids = (
            graph["roots"]
            if plan.mode == "deferred"
            else [i["id"] for i in graph["images"]]
        )
        # Workspace -> image -> task order; runner serializes allocation paths.
        if plan.mode == "force":
            list(
                Workspace.objects.select_for_update()
                .filter(pk__in=[w["id"] for w in graph["workspaces"]])
                .order_by("id")
            )
        active = ImageDeletionRequest.objects.exclude(pk=plan.id).exclude(
            phase__in=DONE
        )
        for other in active:
            overlap = set(image_ids) & {
                key.split(":", 1)[1]
                for key in other.previous
                if key.startswith("image:")
            }
            if overlap:
                raise ConflictError(
                    "Image already reserved by another deletion request"
                )
        previous = dict(plan.previous)
        for image in (
            ImageInstance.objects.select_for_update()
            .filter(pk__in=image_ids)
            .order_by("id")
        ):
            key = "image:" + str(image.id)
            previous.setdefault(key, image.status)
            if image.status not in ["deleted", "deleting"]:
                image.status = "pending_deletion"
                image.delete_requested_at = timezone.now()
                image.save(update_fields=["status", "delete_requested_at"])
        for job in jobs:
            previous.setdefault("assignment:" + str(job.id), job.status)
            ImageBuildJob.objects.filter(pk=job.id).update(status="pending_deletion")
        if plan.target_type == "definition":
            definition = ImageDefinition.objects.get(pk=plan.target_id)
            previous.setdefault("definition:" + str(definition.id), definition.status)
            ImageDefinition.objects.filter(pk=definition.id).update(
                status="pending_deletion"
            )
        plan.previous = previous
        plan.save(update_fields=["previous"])

    @staticmethod
    def cancel(org_id, request_id):
        """Cancellation is possible only before releasing any physical child."""
        with transaction.atomic():
            plan = ImageDeletionRequest.objects.get(
                pk=request_id, organization_id=org_id
            )
            DeletionRepository._lock_runners(org_id, plan.target_type, plan.target_id)
            plan = ImageDeletionRequest.objects.select_for_update().get(pk=plan.id)
            if plan.released_at or plan.children:
                raise ConflictError("Physical deletion has begun; no undo")
            if plan.phase not in DONE:
                # Runner -> assignment -> image -> task, matching build callbacks.
                image_ids = [
                    key.split(":", 1)[1]
                    for key in plan.previous
                    if key.startswith("image:")
                ]
                job_ids = {
                    key.split(":", 1)[1]
                    for key in plan.previous
                    if key.startswith("assignment:")
                } | {
                    str(identity)
                    for identity in ImageInstance.objects.filter(
                        pk__in=image_ids, build_job__isnull=False
                    ).values_list("build_job_id", flat=True)
                }
                jobs = list(
                    ImageBuildJob.objects.select_for_update()
                    .filter(pk__in=job_ids)
                    .order_by("id")
                )
                reserved = {
                    key
                    for other in ImageDeletionRequest.objects.exclude(
                        pk=plan.id
                    ).exclude(phase__in=DONE)
                    for key in other.previous
                }
                for key, state in plan.previous.items():
                    if key in reserved:
                        continue
                    kind, identity = key.split(":", 1)
                    model = {
                        "image": ImageInstance,
                        "assignment": ImageBuildJob,
                        "definition": ImageDefinition,
                    }[kind]
                    # Never overwrite a later user/worker state or resurrect bytes.
                    if kind == "image" and state in ["building", "capturing"]:
                        image = ImageInstance.objects.get(pk=identity)
                        child = Task.objects.filter(pk=image.creating_task_id).first()
                        if child and child.status == "completed":
                            state = "ready"
                        elif child and child.status == "failed":
                            state = "failed"
                    model.objects.filter(pk=identity, status="pending_deletion").update(
                        status=state
                    )
                plan.phase = "cancelled"
                plan.diagnostic = ""
                plan.save()
                for job in jobs:
                    job.refresh_from_db()
                    ImageGenerationRepository.reconcile_assignment(job)
            return DeletionRepository.output(plan)

    @staticmethod
    def output(plan):
        """Stable API/MCP contract, with child task identities and idle diagnostics."""
        return {
            "id": str(plan.id),
            "target_type": plan.target_type,
            "target_id": str(plan.target_id),
            "mode": plan.mode,
            "phase": plan.phase,
            "diagnostic": plan.diagnostic,
            "fingerprint": plan.fingerprint,
            "can_cancel": not plan.released_at
            and not plan.children
            and plan.phase not in DONE,
            "released_at": plan.released_at,
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
            "children": plan.children,
            "approval": plan.approval,
        }

    @staticmethod
    def list(org_id, request_id=None):
        rows = ImageDeletionRequest.objects.filter(organization_id=org_id).order_by(
            "-created_at"
        )
        if request_id:
            rows = rows.filter(pk=request_id)
        return [DeletionRepository.output(p) for p in rows]

    @staticmethod
    def tick():
        """Independent progression; waiting creates no Task and no busy spinner."""
        for identity in ImageDeletionRequest.objects.exclude(
            phase__in=DONE
        ).values_list("id", flat=True):
            try:
                DeletionRepository.advance(identity)
            except Exception:
                structlog.get_logger(__name__).exception(
                    "deletion_advance_failed", request_id=str(identity)
                )

    @staticmethod
    def advance(identity):
        with transaction.atomic():
            plan = ImageDeletionRequest.objects.get(pk=identity)
            DeletionRepository._lock_runners(
                plan.organization_id, plan.target_type, plan.target_id
            )
            plan = ImageDeletionRequest.objects.select_for_update().get(pk=identity)
            if plan.phase in DONE:
                return
            graph = DeletionRepository.graph(
                plan.organization_id, plan.target_type, plan.target_id
            )

            def wait(phase, diagnostic):
                plan.phase, plan.diagnostic = phase, diagnostic
                plan.save(update_fields=["phase", "diagnostic", "updated_at"])

            # Consume exact terminal child results, never storage absence alone.
            for key, task_id in plan.children.items():
                task = Task.objects.get(pk=task_id)
                if task.status == "failed":
                    command = LifecycleCommand.objects.get(task=task)
                    counts = plan.approval.get("retry_counts", {})
                    if (
                        command.phase == "result"
                        and "intervention" not in task.error.lower()
                        and counts.get(key, 0) < 2
                    ):
                        # Only a finished runner failure, never a timeout/ambiguous
                        # handler, permits bounded new execution identities.
                        kind, pk = key.split(":", 1)
                        model = Workspace if kind == "workspace" else ImageInstance
                        model.objects.filter(pk=pk, status="delete_failed").update(
                            status="pending_deletion"
                        )
                        counts[key] = counts.get(key, 0) + 1
                        history = plan.approval.get("failed_children", []) + [
                            str(task.id)
                        ]
                        plan.approval = {
                            **plan.approval,
                            "retry_counts": counts,
                            "failed_children": history,
                        }
                        plan.children = {
                            k: v for k, v in plan.children.items() if k != key
                        }
                        plan.save(update_fields=["children", "approval"])
                        wait(
                            "waiting_inventory",
                            "Finished failure; bounded retry waits for fresh inventory",
                        )
                    else:
                        wait(
                            "intervention",
                            "Child failure requires exact runtime reconciliation: "
                            + task.error,
                        )
                    return
                if task.status != "completed":
                    command = LifecycleCommand.objects.get(task=task)
                    if command.deliveries or command.heartbeat_at:
                        wait("executing", "Waiting for task-bound removal confirmation")
                        return
                    # A never-delivered child may be held while reconfirmed new
                    # leaves are removed. Retain its exact identity for reuse.
                    continue
                kind, pk = key.split(":", 1)
                model = Workspace if kind == "workspace" else ImageInstance
                if not model.objects.filter(pk=pk, status__in=TERMINAL).exists():
                    wait(
                        "intervention",
                        "Task success without matching resource tombstone",
                    )
                    return
            for task_id in [
                *plan.children.values(),
                *plan.approval.get("failed_children", []),
            ]:
                task = Task.objects.get(pk=task_id)
                if not task.completed_at:
                    continue
                scan_ids = [
                    s["snapshot_id"]
                    for s in graph["snapshots"]
                    if s["runner_id"] == str(task.runner_id)
                ]
                if (
                    scan_ids
                    and InventorySnapshot.objects.get(pk=scan_ids[0]).received_at
                    <= task.completed_at
                ):
                    wait("waiting_inventory", "Fresh post-removal inventory required")
                    return
            for key, child_id in plan.children.items():
                if Task.objects.get(pk=child_id).status != "completed":
                    continue
                kind, pk = key.split(":", 1)
                from .models import InventoryResource

                for snapshot in graph["snapshots"]:
                    lookup = {"workspace_id" if kind == "workspace" else "image_id": pk}
                    if InventoryResource.objects.filter(
                        runtime__snapshot_id=snapshot["snapshot_id"], **lookup
                    ).exists():
                        wait(
                            "intervention",
                            "Confirmed removal still present in actual inventory",
                        )
                        return
            if graph["blockers"]:
                phase = (
                    "waiting_offline"
                    if "offline" in graph["blockers"]
                    else "waiting_inventory"
                )
                wait(phase, "; ".join(graph["blockers"]))
                return
            if plan.mode == "force":
                completed = {
                    k
                    for k, v in plan.children.items()
                    if Task.objects.get(pk=v).status == "completed"
                }
                approved_resources = [
                    r
                    for r in plan.approval["resources"]
                    if (
                        "image:" + str(r["image_id"]) not in completed
                        and "workspace:" + str(r["workspace_id"]) not in completed
                    )
                ]
                keys = {r["key"] for r in approved_resources}
                expected = {k: plan.approval[k] for k in ["resources", "edges", "pins"]}
                expected["resources"] = approved_resources
                expected["edges"] = [
                    e for e in expected["edges"] if all(x in keys for x in e)
                ]
                expected["pins"] = [
                    p
                    for p in expected["pins"]
                    if "workspace:" + p[0] not in completed
                    and "image:" + p[1] not in completed
                ]
                if any(graph[k] != expected[k] for k in expected):
                    wait(
                        "reconfirmation_required",
                        "Physical graph or logical pins changed; new preview required",
                    )
                    return
            elif graph["workspaces"] or set(i["id"] for i in graph["images"]) - set(
                graph["roots"]
            ):
                wait(
                    "waiting_dependency", "Dependents retained until confirmed removal"
                )
                return
            images = (
                {i["id"] for i in graph["images"]}
                if plan.mode == "force"
                else set(graph["roots"])
            )
            workspaces = (
                {w["id"] for w in graph["workspaces"]}
                if plan.mode == "force"
                else set()
            )
            if not images and not workspaces:
                _, _, jobs = DeletionRepository.target(
                    plan.organization_id, plan.target_type, plan.target_id
                )
                ImageBuildJob.objects.filter(pk__in=[j.id for j in jobs]).update(
                    status="deleted", delete_confirmed_at=timezone.now()
                )
                if plan.target_type == "definition":
                    ImageDefinition.objects.filter(pk=plan.target_id).update(
                        status="deleted"
                    )
                wait("completed", "")
                return
            # Allocate one leaf, then demand a subsequent complete scan before
            # allocating the next. Multiple physical resources can name one ws.
            resources = {
                r["key"]: (
                    "workspace:" + r["workspace_id"]
                    if r["workspace_id"]
                    else "image:" + r["image_id"]
                )
                for r in graph["resources"]
            }
            blocked = {
                resources[b] for a, b in graph["edges"] if resources[a] != resources[b]
            }
            blocked |= {"image:" + image for ws, image in graph["pins"]}
            candidates = sorted(
                ["workspace:" + w for w in workspaces] + ["image:" + i for i in images],
                key=lambda x: (not x.startswith("workspace:"), x),
            )
            for key in candidates:
                if key in blocked:
                    continue
                if key in plan.children:
                    wait(
                        "executing",
                        "Reusing prepared child after prerequisite confirmation",
                    )
                    return
                kind, pk = key.split(":", 1)
                ws = (
                    Workspace.objects.select_for_update()
                    .select_related("runner")
                    .get(pk=pk)
                    if kind == "workspace"
                    else None
                )
                image = (
                    ImageInstance.objects.select_for_update()
                    .select_related("runner")
                    .get(pk=pk)
                    if kind == "image"
                    else None
                )
                obj = ws or image
                if ws and ws.current_task_id:
                    wait(
                        "waiting_dependency",
                        "Workspace has unresolved lifecycle operation",
                    )
                    return
                if (
                    image
                    and image.creating_task_id
                    and Task.objects.filter(
                        pk=image.creating_task_id, status__in=["pending", "in_progress"]
                    ).exists()
                ):
                    wait("waiting_dependency", "Image publication still unresolved")
                    return
                # Refuse stale scans predating our own previous confirmed removal.
                for task_id in plan.children.values():
                    task = Task.objects.get(pk=task_id)
                    if not task.completed_at:
                        continue
                    scan_ids = [
                        s["snapshot_id"]
                        for s in graph["snapshots"]
                        if s["runner_id"] == str(task.runner_id)
                    ]
                    if (
                        scan_ids
                        and InventorySnapshot.objects.get(pk=scan_ids[0]).received_at
                        <= task.completed_at
                    ):
                        wait(
                            "waiting_inventory", "Fresh post-removal inventory required"
                        )
                        return
                if (
                    image
                    and not image.runner_ref
                    and not image.creating_task_id
                    and plan.previous.get(key) == "failed"
                ):
                    ImageInstance.objects.filter(pk=image.id).update(
                        status="deleted", delete_confirmed_at=timezone.now()
                    )
                    wait(
                        "waiting_inventory",
                        "Never-created placeholder retired without physical command",
                    )
                    return
                task = TaskRepository.create(
                    task_id=uuid.uuid4(),
                    runner=obj.runner,
                    task_type="remove_workspace" if ws else "delete_image",
                    workspace=ws,
                )
                payload = {"task_id": str(task.id)}
                if ws:
                    payload["workspace_id"] = str(ws.id)
                    Workspace.objects.filter(pk=ws.id).update(status="deleting")
                else:
                    payload.update(
                        image_instance_id=str(image.id),
                        runtime_type=image.runtime_type,
                        image_artifact_id=image.runner_ref,
                    )
                    ImageInstance.objects.filter(pk=image.id).update(
                        status="deleting", deleting_task=task
                    )
                OperationRepository.prepare(
                    str(task.id),
                    "task:remove_workspace" if ws else "task:delete_image_artifact",
                    payload,
                )
                plan.children = {**plan.children, key: str(task.id)}
                plan.released_at = plan.released_at or timezone.now()
                plan.phase = "executing"
                plan.diagnostic = ""
                plan.save()
                return
            wait("intervention", "Cyclic or unresolved physical dependencies")

    @staticmethod
    def delivery_allowed(task_id):
        """Recheck graph/freshness at each actual outbox release, not just allocation."""
        task = Task.objects.get(pk=task_id)
        with transaction.atomic():
            from .locking import lock_runner

            lock_runner(task.runner_id)
            plan = next(
                (
                    p
                    for p in ImageDeletionRequest.objects.exclude(phase__in=DONE)
                    if str(task_id) in p.children.values()
                ),
                None,
            )
            if plan is None:
                return True  # Other lifecycle commands retain existing policy.
            graph = DeletionRepository.graph(
                plan.organization_id, plan.target_type, plan.target_id
            )
            if graph["blockers"]:
                return False
            resources_by_key = {
                r["key"]: (
                    "workspace:" + r["workspace_id"]
                    if r["workspace_id"]
                    else "image:" + str(r["image_id"])
                )
                for r in graph["resources"]
            }
            blocked = {
                resources_by_key[b]
                for a, b in graph["edges"]
                if resources_by_key[a] != resources_by_key[b]
            }
            blocked |= {"image:" + i for w, i in graph["pins"]}
            child_key = next(k for k, v in plan.children.items() if v == str(task_id))
            if plan.mode == "deferred":
                return not graph["workspaces"] and not (
                    set(i["id"] for i in graph["images"]) - set(graph["roots"])
                )
            completed = {
                k
                for k, v in plan.children.items()
                if Task.objects.get(pk=v).status == "completed"
            }
            resources = [
                r
                for r in plan.approval["resources"]
                if "image:" + str(r["image_id"]) not in completed
                and "workspace:" + str(r["workspace_id"]) not in completed
            ]
            keys = {r["key"] for r in resources}
            edges = [e for e in plan.approval["edges"] if all(k in keys for k in e)]
            pins = [
                p
                for p in plan.approval["pins"]
                if "workspace:" + p[0] not in completed
                and "image:" + p[1] not in completed
            ]
            if (
                graph["resources"] != resources
                or graph["edges"] != edges
                or graph["pins"] != pins
            ):
                plan.phase = "reconfirmation_required"
                plan.diagnostic = "Graph changed before delivery; command withheld"
                plan.save(update_fields=["phase", "diagnostic"])
                return False
            return child_key not in blocked
