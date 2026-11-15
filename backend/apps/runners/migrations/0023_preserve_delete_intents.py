"""Preserve queued intentions without dispatching or deleting physical resources."""

import uuid

from django.db import migrations


def preserve(apps, schema_editor):
    Image = apps.get_model("runners", "ImageInstance")
    Request = apps.get_model("runners", "ImageDeletionRequest")
    Job = apps.get_model("runners", "ImageBuildJob")
    Definition = apps.get_model("runners", "ImageDefinition")
    seen = set()
    targets = []
    for definition in Definition.objects.filter(
        organization_id__isnull=False,
        status__in=["pending_deletion", "deleting", "delete_failed"],
    ):
        images = list(
            Image.objects.filter(build_job__image_definition_id=definition.id)
        )
        targets.append(
            ("definition", definition.id, definition.organization_id, images)
        )
        seen.update(i.id for i in images)
    for job in Job.objects.filter(
        status__in=["pending_deletion", "deleting", "delete_failed"]
    ).select_related("runner"):
        images = list(Image.objects.filter(build_job_id=job.id))
        if any(i.id in seen for i in images):
            continue
        targets.append(("assignment", job.id, job.runner.organization_id, images))
        seen.update(i.id for i in images)
    for image in Image.objects.filter(
        status__in=["pending_deletion", "deleting", "delete_failed"]
    ).select_related("runner"):
        if image.id not in seen:
            targets.append(("image", image.id, image.runner.organization_id, [image]))
    for kind, identity, org_id, images in targets:
        children = {
            "image:" + str(i.id): str(i.deleting_task_id)
            for i in images
            if i.deleting_task_id
        }
        Request.objects.get_or_create(
            organization_id=org_id,
            target_type=kind,
            target_id=identity,
            defaults={
                "id": uuid.uuid4(),
                "mode": "deferred",
                "phase": "intervention" if children else "waiting_inventory",
                "diagnostic": "Legacy deletion intent retained; fresh inventory required",
                "children": children,
                "previous": {},
                "approval": {
                    "roots": [str(i.id) for i in images],
                    "images": [],
                    "workspaces": [],
                    "resources": [],
                    "edges": [],
                    "pins": [],
                    "blockers": [],
                },
            },
        )
        # Selection fence only. Never issue commands or remove storage here.
        Image.objects.filter(pk__in=[i.id for i in images]).exclude(
            status__in=["deleted", "deleting"]
        ).update(status="pending_deletion")


class Migration(migrations.Migration):
    dependencies = [("runners", "0022_imagedeletionrequest")]
    operations = [migrations.RunPython(preserve, migrations.RunPython.noop)]
