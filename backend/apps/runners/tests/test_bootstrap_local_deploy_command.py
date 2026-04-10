"""Tests for the local deployment bootstrap management command."""

from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.credentials.models import CredentialService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RuntimeType
from apps.runners.management.commands.bootstrap_local_deploy import (
    DEFAULT_WORKSPACE_CUSTOM_DOCKERFILE,
    DEFAULT_WORKSPACE_PACKAGES,
)
from apps.runners.models import ImageArtifact, ImageDefinition, Runner, RunnerImageBuild
from common.utils import hash_token


def test_bootstrap_creates_pending_build(db) -> None:
    """Fresh bootstrap seeds a pending build, not a ready artifact."""
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

    membership = Membership.objects.get(user=user, organization=org)

    assert user.is_superuser is True
    assert membership.role == MembershipRole.ADMIN
    assert runner.api_token_hash == hash_token("runner-secret")
    assert runner.available_runtimes == [RuntimeType.DOCKER]

    # Build should be pending (not active) — the runner will build it
    assert build.status == RunnerImageBuild.Status.PENDING
    assert build.build_task is None
    assert build.image_tag == ""
    assert build.built_at is None

    # No artifact should exist yet
    assert not ImageArtifact.objects.filter(runner_image_build=build).exists()

    # Image definition should have the correct packages and Dockerfile
    assert definition.packages == DEFAULT_WORKSPACE_PACKAGES
    assert definition.custom_dockerfile == DEFAULT_WORKSPACE_CUSTOM_DOCKERFILE
    assert definition.runtime_type == RuntimeType.DOCKER
    assert definition.base_distro == "ubuntu:22.04"

    assert (
        CredentialService.objects.filter(
            slug="github-token",
            env_var_name="GITHUB_TOKEN",
        ).exists()
        is True
    )
    assert "Local deployment bootstrap complete" in out.getvalue()


def test_bootstrap_is_idempotent(db) -> None:
    """Running bootstrap twice should not create duplicate records."""
    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        "repeat@example.com",
        "--admin-password",
        "admin",
        "--organization-name",
        "Repeat Org",
        "--runner-name",
        "repeat-runner",
        "--runner-token",
        "runner-secret",
    )
    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        "repeat@example.com",
        "--admin-password",
        "admin",
        "--organization-name",
        "Repeat Org",
        "--runner-name",
        "repeat-runner",
        "--runner-token",
        "new-token",
    )

    org = Organization.objects.get(slug="repeat-org")
    runner = Runner.objects.get(name="repeat-runner", organization=org)
    definition = ImageDefinition.objects.get(organization=org)
    builds = RunnerImageBuild.objects.filter(
        image_definition=definition, runner=runner
    )

    assert builds.count() == 1
    build = builds.first()
    assert build.status == RunnerImageBuild.Status.PENDING
    assert runner.api_token_hash == hash_token("new-token")


def test_bootstrap_preserves_active_build(db) -> None:
    """If a build already succeeded, bootstrap should not reset it to pending."""
    from django.utils import timezone

    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        "active@example.com",
        "--admin-password",
        "admin",
        "--organization-name",
        "Active Org",
        "--runner-name",
        "active-runner",
        "--runner-token",
        "secret",
    )

    org = Organization.objects.get(slug="active-org")
    runner = Runner.objects.get(name="active-runner", organization=org)
    definition = ImageDefinition.objects.get(organization=org)
    build = RunnerImageBuild.objects.get(
        image_definition=definition, runner=runner
    )

    # Simulate a successful build
    build.status = RunnerImageBuild.Status.ACTIVE
    build.image_tag = "opencuria/custom/local-docker-workspace:test"
    build.built_at = timezone.now()
    build.save(update_fields=["status", "image_tag", "built_at"])

    # Run bootstrap again
    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        "active@example.com",
        "--admin-password",
        "admin",
        "--organization-name",
        "Active Org",
        "--runner-name",
        "active-runner",
        "--runner-token",
        "secret",
    )

    build.refresh_from_db()
    assert build.status == RunnerImageBuild.Status.ACTIVE
    assert build.image_tag == "opencuria/custom/local-docker-workspace:test"


def test_bootstrap_resets_failed_build_to_pending(db) -> None:
    """A failed build should be reset to pending on next bootstrap."""
    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        "failed@example.com",
        "--admin-password",
        "admin",
        "--organization-name",
        "Failed Org",
        "--runner-name",
        "failed-runner",
        "--runner-token",
        "secret",
    )

    org = Organization.objects.get(slug="failed-org")
    runner = Runner.objects.get(name="failed-runner", organization=org)
    definition = ImageDefinition.objects.get(organization=org)
    build = RunnerImageBuild.objects.get(
        image_definition=definition, runner=runner
    )

    # Simulate a failed build
    build.status = RunnerImageBuild.Status.FAILED
    build.save(update_fields=["status"])

    # Run bootstrap again
    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        "failed@example.com",
        "--admin-password",
        "admin",
        "--organization-name",
        "Failed Org",
        "--runner-name",
        "failed-runner",
        "--runner-token",
        "secret",
    )

    build.refresh_from_db()
    assert build.status == RunnerImageBuild.Status.PENDING
    assert build.build_task is None
    assert build.image_tag == ""
    assert build.built_at is None
