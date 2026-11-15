"""Add bounded timeline display projection storage."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("harness", "0024_alter_harnessmessage_options_and_more")]
    operations = [
        migrations.AddField(
            model_name="harnesspart",
            name="display",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Bounded timeline display projection; full details stay in "
                    "input/output/meta."
                ),
            ),
        ),
    ]
