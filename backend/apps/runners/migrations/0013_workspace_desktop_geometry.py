from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("runners", "0012_workspace_credentials_present"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspace",
            name="desktop_height",
            field=models.PositiveIntegerField(
                default=1080,
                help_text=(
                    "Fixed Xvnc framebuffer height in pixels. Applied the next "
                    "time the desktop starts."
                ),
            ),
        ),
        migrations.AddField(
            model_name="workspace",
            name="desktop_width",
            field=models.PositiveIntegerField(
                default=1920,
                help_text=(
                    "Fixed Xvnc framebuffer width in pixels. Applied the next "
                    "time the desktop starts."
                ),
            ),
        ),
    ]
