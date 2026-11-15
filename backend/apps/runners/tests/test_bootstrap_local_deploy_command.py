"""Tests for the local deployment bootstrap management command."""

from __future__ import annotations

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from apps.credentials.models import CredentialService, OrgCredentialServiceActivation
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.models import (
    ImageBuildJob,
    ImageDefinition,
    ImageInstance,
    LifecycleCommand,
    Runner,
    Task,
)
from common.utils import hash_token


def _bootstrap(*, email: str, org: str, runner: str, token: str, **kwargs) -> None:
    call_command(
        "bootstrap_local_deploy",
        "--admin-email",
        email,
        "--admin-password",
        "admin",
        "--organization-name",
        org,
        "--runner-name",
        runner,
        "--runner-token",
        token,
        **kwargs,
    )


def _assert_no_images() -> None:
    """Installation seeds no org recipe and requests no build.

    Only the global recipe templates from migrations remain; they stay unbuilt.
    """
    assert not ImageDefinition.objects.filter(organization__isnull=False).exists()
    assert ImageBuildJob.objects.count() == 0
    assert ImageInstance.objects.count() == 0
    assert Task.objects.count() == 0
    assert LifecycleCommand.objects.count() == 0


def test_bootstrap_creates_admin_org_and_runner_without_images(db) -> None:
    """Fresh bootstrap creates the control-plane records only."""
    out = StringIO()
    services_before = set(CredentialService.objects.values_list("id", flat=True))

    _bootstrap(
        email="local-admin@example.com",
        org="Local Dev",
        runner="local-runner",
        token="runner-secret",
        stdout=out,
    )

    user = get_user_model().objects.get(email="local-admin@example.com")
    org = Organization.objects.get(slug="local-dev")
    runner = Runner.objects.get(name="local-runner", organization=org)
    membership = Membership.objects.get(user=user, organization=org)

    assert user.is_superuser is True
    assert membership.role == MembershipRole.ADMIN
    assert runner.api_token_hash == hash_token("runner-secret")
    # Global services come from migrations; the new org gets them activated.
    assert set(CredentialService.objects.values_list("id", flat=True)) == (
        services_before
    )
    assert OrgCredentialServiceActivation.objects.filter(
        organization=org, credential_service__slug="github"
    ).exists()
    _assert_no_images()
    assert "Local deployment bootstrap complete" in out.getvalue()


def test_bootstrap_is_idempotent(db) -> None:
    """Running bootstrap twice should not create duplicate records."""
    _bootstrap(
        email="repeat@example.com",
        org="Repeat Org",
        runner="repeat-runner",
        token="runner-secret",
    )
    org = Organization.objects.get(slug="repeat-org")
    OrgCredentialServiceActivation.objects.filter(
        organization=org, credential_service__slug="github"
    ).delete()
    _bootstrap(
        email="repeat@example.com",
        org="Repeat Org",
        runner="repeat-runner",
        token="new-token",
    )

    runner = Runner.objects.get(name="repeat-runner", organization=org)

    assert Organization.objects.count() == 1
    assert Runner.objects.count() == 1
    assert runner.api_token_hash == hash_token("new-token")
    # Re-running keeps services the admin deactivated switched off.
    assert not OrgCredentialServiceActivation.objects.filter(
        organization=org, credential_service__slug="github"
    ).exists()
    _assert_no_images()


def test_bootstrap_requires_runner_token(db) -> None:
    """A blank runner token fails before any record is written."""
    with pytest.raises(CommandError, match="--runner-token is required"):
        _bootstrap(
            email="blank@example.com",
            org="Blank Org",
            runner="blank-runner",
            token="  ",
        )
    assert Runner.objects.count() == 0
