"""Add per-session / per-message position columns with deterministic backfill."""

from __future__ import annotations

from django.db import migrations, models


def _backfill_positions(apps, schema_editor):
    """Assign deterministic positions ordered by (created_at, id).

    Messages get per-session 0-based positions; parts get per-message
    0-based positions. Ordering is total (created_at, id) so tied
    timestamps stay stable across DBs.
    """
    HarnessSession = apps.get_model("harness", "HarnessSession")
    HarnessMessage = apps.get_model("harness", "HarnessMessage")
    HarnessPart = apps.get_model("harness", "HarnessPart")

    for session in HarnessSession.objects.all().only("id").iterator():
        ordered_ids = list(
            HarnessMessage.objects.filter(session_id=session.id)
            .order_by("created_at", "id")
            .values_list("id", flat=True)
        )
        for position, message_id in enumerate(ordered_ids):
            HarnessMessage.objects.filter(id=message_id).update(position=position)

    for message in HarnessMessage.objects.all().only("id").iterator():
        ordered_ids = list(
            HarnessPart.objects.filter(message_id=message.id)
            .order_by("created_at", "id")
            .values_list("id", flat=True)
        )
        for position, part_id in enumerate(ordered_ids):
            HarnessPart.objects.filter(id=part_id).update(position=position)


class Migration(migrations.Migration):
    """Reliable chat timeline ordering via explicit positions."""

    dependencies = [
        ("harness", "0022_harnessmessage_skill_ids"),
    ]

    operations = [
        migrations.AddField(
            model_name="harnessmessage",
            name="position",
            field=models.PositiveIntegerField(
                default=0,
                db_index=True,
                help_text="Per-session chronological order (0-based, gap-tolerant).",
            ),
        ),
        migrations.AddField(
            model_name="harnesspart",
            name="position",
            field=models.PositiveIntegerField(
                default=0,
                db_index=True,
                help_text="Per-message chronological order (0-based, gap-tolerant).",
            ),
        ),
        migrations.RunPython(_backfill_positions, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="harnessmessage",
            constraint=models.UniqueConstraint(
                fields=["session", "position"],
                name="harness_message_session_position_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="harnesspart",
            constraint=models.UniqueConstraint(
                fields=["message", "position"],
                name="harness_part_message_position_uniq",
            ),
        ),
    ]
