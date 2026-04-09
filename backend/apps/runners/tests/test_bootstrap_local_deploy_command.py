"""Tests for the local deployment bootstrap management command."""

from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.credentials.models import CredentialService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RuntimeType
from apps.runners.models import ImageArtifact, ImageDefinition, Runner, RunnerImageBuild
from common.utils import hash_token


def test_bootstrap_local_deploy_creates_required_records(db) -> None:
    out = StringIO()

    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        "local-admin@example.com",
        "--admin-password",
        "admin",
        "--organization-name",
        "Local Dev",
        "--runner-name",
        "local-runner",
        "--runner-token",
        "runner-secret",
        "--workspace-image",
        "ghcr.io/opencuria/workspace:test",
        stdout=out,
    )

    user = get_user_model().objects.get(email="local-admin@example.com")
    org = Organization.objects.get(slug="local-dev")
    runner = Runner.objects.get(name="local-runner", organization=org)
    definition = ImageDefinition.objects.get(
        organization=org,
        name="Local Docker Workspace",
    )
    build = RunnerImageBuild.objects.get(image_definition=definition, runner=runner)
    artifact = ImageArtifact.objects.get(runner_image_build=build)

    membership = Membership.objects.get(user=user, organization=org)

    assert user.is_superuser is True
    assert membership.role == MembershipRole.ADMIN
    assert runner.api_token_hash == hash_token("runner-secret")
    assert runner.available_runtimes == [RuntimeType.DOCKER]
    assert build.status == RunnerImageBuild.Status.ACTIVE
    assert build.image_tag == "ghcr.io/opencuria/workspace:test"
    assert artifact.status == ImageArtifact.ArtifactStatus.READY
    assert artifact.runner_artifact_id == "ghcr.io/opencuria/workspace:test"
    assert (
        CredentialService.objects.filter(
            slug="github-token",
            env_var_name="GITHUB_TOKEN",
        ).exists()
        is True
    )
    assert "Local deployment bootstrap complete" in out.getvalue()


def test_bootstrap_local_deploy_is_idempotent_and_updates_runner(db) -> None:
    user_model = get_user_model()
    user = user_model.objects.create_user(
        email="repeat@example.com",
        password="old-password",
    )
    org = Organization.objects.create(name="Repeat Org", slug="repeat-org")
    Membership.objects.create(
        user=user,
        organization=org,
        role=MembershipRole.MEMBER,
    )
    runner = Runner.objects.create(
        name="repeat-runner",
        api_token_hash=hash_token("old-token"),
        organization=org,
        available_runtimes=[],
    )

    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        "repeat@example.com",
        "--admin-password",
        "new-password",
        "--organization-name",
        "Repeat Org",
        "--runner-name",
        "repeat-runner",
        "--runner-token",
        "new-token",
        "--workspace-image",
        "ghcr.io/opencuria/workspace:latest",
    )
    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        "repeat@example.com",
        "--admin-password",
        "new-password",
        "--organization-name",
        "Repeat Org",
        "--runner-name",
        "repeat-runner",
        "--runner-token",
        "new-token",
        "--workspace-image",
        "ghcr.io/opencuria/workspace:latest",
    )

    runner.refresh_from_db()
    membership = Membership.objects.get(user=user, organization=org)
    build = RunnerImageBuild.objects.get(runner=runner)
    artifact = ImageArtifact.objects.get(runner_image_build=build)

    assert Membership.objects.filter(user=user, organization=org).count() == 1
    assert Runner.objects.filter(name="repeat-runner", organization=org).count() == 1
    assert membership.role == MembershipRole.ADMIN
    assert runner.api_token_hash == hash_token("new-token")
    assert runner.available_runtimes == [RuntimeType.DOCKER]
    assert build.status == RunnerImageBuild.Status.ACTIVE
    assert artifact.status == ImageArtifact.ArtifactStatus.READY
