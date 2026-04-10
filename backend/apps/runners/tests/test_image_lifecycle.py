"""
Tests for the image lifecycle changes introduced in issue #4:
- Workspace → ImageArtifact FK (base_image_artifact)
- Retire / unretire flow
- Safe deletion with conflict detection (409)
- Normalized delete dispatches runner cleanup for all artifact kinds
- Side-effect removal from read paths
- New statuses (retired, pending_delete, deleted)
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import (
    ImageArtifact,
    ImageDefinition,
    Runner,
    RunnerImageBuild,
    Workspace,
)
from apps.runners.repositories import ImageArtifactRepository
from common.utils import generate_api_token, hash_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _setup_org_with_runner(*, runner_status=RunnerStatus.ONLINE, runtimes=None):
    """Create org, admin user, membership, and a runner."""
    user_model = get_user_model()
    admin = user_model.objects.create_user(
        email=f"lifecycle-{uuid.uuid4().hex[:8]}@test.com", password="secret"
    )
    org = Organization.objects.create(
        name=f"Lifecycle Org {uuid.uuid4().hex[:6]}",
        slug=f"lifecycle-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name=f"lifecycle-runner-{uuid.uuid4().hex[:6]}",
        api_token_hash=hash_token(generate_api_token()),
        status=runner_status,
        organization=org,
        available_runtimes=runtimes or ["docker"],
    )
    return admin, org, runner


def _create_captured_artifact(*, admin, runner, name="test-artifact", status="ready"):
    """Create a captured image artifact from a workspace."""
    workspace = Workspace.objects.create(
        runner=runner,
        name=f"src-ws-{uuid.uuid4().hex[:6]}",
        status=WorkspaceStatus.RUNNING,
        created_by=admin,
    )
    artifact = ImageArtifact.objects.create(
        source_workspace=workspace,
        runner_artifact_id=f"snap-{uuid.uuid4().hex[:8]}",
        name=name,
        size_bytes=1024,
        status=status,
        artifact_kind=ImageArtifact.ArtifactKind.CAPTURED,
        created_by=admin,
    )
    return artifact, workspace


def _create_built_artifact(*, admin, org, runner, name="built-artifact", status="ready"):
    """Create a built image artifact from a runner image build."""
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name=f"def-{uuid.uuid4().hex[:6]}",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
    )
    build = RunnerImageBuild.objects.create(
        image_definition=definition,
        runner=runner,
        status=RunnerImageBuild.Status.ACTIVE,
        image_tag=f"opencuria/custom/{uuid.uuid4().hex[:8]}:latest",
    )
    artifact = ImageArtifact.objects.create(
        runner_image_build=build,
        runner_artifact_id=build.image_tag,
        name=name,
        size_bytes=2048,
        status=status,
        artifact_kind=ImageArtifact.ArtifactKind.BUILT,
        created_by=admin,
    )
    return artifact, build


# ===========================================================================
# Model / Repository Layer Tests
# ===========================================================================


class TestImageArtifactModel:
    """Tests for new model properties and status choices."""

    @pytest.mark.django_db
    def test_new_status_choices_exist(self):
        assert ImageArtifact.ArtifactStatus.RETIRED == "retired"
        assert ImageArtifact.ArtifactStatus.PENDING_DELETE == "pending_delete"
        assert ImageArtifact.ArtifactStatus.DELETED == "deleted"

    @pytest.mark.django_db
    def test_is_usable_only_when_ready(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)

        assert artifact.status == "ready"
        assert artifact.is_usable is True

        artifact.status = "retired"
        assert artifact.is_usable is False

        artifact.status = "pending_delete"
        assert artifact.is_usable is False

        artifact.status = "deleted"
        assert artifact.is_usable is False

    @pytest.mark.django_db
    def test_is_deletable_when_no_active_workspaces(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)

        assert artifact.is_deletable is True

    @pytest.mark.django_db
    def test_is_not_deletable_when_active_workspace_depends(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)

        # Create a workspace that depends on this artifact
        Workspace.objects.create(
            runner=runner,
            name="dependent-ws",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
            base_image_artifact=artifact,
        )

        assert artifact.is_deletable is False

    @pytest.mark.django_db
    def test_deleted_at_null_by_default(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)
        assert artifact.deleted_at is None


class TestWorkspaceBaseImageArtifactFK:
    """Tests for the Workspace → ImageArtifact FK."""

    @pytest.mark.django_db
    def test_workspace_base_image_artifact_nullable(self):
        admin, org, runner = _setup_org_with_runner()
        workspace = Workspace.objects.create(
            runner=runner,
            name="no-artifact-ws",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
        )
        assert workspace.base_image_artifact is None
        assert workspace.base_image_artifact_id is None

    @pytest.mark.django_db
    def test_workspace_base_image_artifact_set(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)
        workspace = Workspace.objects.create(
            runner=runner,
            name="with-artifact-ws",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
            base_image_artifact=artifact,
        )
        assert workspace.base_image_artifact_id == artifact.id

    @pytest.mark.django_db
    def test_protect_prevents_artifact_deletion_with_workspace(self):
        from django.db.models import ProtectedError

        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)
        Workspace.objects.create(
            runner=runner,
            name="protected-ws",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
            base_image_artifact=artifact,
        )

        with pytest.raises(ProtectedError):
            artifact.delete()

    @pytest.mark.django_db
    def test_dependent_workspaces_reverse_relation(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)
        ws1 = Workspace.objects.create(
            runner=runner,
            name="dep-ws-1",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
            base_image_artifact=artifact,
        )
        ws2 = Workspace.objects.create(
            runner=runner,
            name="dep-ws-2",
            status=WorkspaceStatus.STOPPED,
            created_by=admin,
            base_image_artifact=artifact,
        )

        dependent_ids = set(artifact.dependent_workspaces.values_list("id", flat=True))
        assert ws1.id in dependent_ids
        assert ws2.id in dependent_ids


# ===========================================================================
# Repository Layer Tests
# ===========================================================================


class TestImageArtifactRepository:
    """Tests for new ImageArtifactRepository methods."""

    @pytest.mark.django_db
    def test_mark_retired_from_ready(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner, status="ready")

        assert ImageArtifactRepository.mark_retired(artifact.id) is True
        artifact.refresh_from_db()
        assert artifact.status == "retired"

    @pytest.mark.django_db
    def test_mark_retired_from_non_ready_fails(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner, status="creating")

        assert ImageArtifactRepository.mark_retired(artifact.id) is False
        artifact.refresh_from_db()
        assert artifact.status == "creating"

    @pytest.mark.django_db
    def test_unretire_from_retired(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner, status="retired")

        assert ImageArtifactRepository.unretire(artifact.id) is True
        artifact.refresh_from_db()
        assert artifact.status == "ready"

    @pytest.mark.django_db
    def test_unretire_from_non_retired_fails(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner, status="ready")

        assert ImageArtifactRepository.unretire(artifact.id) is False
        artifact.refresh_from_db()
        assert artifact.status == "ready"

    @pytest.mark.django_db
    def test_mark_pending_delete_from_ready(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner, status="ready")

        assert ImageArtifactRepository.mark_pending_delete(artifact.id) is True
        artifact.refresh_from_db()
        assert artifact.status == "pending_delete"

    @pytest.mark.django_db
    def test_mark_pending_delete_from_retired(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner, status="retired")

        assert ImageArtifactRepository.mark_pending_delete(artifact.id) is True
        artifact.refresh_from_db()
        assert artifact.status == "pending_delete"

    @pytest.mark.django_db
    def test_mark_pending_delete_from_failed(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner, status="failed")

        assert ImageArtifactRepository.mark_pending_delete(artifact.id) is True
        artifact.refresh_from_db()
        assert artifact.status == "pending_delete"

    @pytest.mark.django_db
    def test_mark_deleted_from_pending_delete(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(
            admin=admin, runner=runner, status="pending_delete"
        )

        assert ImageArtifactRepository.mark_deleted(artifact.id) is True
        artifact.refresh_from_db()
        assert artifact.status == "deleted"
        assert artifact.deleted_at is not None

    @pytest.mark.django_db
    def test_mark_deleted_from_non_pending_fails(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner, status="ready")

        assert ImageArtifactRepository.mark_deleted(artifact.id) is False
        artifact.refresh_from_db()
        assert artifact.status == "ready"
        assert artifact.deleted_at is None

    @pytest.mark.django_db
    def test_has_dependent_workspaces_true(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)
        Workspace.objects.create(
            runner=runner,
            name="dep-ws",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
            base_image_artifact=artifact,
        )

        assert ImageArtifactRepository.has_dependent_workspaces(artifact.id) is True

    @pytest.mark.django_db
    def test_has_dependent_workspaces_false_when_none(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)

        assert ImageArtifactRepository.has_dependent_workspaces(artifact.id) is False

    @pytest.mark.django_db
    def test_has_dependent_workspaces_ignores_removed(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)
        Workspace.objects.create(
            runner=runner,
            name="removed-ws",
            status=WorkspaceStatus.REMOVED,
            created_by=admin,
            base_image_artifact=artifact,
        )

        assert ImageArtifactRepository.has_dependent_workspaces(artifact.id) is False

    @pytest.mark.django_db
    def test_count_dependent_workspaces(self):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)

        # 2 active, 1 removed (should not count)
        for status in [WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED]:
            Workspace.objects.create(
                runner=runner,
                name=f"dep-{status}",
                status=status,
                created_by=admin,
                base_image_artifact=artifact,
            )
        Workspace.objects.create(
            runner=runner,
            name="dep-removed",
            status=WorkspaceStatus.REMOVED,
            created_by=admin,
            base_image_artifact=artifact,
        )

        assert ImageArtifactRepository.count_dependent_workspaces(artifact.id) == 2


# ===========================================================================
# API Layer Tests
# ===========================================================================


class TestDeleteImageArtifactConflict:
    """Delete returns 409 when active workspaces depend on the artifact."""

    @pytest.mark.django_db
    def test_delete_blocked_by_active_workspace(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, src_ws = _create_captured_artifact(admin=admin, runner=runner)

        # An active workspace depending on the artifact
        Workspace.objects.create(
            runner=runner,
            name="blocking-ws",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
            base_image_artifact=artifact,
        )

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_DELETE.value],
        )
        response = client.delete(
            f"/api/v1/image-artifacts/{artifact.id}/",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "image_in_use"
        assert "1 active workspace" in body["detail"]

        # Artifact still exists
        assert ImageArtifact.objects.filter(id=artifact.id).exists()

    @pytest.mark.django_db
    def test_delete_allowed_when_only_removed_workspaces(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, src_ws = _create_captured_artifact(admin=admin, runner=runner)

        Workspace.objects.create(
            runner=runner,
            name="removed-ws",
            status=WorkspaceStatus.REMOVED,
            created_by=admin,
            base_image_artifact=artifact,
        )

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_DELETE.value],
        )
        response = client.delete(
            f"/api/v1/image-artifacts/{artifact.id}/",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 204
        assert not ImageArtifact.objects.filter(id=artifact.id).exists()

    @pytest.mark.django_db
    def test_delete_allowed_when_no_dependent_workspaces(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_DELETE.value],
        )
        response = client.delete(
            f"/api/v1/image-artifacts/{artifact.id}/",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 204
        assert not ImageArtifact.objects.filter(id=artifact.id).exists()


class TestRetireUnretireEndpoint:
    """Tests for POST /{image_artifact_id}/retire/ endpoint."""

    @pytest.mark.django_db
    def test_retire_artifact(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_CREATE.value],
        )
        response = client.post(
            f"/api/v1/image-artifacts/{artifact.id}/retire/",
            data=json.dumps({"retired": True}),
            content_type="application/json",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "retired"
        assert body["id"] == str(artifact.id)

        artifact.refresh_from_db()
        assert artifact.status == "retired"

    @pytest.mark.django_db
    def test_unretire_artifact(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(
            admin=admin, runner=runner, status="retired"
        )

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_CREATE.value],
        )
        response = client.post(
            f"/api/v1/image-artifacts/{artifact.id}/retire/",
            data=json.dumps({"retired": False}),
            content_type="application/json",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"

        artifact.refresh_from_db()
        assert artifact.status == "ready"

    @pytest.mark.django_db
    def test_retire_non_ready_returns_409(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(
            admin=admin, runner=runner, status="creating"
        )

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_CREATE.value],
        )
        response = client.post(
            f"/api/v1/image-artifacts/{artifact.id}/retire/",
            data=json.dumps({"retired": True}),
            content_type="application/json",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 409

    @pytest.mark.django_db
    def test_unretire_non_retired_returns_409(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner, status="ready")

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_CREATE.value],
        )
        response = client.post(
            f"/api/v1/image-artifacts/{artifact.id}/retire/",
            data=json.dumps({"retired": False}),
            content_type="application/json",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 409

    @pytest.mark.django_db
    def test_retire_not_found(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_CREATE.value],
        )
        fake_id = uuid.uuid4()
        response = client.post(
            f"/api/v1/image-artifacts/{fake_id}/retire/",
            data=json.dumps({"retired": True}),
            content_type="application/json",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 404

    @pytest.mark.django_db
    def test_retire_forbidden_for_non_owner(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)

        # Create a different user in the same org
        user_model = get_user_model()
        other_user = user_model.objects.create_user(
            email=f"other-{uuid.uuid4().hex[:8]}@test.com", password="secret"
        )
        Membership.objects.create(
            user=other_user, organization=org, role=MembershipRole.ADMIN
        )

        token = _create_api_key(
            user=other_user,
            permissions=[APIKeyPermission.IMAGES_CREATE.value],
        )
        response = client.post(
            f"/api/v1/image-artifacts/{artifact.id}/retire/",
            data=json.dumps({"retired": True}),
            content_type="application/json",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 403


class TestImageArtifactOutSchema:
    """Tests for new fields in ImageArtifactOut."""

    @pytest.mark.django_db
    def test_dependent_workspace_count_in_response(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, src_ws = _create_captured_artifact(admin=admin, runner=runner)

        # Create 2 dependent workspaces
        for i in range(2):
            Workspace.objects.create(
                runner=runner,
                name=f"dep-ws-{i}",
                status=WorkspaceStatus.RUNNING,
                created_by=admin,
                base_image_artifact=artifact,
            )

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_READ.value],
        )
        response = client.get(
            f"/api/v1/workspaces/{src_ws.id}/image-artifacts/",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 200
        body = response.json()
        matching = [a for a in body if a["id"] == str(artifact.id)]
        assert len(matching) == 1
        assert matching[0]["dependent_workspace_count"] == 2

    @pytest.mark.django_db
    def test_deleted_at_in_response(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, src_ws = _create_captured_artifact(admin=admin, runner=runner)

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_READ.value],
        )
        response = client.get(
            f"/api/v1/workspaces/{src_ws.id}/image-artifacts/",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 200
        body = response.json()
        matching = [a for a in body if a["id"] == str(artifact.id)]
        assert len(matching) == 1
        assert matching[0]["deleted_at"] is None


class TestWorkspaceOutSchema:
    """Tests for base_image_artifact_id in WorkspaceOut."""

    @pytest.mark.django_db
    def test_workspace_includes_base_image_artifact_id(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)
        workspace = Workspace.objects.create(
            runner=runner,
            name="ws-with-artifact",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
            base_image_artifact=artifact,
        )

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.WORKSPACES_READ.value],
        )
        response = client.get(
            f"/api/v1/workspaces/{workspace.id}/",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["base_image_artifact_id"] == str(artifact.id)

    @pytest.mark.django_db
    def test_workspace_without_artifact_has_null(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        workspace = Workspace.objects.create(
            runner=runner,
            name="ws-no-artifact",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
        )

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.WORKSPACES_READ.value],
        )
        response = client.get(
            f"/api/v1/workspaces/{workspace.id}/",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["base_image_artifact_id"] is None


class TestCloneWorkspaceRejectsRetired:
    """Cloning from a retired artifact should fail (status != ready)."""

    @pytest.mark.django_db
    def test_clone_from_retired_artifact_returns_409(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, src_ws = _create_captured_artifact(
            admin=admin, runner=runner, status="retired"
        )
        runner.sid = "test-sid"
        runner.save()

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_CLONE.value],
        )
        response = client.post(
            f"/api/v1/image-artifacts/{artifact.id}/workspaces/",
            data=json.dumps({"name": "clone-test"}),
            content_type="application/json",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 409


class TestDeleteBuiltArtifact:
    """Deleting a built artifact should also work and remove DB record."""

    @pytest.mark.django_db
    def test_delete_built_artifact_succeeds(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, build = _create_built_artifact(
            admin=admin, org=org, runner=runner
        )

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_DELETE.value],
        )
        response = client.delete(
            f"/api/v1/image-artifacts/{artifact.id}/",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 204
        assert not ImageArtifact.objects.filter(id=artifact.id).exists()

    @pytest.mark.django_db
    def test_delete_built_artifact_blocked_by_workspace(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, build = _create_built_artifact(
            admin=admin, org=org, runner=runner
        )

        Workspace.objects.create(
            runner=runner,
            name="built-dep-ws",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
            base_image_artifact=artifact,
        )

        token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_DELETE.value],
        )
        response = client.delete(
            f"/api/v1/image-artifacts/{artifact.id}/",
            **_auth_headers(token, str(org.id)),
        )

        assert response.status_code == 409
        assert ImageArtifact.objects.filter(id=artifact.id).exists()


class TestRetireThenDelete:
    """Full lifecycle: retire → remove workspaces → delete."""

    @pytest.mark.django_db
    def test_retire_then_delete_after_workspaces_removed(self, client: Client):
        admin, org, runner = _setup_org_with_runner()
        artifact, _ = _create_captured_artifact(admin=admin, runner=runner)

        dep_ws = Workspace.objects.create(
            runner=runner,
            name="will-be-removed",
            status=WorkspaceStatus.RUNNING,
            created_by=admin,
            base_image_artifact=artifact,
        )

        retire_token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_CREATE.value],
        )

        # Step 1: Retire
        response = client.post(
            f"/api/v1/image-artifacts/{artifact.id}/retire/",
            data=json.dumps({"retired": True}),
            content_type="application/json",
            **_auth_headers(retire_token, str(org.id)),
        )
        assert response.status_code == 200

        # Step 2: Try delete — should fail (workspace still active)
        delete_token = _create_api_key(
            user=admin,
            permissions=[APIKeyPermission.IMAGES_DELETE.value],
        )
        response = client.delete(
            f"/api/v1/image-artifacts/{artifact.id}/",
            **_auth_headers(delete_token, str(org.id)),
        )
        assert response.status_code == 409

        # Step 3: Mark workspace as removed
        dep_ws.status = WorkspaceStatus.REMOVED
        dep_ws.save()

        # Step 4: Delete now succeeds
        response = client.delete(
            f"/api/v1/image-artifacts/{artifact.id}/",
            **_auth_headers(delete_token, str(org.id)),
        )
        assert response.status_code == 204
        assert not ImageArtifact.objects.filter(id=artifact.id).exists()
