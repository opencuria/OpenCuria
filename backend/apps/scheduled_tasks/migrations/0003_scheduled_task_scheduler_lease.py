from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scheduled_tasks", "0002_scheduledtask_is_deleted")]

    operations = [
        migrations.CreateModel(
            name="ScheduledTaskSchedulerLease",
            fields=[
                (
                    "key",
                    models.PositiveSmallIntegerField(
                        default=1, primary_key=True, serialize=False, editable=False
                    ),
                ),
                ("owner_token", models.UUIDField(null=True, blank=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
            ],
            options={"db_table": "scheduled_tasks_scheduler_lease"},
        ),
    ]
