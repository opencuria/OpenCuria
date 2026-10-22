"""Tests for the local deployment bootstrap management command."""

from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.credentials.models import CredentialService, OrgCredentialServiceActivation
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RuntimeType, TaskStatus, TaskType
from apps.runners.management.commands.bootstrap_local_deploy import (
    DEFAULT_WORKSPACE_CUSTOM_DOCKERFILE,
    DEFAULT_WORKSPACE_PACKAGES,
)
from apps.runners.models import (
    ImageBuildJob,
    ImageDefinition,
    ImageInstance,
    LifecycleCommand,
    Runner,
    Task,
)
from apps.runners.repositories import ImageGenerationRepository
from common.utils import hash_token


def test_bootstrap_creates_pending_build(db) -> None:
    """Fresh bootstrap queues a durable generation, not a ready artifact."""
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
    build = ImageBuildJob.objects.get(image_definition=definition, runner=runner)

    membership = Membership.objects.get(user=user, organization=org)

    assert user.is_superuser is True
    assert membership.role == MembershipRole.ADMIN
    assert runner.api_token_hash == hash_token("runner-secret")
    assert runner.available_runtimes == [RuntimeType.DOCKER]

    # The immutable generation and its task are queued for the offline runner.
    assert build.status == ImageBuildJob.Status.PENDING
    assert build.build_task.type == TaskType.BUILD_IMAGE
    assert build.build_task.status == TaskStatus.PENDING
    assert build.built_at is None
    assert build.current_generation_id is None
    generation = build.pending_generation
    assert generation.status == ImageInstance.Status.BUILDING
    assert generation.creating_task_id == build.build_task_id
    assert generation.created_by_id == user.id
    assert generation.generation == 1
    assert generation.revision.recipe["packages"] == DEFAULT_WORKSPACE_PACKAGES
    command = LifecycleCommand.objects.get(task=build.build_task)
    assert command.event == "task:build_image"
    assert command.payload["image_instance_id"] == str(generation.id)
    assert command.payload["image_tag"] == generation.runner_ref
    assert command.payload["_revision_id"] == str(generation.revision_id)
    assert generation.revision.rendered_input["dockerfile_content"]
    assert command.deliveries == 0

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
    first_build = ImageBuildJob.objects.get()
    generation_id = first_build.pending_generation_id
    task_id = first_build.build_task_id
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
    builds = ImageBuildJob.objects.filter(
        image_definition=definition, runner=runner
    )

    assert builds.count() == 1
    build = builds.first()
    assert build.status == ImageBuildJob.Status.PENDING
    assert build.pending_generation_id == generation_id
    assert build.build_task_id == task_id
    assert build.generations.count() == 1
    assert Task.objects.count() == 1
    assert LifecycleCommand.objects.count() == 1
    assert runner.api_token_hash == hash_token("new-token")
    # Re-running activates global services created after the organization.
    assert OrgCredentialServiceActivation.objects.filter(
        organization=org, credential_service__slug="github-token"
    ).exists()


def test_bootstrap_preserves_active_build(db) -> None:
    """If a build already succeeded, bootstrap should not reset it to pending."""
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
    build = ImageBuildJob.objects.get(
        image_definition=definition, runner=runner
    )

    image = build.pending_generation
    task_id = build.build_task_id
    assert ImageGenerationRepository.finish(
        task_id=str(task_id),
        build_job_id=str(build.id),
        runner_id=str(runner.id),
        runner_ref=image.runner_ref,
    )
    build.refresh_from_db()
    built_at = build.built_at

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
    assert build.status == ImageBuildJob.Status.ACTIVE
    assert build.current_generation_id == image.id
    assert build.image_instance.runner_ref == image.runner_ref
    assert build.pending_generation_id is None
    assert build.build_task_id == task_id
    assert build.built_at == built_at
    assert build.generations.count() == 1
    assert Task.objects.count() == 1
    assert LifecycleCommand.objects.count() == 1


def test_bootstrap_retries_failed_build(db) -> None:
    """A failed build gets a new generation and task on next bootstrap."""
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
    build = ImageBuildJob.objects.get(
        image_definition=definition, runner=runner
    )

    failed_image = build.pending_generation
    failed_task = build.build_task
    assert ImageGenerationRepository.finish(
        task_id=str(failed_task.id),
        build_job_id=str(build.id),
        runner_id=str(runner.id),
        error="Build failed",
    )
    build.refresh_from_db()
    assert build.status == ImageBuildJob.Status.FAILED
    assert build.pending_generation_id is None

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
    assert build.status == ImageBuildJob.Status.PENDING
    assert build.build_task_id != failed_task.id
    assert build.build_task.status == TaskStatus.PENDING
    assert build.pending_generation_id != failed_image.id
    assert build.pending_generation.generation == 2
    assert build.pending_generation.creating_task_id == build.build_task_id
    assert build.pending_generation.status == ImageInstance.Status.BUILDING
    assert build.current_generation_id is None
    assert build.built_at is None
    failed_image.refresh_from_db()
    failed_task.refresh_from_db()
    assert failed_image.status == ImageInstance.Status.FAILED
    assert failed_task.status == TaskStatus.FAILED
    assert build.generations.count() == 2
    assert Task.objects.count() == 2
    assert LifecycleCommand.objects.count() == 2
    assert LifecycleCommand.objects.filter(task=build.build_task).exists()
