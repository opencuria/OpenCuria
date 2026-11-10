"""Bootstrap a local all-in-one deployment."""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.organizations.models import Membership, MembershipRole, Organization
from apps.organizations.services import OrganizationService
from apps.runners.models import Runner
from common.utils import hash_token


class Command(BaseCommand):
    """Create the minimum data required for a local docker compose stack."""

    help = (
        "Ensure that a local admin user, organization and runner exist for the "
        "all-in-one compose stack."
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
            default=os.getenv("LOCAL_RUNNER_NAME", "local-qemu"),
            help="Name of the local runner record.",
        )
        parser.add_argument(
            "--runner-token",
            default=os.getenv("LOCAL_RUNNER_API_TOKEN", ""),
            help="Plaintext runner token used by the local runner container.",
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
        runner = self._ensure_runner(
            organization=org,
            name=options["runner_name"].strip(),
            runner_token=runner_token,
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Local deployment bootstrap complete: "
                f"admin={user.email}, org={org.slug}, runner={runner.name}"
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
        return org

    def _ensure_runner(self, *, organization, name: str, runner_token: str) -> Runner:
        runner, _ = Runner.objects.get_or_create(
            organization=organization,
            name=name,
            defaults={
                "api_token_hash": hash_token(runner_token),
            },
        )

        update_fields: list[str] = []
        token_hash = hash_token(runner_token)
        if runner.api_token_hash != token_hash:
            runner.api_token_hash = token_hash
            update_fields.append("api_token_hash")
        if update_fields:
            runner.save(update_fields=update_fields)
        return runner
