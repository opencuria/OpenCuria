"""
Tests for image definition management API endpoints.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus
from apps.runners.models import ImageDefinition, ImageInstance, Runner, ImageBuildJob
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
        base_distro="ubuntu:24.04",
    )
    local = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Local Base",
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
    assert names["Local Base"]["runner_build_summary"] == {
        "active": 0,
        "building": 0,
        "failed": 0,
        "inactive": 0,
        "removing": 0,
    }


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
        base_distro="ubuntu:24.04",
    )
    ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.ACTIVE,
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
        base_distro="ubuntu:24.04",
    )
    ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.ACTIVE,
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
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Artifact Definition",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.ACTIVE,
    )
    runner_ref = "/var/lib/opencuria/base-images/artifact.qcow2"
    artifact = ImageInstance.objects.create(
            is_legacy=True,
        runner=runner,
        origin_type=ImageInstance.OriginType.DEFINITION_BUILD,
        origin_definition=definition,
        created_by=admin,
        build_job=build,
        runner_ref=runner_ref,
        name="Artifact Image",
        status=ImageInstance.Status.READY,
    )
    build.current_generation = artifact
    build.save()

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
def test_list_runner_builds_does_not_backfill_missing_built_artifact(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="image-build-backfill@test.com", password="secret")
    org = Organization.objects.create(name="Backfill Org", slug="backfill-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)

    runner = Runner.objects.create(
        name="backfill-runner",
        api_token_hash=hash_token("backfill-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Backfill Definition",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.ACTIVE,
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
    assert payload[0]["image_artifact_id"] is None
    assert not ImageInstance.objects.filter(build_job=build).exists()


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
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Create Build Definition",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.PENDING,
    )

    async def _trigger_build_job(**kwargs):
        return build

    monkeypatch.setattr(
        "apps.runners.api._get_service",
        lambda: SimpleNamespace(
            trigger_build_job=_trigger_build_job,
        ),
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
    assert payload["status"] == ImageBuildJob.Status.PENDING
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
    )
    definition = ImageDefinition.objects.create(
        organization=None,
        created_by=admin,
        name="Global QEMU Definition",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.ACTIVE,
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
    )
    definition = ImageDefinition.objects.create(
        organization=None,
        created_by=admin,
        name="Global Rebuild Definition",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.FAILED,
    )

    async def _trigger_build_job(**kwargs):
        return build

    monkeypatch.setattr(
        "apps.runners.api._get_service",
        lambda: SimpleNamespace(
            trigger_build_job=_trigger_build_job,
        ),
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


@pytest.mark.django_db
def test_list_image_definitions_excludes_deleted_and_counts_builds(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(
        email="image-list-deleted@test.com", password="secret"
    )
    org = Organization.objects.create(name="Deleted List Org", slug="deleted-list-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="summary-runner",
        api_token_hash=hash_token("summary-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
    )
    second_runner = Runner.objects.create(
        name="summary-runner-2",
        api_token_hash=hash_token("summary-runner-token-2"),
        status=RunnerStatus.ONLINE,
        organization=org,
    )
    visible = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Visible Recipe",
        base_distro="ubuntu:24.04",
    )
    ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Gone Recipe",
        base_distro="ubuntu:24.04",
        status=ImageDefinition.Status.DELETED,
    )
    ImageBuildJob.objects.create(
        image_definition=visible,
        runner=runner,
        status=ImageBuildJob.Status.ACTIVE,
    )
    ImageBuildJob.objects.create(
        image_definition=visible,
        runner=second_runner,
        status=ImageBuildJob.Status.FAILED,
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
    names = {item["name"]: item for item in response.json()}
    assert "Gone Recipe" not in names
    assert names["Visible Recipe"]["runner_build_summary"] == {
        "active": 1,
        "building": 0,
        "failed": 1,
        "inactive": 0,
        "removing": 0,
    }


@pytest.mark.django_db
def test_list_runner_builds_excludes_deleted_jobs(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(
        email="build-list-deleted@test.com", password="secret"
    )
    org = Organization.objects.create(
        name="Deleted Builds Org",
        slug="deleted-builds-org",
    )
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="deleted-build-runner",
        api_token_hash=hash_token("deleted-build-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Deleted Build Definition",
        base_distro="ubuntu:24.04",
    )
    ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.DELETED,
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
    assert response.json() == []


@pytest.mark.django_db
def test_activate_runner_build_uses_activate_action(client: Client, monkeypatch):
    user_model = get_user_model()
    admin = user_model.objects.create_user(
        email="activate-action@test.com", password="secret"
    )
    org = Organization.objects.create(
        name="Activate Action Org",
        slug="activate-action-org",
    )
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="activate-action-runner",
        api_token_hash=hash_token("activate-action-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        sid="activate-action-sid",
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Activate Action Definition",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.DEACTIVATED,
        built_at=timezone.now(),
    )

    triggered = {"rebuild": False, "activate": False}

    async def _trigger_build_job(**kwargs):
        triggered["rebuild"] = True
        return build

    async def _activate_build_job(job, *, created_by=None, message=""):
        triggered["activate"] = True
        job.status = ImageBuildJob.Status.ACTIVE
        return job

    monkeypatch.setattr(
        "apps.runners.api._get_service",
        lambda: SimpleNamespace(
            trigger_build_job=_trigger_build_job,
            activate_build_job=_activate_build_job,
            _ensure_definition_mutable=lambda definition: None,
        ),
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS.value],
    )
    response = client.patch(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/{runner.id}/",
        data=json.dumps({"action": "activate"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    assert triggered == {"rebuild": False, "activate": True}


@pytest.mark.django_db
def test_rebuild_runner_build_uses_rebuild_action(client: Client, monkeypatch):
    user_model = get_user_model()
    admin = user_model.objects.create_user(
        email="rebuild-action@test.com", password="secret"
    )
    org = Organization.objects.create(
        name="Rebuild Action Org",
        slug="rebuild-action-org",
    )
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="rebuild-action-runner",
        api_token_hash=hash_token("rebuild-action-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        sid="rebuild-action-sid",
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Rebuild Action Definition",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.ACTIVE,
    )

    triggered = {"rebuild": False, "activate": False}

    async def _trigger_build_job(**kwargs):
        triggered["rebuild"] = True
        return build

    async def _activate_build_job(job, *, created_by=None, message=""):
        triggered["activate"] = True
        return job

    monkeypatch.setattr(
        "apps.runners.api._get_service",
        lambda: SimpleNamespace(
            trigger_build_job=_trigger_build_job,
            activate_build_job=_activate_build_job,
            _ensure_definition_mutable=lambda definition: None,
        ),
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
    assert triggered == {"rebuild": True, "activate": False}


@pytest.mark.django_db
def test_runner_build_mutation_rejected_for_deleting_definition(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(
        email="locked-def@test.com", password="secret"
    )
    org = Organization.objects.create(name="Locked Def Org", slug="locked-def-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="locked-runner",
        api_token_hash=hash_token("locked-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
        sid="locked-sid",
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Locked Definition",
        base_distro="ubuntu:24.04",
        status=ImageDefinition.Status.DELETING,
    )
    ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.ACTIVE,
    )

    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS.value],
    )
    response = client.patch(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/{runner.id}/",
        data=json.dumps({"action": "deactivate"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 409
    assert "deleting" in response.json()["detail"]


@pytest.mark.django_db
def test_list_runner_builds_omits_full_log_but_reports_size(client: Client):
    """List endpoint stays polling-cheap: no build_log, only build_log_size."""
    user_model = get_user_model()
    admin = user_model.objects.create_user(
        email="build-log-size@test.com", password="secret"
    )
    org = Organization.objects.create(name="Log Size Org", slug="log-size-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="log-size-runner",
        api_token_hash=hash_token("log-size-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Log Size Definition",
        base_distro="ubuntu:24.04",
    )
    log_text = "x" * 5000 + "\n"
    ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.BUILDING,
        build_log=log_text,
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
    assert "build_log" not in payload[0]
    assert payload[0]["build_log_size"] == len(log_text)


@pytest.mark.django_db
def test_handle_image_build_progress_appends_atomically_and_caps_log():
    """Progress writes never read the full log and cap the stored tail."""
    from apps.runners.services import RunnerService

    user_model = get_user_model()
    admin = user_model.objects.create_user(
        email="build-progress@test.com", password="secret"
    )
    org = Organization.objects.create(name="Progress Org", slug="progress-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="progress-runner",
        api_token_hash=hash_token("progress-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Progress Definition",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.PENDING,
        build_log="old\n",
    )

    from apps.runners.repositories import ImageGenerationRepository
    build, image, task = ImageGenerationRepository.request(
        definition=definition, runner=runner, rendered_input={}, created_by=admin
    )
    ImageBuildJob.objects.filter(id=build.id).update(build_log="old\n")
    service = RunnerService(sio_server=None)
    service.handle_image_build_progress(
        str(build.id), "new line", runner_id=str(runner.id), task_id=str(task.id)
    )

    build.refresh_from_db()
    assert build.status == ImageBuildJob.Status.PENDING
    assert build.build_log == "old\nnew line\n"

    # Over-long stored logs are trimmed to the tail cap atomically.
    big = "y" * (RunnerService.BUILD_LOG_MAX_CHARS + 100)
    ImageBuildJob.objects.filter(id=build.id).update(build_log=big)
    service.handle_image_build_progress(str(build.id), "tail", runner_id=None, task_id=str(task.id))
    build.refresh_from_db()
    assert len(build.build_log) == RunnerService.BUILD_LOG_MAX_CHARS
    assert build.build_log.endswith("tail\n")

    # Runner mismatch and unknown jobs are ignored without writes.
    before = build.build_log
    service.handle_image_build_progress(
        str(build.id), "evil", runner_id="00000000-0000-0000-0000-000000000000"
    )
    build.refresh_from_db()
    assert build.build_log == before
    service.handle_image_build_progress(
        "00000000-0000-0000-0000-000000000000", "noop", runner_id=None
    )


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["activate", "deactivate", "delete"])
def test_global_recipe_lifecycle_is_read_only_for_org_admin(client, action):
    user = get_user_model().objects.create_user(email="readonly@test.com", password="secret")
    org = Organization.objects.create(name="Readonly org", slug="readonly-org")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    definition = ImageDefinition.objects.create(name="Readonly global", organization=None)
    token = _create_api_key(user=user, permissions=[APIKeyPermission.IMAGE_DEFINITIONS_WRITE.value])
    url = f"/api/v1/image-definitions/{definition.id}/"
    response = (client.delete(url, **_auth_headers(token, str(org.id))) if action == "delete"
                else client.post(url + action + "/", **_auth_headers(token, str(org.id))))
    assert response.status_code == 404
    definition.refresh_from_db()
    assert definition.status == "active"
