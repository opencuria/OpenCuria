# OAuth authorization state pins provider registration metadata against mutation.
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("credentials", "0009_organization_service_help")]

    # 0008 already introduced this field. Keep this migration as a graph node
    # because it may already be recorded in deployed databases, but do not add
    # the column a second time.
    operations = []
