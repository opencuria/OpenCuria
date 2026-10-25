from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scheduled_tasks", "0007_scheduledtask_cron_expression")]

    operations = [
        migrations.AddField(
            model_name="scheduledtask",
            name="run_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="scheduledtask",
            name="recurrence",
            field=models.CharField(
                choices=[
                    ("once", "Once"),
                    ("daily", "Daily"),
                    ("weekly", "Weekly"),
                    ("cron", "Cron"),
                ],
                max_length=16,
            ),
        ),
    ]
