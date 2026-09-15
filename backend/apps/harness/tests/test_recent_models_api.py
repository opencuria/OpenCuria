"""Tests for the recent-models endpoints (composer Recent list + effort memory).

Covers: empty GET, PUT upsert ordering + effort overwrite, LRU cap at 10,
org/user scoping, blank-model 400, key permissions (harness:read for GET,
harness:run for PUT), unauthenticated 401, no membership 404.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.models import RecentModel
from apps.harness.repositories import RecentModelRepository
from apps.organizations.models import Membership, MembershipRole, Organization

RECENT_URL = "/api/v1/recent-models/"
READ = [APIKeyPermission.HARNESS_READ.value]
RUN = [APIKeyPermission.HARNESS_RUN.value]
BOTH = READ + RUN


@pytest.fixture
def recent_setup(db):
    """Org + two members + outsider (no API keys; clients built per test)."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Recent {uuid.uuid4().hex[:6]}",
        slug=f"recent-{uuid.uuid4().hex[:10]}",
    )
    other_org = Organization.objects.create(
        name=f"Recent Other {uuid.uuid4().hex[:6]}",
        slug=f"recent-other-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"r-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    member = user_model.objects.create_user(
        email=f"r-member-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    outsider = user_model.objects.create_user(
        email=f"r-outsider-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(user=member, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=owner, organization=other_org, role=MembershipRole.MEMBER
    )
    return {
        "org": org,
        "other_org": other_org,
        "owner": owner,
        "member": member,
        "outsider": outsider,
    }


def _client(*, user, org, permissions: list[str]) -> Client:
    token = f"kai_recent_{uuid.uuid4().hex}"
    APIKey.objects.create(
        user=user,
        name=f"recent-{uuid.uuid4().hex[:6]}",
        key_hash=__import__("common.utils", fromlist=["hash_token"]).hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


def _put(client: Client, model: str, effort: str = ""):
    return client.put(
        RECENT_URL,
        data=json.dumps({"model": model, "effort": effort}),
        content_type="application/json",
    )


@pytest.mark.django_db(transaction=True)
def test_recent_models_empty_list(recent_setup):
    """GET with no usage returns an empty list."""
    client = _client(
        user=recent_setup["owner"], org=recent_setup["org"], permissions=BOTH
    )
    response = client.get(RECENT_URL)
    assert response.status_code == 200, response.content[:500]
    assert response.json() == []


@pytest.mark.django_db(transaction=True)
def test_recent_models_put_upsert_and_ordering(recent_setup):
    """PUT creates rows; re-use bumps to front and overwrites effort."""
    client = _client(
        user=recent_setup["owner"], org=recent_setup["org"], permissions=BOTH
    )
    assert _put(client, "openrouter/a", "low").status_code == 200
    assert _put(client, "openrouter/b", "high").status_code == 200

    response = client.get(RECENT_URL)
    assert response.status_code == 200
    body = response.json()
    assert [row["model"] for row in body] == ["openrouter/b", "openrouter/a"]
    assert body[0]["effort"] == "high"

    # Re-using `a` with a new effort moves it first and updates effort.
    again = _put(client, "openrouter/a", "max")
    assert again.status_code == 200
    assert again.json()["effort"] == "max"
    body = client.get(RECENT_URL).json()
    assert [row["model"] for row in body] == ["openrouter/a", "openrouter/b"]
    assert body[0]["effort"] == "max"
    assert (
        RecentModel.objects.filter(
            organization_id=recent_setup["org"].id,
            user_id=recent_setup["owner"].id,
        ).count()
        == 2
    )


@pytest.mark.django_db(transaction=True)
def test_recent_models_lru_cap(recent_setup):
    """Only the 10 newest rows survive; the UI shows the first 6."""
    org = recent_setup["org"]
    owner = recent_setup["owner"]
    for i in range(12):
        RecentModelRepository.record_usage(org.id, owner, model=f"m-{i:02d}")
    rows = RecentModelRepository.list_by_user(org.id, owner.id)
    assert len(rows) == RecentModelRepository.MAX_ENTRIES == 10
    assert [row.model for row in rows] == [f"m-{i:02d}" for i in range(11, 1, -1)]

    client = _client(user=owner, org=org, permissions=BOTH)
    body = client.get(RECENT_URL).json()
    assert len(body) == 10
    assert all("last_used_at" in row for row in body)


@pytest.mark.django_db(transaction=True)
def test_recent_models_scoped_per_user_and_org(recent_setup):
    """Rows never leak across users or organizations."""
    owner_client = _client(
        user=recent_setup["owner"], org=recent_setup["org"], permissions=BOTH
    )
    assert _put(owner_client, "openrouter/solo", "low").status_code == 200

    member_client = _client(
        user=recent_setup["member"], org=recent_setup["org"], permissions=BOTH
    )
    assert member_client.get(RECENT_URL).json() == []

    other_org_client = _client(
        user=recent_setup["owner"],
        org=recent_setup["other_org"],
        permissions=BOTH,
    )
    assert other_org_client.get(RECENT_URL).json() == []


@pytest.mark.django_db(transaction=True)
def test_recent_models_blank_model_is_400(recent_setup):
    """PUT without a model id is rejected (no silent row)."""
    client = _client(
        user=recent_setup["owner"], org=recent_setup["org"], permissions=BOTH
    )
    response = _put(client, "   ")
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    assert (
        RecentModel.objects.filter(
            organization_id=recent_setup["org"].id,
        ).count()
        == 0
    )


@pytest.mark.django_db(transaction=True)
def test_recent_models_permissions(recent_setup):
    """GET needs harness:read; PUT needs harness:run."""
    read_client = _client(
        user=recent_setup["owner"], org=recent_setup["org"], permissions=READ
    )
    assert read_client.get(RECENT_URL).status_code == 200
    denied_put = _put(read_client, "openrouter/a")
    assert denied_put.status_code == 403
    assert denied_put.json()["code"] == "permission_denied"

    run_client = _client(
        user=recent_setup["owner"], org=recent_setup["org"], permissions=RUN
    )
    assert run_client.get(RECENT_URL).status_code == 403
    assert _put(run_client, "openrouter/a").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_recent_models_unauthenticated_is_401(recent_setup):
    """No credentials yields 401."""
    client = Client(HTTP_X_ORGANIZATION_ID=str(recent_setup["org"].id))
    assert client.get(RECENT_URL).status_code == 401


@pytest.mark.django_db(transaction=True)
def test_recent_models_no_membership_is_404(recent_setup):
    """Non-members get 404 (no org existence leak)."""
    client = _client(
        user=recent_setup["outsider"], org=recent_setup["org"], permissions=BOTH
    )
    assert client.get(RECENT_URL).status_code == 404
    assert _put(client, "openrouter/a").status_code == 404
