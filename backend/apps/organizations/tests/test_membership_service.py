"""OrganizationService lookups that other bounded contexts authorize with."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.organizations.models import Membership, MembershipRole, Organization
from apps.organizations.repositories import MembershipRepository
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, NotFoundError


@pytest.fixture
def org_member():
    """Return an organization and an active admin member of it."""
    org = Organization.objects.create(name="Lookup Org", slug="lookup-org")
    user = get_user_model().objects.create_user(
        email=f"member-{uuid.uuid4().hex[:8]}@test.local", password="secret"
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    return org, user


@pytest.mark.django_db
def test_require_organization_returns_existing_and_rejects_unknown(org_member):
    org, _ = org_member
    service = OrganizationService()

    assert service.require_organization(org.id) == org
    unknown = uuid.uuid4()
    with pytest.raises(NotFoundError) as exc_info:
        service.require_organization(unknown)
    assert exc_info.value.message == f"Organization '{unknown}' not found"


@pytest.mark.django_db
def test_active_membership_loads_identity_in_one_query(
    org_member, django_assert_num_queries
):
    org, user = org_member
    service = OrganizationService()

    with django_assert_num_queries(1):
        membership = service.require_active_membership(user.id, org.id)
        assert (membership.user, membership.organization) == (user, org)
        assert membership.role == MembershipRole.ADMIN


@pytest.mark.django_db
def test_deactivated_users_keep_membership_but_lose_active_access(org_member):
    org, user = org_member
    user.is_active = False
    user.save(update_fields=["is_active"])
    service = OrganizationService()

    assert service.get_membership(user.id, org.id) is not None
    assert service.get_active_membership(user.id, org.id) is None
    with pytest.raises(AuthenticationError, match="Active organization membership"):
        service.require_active_membership(user.id, org.id)


@pytest.mark.django_db
def test_non_members_have_no_membership(org_member):
    org, _ = org_member
    outsider = get_user_model().objects.create_user(
        email="outsider@test.local", password="secret"
    )
    service = OrganizationService()

    assert service.get_membership(outsider.id, org.id) is None
    assert service.get_active_membership(outsider.id, org.id) is None
    with pytest.raises(AuthenticationError, match="Active organization membership"):
        service.require_active_membership(outsider.id, org.id)


@pytest.mark.django_db(transaction=True)
def test_membership_lookups_forward_row_locks(org_member, monkeypatch):
    org, user = org_member
    calls = []
    real = MembershipRepository.get_for_user_and_org

    def spy(user_id, org_id, *, lock=False):
        calls.append(lock)
        return real(user_id, org_id, lock=lock)

    monkeypatch.setattr(MembershipRepository, "get_for_user_and_org", spy)
    service = OrganizationService()

    with transaction.atomic():
        assert service.require_active_membership(user.id, org.id, lock=True)
        assert service.get_active_membership(user.id, org.id) is not None
    assert calls == [True, False]
