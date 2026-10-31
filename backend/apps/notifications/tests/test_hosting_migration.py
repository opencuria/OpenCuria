"""Verify removal of populated organization push configuration and secrets."""

import os
import subprocess
import sys
from pathlib import Path


def test_push_migration_purges_old_secrets_and_preserves_devices(tmp_path):
    script = r"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

executor = MigrationExecutor(connection)
before = [
    node for node in executor.loader.graph.leaf_nodes()
    if node[0] not in {"credentials", "notifications"}
] + [
    ("credentials", "0012_alter_credentialservice_credential_type"),
    ("notifications", "0004_server_only_push_credentials"),
]
executor.migrate(before)
old = executor.loader.project_state(before).apps
User = old.get_model("accounts", "User")
Org = old.get_model("organizations", "Organization")
Service = old.get_model("credentials", "CredentialService")
Credential = old.get_model("credentials", "Credential")
Activation = old.get_model("credentials", "OrgCredentialServiceActivation")
Config = old.get_model("notifications", "PushConfiguration")
Device = old.get_model("notifications", "MobileDevice")
Delivery = old.get_model("notifications", "PushDelivery")
Plugin = old.get_model("plugins", "Plugin")
Requirement = old.get_model("plugins", "PluginCredentialRequirement")
Runner = old.get_model("runners", "Runner")
Workspace = old.get_model("runners", "Workspace")

user = User.objects.create(username="push-migration", email="push-migration@test.local")
org = Org.objects.create(name="Push migration", slug="push-migration")
runner = Runner.objects.create(organization=org, name="Runner", api_token_hash="test")
workspace = Workspace.objects.create(runner=runner, name="Workspace", created_by=user)
ordinary = Service.objects.create(
    name="Ordinary", slug="ordinary", credential_type="env", env_var_name="ORDINARY"
)
normal = Credential.objects.create(
    service=ordinary, user=user, name="Keep", encrypted_value="keep-this-value"
)
workspace.credentials.add(normal)
retired = list(Service.objects.filter(slug__in=["mobile-push-apns", "mobile-push-fcm"]))
retired.extend([
    Service.objects.create(
        name="Custom push", slug="custom-push", credential_type="server_secret",
        organization=org,
    ),
    Service.objects.create(
        name="Legacy push file", slug="mobile-push-apns", credential_type="file",
        organization=org, target_path="apns.json",
    ),
])
keys = []
for service in retired:
    Activation.objects.create(organization=org, credential_service=service)
    for owner in ({"organization": org}, {"user": user}):
        key = Credential.objects.create(
            service=service, name="Retired", encrypted_value="old-push-secret", **owner
        )
        workspace.credentials.add(key)
        keys.append(key)
Config.objects.create(organization=org, apns_credential=keys[0], fcm_credential=keys[2])
plugin = Plugin.objects.create(name="Plugin", slug="migration-plugin", organization=org)
Requirement.objects.create(plugin=plugin, key="PUSH", credential_service=retired[0])
device = Device.objects.create(
    user=user, organization=org, platform="ios", app_id="com.opencuria.mobile",
    encrypted_push_token="encrypted-device-token", notifications_enabled=True,
)
delivery = Delivery.objects.create(device=device, event_key="queued", kind="alert")

executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
new = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
Service = new.get_model("credentials", "CredentialService")
Credential = new.get_model("credentials", "Credential")
assert not Service.objects.filter(pk__in=[row.pk for row in retired]).exists()
assert not Credential.objects.filter(pk__in=[row.pk for row in keys]).exists()
Activation = new.get_model("credentials", "OrgCredentialServiceActivation")
assert not Activation.objects.filter(
    credential_service_id__in=[row.pk for row in retired]
).exists()
assert not new.get_model("plugins", "PluginCredentialRequirement").objects.filter(
    plugin_id=plugin.id
).exists()
assert new.get_model("plugins", "Plugin").objects.filter(pk=plugin.pk).exists()
assert Credential.objects.get(pk=normal.pk).encrypted_value == "keep-this-value"
workspace = new.get_model("runners", "Workspace").objects.get(pk=workspace.pk)
assert list(workspace.credentials.values_list("pk", flat=True)) == [normal.pk]
assert new.get_model("notifications", "MobileDevice").objects.get(
    pk=device.pk
).encrypted_push_token == "encrypted-device-token"
Delivery = new.get_model("notifications", "PushDelivery")
assert Delivery.objects.filter(pk=delivery.pk).exists()
assert "notifications_pushconfiguration" not in connection.introspection.table_names()
assert "server_secret" not in dict(Service._meta.get_field("credential_type").choices)
"""
    subprocess.run(
        [sys.executable, "manage.py", "shell", "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.development",
            "SQLITE_PATH": str(tmp_path / "push-migration.sqlite3"),
            "APNS_TEAM_ID": "",
            "APNS_KEY_ID": "",
            "APNS_PRIVATE_KEY_FILE": "",
            "FCM_SERVICE_ACCOUNT_FILE": "",
        },
        check=True,
        capture_output=True,
        text=True,
    )
