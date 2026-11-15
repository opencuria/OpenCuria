# Generated for session-scoped temporary background processes.
#
# Adds ``WorkspaceProcess.kind`` (persistent|temp) and replaces the plain
# (workspace, name) uniqueness with two partial constraints:
# - persistent rows stay unique per (workspace, name)
# - temp rows are unique per (workspace, name, session_id)
# Existing rows backfill to ``persistent``.

from __future__ import annotations

from django.db import migrations, models


def _backfill_persistent(apps, schema_editor):
    WorkspaceProcess = apps.get_model("runners", "WorkspaceProcess")
    WorkspaceProcess.objects.filter(kind="").update(kind="persistent")


def _noop(apps, schema_editor):
    return None


class Migration(migrations.Migration):

    dependencies = [
        ('runners', '0015_workspaceprocess_name_identity'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspaceprocess',
            name='kind',
            field=models.CharField(
                default='persistent',
                help_text=(
                    'persistent: workspace-global app (name unique per '
                    'workspace). temp: session-scoped helper (name unique '
                    'per workspace+session), stopped automatically when '
                    'the owning harness run finishes.'
                ),
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='workspaceprocess',
            name='session_id',
            field=models.UUIDField(
                blank=True,
                db_index=True,
                help_text=(
                    'Agent run (harness session) that owns this process. '
                    'Informational for persistent processes; required for '
                    'temp processes (scope of the temp name + cleanup hook '
                    'target).'
                ),
                null=True,
            ),
        ),
        migrations.RunPython(_backfill_persistent, _noop),
        migrations.RemoveConstraint(
            model_name='workspaceprocess',
            name='uniq_workspace_process_name',
        ),
        migrations.AddIndex(
            model_name='workspaceprocess',
            index=models.Index(
                fields=['workspace', 'session_id', 'status'],
                name='process_ws_session_status_idx',
            ),
        ),
        migrations.AddConstraint(
            model_name='workspaceprocess',
            constraint=models.UniqueConstraint(
                condition=models.Q(('kind', 'persistent')),
                fields=('workspace', 'name'),
                name='uniq_workspace_process_name',
            ),
        ),
        migrations.AddConstraint(
            model_name='workspaceprocess',
            constraint=models.UniqueConstraint(
                condition=models.Q(('kind', 'temp')),
                fields=('workspace', 'name', 'session_id'),
                name='uniq_workspace_temp_process_name',
            ),
        ),
    ]
