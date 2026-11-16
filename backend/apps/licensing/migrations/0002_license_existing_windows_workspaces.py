"""Give Windows workspaces created before licensing an unlicensed license row.

Every Windows workspace holds a license row (running counts rely on it). Rows
of existing workspaces are ``unlicensed``, so their next start applies the
organization's license mode exactly like a create.
"""

from django.db import migrations


def license_existing_windows_workspaces(apps, schema_editor):
    """Insert an ``unlicensed`` row for every live Windows workspace without one."""
    workspace_model = apps.get_model("runners", "Workspace")
    license_model = apps.get_model("licensing", "WindowsWorkspaceLicense")
    licensed = license_model.objects.values("workspace_id")
    workspace_ids = (
        workspace_model.objects.filter(base_image_instance__guest_os="windows")
        .exclude(status="deleted")
        .exclude(pk__in=licensed)
        .values_list("pk", flat=True)
    )
    license_model.objects.bulk_create(
        [
            license_model(workspace_id=workspace_id, kind="unlicensed")
            for workspace_id in workspace_ids
        ]
    )


class Migration(migrations.Migration):
    dependencies = [
        ("licensing", "0001_initial"),
        ("runners", "0034_image_instance_guest_os"),
    ]

    operations = [
        migrations.RunPython(
            license_existing_windows_workspaces, migrations.RunPython.noop
        ),
    ]
