from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scheduled_tasks", "0004_scheduledtaskrun_snapshot")]

    operations = [
        migrations.AddField(
            model_name="scheduledtaskrun",
            name="completion_check_pending",
            field=models.BooleanField(default=False),
        ),
    ]
