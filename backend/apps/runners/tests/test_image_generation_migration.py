"""Exercise the preserving migration against actual historical model state."""

import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_legacy_migration_preserves_rows_resources_pins_and_task_fks():
    old_target = [("runners", "0016_workspaceprocess_kind_temp")]
    new_target = [("runners", "0017_immutable_image_generations")]
    executor = MigrationExecutor(connection)
    executor.migrate(old_target)
    old = executor.loader.project_state(old_target).apps
    Org = old.get_model("organizations", "Organization")
    User = old.get_model("accounts", "User")
    Runner = old.get_model("runners", "Runner")
    Definition = old.get_model("runners", "ImageDefinition")
    Job = old.get_model("runners", "ImageBuildJob")
    Image = old.get_model("runners", "ImageInstance")
    Workspace = old.get_model("runners", "Workspace")
    Task = old.get_model("runners", "Task")
    org = Org.objects.create(name="Migration org", slug="migration-org")
    user = User.objects.create(email="migration@example.com", password="unused")
    runner = Runner.objects.create(organization=org, api_token_hash="migration-runner")
    definition = Definition.objects.create(
        name="Legacy changed recipe",
        organization=org,
        packages=["curl"],
        is_active=False,
        status="active",
    )
    task = Task.objects.create(runner=runner, type="build_image", status="completed")
    job = Job.objects.create(
        image_definition=definition,
        runner=runner,
        status="active",
        deleting_task_id="unknown-correlation",
    )
    image = Image.objects.create(
        runner=runner,
        build_job=job,
        origin_definition=definition,
        origin_type="definition_build",
        runner_ref="/existing/unmodified.qcow2",
        name="Legacy bytes",
        size_bytes=0,
        creating_task_id=str(task.id),
        deleting_task_id="",
    )
    workspace = Workspace.objects.create(
        runner=runner, created_by=user, base_image_instance=image
    )
    unknown = Image.objects.create(
        runner=runner,
        name="Uncertain provenance",
        creating_task_id=str(uuid.uuid4()),
        size_bytes=-5,
    )
    queued_task = Task.objects.create(
        runner=runner, type="delete_image", status="pending"
    )
    queued_job = Job.objects.create(
        image_definition=definition,
        runner=Runner.objects.create(organization=org, api_token_hash="queued-runner"),
        status="pending_deletion",
        deleting_task_id=str(queued_task.id),
    )
    queued_image = Image.objects.create(
        runner=runner,
        name="Queued bytes",
        status="pending_deletion",
        runner_ref="/existing/queued.qcow2",
        deleting_task_id=str(queued_task.id),
    )
    invalid = Image.objects.create(
        runner=runner,
        name="Invalid correlation",
        creating_task_id="not-a-uuid",
        deleting_task_id="malformed-delete",
    )
    ids = (definition.id, job.id, image.id, workspace.id, unknown.id)
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(new_target)
        new = executor.loader.project_state(new_target).apps
        migrated = new.get_model("runners", "ImageInstance").objects.get(id=image.id)
        migrated_job = new.get_model("runners", "ImageBuildJob").objects.get(id=job.id)
        migrated_ws = new.get_model("runners", "Workspace").objects.get(id=workspace.id)
        migrated_definition = new.get_model("runners", "ImageDefinition").objects.get(
            id=definition.id
        )
        uncertain = new.get_model("runners", "ImageInstance").objects.get(id=unknown.id)
        assert (
            migrated_definition.id,
            migrated_job.id,
            migrated.id,
            migrated_ws.id,
            uncertain.id,
        ) == ids
        assert migrated.deleting_task_id is None
        assert migrated.legacy_task_references["deleting_task_id"] == ""
        queued = new.get_model("runners", "ImageInstance").objects.get(
            pk=queued_image.pk
        )
        queued_assignment = new.get_model("runners", "ImageBuildJob").objects.get(
            pk=queued_job.pk
        )
        invalid_row = new.get_model("runners", "ImageInstance").objects.get(
            pk=invalid.pk
        )
        assert queued.status == queued_assignment.status == "pending_deletion"
        assert (
            queued.deleting_task_id
            == queued_assignment.deleting_task_id
            == queued_task.id
        )
        assert queued.runner_ref == "/existing/queued.qcow2"
        assert (
            invalid_row.creating_task_id is None
            and invalid_row.deleting_task_id is None
        )
        assert invalid_row.legacy_task_references == {
            "creating_task_id": "not-a-uuid",
            "deleting_task_id": "malformed-delete",
        }
        # Check actual SQL schema: correlation columns keep indexes and real FKs,
        # and PostgreSQL no longer has string operator classes on UUID columns.
        with connection.cursor() as cursor:
            for table, columns in [
                ("runners_build_job", ["deleting_task_id"]),
                ("runners_image_instance", ["creating_task_id", "deleting_task_id"]),
            ]:
                constraints = connection.introspection.get_constraints(cursor, table)
                for column in columns:
                    assert any(
                        c["index"] and c["columns"] == [column]
                        for c in constraints.values()
                    )
                    assert any(
                        c["foreign_key"] and c["columns"] == [column]
                        for c in constraints.values()
                    )
            if connection.vendor == "postgresql":
                cursor.execute(
                    "SELECT indexdef FROM pg_indexes WHERE tablename IN ('runners_build_job', 'runners_image_instance')"
                )
                assert not any(
                    "varchar_pattern_ops" in row[0] and "task_id" in row[0]
                    for row in cursor.fetchall()
                )
        assert migrated.runner_ref == "/existing/unmodified.qcow2"
        assert migrated_ws.base_image_instance_id == image.id
        assert migrated_job.current_generation_id == image.id
        assert migrated.creating_task_id == task.id
        assert migrated.is_legacy and migrated.revision_id is None
        assert migrated.size_bytes is None and uncertain.size_bytes is None
        assert uncertain.creating_task_id is None
        assert (
            uncertain.legacy_task_references["creating_task_id"]
            == unknown.creating_task_id
        )
        assert (
            migrated_job.legacy_task_references["deleting_task_id"]
            == "unknown-correlation"
        )
        assert migrated_definition.status == "deactivated"
        revision = new.get_model("runners", "ImageRevision").objects.get(
            definition_id=definition.id
        )
        assert revision.recipe["packages"] == ["curl"]
        assert revision.rendered_input is None
        connection.check_constraints()
        # Continue to the final preserving-intent migration with these same
        # historical rows, not a separately synthesized modern fixture.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        final = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
        requests = final.get_model("runners", "ImageDeletionRequest")
        retained = requests.objects.get(target_type="image", target_id=queued_image.id)
        assert retained.phase == "intervention"
        assert retained.children == {
            "image:" + str(queued_image.id): str(queued_task.id)
        }
        assert final.get_model("runners", "Task").objects.count() == 2
        command = final.get_model("runners", "LifecycleCommand").objects.get(
            task_id=queued_task.id
        )
        assert command.event == "" and command.payload == {} and command.deliveries == 0
        assert (
            final.get_model("runners", "ImageInstance")
            .objects.get(pk=image.id)
            .runner_ref
            == "/existing/unmodified.qcow2"
        )
        connection.check_constraints()

    finally:
        # Always restore the schema for the rest of the suite.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
