"""Query-growth and binding regressions for provider-account reads."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import APIKey, User
from apps.credentials.models import CredentialService
from apps.credentials.services import CredentialSvc
from apps.harness.models import ProviderConnection, ProviderSelection
from apps.harness.provider_accounts import ProviderAccountService
from apps.organizations.models import Membership, Organization
from common.exceptions import AuthenticationError, NotFoundError
from common.utils import encrypt_value, hash_token

Reader = tuple[Organization, User, ProviderAccountService]


@pytest.fixture
def reader(organization: Organization) -> Reader:
    """A member reading a mix of shared and private accounts."""
    user = get_user_model().objects.create_user(email="reader@example.invalid")
    Membership.objects.create(user=user, organization=organization)
    return organization, user, ProviderAccountService()


def _account(
    org: Organization, user: User, *, provider: str = "openrouter", shared: bool = False
) -> ProviderConnection:
    """Create a correctly scoped synthetic account with an encrypted secret."""
    secret = CredentialSvc().create_provider_secret(
        organization_id=org.id,
        user_id=None if shared else user.id,
        provider=provider,
        name="Synthetic account",
        value={"api_key": "sk-private-do-not-disclose"},
        created_by=user,
    )
    return ProviderConnection.objects.create(
        organization=org,
        user=None if shared else user,
        provider=provider,
        label=uuid.uuid4().hex,
        credential=secret,
    )


@pytest.mark.django_db
def test_rest_and_selection_queries_do_not_grow_with_account_count(
    reader: Reader,
) -> None:
    """Full REST reads keep constant SQL work across providers and scopes."""
    org, user, service = reader
    token = "synthetic-account-query-test"
    APIKey.objects.create(
        user=user,
        name="Query test",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=["harness:providers"],
    )
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))
    providers = [
        "openrouter",
        "chatgpt",
        "amazon-bedrock",
        "openai-compatible",
        "claude-agent",
    ]
    accounts = []
    query_counts = []
    selection_counts = []
    for count in [1, 30]:
        for index in range(len(accounts), count):
            accounts.append(
                _account(
                    org,
                    user,
                    provider=providers[index % len(providers)],
                    shared=index % 2 == 0,
                )
            )
        service.selected_accounts(org.id, user.id)  # Initialize absent choices.
        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/v1/provider-config/accounts/")
        assert response.status_code == 200
        rows = response.json()
        assert {row["id"] for row in rows} == {str(row.id) for row in accounts}
        assert all(row["api_key_hint"] == "••••lose" for row in rows)
        assert "sk-private-do-not-disclose" not in response.content.decode()
        assert all(row["can_manage"] == (row["scope"] == "personal") for row in rows)
        assert sum(row["is_active"] for row in rows) == min(count, len(providers))
        query_counts.append(len(queries))
        with CaptureQueriesContext(connection) as selections:
            service.selected_accounts(org.id, user.id)
        selection_counts.append(len(selections))
    assert query_counts[0] == query_counts[1]
    assert query_counts[1] <= 14
    assert max(selection_counts) <= 8


@pytest.mark.django_db
def test_filtered_list_initializes_other_providers_without_reading_their_secrets(
    reader: Reader,
) -> None:
    """Filtering changes rows, preserving first-selection side effects."""
    org, user, service = reader
    visible = _account(org, user)
    other = _account(org, user, provider="chatgpt")
    # Listing one provider never decoded the other provider's broken secret.
    other.credential.encrypted_value = "invalid-ciphertext"
    other.credential.save(update_fields=["encrypted_value"])
    rows = service.list_accounts(org.id, user.id, provider="openrouter")
    assert [row["id"] for row in rows] == [str(visible.id)]
    assert (
        ProviderSelection.objects.get(
            organization=org, user=user, provider="chatgpt"
        ).account_id
        == other.id
    )


@pytest.mark.django_db
@pytest.mark.parametrize("revoked", [False, True])
def test_bulk_selection_read_preserves_concurrent_empty_choice(
    reader: Reader, monkeypatch: pytest.MonkeyPatch, revoked: bool
) -> None:
    """Initialization must honor an empty choice written after the batch read."""
    org, user, service = reader
    _account(org, user)
    initialize = service.repository.initialize_selection

    def write_empty_then_initialize(
        organization_id: uuid.UUID, user_id: int, provider: str, account_id: uuid.UUID
    ) -> ProviderSelection:
        ProviderSelection.objects.create(
            organization_id=organization_id,
            user_id=user_id,
            provider=provider,
            account=None,
            is_revoked=revoked,
        )
        return initialize(organization_id, user_id, provider, account_id)

    monkeypatch.setattr(
        service.repository, "initialize_selection", write_empty_then_initialize
    )
    assert service.selected_accounts(org.id, user.id) == {}
    choice = ProviderSelection.objects.get(organization=org, user=user)
    assert choice.account_id is None
    assert choice.is_revoked == revoked


@pytest.mark.django_db
def test_batch_secrets_exclude_foreign_private_and_deleted_accounts(
    reader: Reader,
) -> None:
    """Untrusted IDs cannot expand the batch's user or organization scope."""
    org, user, service = reader
    other = get_user_model().objects.create_user(email="other@example.invalid")
    foreign_org = Organization.objects.create(name="Other", slug="other")
    shared = _account(org, user, shared=True)
    personal = _account(org, user)
    private = _account(org, other)
    foreign = _account(foreign_org, user)
    deleted = _account(org, user)
    ProviderConnection.objects.filter(pk=deleted.id).update(deleted_at=timezone.now())
    ids = [row.id for row in [shared, personal, private, foreign, deleted]]
    secrets = CredentialSvc().get_provider_secrets(
        organization_id=org.id, user_id=user.id, account_ids=ids
    )
    assert set(secrets) == {shared.id, personal.id}
    rows = service.list_accounts(org.id, user.id)
    assert {row["id"] for row in rows} == {str(shared.id), str(personal.id)}


@pytest.mark.django_db
@pytest.mark.parametrize("invalid", ["provider", "owner", "org", "type", "slug"])
def test_single_and_batch_secrets_reject_malformed_bindings(
    reader: Reader, invalid: str
) -> None:
    """Both read paths enforce the same live credential/account binding."""
    org, user, service = reader
    account = _account(org, user)
    if invalid == "provider":
        # A valid account also references this credential: predicates must use
        # the same reverse join, rather than borrowing that account's scope.
        ProviderConnection.objects.create(
            organization=org,
            user=user,
            provider="openrouter",
            label="Valid sibling",
            credential=account.credential,
        )
        account.provider = "chatgpt"
        account.save(update_fields=["provider"])
    elif invalid == "owner":
        account.user = None
        account.save(update_fields=["user"])
    elif invalid == "org":
        account.organization = Organization.objects.create(name="Other", slug="other")
        Membership.objects.create(user=user, organization=account.organization)
        account.save(update_fields=["organization"])
    else:
        field = "credential_type" if invalid == "type" else "slug"
        CredentialService.objects.filter(pk=account.credential.service_id).update(
            **{field: "invalid"}
        )
    with pytest.raises(NotFoundError):
        CredentialSvc().get_provider_secret(
            account.credential_id,
            organization_id=account.organization_id,
            user_id=account.user_id,
            provider=account.provider,
            account_id=account.id,
        )
    assert (
        CredentialSvc().get_provider_secrets(
            organization_id=account.organization_id,
            user_id=user.id,
            account_ids=[account.id],
        )
        == {}
    )
    with pytest.raises(NotFoundError):
        service.list_accounts(account.organization_id, user.id)


@pytest.mark.django_db
@pytest.mark.parametrize("payload", ["not-json", "[]", "null"])
def test_batch_secret_json_fallback_matches_single_read(
    reader: Reader, payload: str
) -> None:
    """Invalid JSON or a non-object secret still produces empty metadata."""
    org, user, service = reader
    account = _account(org, user)
    account.credential.encrypted_value = encrypt_value(payload)
    account.credential.save(update_fields=["encrypted_value"])
    assert service.get_credentials(account) == {}
    assert CredentialSvc().get_provider_secrets(
        organization_id=org.id, user_id=user.id, account_ids=[account.id]
    ) == {account.id: {}}
    assert service.list_accounts(org.id, user.id)[0]["api_key_hint"] == ""


@pytest.mark.django_db
def test_account_reads_recheck_current_membership_and_secret_revisions(
    reader: Reader,
) -> None:
    """Batches are request-local and retain the locked membership gate."""
    org, user, service = reader
    account = _account(org, user)
    assert service.list_accounts(org.id, user.id)[0]["api_key_hint"] == "••••lose"
    service.update_credentials(account.id, {"api_key": "rotated-value-1234"})
    assert service.list_accounts(org.id, user.id)[0]["api_key_hint"] == "••••1234"
    user.is_active = False
    user.save(update_fields=["is_active"])
    with pytest.raises(AuthenticationError):
        service.list_accounts(org.id, user.id)


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_reads_share_one_selection(reader: Reader) -> None:
    """Independent requests serialize initialization under the existing fence."""
    org, user, _service = reader
    first = _account(org, user, shared=True)
    _account(org, user)
    ready = Barrier(2)

    def read() -> list[dict]:
        close_old_connections()
        try:
            ready.wait(timeout=10)
            return ProviderAccountService().list_accounts(org.id, user.id)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reads = [pool.submit(read) for _ in range(2)]
        results = [request.result(timeout=20) for request in reads]
    assert results[0] == results[1]
    choices = ProviderSelection.objects.filter(organization=org, user=user)
    assert choices.count() == 1
    assert choices.get().account_id == first.id


@pytest.mark.django_db
def test_empty_and_disconnected_account_reads(reader: Reader) -> None:
    """Empty batches avoid SQL and a missing credential remains disconnected."""
    org, user, service = reader
    with CaptureQueriesContext(connection) as queries:
        assert (
            CredentialSvc().get_provider_secrets(
                organization_id=org.id, user_id=user.id, account_ids=[]
            )
            == {}
        )
    assert not queries
    assert service.list_accounts(org.id, user.id) == []
    account = ProviderConnection.objects.create(
        organization=org, user=user, provider="openrouter", label="Disconnected"
    )
    (row,) = service.list_accounts(org.id, user.id)
    assert row["id"] == str(account.id)
    assert row["connected"] is False
    assert row["api_key_hint"] == ""
