"""Shared fixtures: an organization with a runner, an admin and a member."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from apps.licensing.services import WindowsLicenseService
from apps.licensing.tests.factories import make_user
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus
from apps.runners.models import Runner
from common.utils import hash_token

# Async tests reach the ORM only through the production sync boundaries.
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(
        name=f"Licensing Org {uuid.uuid4().hex[:6]}",
        slug=f"licensing-{uuid.uuid4().hex[:10]}",
    )


@pytest.fixture
def admin(organization: Organization) -> Any:
    user = make_user("license-admin", first_name="Ada")
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.ADMIN
    )
    return user


@pytest.fixture
def member(organization: Organization) -> Any:
    user = make_user("license-member", first_name="Max")
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    return user


@pytest.fixture
def runner(organization: Organization) -> Runner:
    return Runner.objects.create(
        name="licensing-runner",
        api_token_hash=hash_token(uuid.uuid4().hex),
        status=RunnerStatus.ONLINE,
        sid="licensing-sid",
        organization=organization,
    )


@pytest.fixture
def licenses() -> WindowsLicenseService:
    return WindowsLicenseService()
