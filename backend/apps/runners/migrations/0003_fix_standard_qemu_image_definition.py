from django.db import migrations


STANDARD_QEMU_ID = "2025335b-0e41-4baf-89b9-a7281a4fd62f"
STANDARD_QEMU_NAME = "Ubuntu 24.04"
STANDARD_QEMU_SCRIPT = (
    "#!/bin/bash\n\n"
    "echo \"=== Installing Node.js 22.x ===\"\n"
    "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -\n"
    "apt-get install -y nodejs"
)


def update_standard_qemu_definition(apps, schema_editor):
    image_definition_model = apps.get_model("runners", "ImageDefinition")
    definition = image_definition_model.objects.filter(id=STANDARD_QEMU_ID).first()
    if definition is None:
        definition = image_definition_model.objects.filter(
            name=STANDARD_QEMU_NAME,
            organization__isnull=True,
        ).first()
    if definition is None:
        return

    definition.custom_init_script = STANDARD_QEMU_SCRIPT
    definition.save(update_fields=["custom_init_script", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("runners", "0002_seed_global_agent_and_image_definitions"),
    ]

    operations = [
        migrations.RunPython(
            update_standard_qemu_definition,
            migrations.RunPython.noop,
        ),
    ]
