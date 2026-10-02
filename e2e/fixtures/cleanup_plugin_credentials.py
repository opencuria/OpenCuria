"""Delete only IDs explicitly recorded by this focused E2E run."""

import json
import os
from pathlib import Path

import structlog
from django.contrib.auth import get_user_model

from apps.credentials.models import (
    Credential,
    CredentialService,
    McpOAuthAuthorizationState,
    McpOAuthClientRegistration,
    OrgCredentialServiceActivation,
)
from apps.organizations.models import Membership, Organization
from apps.plugins.models import (
    OrgPluginActivation,
    Plugin,
    PluginCredentialRequirement,
)
from apps.runners.models import (
    ImageBuildJob,
    ImageDefinition,
    ImageInstance,
    Runner,
    Workspace,
)

state_path = Path(os.environ["E2E_FIXTURE_STATE"])
db_path = Path(os.environ["SQLITE_PATH"]).resolve()
if not (
    str(db_path).endswith("/e2e/test-results/focused-db/plugin-e2e.sqlite3")
    or str(db_path).startswith("/workspace/.opencuria/")
):
    raise RuntimeError("Focused E2E teardown refuses to modify a non-test database")
if not state_path.exists():
    raise RuntimeError("Run-owned resource manifest is absent; refusing broad cleanup")
state = json.loads(state_path.read_text())
if not state.get("marker") or not state.get("workspaceId"):
    raise RuntimeError("Focused E2E manifest is invalid")

if state.get("notionPluginActivationCreated"):
    OrgPluginActivation.objects.filter(
        organization_id=state["organizationId"], plugin_id=state["notionPluginId"]
    ).delete()
Credential.objects.filter(id__in=state.get("oauthCredentials", [])).delete()
Credential.objects.filter(id__in=state.get("createdCredentials", [])).delete()
McpOAuthAuthorizationState.objects.filter(
    user__email__in=[state["adminEmail"], state["memberEmail"]]
).delete()
McpOAuthClientRegistration.objects.filter(id=state.get("testRegistrationId")).delete()
Workspace.objects.filter(id=state["workspaceId"]).delete()
Workspace.objects.filter(id__in=state.get("createdWorkspaces", [])).delete()
ImageInstance.objects.filter(id=state["imageArtifactId"]).delete()
ImageBuildJob.objects.filter(id=state["imageBuildId"]).delete()
ImageDefinition.objects.filter(id=state["imageDefinitionId"]).delete()
Runner.objects.filter(id=state["runnerId"], name__startswith=state["marker"]).delete()
User = get_user_model()
member = User.objects.filter(
    id=state["memberUserId"], email=state["memberEmail"]
).first()
if member:
    Membership.objects.filter(
        user=member, organization_id=state["organizationId"]
    ).delete()
    member.delete()
admin = User.objects.filter(
    id=state["adminUserId"], email=state["adminEmail"]
).first()
if admin and state.get("adminMembershipCreated"):
    Membership.objects.filter(
        user=admin, organization_id=state["organizationId"]
    ).delete()
    admin.delete()
for activation_id in state.get("createdCredentialServiceActivations", []):
    if activation_id:
        OrgCredentialServiceActivation.objects.filter(
            id=activation_id, organization_id=state["organizationId"]
        ).delete()
if state.get("githubActivationCreated"):
    OrgCredentialServiceActivation.objects.filter(
        id=state["githubActivationId"], organization_id=state["organizationId"]
    ).delete()
Plugin.objects.filter(id__in=state.get("createdPlugins", [])).delete()
for service_id in state.get("createdServices", []):
    CredentialService.objects.filter(
        id=service_id, organization_id=state["organizationId"]
    ).delete()
if state.get("organizationCreated"):
    PluginCredentialRequirement.objects.filter(
        plugin_id__in=[state["notionPluginId"], *state.get("createdPlugins", [])]
    ).delete()
    Organization.objects.filter(
        id=state["organizationId"], slug=f"focused-{state['marker']}"
    ).delete()
structlog.get_logger(__name__).info(
    "Focused E2E-owned resources removed", marker=state["marker"]
)
