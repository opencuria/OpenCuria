from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scheduled_tasks", "0003_scheduled_task_scheduler_lease")]

    operations = [
        migrations.AddField(
            model_name="scheduledtaskrun",
            name="trigger",
            field=models.CharField(default="scheduled", max_length=16),
        ),
        migrations.AddField(
            model_name="scheduledtaskrun",
            name="configuration_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
