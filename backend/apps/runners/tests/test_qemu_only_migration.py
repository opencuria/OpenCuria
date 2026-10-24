"""Verify upgrades from runners which reported Docker and QEMU inventory."""

import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_qemu_only_migration_preserves_qemu_inventory() -> None:
    old_target = [("runners", "0027_image_process_definitions")]
    new_target = [("runners", "0028_qemu_only")]
    executor = MigrationExecutor(connection)
    executor.migrate(old_target)
    old = executor.loader.project_state(old_target).apps
    organization = old.get_model("organizations", "Organization").objects.create(
        name="Migration inventory", slug="migration-inventory"
    )
    runner = old.get_model("runners", "Runner").objects.create(
        organization=organization, api_token_hash="migration-inventory"
    )
    snapshot_model = old.get_model("runners", "InventorySnapshot")
    runtime_model = old.get_model("runners", "InventoryRuntime")
    resource_model = old.get_model("runners", "InventoryResource")
    alias_model = old.get_model("runners", "InventoryAlias")
    edge_model = old.get_model("runners", "InventoryEdge")
    mixed = snapshot_model.objects.create(
        runner=runner, session="old", epoch=uuid.uuid4(), sequence=1, complete=True
    )
    docker_only = snapshot_model.objects.create(
        runner=runner, session="old", epoch=uuid.uuid4(), sequence=2, complete=True
    )
    qemu = runtime_model.objects.create(
        snapshot=mixed, runtime_type="qemu", collected_at=timezone.now(), complete=True
    )
    for snapshot in (mixed, docker_only):
        docker = runtime_model.objects.create(
            snapshot=snapshot,
            runtime_type="docker",
            collected_at=timezone.now(),
            complete=True,
        )
        resource_model.objects.create(
            runtime=docker, physical_id="container", kind="workspace"
        )
    image = resource_model.objects.create(
        runtime=qemu, physical_id="base.qcow2", kind="image", allocated_bytes=4096
    )
    workspace = resource_model.objects.create(
        runtime=qemu, physical_id="domain", kind="workspace"
    )
    alias = alias_model.objects.create(resource=image, reference="/images/base.qcow2")
    edge = edge_model.objects.create(source=workspace, target=image)
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(new_target)
        new = executor.loader.project_state(new_target).apps
        migrated = new.get_model("runners", "InventoryRuntime").objects
        assert list(migrated.values_list("pk", flat=True)) == [qemu.pk]
        assert migrated.get(pk=qemu.pk).snapshot_id == mixed.pk
        resources = new.get_model("runners", "InventoryResource").objects
        assert set(resources.values_list("pk", flat=True)) == {image.pk, workspace.pk}
        assert resources.get(pk=image.pk).allocated_bytes == 4096
        assert new.get_model("runners", "InventoryAlias").objects.get(pk=alias.pk)
        assert new.get_model("runners", "InventoryEdge").objects.get(pk=edge.pk)
        connection.check_constraints()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
