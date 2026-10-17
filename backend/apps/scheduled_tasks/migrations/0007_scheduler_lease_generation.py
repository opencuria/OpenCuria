from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scheduled_tasks", "0006_scheduledtask_harness_id")]

    operations = [
        migrations.AddField(
            model_name="scheduledtaskschedulerlease",
            name="deployment_version",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="scheduledtaskschedulerlease",
            name="owner_id",
            field=models.CharField(blank=True, default="local", max_length=128),
        ),
    ]
