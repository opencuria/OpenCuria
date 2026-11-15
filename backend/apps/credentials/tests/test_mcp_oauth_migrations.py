"""Forward OAuth migration safety and grant preservation tests."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from django.db import connection, transaction

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def _registration_fingerprint_column_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        columns = [
            row[1]
            for row in connection.execute(
                'PRAGMA table_info("credentials_mcp_oauth_state")'
            )
        ]
    return columns.count("registration_fingerprint")


def _manage(
    db_path: Path, *args: str, env_overrides: dict[str, str] | None = None
) -> None:
    subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=BACKEND_ROOT,
        env={**os.environ, "SQLITE_PATH": str(db_path), **(env_overrides or {})},
        check=True,
        capture_output=True,
        text=True,
    )


def _seed_legacy_grants(
    db_path: Path,
    *,
    multiple_endpoints: bool,
    ambiguous_requirement: bool = False,
    mismatch_service: bool = False,
    long_slug: bool = False,
    env_overrides: dict[str, str] | None = None,
) -> dict:
    script = r"""
import json
import uuid
from datetime import timedelta
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from common.utils import encrypt_value
executor = MigrationExecutor(connection)
state = executor.loader.project_state([
    ("credentials", "0007_forward_repair_mcp_oauth_registration_auth_method"),
    ("plugins", "0007_repair_notion_mcp_oauth"),
])
apps = state.apps
User = apps.get_model("accounts", "User")
Org = apps.get_model("organizations", "Organization")
Runner = apps.get_model("runners", "Runner")
Workspace = apps.get_model("runners", "Workspace")
Service = apps.get_model("credentials", "CredentialService")
Credential = apps.get_model("credentials", "Credential")
Registration = apps.get_model("credentials", "McpOAuthClientRegistration")
State = apps.get_model("credentials", "McpOAuthAuthorizationState")
Plugin = apps.get_model("plugins", "Plugin")
Server = apps.get_model("plugins", "PluginMcpServer")
Requirement = apps.get_model("plugins", "PluginCredentialRequirement")
Membership = apps.get_model("organizations", "Membership")
user = User.objects.create(email=f'migration-{uuid.uuid4().hex}@test.local', password='x')
org = Org.objects.create(name='Migration org', slug=f'migration-{uuid.uuid4().hex}')
Membership.objects.create(user_id=user.id, organization_id=org.id, role='admin')
runner = Runner.objects.create(name='migration runner', organization_id=org.id, api_token_hash='migration-token')
workspace = Workspace.objects.create(runner_id=runner.id, name='linked workspace', created_by_id=user.id)
if not MULTIPLE:
    service = Service.objects.get(slug='notion-notion_oauth-oauth')
    if LONG_SLUG:
        service.slug = 'x' * 255
        service.save(update_fields=['slug'])
    plugin = Plugin.objects.get(slug='notion', organization__isnull=True)
    server = Server.objects.get(plugin_id=plugin.id, slug='notion')
    requirement = Requirement.objects.get(plugin_id=plugin.id, key='notion_oauth')
    registration = Registration.objects.create(server_url=server.url, callback_url='https://api.example/callback',
        issuer='https://auth.example/tenant', client_id='legacy-client', authorization_endpoint='https://auth.example/authorize',
        token_endpoint='https://auth.example/token', registration_endpoint='https://auth.example/register', token_endpoint_auth_method='none')
    payload={'access_token':'legacy-access','refresh_token':'legacy-refresh','token_type':'Bearer',
        'expires_at':'2099-01-01T00:00:00+00:00','server_id':str(server.id),'server_url':server.url,
        'resource':server.url,'registration_id':str(registration.id),'identity':{'workspace_id':'w1'}}
    credential=Credential.objects.create(user_id=user.id,service_id=service.id,name='legacy account',
        encrypted_value=encrypt_value(json.dumps(payload)),created_by_id=user.id,oauth_server_id=server.id,
        oauth_server_url=server.url,oauth_resource=server.url,oauth_registration_id=registration.id,oauth_status='connected')
    workspace.credentials.add(credential)
    State.objects.create(state_hash='a'*64,browser_binding_hash='b'*64,encrypted_code_verifier=encrypt_value('verifier'),
        user_id=user.id,organization_id=org.id,service_id=service.id,server_id=server.id,requirement_key=requirement.key,
        server_url=server.url,resource=server.url,issuer=registration.issuer,registration_id=registration.id,
        organization_credential=False,redirect_uri='https://api.example/callback',expires_at=timezone.now()+timedelta(minutes=5))
    print(json.dumps({'records':[],'service':str(service.id),'workspace':str(workspace.id),
        'requirement':str(requirement.id)}))
else:
    service=Service.objects.create(name='Shared MCP',slug=('x'*255 if LONG_SLUG else 'shared-mcp'),organization_id=org.id,
        credential_type='mcp_oauth',plugin_owned=True,oauth_plugin_slug='',oauth_requirement_key='')
    records=[]
    keys=['shared_auth','shared_auth'] if AMBIGUOUS else ['first_auth','second_auth']
    shared_plugin = None
    shared_requirement = None
    for index,(slug,endpoint,key) in enumerate([('first','https://one.example/mcp',keys[0]),('second','https://two.example/mcp',keys[1])]):
        if AMBIGUOUS:
            plugin = shared_plugin or Plugin.objects.create(name='ambiguous',slug='ambiguous',organization_id=org.id,enabled=True,published=True,created_by_id=user.id)
            shared_plugin = plugin
            requirement = shared_requirement or Requirement.objects.create(plugin_id=plugin.id,key=key,credential_service_id=service.id,required=True)
            shared_requirement = requirement
        else:
            plugin=Plugin.objects.create(name=slug,slug=slug,organization_id=org.id,enabled=True,published=True,created_by_id=user.id)
            requirement=Requirement.objects.create(plugin_id=plugin.id,key=key,credential_service_id=service.id,required=True)
        server=Server.objects.create(plugin_id=plugin.id,name=slug,slug=slug,transport='streamable_http',url=endpoint,auth_type='oauth',oauth_requirement_key=key)
        registration=Registration.objects.create(server_url=endpoint,callback_url='https://api.example/callback',issuer=f'https://auth.example/{slug}',
            client_id=f'client-{slug}',authorization_endpoint=f'https://auth.example/{slug}/authorize',token_endpoint=f'https://auth.example/{slug}/token',
            registration_endpoint=f'https://auth.example/{slug}/register',token_endpoint_auth_method='none')
        payload={'access_token':f'access-{slug}','refresh_token':f'refresh-{slug}','token_type':'Bearer',
            'expires_at':'2099-01-01T00:00:00+00:00','server_id':str(server.id),'server_url':endpoint,'resource':endpoint,'registration_id':str(registration.id)}
        grant_service=service
        if MISMATCH and index==0:
            grant_service=Service.objects.create(name='Unrelated',slug='unrelated-mcp',organization_id=org.id,credential_type='mcp_oauth',plugin_owned=True,oauth_plugin_slug='foreign',oauth_requirement_key='foreign_auth')
        credential=Credential.objects.create(user_id=user.id,service_id=grant_service.id,name=slug,encrypted_value=encrypt_value(json.dumps(payload)),
            created_by_id=user.id,oauth_server_id=server.id,oauth_server_url=endpoint,oauth_resource=endpoint,oauth_registration_id=registration.id,oauth_status='connected')
        workspace.credentials.add(credential)
        records.append((str(credential.id),str(server.id),endpoint,str(requirement.id),str(grant_service.id)))
    print(json.dumps({'records':records,'service':str(service.id),'workspace':str(workspace.id)}))
"""
    script = (
        script.replace("MULTIPLE", repr(multiple_endpoints))
        .replace("AMBIGUOUS", repr(ambiguous_requirement))
        .replace("MISMATCH", repr(mismatch_service))
        .replace("LONG_SLUG", repr(long_slug))
    )
    result = subprocess.run(
        [sys.executable, "manage.py", "shell", "-c", script],
        cwd=BACKEND_ROOT,
        env={**os.environ, "SQLITE_PATH": str(db_path), **(env_overrides or {})},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    line = next(
        line for line in reversed(result.stdout.splitlines()) if line.startswith("{")
    )
    return json.loads(line)


@pytest.mark.django_db(transaction=True)
def test_deferred_constraint_flush_is_postgresql_only():
    """Constraint flushing only issues SQL on PostgreSQL."""
    from importlib import import_module

    flush_credentials = import_module(
        "apps.credentials.migrations.0008_plugin_credential_separation"
    ).flush_deferred_constraints
    flush_plugins = import_module(
        "apps.plugins.migrations.0006_mcp_oauth"
    ).flush_deferred_constraints
    sql_calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def execute(self, sql):
            sql_calls.append(sql)

    class Connection:
        def __init__(self, vendor):
            self.vendor = vendor

        def cursor(self):
            if self.vendor != "postgresql":
                raise AssertionError("Non-PostgreSQL migration issued constraint SQL")
            return Cursor()

    class Editor:
        def __init__(self, vendor):
            self.connection = Connection(vendor)

    for flush in (flush_credentials, flush_plugins):
        flush(None, Editor("sqlite"))
        flush(None, Editor("postgresql"))
    assert sql_calls == ["SET CONSTRAINTS ALL IMMEDIATE"] * 2


@pytest.mark.django_db(transaction=True)
def test_deferred_constraint_flush_releases_postgresql_trigger_events():
    """On PostgreSQL, flush FK events before attempting same-transaction DDL."""
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL deferred-trigger behavior")

    transaction.set_autocommit(False)
    try:
        _test_flush_postgres_deferred_constraint_events()
    finally:
        transaction.rollback()
        transaction.set_autocommit(True)


def _test_flush_postgres_deferred_constraint_events():
    from importlib import import_module

    flush_constraints = import_module(
        "apps.credentials.migrations.0008_plugin_credential_separation"
    ).flush_deferred_constraints

    class Editor:
        def __init__(self):
            self.connection = connection

    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMPORARY TABLE migration_fk_parent (id integer PRIMARY KEY) "
            "ON COMMIT DROP"
        )
        cursor.execute(
            "CREATE TEMPORARY TABLE migration_fk_child ("
            "parent_id integer REFERENCES migration_fk_parent(id) "
            "DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP"
        )
        cursor.execute("INSERT INTO migration_fk_child (parent_id) VALUES (1)")
        cursor.execute("INSERT INTO migration_fk_parent (id) VALUES (1)")

    flush_constraints(None, Editor())
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE INDEX migration_fk_child_parent_idx "
            "ON migration_fk_child (parent_id)"
        )


@pytest.mark.parametrize("multiple_endpoints", [False, True])
def test_forward_migration_preserves_grants_and_splits_shared_endpoints(
    tmp_path, multiple_endpoints
):
    db_path = tmp_path / "oauth.sqlite3"
    _manage(db_path, "migrate", "plugins", "0007_repair_notion_mcp_oauth", "--noinput")
    seed_info = _seed_legacy_grants(db_path, multiple_endpoints=multiple_endpoints)
    _manage(db_path, "migrate", "--noinput")
    _manage(db_path, "migrate", "--noinput")

    script = (
        r"""
import json
from apps.credentials.models import Credential, CredentialService, McpOAuthAuthorizationState
from apps.plugins.models import PluginCredentialRequirement
from apps.runners.models import Workspace
from common.utils import decrypt_value

if MULTIPLE:
    records = RECORDS
    output=[]
    for credential_id, server_id, endpoint, requirement_id, original_service_id in records:
        credential=Credential.objects.get(pk=credential_id)
        data=json.loads(decrypt_value(credential.encrypted_value))
        req=PluginCredentialRequirement.objects.get(pk=requirement_id)
        output.append({'credential_id':str(credential.id), 'service_id':str(credential.service_id),
            'endpoint':credential.service.oauth_server_url, 'saved_endpoint':credential.oauth_server_url,
            'token_service_id':data.get('service_id'), 'server_id_present':'server_id' in data, 'original_service':ORIGINAL_SERVICE_ID,
            'token':data.get('access_token'), 'requirement_service':str(req.credential_service_id),
            'workspace_linked':Workspace.objects.filter(pk=WORKSPACE_ID, credentials=credential).exists(), 'original_service':original_service_id})
    print(json.dumps(output))
else:
    credential=Credential.objects.get(name='legacy account')
    data=json.loads(decrypt_value(credential.encrypted_value))
    service=CredentialService.objects.get(pk=credential.service_id)
    print(json.dumps({'id':str(credential.id), 'service':str(service.id), 'slug':service.slug,
        'endpoint':service.oauth_server_url, 'saved_endpoint':credential.oauth_server_url,
        'token_service_id':data.get('service_id'), 'server_id_present':'server_id' in data, 'original_service':ORIGINAL_SERVICE_ID,
        'token':data.get('access_token'), 'workspace_linked':Workspace.objects.filter(
            pk=WORKSPACE_ID, credentials=credential).exists(),
        'invalidated':McpOAuthAuthorizationState.objects.filter(consumed_at__isnull=False).exists()}))
""".replace("MULTIPLE", repr(multiple_endpoints))
        .replace("RECORDS", repr(seed_info["records"]))
        .replace("WORKSPACE_ID", repr(seed_info["workspace"]))
        .replace("ORIGINAL_SERVICE_ID", repr(seed_info["service"]))
    )
    result = subprocess.run(
        [sys.executable, "manage.py", "shell", "-c", script],
        cwd=BACKEND_ROOT,
        env={**os.environ, "SQLITE_PATH": str(db_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    line = next(
        line
        for line in reversed(result.stdout.splitlines())
        if line.startswith("{") or line.startswith("[")
    )
    data = json.loads(line)
    if multiple_endpoints:
        assert len(data) == 2
        assert {item["token"] for item in data} == {"access-first", "access-second"}
        assert len({item["service_id"] for item in data}) == 2
        assert all(item["service_id"] == item["requirement_service"] for item in data)
        assert all(item["token_service_id"] == item["service_id"] for item in data)
        assert sum(item["service_id"] == item["original_service"] for item in data) == 1
        assert all(
            not item["server_id_present"] and item["workspace_linked"] for item in data
        )
    else:
        assert data["endpoint"] == "https://mcp.notion.com/mcp"
        assert data["saved_endpoint"] == data["endpoint"]
        assert data["slug"] == "notion-oauth"
        assert data["token"] == "legacy-access"
        assert data["token_service_id"] == data["service"]
        assert not data["server_id_present"]
        assert data["workspace_linked"] and data["invalidated"]


@pytest.mark.parametrize(
    ("ambiguous_requirement", "mismatch_service", "long_slug"),
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_forward_migration_fails_closed_for_ambiguous_requirement(
    tmp_path, ambiguous_requirement, mismatch_service, long_slug
):
    db_path = tmp_path / "oauth-safety.sqlite3"
    _manage(db_path, "migrate", "plugins", "0007_repair_notion_mcp_oauth", "--noinput")
    seed = _seed_legacy_grants(
        db_path,
        multiple_endpoints=True,
        ambiguous_requirement=ambiguous_requirement,
        mismatch_service=mismatch_service,
        long_slug=long_slug,
    )
    _manage(db_path, "migrate", "--noinput")
    rows = _migrated_shared_grants(db_path, seed["records"])
    if ambiguous_requirement:
        assert all(
            row["status"] == "reconnect_required" and not row["token"] for row in rows
        )
        assert len({row["requirement_service"] for row in rows}) == 1
        assert all(row["service_url"] == "" for row in rows)
    elif mismatch_service:
        mismatched = next(
            row for row in rows if row["original_service"] != seed["service"]
        )
        assert mismatched["status"] == "reconnect_required"
        assert not mismatched["token"]
        assert mismatched["service"] == mismatched["original_service"]
    else:
        assert all(row["slug_length"] <= 255 for row in rows)
        assert len({row["service"] for row in rows}) == 2


def test_forward_migration_never_adopts_unrelated_service_credential(tmp_path):
    db_path = tmp_path / "oauth-mismatch.sqlite3"
    _manage(db_path, "migrate", "plugins", "0007_repair_notion_mcp_oauth", "--noinput")
    seed = _seed_legacy_grants(db_path, multiple_endpoints=True, mismatch_service=True)
    _manage(db_path, "migrate", "--noinput")
    rows = _migrated_shared_grants(db_path, seed["records"])
    mismatched = next(row for row in rows if row["original_service"] != seed["service"])
    assert mismatched["status"] == "reconnect_required"
    assert not mismatched["token"]
    assert mismatched["service"] == mismatched["original_service"]


def test_forward_migration_split_keeps_long_slugs_within_field_limit(tmp_path):
    db_path = tmp_path / "oauth-long-slug.sqlite3"
    _manage(db_path, "migrate", "plugins", "0007_repair_notion_mcp_oauth", "--noinput")
    seed = _seed_legacy_grants(db_path, multiple_endpoints=True, long_slug=True)
    _manage(db_path, "migrate", "--noinput")
    rows = _migrated_shared_grants(db_path, seed["records"])
    assert all(row["slug_length"] <= 255 for row in rows)
    assert len({row["service"] for row in rows}) == 2


@pytest.mark.parametrize("stop_at_0009", [False, True])
def test_registration_fingerprint_field_exists_once_and_migration_is_idempotent(
    tmp_path, stop_at_0009
):
    db_path = tmp_path / f"fingerprint-{stop_at_0009}.sqlite3"
    if stop_at_0009:
        _manage(
            db_path,
            "migrate",
            "credentials",
            "0009_organization_service_help",
            "--noinput",
        )
        assert _registration_fingerprint_column_count(db_path) == 1

    _manage(db_path, "migrate", "--noinput")
    assert _registration_fingerprint_column_count(db_path) == 1
    _manage(db_path, "migrate", "--noinput")
    assert _registration_fingerprint_column_count(db_path) == 1

    with sqlite3.connect(db_path) as connection:
        applied = set(
            connection.execute(
                "SELECT name FROM django_migrations WHERE app = 'credentials'"
            )
        )
    assert ("0008_plugin_credential_separation",) in applied
    assert ("0009_organization_service_help",) in applied
    assert ("0010_oauth_registration_fingerprint",) in applied


def test_wrong_encryption_key_aborts_migration_without_modifying_grants(tmp_path):
    db_path = tmp_path / "wrong-encryption-key.sqlite3"
    correct_key = Fernet.generate_key().decode()
    wrong_key = Fernet.generate_key().decode()
    env = {"CREDENTIAL_ENCRYPTION_KEY": correct_key}
    _manage(
        db_path,
        "migrate",
        "plugins",
        "0007_repair_notion_mcp_oauth",
        "--noinput",
        env_overrides=env,
    )
    seed = _seed_legacy_grants(
        db_path, multiple_endpoints=False, env_overrides=env
    )
    with sqlite3.connect(db_path) as connection:
        credential_before = connection.execute(
            "SELECT service_id, encrypted_value, oauth_status "
            "FROM credentials_credential WHERE name = 'legacy account'"
        ).fetchone()
        requirement_before = connection.execute(
            "SELECT credential_service_id FROM plugins_credential_requirement "
            "WHERE id = ?",
            (seed["requirement"].replace("-", ""),),
        ).fetchone()

    failed = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=BACKEND_ROOT,
        env={
            **os.environ,
            "SQLITE_PATH": str(db_path),
            "CREDENTIAL_ENCRYPTION_KEY": wrong_key,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode != 0

    with sqlite3.connect(db_path) as connection:
        credential_after = connection.execute(
            "SELECT service_id, encrypted_value, oauth_status "
            "FROM credentials_credential WHERE name = 'legacy account'"
        ).fetchone()
        requirement_after = connection.execute(
            "SELECT credential_service_id FROM plugins_credential_requirement "
            "WHERE id = ?",
            (seed["requirement"].replace("-", ""),),
        ).fetchone()
        applied = set(
            connection.execute(
                "SELECT name FROM django_migrations WHERE app = 'credentials'"
            )
        )
        columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info("credentials_mcp_oauth_state")'
            )
        }
    assert credential_after == credential_before
    assert requirement_after == requirement_before
    assert ("0008_plugin_credential_separation",) not in applied
    assert "registration_fingerprint" not in columns


def _migrated_shared_grants(db_path: Path, records: list) -> list[dict]:
    script = r"""
import json
from apps.credentials.models import Credential
from apps.plugins.models import PluginCredentialRequirement
from common.utils import decrypt_value
rows=[]
for cid, sid, endpoint, rid, original_service_id in RECORDS:
    credential=Credential.objects.get(pk=cid)
    try:
        token=json.loads(decrypt_value(credential.encrypted_value)) if credential.encrypted_value else {}
    except Exception:
        token={}
    requirement=PluginCredentialRequirement.objects.get(pk=rid)
    rows.append({"service":str(credential.service_id), "original_service":original_service_id,
        "status":credential.oauth_status, "token":token.get("access_token", ""),
        "token_service":token.get("service_id"), "server_id_present":"server_id" in token,
        "requirement_service":str(requirement.credential_service_id),
        "slug_length":len(credential.service.slug), "service_url":credential.service.oauth_server_url})
print(json.dumps(rows))
""".replace("RECORDS", repr(records))
    result = subprocess.run(
        [sys.executable, "manage.py", "shell", "-c", script],
        cwd=BACKEND_ROOT,
        env={**os.environ, "SQLITE_PATH": str(db_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(
        next(
            line
            for line in reversed(result.stdout.splitlines())
            if line.startswith("[")
        )
    )


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
