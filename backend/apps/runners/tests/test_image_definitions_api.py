"""
Tests for image definition management API endpoints.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import ImageArtifact, ImageDefinition, Runner, RunnerImageBuild, Workspace
from common.utils import generate_api_token, hash_token


@pytest.fixture
def client() -> Client:
    return Client()


def _auth_headers(token: str, org_id: str) -> dict[str, str]:
    return {
        "HTTP_X_API_KEY": token,
        "HTTP_X_ORGANIZATION_ID": org_id,
    }


def _create_api_key(*, user, permissions: list[str]) -> str:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="test-key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return token


@pytest.mark.django_db
def test_list_includes_global_and_org_image_definitions(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-list@test.com", password="secret")
    org = Organization.objects.create(name="Image Org", slug="image-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    ImageDefinition.objects.create(
        organization=None,
        created_by=admin,
        name="Global Base",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
    )
    local = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Local Base",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_READ.value],
    )

    response = client.get(
        "/api/v1/image-definitions/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    payload = response.json()
    names = {item["name"]: item for item in payload}
    assert names["Global Base"]["is_standard"] is True
    assert names["Global Base"]["organization_id"] is None
    assert names["Local Base"]["is_standard"] is False
    assert names["Local Base"]["organization_id"] == str(local.organization_id)


@pytest.mark.django_db
def test_org_admin_can_duplicate_global_image_definition(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-dup@test.com", password="secret")
    org = Organization.objects.create(name="Dup Org", slug="dup-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    source = ImageDefinition.objects.create(
        organization=None,
        created_by=admin,
        name="Global Python",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
        packages=["python3"],
        env_vars={"HELLO": "world"},
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_WRITE.value],
    )

    response = client.post(
        f"/api/v1/image-definitions/{source.id}/duplicate/",
        data=json.dumps({}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["organization_id"] == str(org.id)
    assert payload["is_standard"] is False
    assert payload["name"] == "Global Python"

    copied = ImageDefinition.objects.get(id=payload["id"])
    assert copied.organization_id == org.id
    assert copied.packages == ["python3"]
    assert copied.env_vars == {"HELLO": "world"}


@pytest.mark.django_db
def test_foreign_org_cannot_list_runner_builds(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-admin@test.com", password="secret")
    outsider = user_model.objects.create_user(email="image-outsider@test.com", password="secret")

    owner_org = Organization.objects.create(name="Owner Org", slug="owner-org")
    outsider_org = Organization.objects.create(name="Outsider Org", slug="outsider-org")
    Membership.objects.create(user=admin, organization=owner_org, role=MembershipRole.ADMIN)
    Membership.objects.create(user=outsider, organization=outsider_org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="owner-runner",
        api_token_hash=hash_token("runner-token"),
        organization=owner_org,
    )
    definition = ImageDefinition.objects.create(
        organization=owner_org,
        created_by=admin,
        name="Owner Definition",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
    )
    RunnerImageBuild.objects.create(
        image_definition=definition,
        runner=runner,
        status=RunnerImageBuild.Status.ACTIVE,
        image_tag="opencuria/custom/owner:1",
    )

    token = _create_api_key(
        user=outsider,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_READ.value],
    )

    response = client.get(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/",
        **_auth_headers(token, str(outsider_org.id)),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Image definition not found"


@pytest.mark.django_db
def test_foreign_org_cannot_update_runner_build(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="build-admin@test.com", password="secret")
    outsider = user_model.objects.create_user(email="build-outsider@test.com", password="secret")

    owner_org = Organization.objects.create(name="Build Owner Org", slug="build-owner-org")
    outsider_org = Organization.objects.create(name="Build Outsider Org", slug="build-outsider-org")
    Membership.objects.create(user=admin, organization=owner_org, role=MembershipRole.ADMIN)
    Membership.objects.create(user=outsider, organization=outsider_org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="owner-runner",
        api_token_hash=hash_token("runner-token-2"),
        organization=owner_org,
    )
    definition = ImageDefinition.objects.create(
        organization=owner_org,
        created_by=admin,
        name="Build Definition",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
    )
    RunnerImageBuild.objects.create(
        image_definition=definition,
        runner=runner,
        status=RunnerImageBuild.Status.ACTIVE,
        image_tag="opencuria/custom/build:1",
    )

    token = _create_api_key(
        user=outsider,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS.value],
    )

    response = client.patch(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/{runner.id}/",
        data=json.dumps({"action": "deactivate"}),
        content_type="application/json",
        **_auth_headers(token, str(outsider_org.id)),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Runner image build not found"


@pytest.mark.django_db
def test_list_user_images_includes_source_runner_online_flag(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-list-user@test.com", password="secret")
    org = Organization.objects.create(name="Images Org", slug="images-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner_online = Runner.objects.create(
        name="runner-online",
        api_token_hash=hash_token("runner-online-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
    )
    runner_offline = Runner.objects.create(
        name="runner-offline",
        api_token_hash=hash_token("runner-offline-token"),
        status=RunnerStatus.OFFLINE,
        organization=org,
    )
    ws_online = Workspace.objects.create(
        runner=runner_online,
        name="Workspace Online",
        status=WorkspaceStatus.RUNNING,
        created_by=admin,
    )
    ws_offline = Workspace.objects.create(
        runner=runner_offline,
        name="Workspace Offline",
        status=WorkspaceStatus.RUNNING,
        created_by=admin,
    )
    image_online = ImageArtifact.objects.create(
        source_workspace=ws_online,
        created_by=admin,
        name="Captured Online",
        runner_artifact_id="img-online",
        status=ImageArtifact.ArtifactStatus.READY,
        artifact_kind=ImageArtifact.ArtifactKind.CAPTURED,
    )
    image_offline = ImageArtifact.objects.create(
        source_workspace=ws_offline,
        created_by=admin,
        name="Captured Offline",
        runner_artifact_id="img-offline",
        status=ImageArtifact.ArtifactStatus.READY,
        artifact_kind=ImageArtifact.ArtifactKind.CAPTURED,
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGES_READ.value],
    )
    response = client.get(
        "/api/v1/image-artifacts/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    payload = {entry["id"]: entry for entry in response.json()}
    assert payload[str(image_online.id)]["source_runner_online"] is True
    assert payload[str(image_offline.id)]["source_runner_online"] is False


@pytest.mark.django_db
def test_clone_workspace_from_offline_image_returns_runner_offline(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-clone-offline@test.com", password="secret")
    org = Organization.objects.create(name="Clone Org", slug="clone-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner_offline = Runner.objects.create(
        name="runner-offline-clone",
        api_token_hash=hash_token("runner-offline-clone-token"),
        status=RunnerStatus.OFFLINE,
        organization=org,
    )
    ws_offline = Workspace.objects.create(
        runner=runner_offline,
        name="Workspace Offline Clone",
        status=WorkspaceStatus.RUNNING,
        created_by=admin,
    )
    image_offline = ImageArtifact.objects.create(
        source_workspace=ws_offline,
        created_by=admin,
        name="Captured Offline Clone",
        runner_artifact_id="img-offline-clone",
        status=ImageArtifact.ArtifactStatus.READY,
        artifact_kind=ImageArtifact.ArtifactKind.CAPTURED,
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGES_CLONE.value],
    )
    response = client.post(
        f"/api/v1/image-artifacts/{image_offline.id}/workspaces/",
        data=json.dumps({"name": "clone-offline"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "runner_offline"


@pytest.mark.django_db
def test_assign_runner_build_rejects_incompatible_runtime(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-build-runtime@test.com", password="secret")
    org = Organization.objects.create(name="Runtime Org", slug="runtime-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="docker-only-runner",
        api_token_hash=hash_token("docker-only-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        available_runtimes=["docker"],
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="QEMU Definition",
        runtime_type="qemu",
        base_distro="ubuntu:24.04",
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS.value],
    )
    response = client.post(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/",
        data=json.dumps({"runner_id": str(runner.id), "activate": True}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Runner does not support runtime 'qemu'"


@pytest.mark.django_db
def test_clone_workspace_from_image_rejects_incompatible_runtime(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-clone-runtime@test.com", password="secret")
    org = Organization.objects.create(name="Clone Runtime Org", slug="clone-runtime-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="docker-only-clone-runner",
        api_token_hash=hash_token("docker-only-clone-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="QEMU Workspace",
        status=WorkspaceStatus.RUNNING,
        runtime_type="qemu",
        created_by=admin,
        qemu_vcpus=2,
        qemu_memory_mb=4096,
        qemu_disk_size_gb=50,
    )
    artifact = ImageArtifact.objects.create(
        source_workspace=workspace,
        created_by=admin,
        name="QEMU Clone Source",
        runner_artifact_id="qemu-clone-artifact",
        status=ImageArtifact.ArtifactStatus.READY,
        artifact_kind=ImageArtifact.ArtifactKind.CAPTURED,
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGES_CLONE.value],
    )
    response = client.post(
        f"/api/v1/image-artifacts/{artifact.id}/workspaces/",
        data=json.dumps({"name": "clone-runtime-mismatch"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Runner does not support runtime 'qemu'"


@pytest.mark.django_db
def test_list_runner_builds_includes_image_artifact_id(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-build-artifact@test.com", password="secret")
    org = Organization.objects.create(name="Artifact Org", slug="artifact-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="artifact-runner",
        api_token_hash=hash_token("artifact-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        available_runtimes=["qemu"],
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Artifact Definition",
        runtime_type="qemu",
        base_distro="ubuntu:24.04",
    )
    build = RunnerImageBuild.objects.create(
        image_definition=definition,
        runner=runner,
        status=RunnerImageBuild.Status.ACTIVE,
        image_path="/var/lib/opencuria/base-images/artifact.qcow2",
    )
    artifact = ImageArtifact.objects.create(
        source_workspace=None,
        created_by=admin,
        artifact_kind=ImageArtifact.ArtifactKind.BUILT,
        runner_image_build=build,
        runner_artifact_id=build.image_path,
        name="Artifact Image",
        status=ImageArtifact.ArtifactStatus.READY,
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_READ.value],
    )
    response = client.get(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["image_artifact_id"] == str(artifact.id)


@pytest.mark.django_db
def test_list_runner_builds_backfills_missing_built_artifact(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-build-backfill@test.com", password="secret")
    org = Organization.objects.create(name="Backfill Org", slug="backfill-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="backfill-runner",
        api_token_hash=hash_token("backfill-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        available_runtimes=["docker"],
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Backfill Definition",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
    )
    build = RunnerImageBuild.objects.create(
        image_definition=definition,
        runner=runner,
        status=RunnerImageBuild.Status.ACTIVE,
        image_tag="opencuria/custom/backfill:1",
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_READ.value],
    )
    response = client.get(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    payload = response.json()
    artifact = ImageArtifact.objects.get(runner_image_build=build)
    assert payload[0]["image_artifact_id"] == str(artifact.id)
    assert artifact.runner_artifact_id == "opencuria/custom/backfill:1"


@pytest.mark.django_db
def test_create_runner_build_returns_pending_build_without_artifact(client: Client, monkeypatch):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-build-create@test.com", password="secret")
    org = Organization.objects.create(name="Create Build Org", slug="create-build-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="create-build-runner",
        api_token_hash=hash_token("create-build-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        available_runtimes=["qemu"],
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Create Build Definition",
        runtime_type="qemu",
        base_distro="ubuntu:24.04",
    )
    build = RunnerImageBuild.objects.create(
        image_definition=definition,
        runner=runner,
        status=RunnerImageBuild.Status.PENDING,
    )

    async def _trigger_runner_image_build(**kwargs):
        return build

    monkeypatch.setattr(
        "apps.runners.api._get_service",
        lambda: SimpleNamespace(trigger_runner_image_build=_trigger_runner_image_build),
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS.value],
    )
    response = client.post(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/",
        data=json.dumps({"runner_id": str(runner.id), "activate": True}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["id"] == str(build.id)
    assert payload["status"] == RunnerImageBuild.Status.PENDING
    assert payload["image_artifact_id"] is None


@pytest.mark.django_db
def test_list_runner_builds_includes_global_definition_builds(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-build-global@test.com", password="secret")
    org = Organization.objects.create(name="Global Build Org", slug="global-build-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="global-build-runner",
        api_token_hash=hash_token("global-build-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        available_runtimes=["qemu"],
    )
    definition = ImageDefinition.objects.create(
        organization=None,
        created_by=admin,
        name="Global QEMU Definition",
        runtime_type="qemu",
        base_distro="ubuntu:24.04",
    )
    build = RunnerImageBuild.objects.create(
        image_definition=definition,
        runner=runner,
        status=RunnerImageBuild.Status.ACTIVE,
        image_path="/var/lib/opencuria/base-images/global.qcow2",
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_READ.value],
    )
    response = client.get(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == str(build.id)


@pytest.mark.django_db
def test_rebuild_runner_build_allows_global_definition_builds(client: Client, monkeypatch):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-build-global-rebuild@test.com", password="secret")
    org = Organization.objects.create(name="Global Rebuild Org", slug="global-rebuild-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="global-rebuild-runner",
        api_token_hash=hash_token("global-rebuild-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        available_runtimes=["qemu"],
    )
    definition = ImageDefinition.objects.create(
        organization=None,
        created_by=admin,
        name="Global Rebuild Definition",
        runtime_type="qemu",
        base_distro="ubuntu:24.04",
    )
    build = RunnerImageBuild.objects.create(
        image_definition=definition,
        runner=runner,
        status=RunnerImageBuild.Status.FAILED,
    )

    async def _trigger_runner_image_build(**kwargs):
        return build

    monkeypatch.setattr(
        "apps.runners.api._get_service",
        lambda: SimpleNamespace(trigger_runner_image_build=_trigger_runner_image_build),
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS.value],
    )
    response = client.patch(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/{runner.id}/",
        data=json.dumps({"action": "rebuild"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(build.id)
