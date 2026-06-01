from django.db import migrations, models
import django.db.models.deletion
import uuid


DEFAULT_DESKTOP_START_COMMAND_NAME = "Browser"
DEFAULT_DESKTOP_START_COMMAND_COMMAND = "/usr/local/bin/opencuria-desktop-browser"


def _create_default_workspace_desktop_start_commands(apps, schema_editor):
    Workspace = apps.get_model("runners", "Workspace")
    WorkspaceDesktopStartCommand = apps.get_model(
        "runners", "WorkspaceDesktopStartCommand"
    )

    existing_workspace_ids = set(
        WorkspaceDesktopStartCommand.objects.values_list("workspace_id", flat=True)
    )
    WorkspaceDesktopStartCommand.objects.bulk_create(
        [
            WorkspaceDesktopStartCommand(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                name=DEFAULT_DESKTOP_START_COMMAND_NAME,
                command=DEFAULT_DESKTOP_START_COMMAND_COMMAND,
            )
            for workspace in Workspace.objects.exclude(id__in=existing_workspace_ids)
        ]
    )


class Migration(migrations.Migration):

    dependencies = [
        ("credentials", "0003_credentialservice_target_path_and_more"),
        ("runners", "0010_imagedefinition_delete_attempt_count_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkspaceDesktopStartCommand",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("command", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="desktop_start_commands",
                        to="runners.workspace",
                    ),
                ),
            ],
            options={
                "db_table": "runners_workspace_desktop_start_command",
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddField(
            model_name="imageinstance",
            name="desktop_start_commands_snapshot",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Snapshot of workspace desktop start commands captured with this "
                    "image. Each entry stores 'name' and 'command'."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="imageinstance",
            name="credentials",
            field=models.ManyToManyField(
                blank=True,
                help_text=(
                    "Credentials associated with this image instance. Workspace "
                    "cloning must still supply credentials explicitly."
                ),
                related_name="image_instances",
                to="credentials.credential",
            ),
        ),
        migrations.RunPython(
            _create_default_workspace_desktop_start_commands,
            migrations.RunPython.noop,
        ),
    ]
