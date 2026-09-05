"""Add reasoning_effort to HarnessSession."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Store OpenRouter reasoning effort per harness session."""

    dependencies = [
        ("harness", "0006_questionrequest"),
    ]

    operations = [
        migrations.AddField(
            model_name="harnesssession",
            name="reasoning_effort",
            field=models.CharField(
                blank=True,
                default="",
                help_text="OpenRouter reasoning effort (low|medium|high|xhigh|max).",
                max_length=16,
            ),
        ),
    ]
