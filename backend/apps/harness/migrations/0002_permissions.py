# Generated for harness M3: permission request flow models.

import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('harness', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='PermissionRequest',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('organization_id', models.UUIDField(help_text='Owning organization (scoping, no FK to avoid cycles).')),
                ('workspace_id', models.UUIDField(blank=True, help_text='Workspace UUID (plain field until M6).', null=True)),
                ('session_id', models.UUIDField(help_text='Harness session UUID (FK to HarnessSession in M6).')),
                ('message_id', models.UUIDField(blank=True, null=True)),
                ('call_id', models.CharField(blank=True, default='', max_length=255)),
                ('tool', models.CharField(max_length=64)),
                ('pattern', models.CharField(help_text='Action/pattern the user approves (path or command).', max_length=1024)),
                ('title', models.CharField(blank=True, default='', max_length=512)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected')], default='pending', max_length=16)),
                ('remember', models.CharField(choices=[('once', 'Once'), ('always', 'Always')], default='once', max_length=16)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'harness_permission_request',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='PermissionAllowlist',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('organization_id', models.UUIDField(help_text='Owning organization (scoping, no FK to avoid cycles).')),
                ('workspace_id', models.UUIDField(blank=True, null=True)),
                ('tool', models.CharField(max_length=64)),
                ('pattern', models.CharField(max_length=1024)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'harness_permission_allowlist',
                'ordering': ['-created_at'],
                'constraints': [models.UniqueConstraint(fields=['organization_id', 'workspace_id', 'tool', 'pattern'], name='uniq_allowlist_scope_tool_pattern')],
            },
        ),
    ]
