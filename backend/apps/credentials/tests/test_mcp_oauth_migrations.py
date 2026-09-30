"""Regression tests for repairing databases from earlier MCP OAuth iterations."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def _manage(db_path: Path, *args: str) -> None:
    env = {**os.environ, "SQLITE_PATH": str(db_path)}
    subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _legacy_oauth_rows(db_path: Path, *, shared_service: bool = False) -> None:
    env = {**os.environ, "SQLITE_PATH": str(db_path), "SHARED_SERVICE": str(shared_service)}
    script = """
import os
import uuid
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.utils import timezone
from apps.credentials.models import (
    Credential,
    CredentialService,
    McpOAuthAuthorizationState,
    McpOAuthClientRegistration,
)
from apps.organizations.models import Organization
from apps.plugins.models import Plugin, PluginCredentialRequirement

user = get_user_model().objects.create_user(
    email=f'legacy-{uuid.uuid4().hex}@test.local', password='x'
)
org = Organization.objects.create(
    name='Legacy', slug=f'legacy-{uuid.uuid4().hex}'
)
service = CredentialService.objects.get(slug='notion-notion_oauth-oauth')
if os.environ['SHARED_SERVICE'] == 'True':
    CredentialService.objects.filter(pk=service.pk).update(slug='mcp-oauth')
    service.refresh_from_db()
registration = McpOAuthClientRegistration.objects.create(
    server_url='https://mcp.notion.com/mcp',
    callback_url='https://api.test/callback',
    issuer=f'https://auth.notion.com/{uuid.uuid4().hex}',
    client_id='legacy-client',
    authorization_endpoint='https://auth.notion.com/authorize',
    token_endpoint='https://auth.notion.com/token',
)
notion = Plugin.objects.get(slug='notion', organization__isnull=True)
requirement = PluginCredentialRequirement.objects.get(plugin=notion, key='notion_oauth')
notion_server = notion.mcp_servers.get(slug='notion')
requirement.credential_service = service
requirement.save(update_fields=['credential_service'])
if os.environ['SHARED_SERVICE'] == 'True':
    from apps.plugins.models import Plugin
    from apps.credentials.models import CredentialService
    playwright = Plugin.objects.get(slug='playwright', organization__isnull=True)
    PluginCredentialRequirement.objects.create(
        plugin=playwright,
        key='shared-oauth',
        credential_service=service,
        required=True,
        plugin_owned_service=False,
    )
McpOAuthAuthorizationState.objects.create(
    state_hash='a' * 64,
    browser_binding_hash='b' * 64,
    encrypted_code_verifier='encrypted',
    user=user,
    organization=org,
    service=service,
    server_id=notion_server.id,
    requirement_key='notion_oauth',
    server_url=notion_server.url,
    resource='https://resource.notion.com',
    issuer='',
    registration=registration,
    redirect_uri='https://api.test/callback',
    expires_at=timezone.now() + timedelta(minutes=5),
)
Credential.objects.create(
    user=user,
    service=service,
    name='Legacy Notion authorization',
    encrypted_value='encrypted-token',
    created_by=user,
    oauth_server_id=notion_server.id,
    oauth_server_url=notion_server.url,
    oauth_resource='https://resource.notion.com',
    oauth_registration=registration,
    oauth_status='connected',
)
"""
    result = subprocess.run(
        [sys.executable, "manage.py", "shell", "-c", script],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def _remove_legacy_columns(db_path: Path, *, shared_service: bool = False) -> None:
    """Simulate the old physical schema while leaving migration records intact."""
    remove = {
        "credentials_service": {
            "plugin_owned",
            "oauth_plugin_slug",
            "oauth_requirement_key",
        },
        "credentials_credential": {"oauth_server_id"},
        "credentials_mcp_oauth_registration": {"token_endpoint_auth_method"},
        "credentials_mcp_oauth_state": {"requirement_key", "resource", "issuer"},
    }
    with sqlite3.connect(db_path) as connection:
        for table, columns in remove.items():
            indexes = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
            for index in indexes:
                index_name = index[1]
                indexed_columns = {
                    item[2]
                    for item in connection.execute(
                        f'PRAGMA index_info("{index_name}")'
                    ).fetchall()
                }
                if indexed_columns & columns:
                    connection.execute(f'DROP INDEX "{index_name}"')
            for column in columns:
                connection.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')

        # Earlier 0005 revisions had not created the personal/org binding
        # partial indexes either.
        for index_name in (
            "unique_personal_mcp_oauth_binding",
            "unique_org_mcp_oauth_binding",
        ):
            connection.execute(f'DROP INDEX IF EXISTS "{index_name}"')

        # Recreate the Notion row as the legacy shared OAuth catalog service.
        connection.execute(
            "UPDATE credentials_service SET slug = 'mcp-oauth' "
            "WHERE slug = 'notion-notion_oauth-oauth'"
        )


@pytest.mark.parametrize(
    ("legacy", "shared_service"),
    [(False, False), (True, False), (True, True)],
    ids=["fresh-schema", "legacy-schema", "shared-legacy-service"],
)
def test_oauth_repair_migrations_are_safe_and_idempotent(
    tmp_path, legacy, shared_service
):
    db_path = tmp_path / "oauth.sqlite3"
    _manage(db_path, "migrate", "plugins", "0006_mcp_oauth", "--noinput")

    if legacy:
        _legacy_oauth_rows(db_path, shared_service=shared_service)
        _remove_legacy_columns(db_path)

    _manage(db_path, "migrate", "--noinput")
    # Applying migrations a second time must not change/fail on a healthy schema.
    _manage(db_path, "migrate", "--noinput")

    with sqlite3.connect(db_path) as connection:
        for table, expected in {
            "credentials_service": {
                "plugin_owned",
                "oauth_plugin_slug",
                "oauth_requirement_key",
            },
            "credentials_credential": {"oauth_server_id"},
            "credentials_mcp_oauth_state": {
                "requirement_key",
                "resource",
                "issuer",
            },
            "credentials_mcp_oauth_registration": {
                "token_endpoint_auth_method",
            },
        }.items():
            columns = {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            assert expected <= columns

        indexes = {
            row[1]
            for row in connection.execute('PRAGMA index_list("credentials_credential")')
        }
        assert "unique_personal_mcp_oauth_binding" in indexes
        assert "unique_org_mcp_oauth_binding" in indexes

        notion_service = connection.execute(
            "SELECT id, slug, plugin_owned, oauth_plugin_slug, oauth_requirement_key "
            "FROM credentials_service WHERE slug = ?",
            ("notion-notion_oauth-oauth",),
        ).fetchone()
        assert notion_service is not None
        assert notion_service[2:] == (1, "notion", "notion_oauth")
        requirement_service = connection.execute(
            "SELECT credential_service_id FROM plugins_credential_requirement "
            "WHERE key = 'notion_oauth'"
        ).fetchone()
        assert requirement_service == (notion_service[0],)

        if shared_service:
            other_requirement = connection.execute(
                "SELECT credential_service_id FROM plugins_credential_requirement "
                "WHERE key = 'shared-oauth'"
            ).fetchone()
            assert other_requirement is not None
            assert other_requirement[0] != notion_service[0]

        if legacy:
            repaired_state = connection.execute(
                "SELECT s.requirement_key, s.resource, s.issuer, r.issuer "
                "FROM credentials_mcp_oauth_state s "
                "JOIN credentials_mcp_oauth_registration r "
                "ON r.id = s.registration_id"
            ).fetchone()
            assert repaired_state is not None
            assert repaired_state == (
                "notion_oauth",
                "https://mcp.notion.com/mcp",
                repaired_state[3],
                repaired_state[3],
            )
            registration_auth_method = connection.execute(
                "SELECT token_endpoint_auth_method "
                "FROM credentials_mcp_oauth_registration"
            ).fetchone()
            assert registration_auth_method == ("none",)

            migrated_credential = connection.execute(
                "SELECT c.service_id, c.oauth_server_id, c.encrypted_value, "
                "c.oauth_registration_id "
                "FROM credentials_credential c"
            ).fetchone()
            assert migrated_credential is not None
            assert migrated_credential[0] == notion_service[0]
            assert migrated_credential[1] is not None
            assert migrated_credential[2] == "encrypted-token"
            assert migrated_credential[3] is not None


def test_forward_repair_adds_missing_registration_auth_method(tmp_path):
    db_path = tmp_path / "registration-auth-method.sqlite3"
    _manage(db_path, "migrate", "plugins", "0007_repair_notion_mcp_oauth", "--noinput")

    registration_id = "6ee872c4-98a6-41f4-b1cc-30b7a3252915"
    script = f"""
from apps.credentials.models import McpOAuthClientRegistration
McpOAuthClientRegistration.objects.create(
    id='{registration_id}',
    server_url='https://mcp.example.test/',
    callback_url='https://api.example.test/callback',
    issuer='https://auth.example.test/',
    client_id='legacy-client-id',
    encrypted_client_secret='preserve-this-secret',
    authorization_endpoint='https://auth.example.test/authorize',
    token_endpoint='https://auth.example.test/token',
    token_endpoint_auth_method='client_secret_post',
)
"""
    result = subprocess.run(
        [sys.executable, "manage.py", "shell", "-c", script],
        cwd=BACKEND_ROOT,
        env={**os.environ, "SQLITE_PATH": str(db_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\\n{result.stderr}"

    # Reproduce the live database shape after 0006 and plugin 0007 have already
    # been recorded, but before the missed registration column existed.
    with sqlite3.connect(db_path) as connection:
        applied = set(
            connection.execute(
                "SELECT app, name FROM django_migrations WHERE app IN (?, ?)",
                ("credentials", "plugins"),
            )
        )
        assert ("credentials", "0006_repair_legacy_mcp_oauth") in applied
        assert ("plugins", "0007_repair_notion_mcp_oauth") in applied
        connection.execute(
            'ALTER TABLE "credentials_mcp_oauth_registration" '
            'DROP COLUMN "token_endpoint_auth_method"'
        )

    _manage(
        db_path,
        "migrate",
        "credentials",
        "0007_forward_repair_mcp_oauth_registration_auth_method",
        "--noinput",
    )

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info("credentials_mcp_oauth_registration")'
            )
        }
        assert "token_endpoint_auth_method" in columns
        row = connection.execute(
            "SELECT id, encrypted_client_secret, token_endpoint_auth_method "
            "FROM credentials_mcp_oauth_registration"
        ).fetchone()
        assert row == (registration_id.replace("-", ""), "preserve-this-secret", "none")
