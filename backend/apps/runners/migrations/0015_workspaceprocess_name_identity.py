# Generated for persistent process list: name = identity per workspace.

from __future__ import annotations

from django.db import migrations, models


def _normalize_processes(apps, schema_editor):
    """Backfill names and run counts idempotently.

    - Blank/empty names become ``process-<id[:8]>``.
    - Duplicate names per workspace get ``-2``/``-3`` suffixes
      (exact, case-sensitive match).
    - ``run_count`` of 0/NULL becomes 1.
    """
    WorkspaceProcess = apps.get_model("runners", "WorkspaceProcess")
    qs = WorkspaceProcess.objects.all().order_by("workspace_id", "started_at", "id")
    seen: dict[tuple[str, str], int] = {}
    for process in qs.iterator():
        name = (process.name or "").strip()
        if not name:
            name = f"process-{str(process.id)[:8]}"
        key = (str(process.workspace_id), name)
        count = seen.get(key, 0)
        if count:
            candidate = f"{name}-{count + 1}"
            while (str(process.workspace_id), candidate) in seen:
                count += 1
                candidate = f"{name}-{count + 1}"
            name = candidate
            key = (str(process.workspace_id), name)
        seen[key] = seen.get(key, 0) + 1
        run_count = process.run_count or 0
        if run_count <= 0:
            run_count = 1
        updates: dict = {}
        if name != (process.name or ""):
            updates["name"] = name
        if run_count != (process.run_count or 0):
            updates["run_count"] = run_count
        if updates:
            WorkspaceProcess.objects.filter(pk=process.pk).update(**updates)


def _noop(apps, schema_editor):
    return None


class Migration(migrations.Migration):

    dependencies = [
        ('runners', '0014_workspaceprocess'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspaceprocess',
            name='run_count',
            field=models.PositiveIntegerField(
                default=0,
                help_text='Number of starts of this named application.',
            ),
        ),
        migrations.AlterField(
            model_name='workspaceprocess',
            name='name',
            field=models.CharField(
                default='',
                help_text=(
                    'Identity of this persistent application within its '
                    'workspace. Unique per workspace (case-sensitive); '
                    'restarts reuse the row.'
                ),
                max_length=255,
            ),
        ),
        migrations.RunPython(_normalize_processes, _noop),
        migrations.AddConstraint(
            model_name='workspaceprocess',
            constraint=models.UniqueConstraint(
                fields=('workspace', 'name'),
                name='uniq_workspace_process_name',
            ),
        ),
    ]
