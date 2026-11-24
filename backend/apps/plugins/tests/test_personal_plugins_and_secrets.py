"""Personal plugins, plugin credentials and plugin skills.

Covers plugin scopes and their permissions over REST and the service,
workspace availability of personal plugins, the single credential link of
a plugin (credential service plus optional credential, values stored as
credentials and never returned), stdio command lines, SKILL.md
parsing/rendering, stable skill IDs and the selection of usable skills.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.credentials.models import (
    Credential,
    CredentialService,
    OrgCredentialServiceActivation,
)
from apps.credentials.services import CredentialServiceSvc, CredentialSvc
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.plugins import runtime as plugin_runtime
from apps.plugins.models import (
    OrgPluginActivation,
    Plugin,
    PluginMcpServer,
    PluginSkill,
    WorkspacePluginActivation,
)
from apps.plugins.services import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    PluginPermissionError,
    PluginService,
    normalize_mcp_payload,
    normalize_skill_payload,
)
from apps.plugins.skill_documents import (
    parse_skill_markdown,
    render_skill_markdown,
)
from apps.plugins.skills import PluginSkillService
from apps.runners.models import Runner, Workspace
from apps.runners.services.workspace_configuration import (
    WorkspaceConfigurationService,
)
from common.exceptions import ConflictError, NotFoundError
from common.utils import decrypt_value, generate_api_token, hash_token

PLUGINS = "/api/v1/plugins/"

DEFAULT_PERMISSIONS = [
    APIKeyPermission.PLUGINS_READ.value,
    APIKeyPermission.PLUGINS_WRITE.value,
    APIKeyPermission.SKILLS_READ.value,
    APIKeyPermission.WORKSPACES_UPDATE.value,
    APIKeyPermission.CREDENTIALS_READ.value,
]


@dataclass
class Caller:
    """A user with an API key for one organization."""

    user: Any
    org: Organization
    headers: dict[str, str]

    def get(self, client: Client, path: str):
        return client.get(path, **self.headers)

    def post(self, client: Client, path: str, body: dict):
        return client.post(
            path, data=json.dumps(body), content_type="application/json", **self.headers
        )

    def patch(self, client: Client, path: str, body: dict):
        return client.patch(
            path, data=json.dumps(body), content_type="application/json", **self.headers
        )

    def delete(self, client: Client, path: str):
        return client.delete(path, **self.headers)


def _org(name: str = "Org") -> Organization:
    return Organization.objects.create(
        name=f"{name} {uuid.uuid4().hex[:6]}", slug=f"org-{uuid.uuid4().hex[:10]}"
    )


def _caller(
    org: Organization,
    *,
    role: str = MembershipRole.MEMBER,
    user: Any = None,
    permissions: list[str] | None = None,
) -> Caller:
    if user is None:
        user = get_user_model().objects.create_user(
            email=f"user-{uuid.uuid4().hex[:8]}@example.com", password="secret"
        )
    Membership.objects.create(user=user, organization=org, role=role)
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="personal-plugin-test",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=DEFAULT_PERMISSIONS if permissions is None else permissions,
    )
    return Caller(
        user=user,
        org=org,
        headers={"HTTP_X_API_KEY": token, "HTTP_X_ORGANIZATION_ID": str(org.id)},
    )


def _workspace(caller: Caller) -> Workspace:
    runner = Runner.objects.create(
        name=f"runner-{uuid.uuid4().hex[:6]}",
        api_token_hash=hash_token(uuid.uuid4().hex),
        organization=caller.org,
    )
    return Workspace.objects.create(
        runner=runner, name=f"ws-{uuid.uuid4().hex[:6]}", created_by=caller.user
    )


def _body(name: str = "Acme", **extra: Any) -> dict:
    return {
        "name": name,
        "skills": [{"name": "Guide", "body": "# Guide\n\nUse the v2 API."}],
        "mcp_servers": [{"name": "Tools", "command": "npx -y @acme/mcp"}],
        **extra,
    }


def _create(service_user: Any, org: Organization, **values: Any) -> dict:
    values.setdefault("name", "Personal")
    return PluginService().create_plugin(org_id=org.id, user=service_user, **values)


@pytest.fixture
def client() -> Client:
    return Client()


# ---------------------------------------------------------------------------
# Scopes and permissions
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_member_manages_own_personal_plugin_via_rest(client: Client):
    member = _caller(_org())

    created = member.post(client, PLUGINS, _body())
    assert created.status_code == 201, created.content[:500]
    plugin = created.json()
    assert plugin["scope"] == "personal"
    assert plugin["organization_id"] is None
    assert plugin["is_global"] is False
    assert (plugin["available"], plugin["editable"]) == (True, True)
    assert plugin["org_enabled"] is False
    assert plugin["skills"][0]["description"] == "Guide"
    assert Plugin.objects.get(id=plugin["id"]).owner_id == member.user.id
    skill_id = plugin["skills"][0]["id"]
    path = f"{PLUGINS}{plugin['id']}/"

    renamed = member.patch(
        client,
        path,
        {
            "name": "Acme v2",
            "skills": [{"id": skill_id, "name": "Handbook", "body": "Read it."}],
        },
    )
    assert renamed.status_code == 200, renamed.content[:500]
    assert renamed.json()["name"] == "Acme v2"
    skill = renamed.json()["skills"][0]
    assert (skill["id"], skill["name"], skill["slug"]) == (
        skill_id,
        "Handbook",
        "handbook",
    )

    listed = {row["id"]: row for row in member.get(client, PLUGINS).json()}
    # Owners see their personal plugins in full.
    assert listed[plugin["id"]]["mcp_servers"][0]["command"] == "npx"

    assert member.delete(client, path).status_code == 204
    assert not Plugin.objects.filter(id=plugin["id"]).exists()


@pytest.mark.django_db
def test_personal_plugin_is_private_to_its_owner(client: Client):
    org = _org()
    owner = _caller(org)
    plugin = owner.post(client, PLUGINS, _body()).json()
    path = f"{PLUGINS}{plugin['id']}/"

    for other in (_caller(org), _caller(org, role=MembershipRole.ADMIN)):
        listed = other.get(client, PLUGINS).json()
        assert plugin["id"] not in {row["id"] for row in listed}
        assert other.patch(client, path, {"name": "Taken"}).status_code == 404
        assert other.delete(client, path).status_code == 404
        skills = other.get(client, "/api/v1/skills/").json()
        assert plugin["skills"][0]["id"] not in {row["id"] for row in skills}
    assert Plugin.objects.get(id=plugin["id"]).name == "Acme"

    # Personal plugins follow their owner into every organization.
    elsewhere = _caller(_org("Second"), user=owner.user)
    assert plugin["id"] in {row["id"] for row in elsewhere.get(client, PLUGINS).json()}


@pytest.mark.django_db
def test_admin_cannot_change_another_users_personal_plugin():
    org = _org()
    owner = _caller(org)
    admin = _caller(org, role=MembershipRole.ADMIN)
    plugin = _create(owner.user, org)
    service = PluginService()
    model = Plugin.objects.get(id=plugin["id"])

    assert service.can_edit(model, user_id=admin.user.id, admin=True) is False
    assert service.can_edit(model, user_id=owner.user.id, admin=False) is True
    with pytest.raises(NotFoundError):
        service.update_plugin(
            plugin["id"], org_id=org.id, user=admin.user, admin=True, name="Mine"
        )
    with pytest.raises(NotFoundError):
        service.delete_plugin(plugin["id"], org_id=org.id, user=admin.user, admin=True)
    assert Plugin.objects.get(id=plugin["id"]).name == "Personal"


@pytest.mark.django_db
def test_members_cannot_create_or_change_organization_plugins(client: Client):
    org = _org()
    member = _caller(org)
    admin = _caller(org, role=MembershipRole.ADMIN)

    denied = member.post(client, PLUGINS, _body(scope="organization"))
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"
    assert not Plugin.objects.filter(organization=org).exists()
    with pytest.raises(PluginPermissionError):
        _create(member.user, org, scope="organization", admin=False)
    with pytest.raises(ValueError, match="scope"):
        _create(admin.user, org, scope="global", admin=True)

    shared = admin.post(client, PLUGINS, _body("Team", scope="organization")).json()
    assert (shared["scope"], shared["editable"]) == ("organization", True)
    path = f"{PLUGINS}{shared['id']}/"
    activation = admin.post(client, f"{path}activation/", {"active": True})
    assert activation.status_code == 200

    seen = next(
        row for row in member.get(client, PLUGINS).json() if row["id"] == shared["id"]
    )
    # Members use released plugins without seeing their server configuration.
    assert seen["editable"] is False
    assert seen["mcp_servers"][0]["command"] == ""
    for response in (
        member.patch(client, path, {"name": "Mine"}),
        member.delete(client, path),
    ):
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"
    member_release = member.post(client, f"{path}activation/", {"active": False})
    assert member_release.status_code == 403
    assert Plugin.objects.get(id=shared["id"]).name == "Team"


@pytest.mark.django_db
def test_personal_plugins_cannot_be_released_for_the_organization(client: Client):
    admin = _caller(_org(), role=MembershipRole.ADMIN)
    plugin = admin.post(client, PLUGINS, _body()).json()

    response = admin.post(
        client, f"{PLUGINS}{plugin['id']}/activation/", {"active": True}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "personal_plugin"
    assert not OrgPluginActivation.objects.filter(plugin_id=plugin["id"]).exists()
    with pytest.raises(ConflictError) as error:
        PluginService().set_org_activation(
            plugin["id"], org_id=admin.org.id, user=admin.user, active=False
        )
    assert error.value.code == "personal_plugin"


# ---------------------------------------------------------------------------
# Workspaces and runtime
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_personal_plugin_runs_in_owner_workspace_without_release(client: Client):
    org = _org()
    owner = _caller(org)
    plugin = owner.post(
        client,
        PLUGINS,
        _body(
            skills=[
                {
                    "name": "Guide",
                    "description": 'Use for "Acme" API calls.',
                    "body": "# Guide\n\nUse the v2 API.",
                }
            ]
        ),
    ).json()
    workspace = _workspace(owner)

    enabled = owner.patch(
        client, f"/api/v1/workspaces/{workspace.id}/", {"plugin_ids": [plugin["id"]]}
    )

    assert enabled.status_code == 200, enabled.content[:500]
    assert enabled.json()["plugin_ids"] == [plugin["id"]]
    rows = PluginService().list_workspace_plugins(workspace=workspace, org_id=org.id)
    row = next(row for row in rows if str(row["id"]) == plugin["id"])
    assert (row["scope"], row["workspace_enabled"], row["ready"]) == (
        "personal",
        True,
        True,
    )
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=workspace, org_id=org.id
    )
    assert [(str(p.id), p.scope) for p in snapshot.plugins] == [
        (plugin["id"], "personal")
    ]
    assert snapshot.plugin_skills == [
        "---\n"
        "name: guide\n"
        'description: "Use for \\"Acme\\" API calls."\n'
        "---\n\n"
        "# Guide\n\nUse the v2 API.\n"
    ]
    assert [entry.identifier for entry in snapshot.skill_entries] == ["guide"]


@pytest.mark.django_db
def test_personal_plugin_is_not_available_in_other_users_workspaces(client: Client):
    org = _org()
    owner = _caller(org)
    other = _caller(org)
    plugin = owner.post(client, PLUGINS, _body()).json()
    workspace = _workspace(other)

    response = other.patch(
        client, f"/api/v1/workspaces/{workspace.id}/", {"plugin_ids": [plugin["id"]]}
    )

    assert response.status_code == 404, response.content[:500]
    assert not workspace.plugin_activations.exists()
    # A stale or forged selection row stays ineffective.
    WorkspacePluginActivation.objects.create(
        workspace=workspace, plugin_id=plugin["id"]
    )
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=workspace, org_id=org.id
    )
    assert snapshot.plugins == ()
    rows = PluginService().list_workspace_plugins(workspace=workspace, org_id=org.id)
    assert plugin["id"] not in {str(row["id"]) for row in rows}


@pytest.mark.django_db
def test_personal_plugin_credentials_guard_uses_the_workspace_owner():
    org = _org()
    owner = _caller(org)
    service = CredentialService.objects.create(
        name="Acme token",
        slug=f"acme-{uuid.uuid4().hex[:6]}",
        organization=org,
        credential_type="env",
        env_var_name="ACME_TOKEN",
    )
    OrgCredentialServiceActivation.objects.create(
        organization=org, credential_service=service
    )
    plugin = _create(
        owner.user,
        org,
        mcp_servers=[
            {
                "name": "Tools",
                "command": "npx",
                "env": {"TOKEN": "{{credential.token}}"},
            }
        ],
        credential_requirements=[{"key": "token", "service_id": str(service.id)}],
    )
    credential = CredentialSvc().create_personal_credential(
        service_id=service.id,
        name="Mine",
        value="personal-secret",
        user=owner.user,
        org_id=org.id,
    )
    workspace = _workspace(owner)
    WorkspaceConfigurationService().update(
        workspace_id=workspace.id,
        user=owner.user,
        organization_id=org.id,
        credentials=[credential],
        plugin_ids=[plugin["id"]],
    )

    plugins = PluginService()
    gaps = plugins.credential_removal_gaps(
        credential, workspace_id=workspace.id, org_id=org.id, owner_id=owner.user.id
    )
    assert [(gap["plugin_id"], gap["key"]) for gap in gaps] == [
        (str(plugin["id"]), "token")
    ]
    # Without the owner, personal plugins are not effective anywhere.
    assert (
        plugins.credential_removal_gaps(
            credential, workspace_id=workspace.id, org_id=org.id
        )
        == []
    )
    with pytest.raises(ConflictError) as error:
        CredentialSvc().delete_credential(
            credential.id, org_id=org.id, user=owner.user, is_admin=False
        )
    assert error.value.code == "plugin_credentials_in_use"


@pytest.mark.django_db
def test_workspace_fingerprint_changes_when_a_personal_plugin_changes():
    org = _org()
    owner = _caller(org)
    plugin = _create(owner.user, org, skills=[{"name": "Guide", "body": "One."}])
    workspace = _workspace(owner)
    WorkspacePluginActivation.objects.create(
        workspace=workspace, plugin_id=plugin["id"]
    )
    before = plugin_runtime.workspace_plugin_fingerprint(workspace_id=workspace.id)

    PluginService().update_plugin(
        plugin["id"],
        org_id=org.id,
        user=owner.user,
        skills=[{"name": "Guide", "body": "Two."}],
    )

    after = plugin_runtime.workspace_plugin_fingerprint(workspace_id=workspace.id)
    assert before and after != before


@pytest.mark.django_db
def test_personal_plugin_never_inherits_builtin_guest_overrides():
    org = _org()
    owner = _caller(org)
    plugin = _create(
        owner.user,
        org,
        name="Playwright",
        slug="playwright",
        mcp_servers=[{"name": "Playwright", "slug": "playwright", "command": "npx"}],
    )
    workspace = _workspace(owner)
    WorkspacePluginActivation.objects.create(
        workspace=workspace, plugin_id=plugin["id"]
    )

    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=workspace, org_id=org.id
    )

    assert snapshot.plugins[0].mcp_servers[0].guest_overrides == {}


@pytest.mark.django_db
def test_skill_identifiers_are_prefixed_when_slugs_collide():
    org = _org()
    owner = _caller(org)
    personal = _create(
        owner.user, org, name="Mine", skills=[{"name": "Review", "body": "Mine."}]
    )
    shared = _create(
        owner.user,
        org,
        name="Team",
        scope="organization",
        admin=True,
        skills=[{"name": "Review", "body": "Team."}],
    )
    OrgPluginActivation.objects.create(organization=org, plugin_id=shared["id"])
    workspace = _workspace(owner)
    for plugin in (personal, shared):
        WorkspacePluginActivation.objects.create(
            workspace=workspace, plugin_id=plugin["id"]
        )

    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=workspace, org_id=org.id
    )

    assert sorted(entry.identifier for entry in snapshot.skill_entries) == [
        "mine-review",
        "team-review",
    ]


@pytest.mark.django_db
def test_skill_identifiers_stay_unique_when_plugin_slugs_collide():
    org = _org()
    owner = _caller(org)
    personal = _create(
        owner.user, org, name="Team", skills=[{"name": "Review", "body": "Mine."}]
    )
    shared = _create(
        owner.user,
        org,
        name="Team",
        scope="organization",
        admin=True,
        skills=[{"name": "Review", "body": "Team."}],
    )
    OrgPluginActivation.objects.create(organization=org, plugin_id=shared["id"])
    workspace = _workspace(owner)
    for plugin in (personal, shared):
        WorkspacePluginActivation.objects.create(
            workspace=workspace, plugin_id=plugin["id"]
        )

    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=workspace, org_id=org.id
    )

    identifiers = [entry.identifier for entry in snapshot.skill_entries]
    assert len(identifiers) == len(set(identifiers)) == 2


# ---------------------------------------------------------------------------
# MCP server secrets, command lines and timeouts
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_plugin_credentials_link_a_service_and_optionally_a_credential(client: Client):
    owner = _caller(
        _org(),
        permissions=[*DEFAULT_PERMISSIONS, APIKeyPermission.CREDENTIALS_WRITE.value],
    )
    created = owner.post(
        client,
        PLUGINS,
        _body(
            mcp_servers=[
                {
                    "name": "Tools",
                    "command": "npx -y @acme/mcp",
                    "env": {"MODE": "fast", "ACME_KEY": "{{credential.acme_key}}"},
                }
            ],
            credential_requirements=[
                {
                    "key": "acme_key",
                    "new_service": {"name": "Acme key", "env_var_name": "ACME_KEY"},
                    "value": "sk-live-123",
                }
            ],
        ),
    )
    assert created.status_code == 201, created.content[:500]
    plugin = created.json()
    requirement = plugin["credential_requirements"][0]
    assert requirement["credential_id"]
    assert requirement["credential_scope"] == "personal"
    assert "sk-live-123" not in created.content.decode()
    assert "sk-live-123" not in owner.get(client, PLUGINS).content.decode()

    # One link: a personal service of the owner, a credential of that service.
    service = CredentialService.objects.get(id=requirement["service_id"])
    assert (service.owner_id, service.organization_id) == (owner.user.id, None)
    credential = Credential.objects.get(id=requirement["credential_id"])
    assert credential.service_id == service.id
    assert "sk-live-123" not in credential.encrypted_value
    assert decrypt_value(credential.encrypted_value) == "sk-live-123"

    # The plugin brings its credential: nothing is attached to the workspace.
    workspace = _workspace(owner)
    WorkspacePluginActivation.objects.create(
        workspace=workspace, plugin_id=plugin["id"]
    )
    plugin_id = uuid.UUID(plugin["id"])

    def plaintexts() -> dict[str, str]:
        prepared = plugin_runtime.prepare_workspace_runtime(
            workspace=workspace, org_id=owner.org.id
        )
        assert "sk-live" not in repr(prepared)
        return prepared.plaintexts_for(plugin_id)

    assert plaintexts() == {"acme_key": "sk-live-123"}

    path = f"{PLUGINS}{plugin['id']}/"
    link = {
        "key": "acme_key",
        "service_id": requirement["service_id"],
        "credential_id": requirement["credential_id"],
    }
    kept = owner.patch(client, path, {"credential_requirements": [link]})
    assert kept.status_code == 200, kept.content[:500]
    assert kept.json()["credential_requirements"][0]["credential_id"] == str(
        credential.id
    )
    assert plaintexts() == {"acme_key": "sk-live-123"}

    # A new value replaces the value of the linked credential in place.
    replaced = owner.patch(
        client, path, {"credential_requirements": [{**link, "value": "sk-new"}]}
    )
    assert replaced.status_code == 200, replaced.content[:500]
    assert replaced.json()["credential_requirements"][0]["credential_id"] == str(
        credential.id
    )
    assert plaintexts() == {"acme_key": "sk-new"}

    # Without a credential, each workspace attaches its own.
    per_workspace = owner.patch(
        client, path, {"credential_requirements": [{**link, "credential_id": None}]}
    )
    assert per_workspace.status_code == 200
    assert per_workspace.json()["credential_requirements"][0]["credential_id"] is None
    with pytest.raises(plugin_runtime.PluginCredentialConfigError):
        plaintexts()
    WorkspaceConfigurationService().update(
        workspace_id=workspace.id,
        user=owner.user,
        organization_id=owner.org.id,
        credentials=[credential],
        plugin_ids=[plugin_id],
    )
    assert plaintexts() == {"acme_key": "sk-new"}

    # The credential must belong to the linked service.
    other_service = CredentialServiceSvc().create_secret_service(
        name="Other", env_var_name="OTHER", owner=owner.user
    )
    other = CredentialSvc().create_personal_credential(
        service_id=other_service.id,
        name="Other",
        value="x",
        user=owner.user,
        org_id=owner.org.id,
    )
    mismatch = owner.patch(
        client,
        path,
        {"credential_requirements": [{**link, "credential_id": str(other.id)}]},
    )
    assert mismatch.status_code == 400
    assert "does not belong" in mismatch.json()["detail"]


@pytest.mark.django_db
def test_organization_plugins_share_organization_credentials_only(client: Client):
    org = _org()
    admin = _caller(
        org,
        role=MembershipRole.ADMIN,
        permissions=[*DEFAULT_PERMISSIONS, APIKeyPermission.CREDENTIALS_WRITE.value],
    )
    created = admin.post(
        client,
        PLUGINS,
        _body(
            scope="organization",
            mcp_servers=[
                {
                    "name": "Remote",
                    "transport": "streamable_http",
                    "url": "https://mcp.example.com/mcp",
                    "headers": {"Authorization": "{{credential.authorization}}"},
                }
            ],
            credential_requirements=[
                {
                    "key": "authorization",
                    "new_service": {"name": "Remote token"},
                    "value": "Bearer team-token",
                }
            ],
        ),
    )
    assert created.status_code == 201, created.content[:500]
    requirement = created.json()["credential_requirements"][0]
    assert requirement["credential_scope"] == "organization"
    service = CredentialService.objects.get(id=requirement["service_id"])
    assert (service.owner_id, service.organization_id) == (None, org.id)

    personal = CredentialSvc().create_personal_credential(
        service_id=service.id,
        name="Mine",
        value="Bearer mine",
        user=admin.user,
        org_id=org.id,
    )
    rejected = admin.patch(
        client,
        f"{PLUGINS}{created.json()['id']}/",
        {
            "credential_requirements": [
                {
                    "key": "authorization",
                    "service_id": str(service.id),
                    "credential_id": str(personal.id),
                }
            ]
        },
    )
    assert rejected.status_code == 400
    assert "organization credentials" in rejected.json()["detail"]


@pytest.mark.django_db
def test_storing_credential_values_needs_the_credentials_permission(client: Client):
    member = _caller(_org())
    response = member.post(
        client,
        PLUGINS,
        _body(
            credential_requirements=[
                {"key": "token", "new_service": {"name": "Token"}, "value": "x"}
            ]
        ),
    )
    assert response.status_code == 403
    assert "credentials:write" in response.json()["detail"]
    assert not CredentialService.objects.filter(name="Token").exists()


@pytest.mark.django_db
def test_stdio_command_line_is_split_without_a_shell(client: Client):
    split = normalize_mcp_payload(
        {"name": "Acme", "command": "npx -y @acme/mcp --flag 'a b'"}
    )
    assert (split["command"], split["args"]) == (
        "npx",
        ["-y", "@acme/mcp", "--flag", "a b"],
    )
    plain = normalize_mcp_payload({"name": "Acme", "command": "npx", "args": ["-y"]})
    assert (plain["command"], plain["args"]) == ("npx", ["-y"])
    with pytest.raises(ValueError, match="whole command line"):
        normalize_mcp_payload({"name": "Acme", "command": "npx -y", "args": ["x"]})
    with pytest.raises(ValueError, match="cannot be parsed"):
        normalize_mcp_payload({"name": "Acme", "command": "npx 'unterminated"})

    owner = _caller(_org())
    created = owner.post(
        client,
        PLUGINS,
        _body(
            mcp_servers=[{"name": "Acme", "command": "npx -y @acme/mcp --flag 'a b'"}]
        ),
    )
    assert created.status_code == 201, created.content[:500]
    server = created.json()["mcp_servers"][0]
    assert (server["command"], server["args"]) == (
        "npx",
        ["-y", "@acme/mcp", "--flag", "a b"],
    )
    ambiguous = owner.post(
        client,
        PLUGINS,
        _body(
            "Ambiguous",
            mcp_servers=[{"name": "Acme", "command": "npx -y", "args": ["x"]}],
        ),
    )
    assert ambiguous.status_code == 400


@pytest.mark.django_db
def test_mcp_timeouts_are_internal_defaults(client: Client):
    normalized = normalize_mcp_payload({"name": "Acme", "command": "npx"})
    assert normalized["startup_timeout_seconds"] == DEFAULT_STARTUP_TIMEOUT_SECONDS
    assert normalized["request_timeout_seconds"] == DEFAULT_REQUEST_TIMEOUT_SECONDS
    assert (DEFAULT_STARTUP_TIMEOUT_SECONDS, DEFAULT_REQUEST_TIMEOUT_SECONDS) == (
        60,
        600,
    )
    internal = normalize_mcp_payload(
        {"name": "Acme", "command": "npx", "startup_timeout_seconds": 120}
    )
    assert internal["startup_timeout_seconds"] == 120
    with pytest.raises(ValueError, match="between 1 and 600"):
        normalize_mcp_payload(
            {"name": "Acme", "command": "npx", "request_timeout_seconds": 601}
        )

    owner = _caller(_org())
    created = owner.post(client, PLUGINS, _body())
    server = created.json()["mcp_servers"][0]
    assert not {"startup_timeout_seconds", "request_timeout_seconds"} & set(server)
    stored = PluginMcpServer.objects.get(id=server["id"])
    assert (stored.startup_timeout_seconds, stored.request_timeout_seconds) == (
        DEFAULT_STARTUP_TIMEOUT_SECONDS,
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def test_parse_skill_markdown_reads_frontmatter():
    folded = parse_skill_markdown(
        "---\n"
        "name: release-checklist\n"
        "description: >\n"
        "  Use when cutting a release\n"
        "  of the backend.\n"
        "license: MIT\n"
        "---\n"
        "\n"
        "# Steps\n"
        "1. Tag\n"
    )
    assert folded.name == "release-checklist"
    assert folded.description == "Use when cutting a release of the backend."
    assert folded.body == "# Steps\n1. Tag"

    literal = parse_skill_markdown(
        "---\ndescription: |\n  First line\n  Second line\n---\nBody"
    )
    assert literal.description == "First line\nSecond line"
    quoted = parse_skill_markdown("---\nname: 'Deploy'\ndescription: \"A: b\"\n---\nX")
    assert (quoted.name, quoted.description, quoted.body) == ("Deploy", "A: b", "X")

    plain = parse_skill_markdown("  # Title\n\nNo frontmatter here.  ")
    assert (plain.name, plain.description) == ("", "")
    assert plain.body == "# Title\n\nNo frontmatter here."
    late = parse_skill_markdown("Intro\n---\nname: x\n---\n")
    assert late.name == "" and late.body.startswith("Intro")


def test_render_skill_markdown_quotes_the_description():
    rendered = render_skill_markdown(
        identifier="Release Checklist!", description='Say "hi"\n now', body="Do it.\n"
    )
    assert rendered == (
        '---\nname: release-checklist\ndescription: "Say \\"hi\\" now"\n---\n\nDo it.\n'
    )
    fallback = render_skill_markdown(identifier="x", description="", body="# Hi\nBody")
    assert 'description: "Hi"' in fallback


def test_normalize_skill_payload_uses_and_strips_frontmatter():
    document = "---\nname: Deploy\ndescription: Ships the app.\n---\n\nRun deploy."
    normalized = normalize_skill_payload({"body": document}, 0)
    assert normalized["name"] == "Deploy"
    assert normalized["slug"] == "deploy"
    assert normalized["description"] == "Ships the app."
    assert normalized["body"] == "Run deploy."

    explicit = normalize_skill_payload(
        {"name": "Ship", "description": "Mine.", "body": document}, 3
    )
    assert (explicit["name"], explicit["description"], explicit["position"]) == (
        "Ship",
        "Mine.",
        3,
    )
    derived = normalize_skill_payload(
        {"name": "Docs", "body": "```\n# Heading\n\nProse  line.\n"}, 0
    )
    assert derived["description"] == "Heading"

    for item, message in (
        ({"name": "Long", "description": "x" * 1025, "body": "b"}, "1024"),
        ({"name": "Empty", "body": "---\nname: x\n---\n"}, "must not be empty"),
        ({"body": "No name anywhere."}, "Skill name must not be empty"),
        ({"name": "Typed", "description": 5, "body": "b"}, "must be text"),
    ):
        with pytest.raises(ValueError, match=message):
            normalize_skill_payload(item, 0)


@pytest.mark.django_db
def test_skill_ids_survive_renames_and_reorders():
    org = _org()
    owner = _caller(org)
    plugin = _create(
        owner.user,
        org,
        skills=[{"name": "Alpha", "body": "A."}, {"name": "Beta", "body": "B."}],
    )
    alpha, beta = plugin["skills"]
    service = PluginService()

    updated = service.update_plugin(
        plugin["id"],
        org_id=org.id,
        user=owner.user,
        skills=[
            {"id": str(beta["id"]), "name": "Bee", "body": "B2."},
            {"name": "Alpha", "body": "A2."},
        ],
    )
    assert [(s["id"], s["name"], s["position"]) for s in updated["skills"]] == [
        (beta["id"], "Bee", 0),
        (alpha["id"], "Alpha", 1),
    ]

    other = _create(
        owner.user, org, name="Other", skills=[{"name": "Gamma", "body": "G."}]
    )
    gamma_id = other["skills"][0]["id"]
    replaced = service.update_plugin(
        plugin["id"],
        org_id=org.id,
        user=owner.user,
        skills=[{"id": str(gamma_id), "name": "Delta", "body": "D."}],
    )
    # IDs of another plugin's skills are never adopted.
    assert replaced["skills"][0]["id"] not in {alpha["id"], beta["id"], gamma_id}
    assert PluginSkill.objects.get(id=gamma_id).plugin_id == other["id"]
    assert not PluginSkill.objects.filter(id__in=[alpha["id"], beta["id"]]).exists()


@pytest.mark.django_db
def test_plugin_skill_service_selects_usable_skills():
    org = _org()
    member = _caller(org)
    stranger = _caller(org)
    admin = _caller(org, role=MembershipRole.ADMIN)
    mine = _create(
        member.user,
        org,
        name="Mine",
        skills=[{"name": "Mine", "description": "Personal.", "body": "Mine body."}],
    )

    def shared(name: str, *, released: bool, **fields: Any) -> dict:
        plugin = _create(
            admin.user,
            org,
            name=name,
            scope="organization",
            admin=True,
            skills=[{"name": name, "body": f"{name} body."}],
            **fields,
        )
        if released:
            OrgPluginActivation.objects.create(organization=org, plugin_id=plugin["id"])
        return plugin

    team = shared("Team", released=True)
    draft = shared("Draft", released=False)
    shared("Disabled", released=True, enabled=False)
    _create(stranger.user, org, name="Theirs", skills=[{"name": "Theirs", "body": "T"}])
    builtin = Plugin.objects.create(name="Builtin", slug=f"b-{uuid.uuid4().hex[:6]}")
    PluginSkill.objects.create(plugin=builtin, name="Builtin", slug="builtin", body="B")
    OrgPluginActivation.objects.create(organization=org, plugin=builtin)
    foreign_org = _org("Foreign")
    foreign = shared("Foreign", released=False)
    Plugin.objects.filter(id=foreign["id"]).update(organization=foreign_org)
    OrgPluginActivation.objects.create(
        organization=foreign_org, plugin_id=foreign["id"]
    )
    created = {"Mine", "Team", "Draft", "Disabled", "Theirs", "Builtin", "Foreign"}
    service = PluginSkillService()

    usable = [
        skill
        for skill in service.list_usable(user_id=member.user.id, organization_id=org.id)
        if skill.name in created
    ]
    assert sorted((s.name, s.plugin.scope) for s in usable) == [
        ("Builtin", "builtin"),
        ("Mine", "personal"),
        ("Team", "organization"),
    ]
    mine_id, team_id = str(mine["skills"][0]["id"]), str(team["skills"][0]["id"])
    draft_id = str(draft["skills"][0]["id"])

    bodies = service.resolve_bodies(
        [team_id, mine_id], user_id=member.user.id, organization_id=org.id
    )
    assert bodies == [
        '---\nname: team\ndescription: "Team body."\n---\n\nTeam body.\n',
        '---\nname: mine\ndescription: "Personal."\n---\n\nMine body.\n',
    ]
    with pytest.raises(ValueError, match="not found or not accessible"):
        service.resolve_bodies(
            [draft_id], user_id=member.user.id, organization_id=org.id
        )
    with pytest.raises(ValueError, match="Invalid skill_id"):
        service.resolve_bodies(["junk"], user_id=member.user.id, organization_id=org.id)
    assert service.filter_available_ids(
        [mine_id, draft_id, "junk", mine_id, team_id],
        user_id=member.user.id,
        organization_id=org.id,
    ) == [mine_id, team_id]

    # Personal skills follow their owner; shared ones stay in their org.
    elsewhere = service.list_usable(
        user_id=member.user.id, organization_id=foreign_org.id
    )
    assert {s.name for s in elsewhere} & created == {"Mine", "Foreign"}


@pytest.mark.django_db
def test_skills_endpoint_lists_usable_plugin_skills(client: Client):
    org = _org()
    member = _caller(org)
    admin = _caller(org, role=MembershipRole.ADMIN)
    mine = _create(
        member.user,
        org,
        name="Mine",
        skills=[{"name": "Mine", "description": "Personal.", "body": "Body."}],
    )
    team = _create(
        admin.user,
        org,
        name="Team",
        scope="organization",
        admin=True,
        skills=[{"name": "Team", "body": "Team body."}],
    )
    _create(
        admin.user,
        org,
        name="Draft",
        scope="organization",
        admin=True,
        skills=[{"name": "Draft", "body": "Draft body."}],
    )
    OrgPluginActivation.objects.create(organization=org, plugin_id=team["id"])

    response = member.get(client, "/api/v1/skills/")

    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()}
    assert "Draft" not in rows
    assert rows["Mine"] == {
        "id": str(mine["skills"][0]["id"]),
        "name": "Mine",
        "description": "Personal.",
        "plugin_id": str(mine["id"]),
        "plugin_name": "Mine",
        "scope": "personal",
    }
    assert (rows["Team"]["scope"], rows["Team"]["plugin_name"]) == (
        "organization",
        "Team",
    )
    created = member.post(client, "/api/v1/skills/", {"name": "X", "body": "Y"})
    assert created.status_code == 405
    no_permission = _caller(org, permissions=[APIKeyPermission.PLUGINS_READ.value])
    denied = no_permission.get(client, "/api/v1/skills/")
    assert denied.status_code == 403
    assert denied.json()["code"] == "permission_denied"
    assert "skills:write" not in {permission.value for permission in APIKeyPermission}


@pytest.mark.django_db
def test_members_cannot_probe_unreleased_organization_plugins(client: Client):
    """A plugin hidden from the member list answers 404 to edits, not 403."""
    org = _org()
    admin = _caller(org, role=MembershipRole.ADMIN)
    member = _caller(org)
    hidden = _create(admin.user, org, scope="organization", admin=True)
    path = f"/api/v1/plugins/{hidden['id']}/"
    assert member.patch(client, path, {"name": "Mine"}).status_code == 404
    assert member.delete(client, path).status_code == 404
    PluginService().set_org_activation(
        hidden["id"], org_id=org.id, user=admin.user, active=True
    )
    # Released: visible, but still not the member's to change.
    assert member.patch(client, path, {"name": "Mine"}).status_code == 403
