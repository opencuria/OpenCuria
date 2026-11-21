"""Seed the global "Windows 11" image definition next to "Ubuntu 24.04".

Like every global recipe it is visible to all organizations and never built
automatically; an admin builds it on a runner that has the Windows 11 ISO.
"""

import uuid

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

WINDOWS_11_ID = uuid.UUID("6a13aa67-7bed-4d24-b258-6c7d525f7f02")
WINDOWS_11_SEED = {
    "name": "Windows 11",
    "description": "",
    "base_distro": "windows:11",
    "packages": ["OpenJS.NodeJS.LTS"],
    "env_vars": {},
    "custom_init_script": "",
}


def seed_windows_11(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Create the recipe unless it, or a global recipe of that name, exists."""
    definitions = apps.get_model("runners", "ImageDefinition").objects.using(
        schema_editor.connection.alias
    )
    if (
        definitions.filter(id=WINDOWS_11_ID).exists()
        or definitions.filter(
            name=WINDOWS_11_SEED["name"], organization__isnull=True
        ).exists()
    ):
        return
    definitions.create(
        id=WINDOWS_11_ID, organization=None, created_by=None, **WINDOWS_11_SEED
    )


def unseed_windows_11(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Remove the recipe again while nothing was built from it."""
    alias = schema_editor.connection.alias
    references = (
        ("ImageBuildJob", "image_definition_id"),
        ("ImageRevision", "definition_id"),
        ("ImageInstance", "origin_definition_id"),
    )
    for model, field in references:
        rows = apps.get_model("runners", model).objects.using(alias)
        if rows.filter(**{field: WINDOWS_11_ID}).exists():
            return
    apps.get_model("runners", "ImageDefinition").objects.using(alias).filter(
        id=WINDOWS_11_ID
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("runners", "0034_image_instance_guest_os"),
    ]

    operations = [
        migrations.RunPython(seed_windows_11, unseed_windows_11),
    ]
