"""Durable deletion contract: exact graph, retirement, evidence and bottom-up order."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from django.utils import timezone

from apps.organizations.models import Membership, Organization
from apps.runners.deletion_repository import DeletionRepository as Repo
from apps.runners.inventory_repository import InventoryRepository
from apps.runners.models import (
    ImageBuildJob,
    ImageDefinition,
    ImageDeletionRequest,
    ImageInstance,
    InventorySnapshot,
    LifecycleCommand,
    Task,
    Workspace,
)
from apps.runners.operations import apply_result
from apps.runners.services import RunnerService
from apps.runners.services.deletion import DeletionService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

pytestmark = pytest.mark.django_db(transaction=True)


def image(runner, user, name="base", **kw):
    return ImageInstance.objects.create(
        runner=runner,
        created_by=user,
        name=name,
        runner_ref="/images/" + name,
        runtime_type="qemu",
        is_legacy=True,
        **kw,
    )


def resource(obj, dependencies=()):
    if isinstance(obj, ImageInstance):
        return {
            "resource_id": obj.runner_ref,
            "kind": "image",
            "managed": True,
            "state": "ready",
            "dependencies": list(dependencies),
        }
    return {
        "resource_id": "/workspaces/" + str(obj.id),
        "kind": "workspace",
        "managed": True,
        "state": obj.status,
        "metadata": {"workspace_id": str(obj.id)},
        "dependencies": list(dependencies),
    }


def observe(runner, resources, complete=True):
    previous = InventorySnapshot.objects.filter(runner=runner).order_by("-id").first()
    payload = {
        "schema_version": 1,
        "inventory_epoch": str(previous.epoch) if previous else str(uuid.uuid4()),
        "inventory_sequence": previous.sequence + 1 if previous else 1,
        "complete": complete,
        "runtimes": [
            {
                "runtime_type": runtime,
                "collected_at": timezone.now().isoformat(),
                "complete": complete,
                "resources": resources if runtime == "qemu" else [],
            }
            for runtime in runner.available_runtimes
        ],
    }
    assert InventoryRepository.record(str(runner.id), runner.sid, payload)


def request(runner, user, root, force=False):
    graph = Repo.preview(runner.organization_id, "image", root.id)
    return Repo.request(
        runner.organization_id,
        user,
        "image",
        root.id,
        "force" if force else "deferred",
        graph["fingerprint"] if force else "",
    )


def finish(service, runner, task_id):
    command = LifecycleCommand.objects.get(task_id=task_id)
    payload = dict(command.payload)
    payload["result"] = "deleted"
    data = {
        **payload,
        "operation_id": str(task_id),
        "attempt": command.attempt,
        "target": command.target,
        "runner_id": str(runner.id),
    }
    event = (
        "workspace:removed" if command.task.workspace_id else "image_artifact:deleted"
    )
    assert apply_result(service, str(runner.id), event, data)


def test_force_physical_chain_excludes_independent_capture(runner, user):
    base = image(runner, user)
    w1 = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="w1",
        runtime_type="qemu",
        status="stopped",
        base_image_instance=base,
    )
    capture = image(runner, user, "dependent", origin_workspace=w1)
    independent = image(runner, user, "independent", origin_workspace=w1)
    w2 = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="w2",
        runtime_type="qemu",
        status="running",
        base_image_instance=capture,
    )
    actual = [
        resource(base),
        resource(w1, [base.runner_ref]),
        resource(capture, ["/workspaces/" + str(w1.id)]),
        resource(w2, [capture.runner_ref]),
        resource(independent),
    ]
    observe(runner, actual)
    preview = Repo.preview(runner.organization_id, "image", base.id)
    assert preview["counts"] == {"images": 2, "workspaces": 2}
    assert str(independent.id) not in [i["id"] for i in preview["images"]]
    base.refresh_from_db()
    assert base.status == "ready" and not Task.objects.exists()
    plan = request(runner, user, base, True)
    service = RunnerService(sio_server=AsyncMock())
    for obj in [w2, capture, w1, base]:
        Repo.tick()
        row = ImageDeletionRequest.objects.get(pk=plan["id"])
        key = ("workspace:" if isinstance(obj, Workspace) else "image:") + str(obj.id)
        assert key in row.children
        task_id = row.children[key]
        command = LifecycleCommand.objects.get(task_id=task_id)
        assert command.target == str(obj.id)
        assert command.event
        assert command.payload.get("image_artifact_id", str(obj.id)) == (
            obj.runner_ref if isinstance(obj, ImageInstance) else str(obj.id)
        )
        with pytest.raises(ConflictError, match="no undo"):
            Repo.cancel(runner.organization_id, row.id)
        Repo.tick()
        assert Task.objects.count() == len(row.children)
        finish(service, runner, task_id)
        Repo.tick()
        assert ImageDeletionRequest.objects.get(pk=row.id).phase == "waiting_inventory"
        actual = [r for r in actual if r["resource_id"] != resource(obj)["resource_id"]]
        observe(runner, actual)
    Repo.tick()
    assert ImageDeletionRequest.objects.get(pk=plan["id"]).phase == "completed"
    independent.refresh_from_db()
    assert independent.status == "ready"
    assert Workspace.objects.filter(pk__in=[w1.id, w2.id]).count() == 2


def test_cancel_retirement_and_changed_preview(runner, user):
    base = image(runner, user)
    observe(runner, [resource(base)])
    preview = Repo.preview(runner.organization_id, "image", base.id)
    # Refreshing timestamps/sequences alone must not change fingerprint.
    observe(runner, [resource(base)])
    assert (
        Repo.preview(runner.organization_id, "image", base.id)["fingerprint"]
        == preview["fingerprint"]
    )
    plan = request(runner, user, base, True)
    base.refresh_from_db()
    assert base.status == "pending_deletion"
    assert Repo.cancel(runner.organization_id, plan["id"])["phase"] == "cancelled"
    base.refresh_from_db()
    assert base.status == "ready"
    plan = request(runner, user, base, True)
    ws = Workspace.objects.create(
        runner=runner, created_by=user, name="external", base_image_instance=base
    )
    observe(runner, [resource(base), resource(ws, [base.runner_ref])])
    Repo.tick()
    assert (
        ImageDeletionRequest.objects.get(pk=plan["id"]).phase
        == "reconfirmation_required"
    )
    assert not Task.objects.exists()
    with pytest.raises(ConflictError):
        Repo.request(
            runner.organization_id,
            user,
            "image",
            base.id,
            "force",
            preview["fingerprint"],
        )
    fresh = Repo.preview(runner.organization_id, "image", base.id)
    Repo.request(
        runner.organization_id, user, "image", base.id, "force", fresh["fingerprint"]
    )
    Repo.tick()
    assert Task.objects.get().workspace_id == ws.id


@pytest.mark.parametrize(
    "status", ["running", "stopped", "deleting", "pending_deletion"]
)
def test_deferred_waits_for_every_pin_and_auto_advances(runner, user, status):
    base = image(runner, user)
    ws = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="pin",
        base_image_instance=base,
        status=status,
    )
    observe(runner, [resource(base), resource(ws, [base.runner_ref])])
    plan = request(runner, user, base)
    Repo.tick()
    assert ImageDeletionRequest.objects.get(pk=plan["id"]).phase == "waiting_dependency"
    assert not Task.objects.exists()
    Workspace.objects.filter(pk=ws.id).update(status="deleted")
    observe(runner, [resource(base)])
    Repo.tick()
    assert LifecycleCommand.objects.get().target == str(base.id)


@pytest.mark.parametrize("bad", ["partial", "stale", "foreign", "unknown", "missing"])
def test_bad_evidence_blocks_even_force(runner, user, bad):
    base = image(runner, user)
    actual = [resource(base)]
    if bad in ["foreign", "unknown"]:
        actual.append(
            {
                "resource_id": "foreign",
                "kind": "unknown",
                "managed": False,
                "dependencies": [base.runner_ref],
                "state": "unknown",
            }
        )
    if bad == "missing":
        actual = []
    observe(runner, actual, bad != "partial")
    if bad == "stale":
        InventorySnapshot.objects.update(
            received_at=timezone.now() - timedelta(minutes=10)
        )
    graph = Repo.preview(runner.organization_id, "image", base.id)
    assert graph["blockers"]
    with pytest.raises(ConflictError):
        Repo.request(
            runner.organization_id,
            user,
            "image",
            base.id,
            "force",
            graph["fingerprint"],
        )
    request(runner, user, base)
    Repo.tick()
    assert not Task.objects.exists()


def test_owner_admin_crossorg_auth(runner, user):
    service = DeletionService()
    base = image(runner, user)
    Membership.objects.create(
        user=user, organization=runner.organization, role="member"
    )
    assert service.request(user, runner.organization_id, "image", base.id)["can_cancel"]
    with pytest.raises(AuthenticationError):
        service.preview(user, runner.organization_id, "image", base.id)
    base.created_by = None
    base.save(update_fields=["created_by"])
    with pytest.raises(AuthenticationError):
        service.request(user, runner.organization_id, "image", base.id)
    Membership.objects.filter(user=user).update(role="admin")
    assert service.request(user, runner.organization_id, "image", base.id)
    other = Organization.objects.create(name="other", slug="delete-other")
    Membership.objects.create(user=user, organization=other, role="admin")
    with pytest.raises(NotFoundError):
        service.request(user, other.id, "image", base.id)


@pytest.mark.asyncio
async def test_offline_restart_worker_no_http(runner, user):
    from apps.runners.services.recovery import RecoveryService

    base = image(runner, user)
    runner.status = "offline"
    runner.save()
    plan = request(runner, user, base)
    await RecoveryService().tick(AsyncMock())
    assert ImageDeletionRequest.objects.get(pk=plan["id"]).phase == "waiting_offline"
    assert not Task.objects.exists()
    runner.status = "online"
    runner.save()
    observe(runner, [resource(base)])
    transport = AsyncMock()
    await RecoveryService().tick(transport)
    assert transport.emit.await_args.args[0] == "task:delete_image_artifact"
    assert transport.emit.await_args.args[1]["target"] == str(base.id)


def test_assignment_all_generations_retired(runner, user):
    definition = ImageDefinition.objects.create(
        organization=runner.organization, name="recipe"
    )
    job = ImageBuildJob.objects.create(runner=runner, image_definition=definition)
    a = image(runner, user, "a", build_job=job)
    b = image(runner, user, "b", build_job=job)
    observe(runner, [resource(a), resource(b)])
    plan = Repo.request(runner.organization_id, user, "assignment", job.id)
    assert (
        ImageInstance.objects.filter(build_job=job, status="pending_deletion").count()
        == 2
    )
    Repo.tick()
    assert len(ImageDeletionRequest.objects.get(pk=plan["id"]).children) == 1


def test_retirement_blocks_clone_and_capture(runner, user):
    from apps.runners.capture_repository import CaptureRepository
    from apps.runners.repositories import ImageGenerationRepository

    base = image(runner, user)
    ws = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="source",
        runtime_type="qemu",
        status="stopped",
        base_image_instance=base,
        credentials_present=False,
    )
    request(runner, user, base)
    from django.db import transaction

    with transaction.atomic(), pytest.raises(ConflictError, match="ready"):
        ImageGenerationRepository.validate_selection(base.id)
    with pytest.raises(ConflictError, match="retired"):
        CaptureRepository.allocate(ws.id, "new capture", False)


def test_false_and_stale_task_success_fenced(runner, user):
    base = image(runner, user)
    observe(runner, [resource(base)])
    plan = request(runner, user, base)
    Repo.tick()
    row = ImageDeletionRequest.objects.get(pk=plan["id"])
    task_id = row.children["image:" + str(base.id)]
    cmd = LifecycleCommand.objects.get(task_id=task_id)
    service = RunnerService(sio_server=AsyncMock())
    data = {
        **cmd.payload,
        "operation_id": task_id,
        "attempt": cmd.attempt,
        "target": str(base.id),
        "runner_id": str(runner.id),
        "result": "not_confirmed",
    }
    assert not apply_result(
        service, str(runner.id), "image_artifact:deleted", {**data, "target": "wrong"}
    )
    assert apply_result(service, str(runner.id), "image_artifact:deleted", data)
    Repo.tick()
    assert ImageDeletionRequest.objects.get(pk=row.id).phase == "waiting_inventory"
    Repo.tick()
    assert Task.objects.count() == 1
    base.refresh_from_db()
    assert base.status != "deleted"


def test_no_ref_ready_is_not_proof_of_no_bytes(runner, user):
    base = image(runner, user)
    ImageInstance.objects.filter(pk=base.id).update(runner_ref="")
    observe(runner, [])
    request(runner, user, base)
    Repo.tick()
    base.refresh_from_db()
    assert base.status == "pending_deletion"
    assert not Task.objects.exists()


def test_definition_own_assignments_and_global_readonly(runner, user):
    definition = ImageDefinition.objects.create(
        organization=runner.organization, name="all"
    )
    job = ImageBuildJob.objects.create(runner=runner, image_definition=definition)
    a = image(runner, user, "a", build_job=job)
    b = image(runner, user, "b", build_job=job)
    observe(runner, [resource(a), resource(b)])
    plan = Repo.request(runner.organization_id, user, "definition", definition.id)
    service = RunnerService(sio_server=AsyncMock())
    for _ in range(2):
        Repo.tick()
        row = ImageDeletionRequest.objects.get(pk=plan["id"])
        task_id = list(row.children.values())[-1]
        # Dict ordering isn't execution order; find the live task.
        task_id = str(Task.objects.exclude(status="completed").get().id)
        finish(service, runner, task_id)
        remaining = list(ImageInstance.objects.exclude(status="deleted"))
        observe(runner, [resource(i) for i in remaining])
    Repo.tick()
    job.refresh_from_db()
    definition.refresh_from_db()
    assert job.status == definition.status == "deleted"
    global_recipe = ImageDefinition.objects.create(name="global")
    with pytest.raises(NotFoundError):
        Repo.request(runner.organization_id, user, "definition", global_recipe.id)


def test_legacy_migration_preserves_queued_without_physical_dispatch(runner, user):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    old = [("runners", "0022_imagedeletionrequest")]
    executor.migrate(old)
    try:
        apps = executor.loader.project_state(old).apps
        Image = apps.get_model("runners", "ImageInstance")
        queued = Image.objects.create(
            runner_id=runner.id,
            created_by_id=user.id,
            name="legacy",
            runner_ref="/legacy",
            status="pending_deletion",
            is_legacy=True,
        )
        executor = MigrationExecutor(connection)
        executor.migrate([("runners", "0023_preserve_delete_intents")])
        row = ImageDeletionRequest.objects.get(target_id=queued.id)
        assert row.phase == "waiting_inventory" and row.children == {}
        assert ImageInstance.objects.get(pk=queued.id).runner_ref == "/legacy"
        assert not Task.objects.exists()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_runner_finished_failures_bounded_but_unknown_never_retry(runner, user):
    base = image(runner, user)
    observe(runner, [resource(base)])
    plan = request(runner, user, base)
    service = RunnerService(sio_server=AsyncMock())
    for attempt in range(3):
        Repo.tick()
        task = Task.objects.exclude(status="failed").get()
        command = LifecycleCommand.objects.get(task=task)
        data = {
            **command.payload,
            "operation_id": str(task.id),
            "runner_id": str(runner.id),
            "target": command.target,
            "attempt": command.attempt,
            "error": "Runtime refused removal",
        }
        assert apply_result(
            service, str(runner.id), "image_artifact:delete_failed", data
        )
        Repo.tick()
        observe(runner, [resource(base)])
    Repo.tick()
    assert ImageDeletionRequest.objects.get(pk=plan["id"]).phase == "intervention"
    assert Task.objects.count() == 3


def test_real_qemu_domain_and_disk_join(runner, user):
    base = image(runner, user)
    ws = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="vm",
        runtime_type="qemu",
        status="stopped",
        base_image_instance=base,
    )
    domain = resource(ws, ["/disks/vm.qcow2"])
    disk = {
        "resource_id": "/disks/vm.qcow2",
        "kind": "disk",
        "managed": True,
        "state": "observed",
        "metadata": {"workspace_id": str(ws.id)},
        "dependencies": [base.runner_ref],
    }
    observe(runner, [resource(base), domain, disk])
    preview = Repo.preview(runner.organization_id, "image", base.id)
    assert not preview["blockers"]
    assert preview["counts"] == {"images": 1, "workspaces": 1}
    plan = request(runner, user, base, True)
    Repo.tick()
    assert Task.objects.get().workspace_id == ws.id
    finish(RunnerService(sio_server=AsyncMock()), runner, Task.objects.get().id)
    observe(runner, [resource(base)])
    Repo.tick()
    assert Task.objects.filter(type="delete_image").count() == 1


def test_changed_graph_withheld_before_outbox_emission(runner, user):
    base = image(runner, user)
    observe(runner, [resource(base)])
    plan = request(runner, user, base, True)
    Repo.tick()
    task = Task.objects.get()
    ws = Workspace.objects.create(
        runner=runner, created_by=user, name="new dep", base_image_instance=base
    )
    observe(runner, [resource(base), resource(ws, [base.runner_ref])])
    assert not Repo.delivery_allowed(task.id)
    assert (
        ImageDeletionRequest.objects.get(pk=plan["id"]).phase
        == "reconfirmation_required"
    )


def test_rest_mcp_contract_and_owner_force_denial(runner, user):
    import json
    from types import SimpleNamespace

    from django.test import Client

    from apps.accounts.models import APIKey, APIKeyPermission
    from apps.mcp_app.server import (
        _call_list_image_deletions,
        _call_preview_image_deletion,
    )
    from common.utils import generate_api_token, hash_token

    base = image(runner, user)
    observe(runner, [resource(base)])
    membership = Membership.objects.create(
        user=user, organization=runner.organization, role="member"
    )
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="delete-contract",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.IMAGES_DELETE.value],
    )
    client = Client(
        HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(runner.organization_id)
    )
    payload = {"target_type": "image", "target_id": str(base.id)}
    response = client.post(
        "/api/v1/image-artifacts/deletions/preview/",
        json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 403
    membership.role = "admin"
    membership.save()
    preview = client.post(
        "/api/v1/image-artifacts/deletions/preview/",
        json.dumps(payload),
        content_type="application/json",
    )
    assert preview.status_code == 200
    assert (
        json.loads(
            _call_preview_image_deletion(
                SimpleNamespace(user=user), runner.organization_id, payload
            )[0].text
        )["fingerprint"]
        == preview.json()["fingerprint"]
    )
    response = client.post(
        "/api/v1/image-artifacts/deletions/",
        json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 200
    row = response.json()
    assert row["can_cancel"] and row["phase"] == "waiting_inventory"
    assert (
        json.loads(
            _call_list_image_deletions(
                SimpleNamespace(user=user), runner.organization_id, {}
            )[0].text
        )[0]["id"]
        == row["id"]
    )
    assert (
        client.get("/api/v1/image-artifacts/deletions/" + row["id"] + "/").json()[0][
            "id"
        ]
        == row["id"]
    )
    assert (
        client.post(
            "/api/v1/image-artifacts/deletions/" + row["id"] + "/cancel/"
        ).json()["phase"]
        == "cancelled"
    )


def test_reconfirmation_new_leaves_preserves_prepared_task(runner, user):
    base = image(runner, user)
    observe(runner, [resource(base)])
    plan = request(runner, user, base, True)
    Repo.tick()
    original = Task.objects.get()
    ws = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="new",
        status="stopped",
        base_image_instance=base,
    )
    observe(runner, [resource(base), resource(ws, [base.runner_ref])])
    assert not Repo.delivery_allowed(original.id)
    fresh = Repo.preview(runner.organization_id, "image", base.id)
    Repo.request(
        runner.organization_id, user, "image", base.id, "force", fresh["fingerprint"]
    )
    Repo.tick()
    assert Task.objects.count() == 2
    child = Task.objects.get(workspace=ws)
    assert not Repo.delivery_allowed(original.id)
    assert Repo.delivery_allowed(child.id)
    finish(RunnerService(sio_server=AsyncMock()), runner, child.id)
    observe(runner, [resource(base)])
    Repo.tick()
    assert Repo.delivery_allowed(original.id)
    assert Task.objects.count() == 2


@pytest.mark.parametrize("target", ["assignment", "definition", "image"])
@pytest.mark.parametrize("rebuild", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_cancel_reconciles_latest_build(runner, user, target, rebuild, failed):
    from apps.runners.repositories import ImageGenerationRepository as Generations

    definition = ImageDefinition.objects.create(
        organization=runner.organization, name="cancel recipe", runtime_type="docker"
    )

    def allocate():
        return Generations.request(
            definition=definition,
            runner=runner,
            created_by=user,
            rendered_input={"dockerfile_content": "FROM ubuntu\n"},
        )

    job, first, task = allocate()
    old_default = None
    if rebuild:
        assert Generations.finish(
            task_id=task.id,
            build_job_id=job.id,
            runner_id=runner.id,
            runner_ref=first.runner_ref,
        )
        old_default = first.id
        job, latest, task = allocate()
    else:
        latest = first
    identity = {"assignment": job.id, "definition": definition.id, "image": latest.id}[
        target
    ]
    plan = Repo.request(runner.organization_id, user, target, identity)
    assert Generations.finish(
        task_id=task.id,
        build_job_id=job.id,
        runner_id=runner.id,
        runner_ref=latest.runner_ref,
        error="failed" if failed else None,
    )
    job.refresh_from_db()
    assert job.current_generation_id == old_default
    Repo.cancel(runner.organization_id, plan["id"])
    job.refresh_from_db()
    latest.refresh_from_db()
    task.refresh_from_db()
    assert latest.status == ("failed" if failed else "ready")
    assert task.status == ("failed" if failed else "completed")
    assert job.current_generation_id == (old_default if failed else latest.id)
    assert job.pending_generation_id is None
    assert job.status == ("failed" if failed and not rebuild else "active")


@pytest.mark.parametrize("target", ["assignment", "definition"])
@pytest.mark.parametrize("failed", [False, True])
def test_cancel_preserves_deactivation_and_superseded_build(
    runner, user, target, failed
):
    from apps.runners.repositories import ImageGenerationRepository as Generations

    definition = ImageDefinition.objects.create(
        organization=runner.organization, name="inactive recipe", runtime_type="docker"
    )
    job, older, old_task = Generations.request(
        definition=definition, runner=runner, rendered_input={}
    )
    job, latest, task = Generations.request(
        definition=definition, runner=runner, rendered_input={}
    )
    ImageBuildJob.objects.filter(pk=job.id).update(status="deactivated")
    plan = Repo.request(
        runner.organization_id,
        user,
        target,
        job.id if target == "assignment" else definition.id,
    )
    assert Generations.finish(
        task_id=task.id,
        build_job_id=job.id,
        runner_id=runner.id,
        runner_ref=latest.runner_ref,
        error="failed" if failed else None,
    )
    Repo.cancel(runner.organization_id, plan["id"])
    job.refresh_from_db()
    assert job.status == "deactivated"
    assert job.current_generation_id == (None if failed else latest.id)
    assert Generations.finish(
        task_id=old_task.id,
        build_job_id=job.id,
        runner_id=runner.id,
        runner_ref=older.runner_ref,
    )
    job.refresh_from_db()
    assert job.current_generation_id == (None if failed else latest.id)
    assert job.status == "deactivated"


@pytest.mark.parametrize("target", ["assignment", "definition"])
def test_cancel_then_independent_generation_retirement(runner, user, target):
    from apps.runners.repositories import ImageGenerationRepository as Generations

    definition = ImageDefinition.objects.create(
        organization=runner.organization, name="retired recipe", runtime_type="docker"
    )
    job, older, old_task = Generations.request(
        definition=definition, runner=runner, rendered_input={}
    )
    job, latest, task = Generations.request(
        definition=definition, runner=runner, rendered_input={}
    )
    plan = Repo.request(
        runner.organization_id,
        user,
        target,
        job.id if target == "assignment" else definition.id,
    )
    Repo.cancel(runner.organization_id, plan["id"])
    separate = Repo.request(runner.organization_id, user, "image", latest.id)
    assert Generations.finish(
        task_id=task.id,
        build_job_id=job.id,
        runner_id=runner.id,
        runner_ref=latest.runner_ref,
    )
    # Replaying cancellation must not undo the independent retirement.
    Repo.cancel(runner.organization_id, plan["id"])
    job.refresh_from_db()
    assert job.current_generation_id is None
    assert Generations.finish(
        task_id=old_task.id,
        build_job_id=job.id,
        runner_id=runner.id,
        runner_ref=older.runner_ref,
    )
    job.refresh_from_db()
    assert job.current_generation_id is None
    Repo.cancel(runner.organization_id, separate["id"])
    job.refresh_from_db()
    assert job.current_generation_id == latest.id


def test_force_real_qcow_overlay_domain_and_cloud_init(runner, user, tmp_path):
    """Feed actual runner discovery to policy, not a hand-written blank VM graph."""
    import json
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    runner_dir = root / "runner"
    if not (runner_dir / ".venv/bin/python").exists():
        pytest.skip("Requires runner environment and qemu-img")
    base = image(runner, user, "published")
    ws = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="full VM",
        runtime_type="qemu",
        status="stopped",
        base_image_instance=base,
    )
    output = tmp_path / "scan.json"
    # Only libvirt is mocked: qemu-img creates/inspects the base and overlay.
    code = """
import asyncio, json, sys
from pathlib import Path
from dataclasses import asdict
sys.path.insert(0, 'tests')
from test_storage_inventory import runtime, qcow, cloud_init_domain
async def run():
    r = runtime(Path(sys.argv[1]))
    base = r._snapshot_dir / 'published.qcow2'
    source = r._disk_dir / 'source.qcow2'
    qcow(source)
    await r._publish_image(source, base, {'artifact_id': sys.argv[2]})
    source.unlink()
    disk = r._disk_path(sys.argv[3])
    qcow(disk, base)
    cloud_init_domain(r, sys.argv[3], disk)
    scan = await r.inventory()
    assert scan.complete, scan.errors
    Path(sys.argv[4]).write_text(json.dumps(asdict(scan)))
asyncio.run(run())
"""
    subprocess.run(
        [
            str(runner_dir / ".venv/bin/python"),
            "-c",
            code,
            str(tmp_path),
            str(base.id),
            str(ws.id),
            str(output),
        ],
        cwd=runner_dir,
        check=True,
        capture_output=True,
    )
    scan = json.loads(output.read_text())
    base.runner_ref = str(tmp_path / "snapshots/published.qcow2")
    base.save(update_fields=["runner_ref"])
    observe(runner, scan["resources"])
    graph = Repo.preview(runner.organization_id, "image", base.id)
    assert not graph["blockers"]
    assert graph["counts"] == {"images": 1, "workspaces": 1}
    assert len(graph["resources"]) == 4  # image -> overlay -> domain + seed
    plan = request(runner, user, base, True)
    Repo.tick()
    row = ImageDeletionRequest.objects.get(pk=plan["id"])
    assert list(row.children) == ["workspace:" + str(ws.id)]
    assert Repo.delivery_allowed(next(iter(row.children.values())))
