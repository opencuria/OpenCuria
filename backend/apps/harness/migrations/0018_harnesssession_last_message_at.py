"""Add last_message_at for conversation feed ordering and display."""

from django.db import migrations, models
from django.db.models import Max
from django.utils import timezone


def _backfill_last_message_at(apps, schema_editor):
    """Set last_message_at from the latest user or completed assistant message."""
    HarnessSession = apps.get_model("harness", "HarnessSession")
    HarnessMessage = apps.get_model("harness", "HarnessMessage")

    user_latest = {
        session_id: latest
        for session_id, latest in (
            HarnessMessage.objects.filter(role="user")
            .values("session_id")
            .annotate(latest=Max("created_at"))
            .values_list("session_id", "latest")
        )
        if latest is not None
    }
    assistant_latest = {
        session_id: latest
        for session_id, latest in (
            HarnessMessage.objects.filter(
                role="assistant",
                completed_at__isnull=False,
            )
            .values("session_id")
            .annotate(latest=Max("completed_at"))
            .values_list("session_id", "latest")
        )
        if latest is not None
    }

    pending: list = []
    for session in HarnessSession.objects.all().iterator():
        candidates = [session.created_at]
        user_at = user_latest.get(session.id)
        if user_at is not None:
            candidates.append(user_at)
        assistant_at = assistant_latest.get(session.id)
        if assistant_at is not None:
            candidates.append(assistant_at)
        session.last_message_at = max(
            stamp for stamp in candidates if stamp is not None
        )
        pending.append(session)
        if len(pending) >= 500:
            HarnessSession.objects.bulk_update(pending, ["last_message_at"])
            pending = []
    if pending:
        HarnessSession.objects.bulk_update(pending, ["last_message_at"])


class Migration(migrations.Migration):
    """Persist last completed user/assistant message time on sessions."""

    dependencies = [
        ("harness", "0017_agentsconfig"),
    ]

    operations = [
        migrations.AddField(
            model_name="harnesssession",
            name="last_message_at",
            field=models.DateTimeField(
                db_index=True,
                default=timezone.now,
                help_text=(
                    "Last completed user or assistant message "
                    "(not tools, not last open)."
                ),
                null=True,
            ),
        ),
        migrations.RunPython(_backfill_last_message_at, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="harnesssession",
            name="last_message_at",
            field=models.DateTimeField(
                db_index=True,
                default=timezone.now,
                help_text=(
                    "Last completed user or assistant message "
                    "(not tools, not last open)."
                ),
            ),
        ),
    ]
