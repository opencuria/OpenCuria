"""Remove redundant Codex turn snapshots already represented by file patches."""

from __future__ import annotations

import uuid

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Exists, OuterRef

BATCH_SIZE = 500


def remove_turn_diff_snapshots(
    apps: Apps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Keep actual edits and text; remove only identifiable derived snapshots."""
    parts = apps.get_model("harness", "HarnessPart").objects.using(
        schema_editor.connection.alias
    )
    file_patches = parts.filter(
        message_id=OuterRef("message_id"),
        type="patch",
        meta__path__startswith="/",
    )
    linked_tools = parts.filter(
        message_id=OuterRef("message_id"), type="tool", call_id=OuterRef("call_id")
    )
    candidates = (
        parts.filter(
            type="patch",
            state="completed",
            message__harness_id="codex",
            message__completed_at__isnull=False,
            meta__path="",
            meta__tool="edit",
            meta__step=1,
        )
        .filter(Exists(file_patches), ~Exists(linked_tools))
        .order_by("pk")
    )
    last_pk = None
    while True:
        batch = list(
            (candidates.filter(pk__gt=last_pk) if last_pk else candidates).values_list(
                "pk", "call_id"
            )[:BATCH_SIZE]
        )
        if not batch:
            return
        snapshot_ids = []
        for pk, call_id in batch:
            try:
                # Turn snapshots used the upstream turn UUID. Native file
                # changes instead use their tool call's ID and a file path.
                if str(uuid.UUID(call_id)) == call_id:
                    snapshot_ids.append(pk)
            except ValueError:
                continue
        parts.filter(pk__in=snapshot_ids).delete()
        last_pk = batch[-1][0]


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("harness", "0039_remove_legacy_agent_defaults")]
    operations = [
        migrations.RunPython(remove_turn_diff_snapshots, migrations.RunPython.noop)
    ]
