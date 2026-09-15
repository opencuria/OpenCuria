# Generated for harness message notice dismissal.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('harness', '0020_recentmodel'),
    ]

    operations = [
        migrations.AddField(
            model_name='harnessmessage',
            name='notice_dismissed_at',
            field=models.DateTimeField(
                blank=True,
                help_text='When the user dismissed the stopped/failed notice for this message.',
                null=True,
            ),
        ),
    ]
