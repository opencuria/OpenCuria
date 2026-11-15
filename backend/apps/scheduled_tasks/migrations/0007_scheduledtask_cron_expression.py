from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scheduled_tasks", "0006_scheduledtask_harness_id")]

    operations = [
        migrations.AddField(
            model_name="scheduledtask",
            name="cron_expression",
            field=models.CharField(blank=True, default="", max_length=1000),
        ),
        migrations.AlterField(
            model_name="scheduledtask",
            name="recurrence",
            field=models.CharField(
                choices=[("daily", "Daily"), ("weekly", "Weekly"), ("cron", "Cron")],
                max_length=16,
            ),
        ),
    ]
