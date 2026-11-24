"""Forget registrations made for direct APNs/FCM delivery.

Their native tokens cannot be used through the push proxy; the app registers
again on its next start. Kept apart from the schema change so PostgreSQL does
not alter a table with pending deferred-constraint events.
"""

from django.db import migrations


def drop_registrations(apps, schema_editor):
    apps.get_model("notifications", "PushDelivery").objects.all().delete()
    apps.get_model("notifications", "MobileDevice").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0005_remove_pushconfiguration")]

    operations = [migrations.RunPython(drop_registrations, migrations.RunPython.noop)]
