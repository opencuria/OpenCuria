"""Bootstrap a local all-in-one deployment."""

from __future__ import annotations

import os
import textwrap

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.credentials.models import CredentialService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.organizations.services import OrganizationService
from apps.runners.enums import RuntimeType
from apps.runners.models import ImageDefinition, Runner, RunnerImageBuild
from common.utils import hash_token

# Default packages installed in the workspace image.
DEFAULT_WORKSPACE_PACKAGES = [
    "curl",
    "wget",
    "git",
    "openssh-client",
    "build-essential",
    "ca-certificates",
    "gnupg",
    "lsb-release",
    "software-properties-common",
    "python3",
    "python3-pip",
    "python3-venv",
    "vim",
    "nano",
    "jq",
    "zip",
    "unzip",
    "tini",
]

# Custom Dockerfile fragment appended after package installation.
# Installs Node.js 22.x, GitHub CLI, sets up /workspace with default
# agent instruction files, and configures tini as entrypoint.
DEFAULT_WORKSPACE_CUSTOM_DOCKERFILE = textwrap.dedent("""\
    # Node.js 22.x
    RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
        && apt-get install -y nodejs \\
        && rm -rf /var/lib/apt/lists/*

    # GitHub CLI
    RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \\
          | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \\
        && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \\
        && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \\
          | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \\
        && apt-get update && apt-get install -y gh \\
        && rm -rf /var/lib/apt/lists/*

    WORKDIR /workspace

    # Default agent instruction files
    RUN printf '%s\\n' \\
          '# Agent Environment & Operating Protocol' \\
          '' \\
          'You are operating within a **dedicated, isolated Virtual Machine (VM)**.' \\
          'You have **full administrative access** to this system.' \\
          '' \\
          '## The AGENTS.md Protocol' \\
          '1. **Read on Startup:** Always check this file first.' \\
          '2. **Self-Modification:** You may edit this file.' \\
        > /workspace/AGENTS.md \\
      && printf '%s\\n' \\
          '# Critical Instructions for Claude / AI Agent' \\
          '' \\
          '## Mandatory Initialization' \\
          'Before performing any task, read the AGENTS.md file in the root directory.' \\
        > /workspace/CLAUDE.md

    ENTRYPOINT ["/usr/bin/tini", "--"]
""")

# Default environment variables baked into the workspace image.
_DEFAULT_ENV_VARS = {"PATH": "/root/.local/bin:$" + "{PATH}"}


class Command(BaseCommand):
    """Create the minimum data required for a local docker compose stack."""

    help = (
        "Ensure that a local admin user, organization, runner, and default "
        "workspace image definition exist for the all-in-one compose stack."
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
        build = self._ensure_pending_image_build(
            organization=org,
            user=user,
            runner=runner,
            definition_name=options["image_definition_name"].strip(),
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Local deployment bootstrap complete: "
                f"admin={user.email}, org={org.slug}, runner={runner.name}, "
                f"image_build={build.id} (status={build.status})"
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

    def _ensure_pending_image_build(
        self,
        *,
        organization,
        user,
        runner: Runner,
        definition_name: str,
    ) -> RunnerImageBuild:
        """Seed the default image definition and a pending build record.

        The build is created with status ``pending`` and **no** task or
        artifact.  Once the runner comes online it will pick up the pending
        build, execute it, and create the artifact on success.
        """
        definition, _ = ImageDefinition.objects.get_or_create(
            organization=organization,
            name=definition_name,
            defaults={
                "created_by": user,
                "description": "Default workspace image for the local all-in-one stack.",
                "runtime_type": RuntimeType.DOCKER,
                "base_distro": "ubuntu:22.04",
                "packages": DEFAULT_WORKSPACE_PACKAGES,
                "custom_dockerfile": DEFAULT_WORKSPACE_CUSTOM_DOCKERFILE,
                "env_vars": _DEFAULT_ENV_VARS,
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
        if definition.packages != DEFAULT_WORKSPACE_PACKAGES:
            definition.packages = DEFAULT_WORKSPACE_PACKAGES
            definition_updates.append("packages")
        if definition.custom_dockerfile != DEFAULT_WORKSPACE_CUSTOM_DOCKERFILE:
            definition.custom_dockerfile = DEFAULT_WORKSPACE_CUSTOM_DOCKERFILE
            definition_updates.append("custom_dockerfile")
        if definition.env_vars != _DEFAULT_ENV_VARS:
            definition.env_vars = _DEFAULT_ENV_VARS
            definition_updates.append("env_vars")
        if definition.description != "Default workspace image for the local all-in-one stack.":
            definition.description = (
                "Default workspace image for the local all-in-one stack."
            )
            definition_updates.append("description")
        if definition_updates:
            definition.save(update_fields=definition_updates)

        build, created = RunnerImageBuild.objects.get_or_create(
            image_definition=definition,
            runner=runner,
            defaults={
                "status": RunnerImageBuild.Status.PENDING,
            },
        )

        # If the build already exists and is active (from a previous
        # successful run), leave it alone so workspaces keep working.
        if not created and build.status == RunnerImageBuild.Status.ACTIVE:
            return build

        # For new or non-active builds, ensure they are in pending state
        # so the runner will pick them up.
        if not created and build.status != RunnerImageBuild.Status.PENDING:
            build.status = RunnerImageBuild.Status.PENDING
            build.build_task = None
            build.image_tag = ""
            build.image_path = ""
            build.built_at = None
            build.save(
                update_fields=[
                    "status",
                    "build_task",
                    "image_tag",
                    "image_path",
                    "built_at",
                    "updated_at",
                ]
            )

        return build
