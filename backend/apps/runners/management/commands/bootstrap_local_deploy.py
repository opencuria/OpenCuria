"""Bootstrap a local all-in-one deployment."""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.credentials.models import CredentialService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.organizations.services import OrganizationService
from apps.runners.enums import RuntimeType
from apps.runners.models import ImageArtifact, ImageDefinition, Runner, RunnerImageBuild
from common.utils import hash_token


class Command(BaseCommand):
    """Create the minimum data required for a local docker compose stack."""

    help = (
        "Ensure that a local admin user, organization, runner, and default "
        "workspace image artifact exist for the all-in-one compose stack."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--admin-email",
            default=os.getenv("LOCAL_ADMIN_EMAIL", "admin@localhost.test"),
            help="Email address of the local admin user.",
        )
        parser.add_argument(
            "--admin-password",
            default=os.getenv(
                "LOCAL_ADMIN_PASSWORD",
                "CHANGE_ME_local_admin_password",
            ),
            help="Password for the local admin user.",
        )
        parser.add_argument(
            "--organization-name",
            default=os.getenv("LOCAL_ORGANIZATION_NAME", "Local"),
            help="Name of the local default organization.",
        )
        parser.add_argument(
            "--runner-name",
            default=os.getenv("LOCAL_RUNNER_NAME", "local-docker"),
            help="Name of the local runner record.",
        )
        parser.add_argument(
            "--runner-token",
            default=os.getenv("LOCAL_RUNNER_API_TOKEN", ""),
            help="Plaintext runner token used by the local runner container.",
        )
        parser.add_argument(
            "--workspace-image",
            default=os.getenv(
                "LOCAL_WORKSPACE_IMAGE",
                "ghcr.io/opencuria/workspace:latest",
            ),
            help="Docker image tag for locally created workspaces.",
        )
        parser.add_argument(
            "--image-definition-name",
            default=os.getenv(
                "LOCAL_IMAGE_DEFINITION_NAME",
                "Local Docker Workspace",
            ),
            help="Display name of the seeded default image definition.",
        )

    def handle(self, *args, **options) -> None:
        runner_token = options["runner_token"].strip()
        if not runner_token:
            raise CommandError("--runner-token is required")

        user = self._ensure_user(
            email=options["admin_email"].strip(),
            password=options["admin_password"],
        )
        org = self._ensure_organization(
            name=options["organization_name"].strip(),
            user=user,
        )
        self._ensure_credential_services()
        runner = self._ensure_runner(
            organization=org,
            name=options["runner_name"].strip(),
            runner_token=runner_token,
        )
        artifact = self._ensure_workspace_artifact(
            organization=org,
            user=user,
            runner=runner,
            definition_name=options["image_definition_name"].strip(),
            workspace_image=options["workspace_image"].strip(),
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Local deployment bootstrap complete: "
                f"admin={user.email}, org={org.slug}, runner={runner.name}, "
                f"image_artifact={artifact.id}"
            )
        )

    def _ensure_user(self, *, email: str, password: str):
        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            email=email,
            defaults={
                "username": email,
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created:
            user.set_password(password)
            user.save(update_fields=["password"])
        else:
            updated_fields: list[str] = []
            if not user.is_staff:
                user.is_staff = True
                updated_fields.append("is_staff")
            if not user.is_superuser:
                user.is_superuser = True
                updated_fields.append("is_superuser")
            if password:
                user.set_password(password)
                updated_fields.append("password")
            if updated_fields:
                user.save(update_fields=updated_fields)
        return user

    def _ensure_organization(self, *, name: str, user):
        org_service = OrganizationService()
        slug = org_service._generate_slug(name)
        org = Organization.objects.filter(slug=slug).first()
        if org is None:
            return org_service.create_organization(name=name, user=user)

        Membership.objects.get_or_create(
            user=user,
            organization=org,
            defaults={"role": MembershipRole.ADMIN},
        )
        Membership.objects.filter(
            user=user,
            organization=org,
        ).exclude(role=MembershipRole.ADMIN).update(role=MembershipRole.ADMIN)
        org_service._seed_activations(org)
        return org

    def _ensure_credential_services(self) -> None:
        CredentialService.objects.get_or_create(
            slug="github-token",
            defaults={
                "name": "GitHub Token",
                "credential_type": "env",
                "env_var_name": "GITHUB_TOKEN",
                "label": "GitHub PAT",
            },
        )

    def _ensure_runner(self, *, organization, name: str, runner_token: str) -> Runner:
        runner, _ = Runner.objects.get_or_create(
            organization=organization,
            name=name,
            defaults={
                "api_token_hash": hash_token(runner_token),
                "available_runtimes": [RuntimeType.DOCKER],
            },
        )

        update_fields: list[str] = []
        token_hash = hash_token(runner_token)
        if runner.api_token_hash != token_hash:
            runner.api_token_hash = token_hash
            update_fields.append("api_token_hash")
        if runner.available_runtimes != [RuntimeType.DOCKER]:
            runner.available_runtimes = [RuntimeType.DOCKER]
            update_fields.append("available_runtimes")
        if update_fields:
            runner.save(update_fields=update_fields)
        return runner

    def _ensure_workspace_artifact(
        self,
        *,
        organization,
        user,
        runner: Runner,
        definition_name: str,
        workspace_image: str,
    ) -> ImageArtifact:
        definition, _ = ImageDefinition.objects.get_or_create(
            organization=organization,
            name=definition_name,
            defaults={
                "created_by": user,
                "description": "Default workspace image for the local all-in-one stack.",
                "runtime_type": RuntimeType.DOCKER,
                "base_distro": "ubuntu:22.04",
            },
        )
        definition_updates: list[str] = []
        if definition.created_by_id is None:
            definition.created_by = user
            definition_updates.append("created_by")
        if definition.runtime_type != RuntimeType.DOCKER:
            definition.runtime_type = RuntimeType.DOCKER
            definition_updates.append("runtime_type")
        if definition.base_distro != "ubuntu:22.04":
            definition.base_distro = "ubuntu:22.04"
            definition_updates.append("base_distro")
        if definition.description != "Default workspace image for the local all-in-one stack.":
            definition.description = (
                "Default workspace image for the local all-in-one stack."
            )
            definition_updates.append("description")
        if definition_updates:
            definition.save(update_fields=definition_updates)

        build, _ = RunnerImageBuild.objects.get_or_create(
            image_definition=definition,
            runner=runner,
            defaults={
                "status": RunnerImageBuild.Status.ACTIVE,
                "image_tag": workspace_image,
                "built_at": timezone.now(),
            },
        )

        build_updates: list[str] = []
        if build.status != RunnerImageBuild.Status.ACTIVE:
            build.status = RunnerImageBuild.Status.ACTIVE
            build_updates.append("status")
        if build.image_tag != workspace_image:
            build.image_tag = workspace_image
            build_updates.append("image_tag")
        if build.image_path:
            build.image_path = ""
            build_updates.append("image_path")
        if build.built_at is None:
            build.built_at = timezone.now()
            build_updates.append("built_at")
        if build_updates:
            build.save(update_fields=build_updates)

        artifact, _ = ImageArtifact.objects.get_or_create(
            runner_image_build=build,
            defaults={
                "created_by": user,
                "artifact_kind": ImageArtifact.ArtifactKind.BUILT,
                "runner_artifact_id": workspace_image,
                "name": definition_name,
                "status": ImageArtifact.ArtifactStatus.READY,
            },
        )

        artifact_updates: list[str] = []
        if artifact.created_by_id is None:
            artifact.created_by = user
            artifact_updates.append("created_by")
        if artifact.artifact_kind != ImageArtifact.ArtifactKind.BUILT:
            artifact.artifact_kind = ImageArtifact.ArtifactKind.BUILT
            artifact_updates.append("artifact_kind")
        if artifact.runner_artifact_id != workspace_image:
            artifact.runner_artifact_id = workspace_image
            artifact_updates.append("runner_artifact_id")
        if artifact.name != definition_name:
            artifact.name = definition_name
            artifact_updates.append("name")
        if artifact.status != ImageArtifact.ArtifactStatus.READY:
            artifact.status = ImageArtifact.ArtifactStatus.READY
            artifact_updates.append("status")
        if artifact_updates:
            artifact.save(update_fields=artifact_updates)

        return artifact
