from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scheduled_tasks", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="scheduledtask",
            name="is_deleted",
            field=models.BooleanField(default=False),
        )
    ]
