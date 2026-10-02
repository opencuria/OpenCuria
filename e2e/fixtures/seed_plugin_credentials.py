"""Create own-row fixtures in an isolated, migrated E2E SQLite database."""

import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

import structlog
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.credentials.models import (
    Credential,
    CredentialService,
    McpOAuthClientRegistration,
    OrgCredentialServiceActivation,
)
from apps.organizations.models import Membership, Organization
from apps.plugins.models import OrgPluginActivation, Plugin
from apps.runners.enums import RunnerStatus, RuntimeType, WorkspaceStatus
from apps.runners.models import (
    ImageBuildJob,
    ImageDefinition,
    ImageInstance,
    Runner,
    Workspace,
)
from common.utils import encrypt_value

marker = os.environ["E2E_RUN_ID"]
state_path = Path(os.environ["E2E_FIXTURE_STATE"])
db_path = Path(os.environ["SQLITE_PATH"]).resolve()
if not (
    str(db_path).endswith("/e2e/test-results/focused-db/plugin-e2e.sqlite3")
    or str(db_path).startswith("/workspace/.opencuria/")
):
    raise RuntimeError("Focused E2E fixture refuses to modify a non-test database")
organization, organization_created = Organization.objects.get_or_create(
    slug=f"focused-{marker}", defaults={"name": f"Focused E2E {marker}"}
)
User = get_user_model()
admin_email = f"{marker}-admin@localhost.test"
member_email = f"{marker}-member@localhost.test"
password = "FocusedE2E-Only-2026!"
admin_password = password
admin = User.objects.create_user(
    email=admin_email, password=password, is_staff=False, is_superuser=False
)
member = User.objects.create_user(
    email=member_email, password=password, is_staff=False, is_superuser=False
)
Membership.objects.create(user=admin, organization=organization, role="admin")
Membership.objects.create(user=member, organization=organization, role="member")
admin_membership_created = True
service = CredentialService.objects.filter(
    slug="notion-oauth", organization__isnull=True
).first()
service_created = service is None
if service is None:
    service = CredentialService.objects.create(
        name="Notion OAuth",
        slug="notion-oauth",
        description="Connect to the Notion MCP server.",
        credential_type="mcp_oauth",
        oauth_server_url="https://mcp.notion.com/mcp",
    )

notion_activation, notion_activation_created = (
    OrgCredentialServiceActivation.objects.get_or_create(
        organization=organization, credential_service=service
    )
)
github_service = CredentialService.objects.filter(
    slug="github", organization__isnull=True
).first()
github_activation = None
github_activation_created = False
if github_service is not None:
    github_activation, github_activation_created = (
        OrgCredentialServiceActivation.objects.get_or_create(
            organization=organization, credential_service=github_service
        )
    )
callback_url = os.getenv(
    "MCP_OAUTH_CALLBACK_URL",
    "http://127.0.0.1:8000/api/v1/mcp-oauth/callback/",
)
registration = McpOAuthClientRegistration.objects.create(
    server_url=service.oauth_server_url,
    callback_url=callback_url,
    issuer="https://issuer.focused-e2e.invalid",
    client_id="focused-e2e-client",
    authorization_endpoint="https://issuer.focused-e2e.invalid/authorize",
    token_endpoint="https://issuer.focused-e2e.invalid/token",
    token_endpoint_auth_method="none",
    scopes_supported=["mcp"],
)


def create_grant(name: str, *, organization_scope: bool = False) -> Credential:
    """Seed valid encrypted non-provider test material for read/selection tests."""
    grant = {
        "access_token": f"test-only-{uuid.uuid4()}",
        "refresh_token": "",
        "token_type": "Bearer",
        "expires_at": (timezone.now() + timedelta(hours=4)).isoformat(),
        "scope": "mcp",
        "service_id": str(service.id),
        "server_url": service.oauth_server_url,
        "resource": service.oauth_server_url,
        "registration_id": str(registration.id),
        "identity": {},
    }
    return Credential.objects.create(
        user=None if organization_scope else admin,
        organization=organization if organization_scope else None,
        service=service,
        name=name,
        encrypted_value=encrypt_value(json.dumps(grant)),
        oauth_server_url=service.oauth_server_url,
        oauth_resource=service.oauth_server_url,
        oauth_status="connected",
        oauth_registration=registration,
        created_by=admin,
    )


personal = create_grant(f"{marker} Notion Personal")
shared = create_grant(f"{marker} Notion Organization", organization_scope=True)
runner = Runner.objects.create(
    name=f"{marker} Offline Fixture Runner",
    api_token_hash=f"{uuid.uuid4().hex:0<64}"[:64],
    organization=organization,
    status=RunnerStatus.ONLINE,
    available_runtimes=[RuntimeType.QEMU],
)
image_definition = ImageDefinition.objects.create(
    organization=organization,
    created_by=admin,
    name=f"{marker} Fixture Image Definition",
    runtime_type=RuntimeType.QEMU,
    status=ImageDefinition.Status.ACTIVE,
)
build = ImageBuildJob.objects.create(
    image_definition=image_definition,
    runner=runner,
    status=ImageBuildJob.Status.ACTIVE,
)
image = ImageInstance.objects.create(
    runner=runner,
    build_job=build,
    runtime_type=RuntimeType.QEMU,
    origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
    origin_definition=image_definition,
    name=f"{marker} Fixture Image",
    runner_ref="/test/fixture.qcow2",
    status=ImageInstance.Status.READY,
    created_by=admin,
)
workspace = Workspace.objects.create(
    runner=runner,
    runtime_type=RuntimeType.QEMU,
    status=WorkspaceStatus.STOPPED,
    name=f"{marker} Workspace",
    qemu_vcpus=1,
    qemu_memory_mb=1024,
    qemu_disk_size_gb=20,
    base_image_instance=image,
    created_by=admin,
)
notion = Plugin.objects.filter(slug="notion", organization__isnull=True).first()
plugin_created = notion is None
if notion is None:
    notion = Plugin.objects.create(
        name="Notion",
        slug="notion",
        description="Use Notion through MCP.",
        enabled=True,
        published=True,
    )
    from apps.plugins.models import PluginCredentialRequirement, PluginMcpServer

    PluginMcpServer.objects.create(
        plugin=notion,
        name="Notion MCP",
        slug="notion",
        transport="streamable_http",
        url=service.oauth_server_url,
        auth_type="oauth",
        oauth_requirement_key="notion_oauth",
    )
    PluginCredentialRequirement.objects.create(
        plugin=notion,
        key="notion_oauth",
        required=True,
        description="Authorize Notion.",
        credential_service=service,
    )
notion_originally_enabled = OrgPluginActivation.objects.filter(
    organization=organization, plugin=notion
).exists()
notion_activation_row, notion_activation_created = (
    OrgPluginActivation.objects.get_or_create(
        organization=organization, plugin=notion, defaults={"enabled_by": admin}
    )
)
state = {
    "marker": marker,
    "organizationCreated": organization_created,
    "organizationId": str(organization.id),
    "adminEmail": admin_email,
    "adminUserId": admin.id,
    "memberEmail": member_email,
    "memberUserId": member.id,
    "password": admin_password,
    "memberPassword": password,
    "notionServiceId": str(service.id),
    "githubServiceId": str(github_service.id) if github_service else None,
    "githubActivationId": str(github_activation.id) if github_activation else None,
    "githubActivationCreated": github_activation_created,
    "serviceCreated": service_created,
    "notionPluginId": str(notion.id),
    "pluginCreated": plugin_created,
    "oauthCredentials": [str(personal.id), str(shared.id)],
    "oauthNames": [personal.name, shared.name],
    "workspaceId": str(workspace.id),
    "imageArtifactId": str(image.id),
    "imageDefinitionId": str(image_definition.id),
    "imageBuildId": str(build.id),
    "runnerId": str(runner.id),
    "workspaceName": workspace.name,
    "testRegistrationId": str(registration.id),
    "createdServices": [],
    "createdPlugins": [],
    "createdCredentials": [],
    "notionOriginalEnabled": notion_originally_enabled,
    "adminMembershipCreated": admin_membership_created,
    "notionServiceActivationCreated": notion_activation_created,
    "notionPluginActivationCreated": notion_activation_created,
    "createdCredentialServiceActivations": [
        str(notion_activation_row.id) if notion_activation_created else None
    ],
}
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(state, indent=2))
state_path.chmod(0o600)
structlog.get_logger(__name__).info("Focused E2E fixture created", marker=marker)
