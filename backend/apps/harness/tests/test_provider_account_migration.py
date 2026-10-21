"""Populated-database migration coverage for legacy provider accounts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_legacy_provider_accounts_migrate_with_secrets_and_session_identity(
    tmp_path: Path,
) -> None:
    """Move populated legacy records using the real old and current schemas."""
    database_path = tmp_path / "provider-accounts-migration.sqlite3"
    script = r"""
import json
import uuid
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from common.utils import decrypt_value, encrypt_value

executor = MigrationExecutor(connection)
old_targets = [
    ("credentials", "0010_oauth_registration_fingerprint"),
    ("harness", "0030_harnesssession_summary"),
]
executor.migrate(old_targets)
old_apps = executor.loader.project_state(old_targets).apps
User = old_apps.get_model("accounts", "User")
Organization = old_apps.get_model("organizations", "Organization")
CredentialService = old_apps.get_model("credentials", "CredentialService")
Credential = old_apps.get_model("credentials", "Credential")
ProviderConnection = old_apps.get_model("harness", "ProviderConnection")
HarnessConnection = old_apps.get_model("harness", "HarnessConnection")
Runner = old_apps.get_model("runners", "Runner")
Workspace = old_apps.get_model("runners", "Workspace")
HarnessSession = old_apps.get_model("harness", "HarnessSession")

user = User.objects.create(
    username="migration-user", email="migration-user@example.test"
)
org = Organization.objects.create(
    name="With members", slug="provider-migration-members"
)
empty_org = Organization.objects.create(
    name="No members", slug="provider-migration-no-members"
)

native_id = uuid.uuid4()
ProviderConnection.objects.create(
    id=native_id,
    organization_id=org.id,
    provider="openrouter",
    credentials_encrypted=encrypt_value(json.dumps({"api_key": "native-secret"})),
    config={"base_url": "https://router.example/v1"},
)
empty_id = uuid.uuid4()
ProviderConnection.objects.create(
    id=empty_id,
    organization_id=empty_org.id,
    provider="amazon-bedrock",
    credentials_encrypted="",
    config={"region": "us-east-1"},
)
claude_service = CredentialService.objects.get(
    slug="claude-agent-api-token", organization__isnull=True
)
legacy_credential = Credential.objects.create(
    user_id=user.id,
    organization_id=None,
    service_id=claude_service.id,
    name="Legacy Claude token",
    encrypted_value=encrypt_value("claude-legacy-token"),
    created_by_id=user.id,
)
claude_id = uuid.uuid4()
HarnessConnection.objects.create(
    id=claude_id,
    organization_id=org.id,
    user_id=user.id,
    credential_id=legacy_credential.id,
    auth_type="api_token",
    label="Legacy Claude",
)
runner = Runner.objects.create(
    name="provider-migration-runner",
    api_token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
    organization_id=org.id,
)
workspace = Workspace.objects.create(
    runner_id=runner.id,
    name="legacy workspace",
    created_by_id=user.id,
)
session_id = uuid.uuid4()
HarnessSession.objects.create(
    id=session_id,
    workspace_id=workspace.id,
    organization_id=org.id,
    connection_id=claude_id,
    harness_id="claude",
    created_by_id=user.id,
    external_session_id="resumable-claude-session",
    engine_state={"resume": "preserved"},
)

executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
new_apps = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
ProviderAccount = new_apps.get_model("harness", "ProviderConnection")
NewCredential = new_apps.get_model("credentials", "Credential")
NewSession = new_apps.get_model("harness", "HarnessSession")
Membership = new_apps.get_model("organizations", "Membership")

native = ProviderAccount.objects.get(pk=native_id)
empty = ProviderAccount.objects.get(pk=empty_id)
claude = ProviderAccount.objects.get(pk=claude_id)
native_secret = NewCredential.objects.get(pk=native.credential_id)
claude_secret = NewCredential.objects.get(pk=claude.credential_id)
session = NewSession.objects.get(pk=session_id)
assert native.user_id is None
assert json.loads(decrypt_value(native_secret.encrypted_value)) == {
    "api_key": "native-secret"
}
assert native_secret.service.credential_type == "provider_secret"
assert json.loads(decrypt_value(empty.credential.encrypted_value)) == {}
assert not Membership.objects.filter(organization_id=empty_org.id).exists()
assert claude.user_id == user.id
assert claude.config == {"auth_type": "api_token"}
assert json.loads(decrypt_value(claude_secret.encrypted_value)) == {
    "token": "claude-legacy-token"
}
assert not NewCredential.objects.filter(pk=legacy_credential.id).exists()
assert "harness_connection" not in connection.introspection.table_names()
assert "connection" not in {field.name for field in NewSession._meta.get_fields()}
assert session.harness_id == "claude"
assert session.created_by_id == user.id
assert session.external_session_id == "resumable-claude-session"
assert session.engine_state == {"resume": "preserved"}
"""
    backend_root = Path(__file__).resolve().parents[3]
    subprocess.run(
        [sys.executable, "manage.py", "shell", "-c", script],
        cwd=backend_root,
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.development",
            "SQLITE_PATH": str(database_path),
        },
        check=True,
        timeout=300,
    )
