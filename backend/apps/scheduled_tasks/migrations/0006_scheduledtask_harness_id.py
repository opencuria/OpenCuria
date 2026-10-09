from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scheduled_tasks", "0005_scheduledtaskrun_completion_check_pending")
    ]

    operations = [
        migrations.AddField(
            model_name="scheduledtask",
            name="harness_id",
            field=models.CharField(default="native", max_length=16),
        ),
    ]
