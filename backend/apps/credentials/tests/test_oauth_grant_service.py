"""Characterize the locked authorization checks at OAuth grant commit time."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

from apps.credentials.models import (
    Credential,
    CredentialService,
    McpOAuthAuthorizationState,
    McpOAuthClientRegistration,
    OrgCredentialServiceActivation,
)
from apps.credentials.oauth_grant_service import OAuthGrantService
from apps.credentials.oauth_metadata import registration_fingerprint
from apps.credentials.repositories import McpOAuthRepository
from apps.credentials.services import CredentialOAuthSvc
from apps.organizations.models import Membership, Organization
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError
from common.utils import encrypt_value


@pytest.fixture
def grant_state(db: None) -> McpOAuthAuthorizationState:
    """A callback's loaded snapshot and the live rows it must recheck."""
    user = get_user_model().objects.create_user(email="grant@test.local", password="x")
    organization = Organization.objects.create(name="Grant org", slug="grant-org")
    Membership.objects.create(user=user, organization=organization, role="admin")
    endpoint = "https://mcp.example/mcp"
    service = CredentialService.objects.create(
        name="Grant MCP",
        slug="grant-mcp",
        organization=organization,
        credential_type="mcp_oauth",
        oauth_server_url=endpoint,
    )
    OrgCredentialServiceActivation.objects.create(
        organization=organization, credential_service=service
    )
    registration = McpOAuthClientRegistration.objects.create(
        server_url=endpoint,
        callback_url="https://app.example/callback/",
        issuer="https://auth.example",
        client_id="client",
        authorization_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
    )
    return McpOAuthAuthorizationState(
        user=user,
        organization=organization,
        service=service,
        registration=registration,
        registration_fingerprint=registration_fingerprint(registration),
        server_url=endpoint,
        resource=endpoint,
        issuer=registration.issuer,
        redirect_uri=registration.callback_url,
    )


@pytest.mark.parametrize("organization_credential", [False, True])
@pytest.mark.parametrize("name", ["", "Named grant"])
def test_create_preserves_grant_ownership_and_fields(
    grant_state: McpOAuthAuthorizationState, organization_credential: bool, name: str
) -> None:
    """Fresh grants keep their owner, endpoint binding, name and encrypted value."""
    grant_state.organization_credential = organization_credential
    grant_state.credential_name = name
    if not organization_credential:
        Membership.objects.filter(user=grant_state.user).update(role="member")

    credential_id = OAuthGrantService().persist(
        state=grant_state, encrypted_value="encrypted-grant"
    )

    credential = Credential.objects.get(pk=credential_id)
    assert credential.user_id == (
        None if organization_credential else grant_state.user_id
    )
    assert credential.organization_id == (
        grant_state.organization_id if organization_credential else None
    )
    assert credential.created_by_id == grant_state.user_id
    assert credential.name == (name or "Grant MCP OAuth")
    assert credential.service_id == grant_state.service_id
    assert credential.oauth_registration_id == grant_state.registration_id
    assert credential.oauth_server_url == grant_state.server_url
    assert credential.oauth_resource == grant_state.resource
    assert credential.oauth_status == "connected"
    assert credential.encrypted_value == "encrypted-grant"


@pytest.mark.parametrize(
    ("row", "changes", "message"),
    [
        ("membership", None, "OAuth service, registration, or membership changed"),
        (
            "membership",
            {"role": "member"},
            "OAuth service, registration, or membership changed",
        ),
        (
            "user",
            {"is_active": False},
            "OAuth service, registration, or membership changed",
        ),
        ("service", None, "OAuth service, registration, or membership changed"),
        (
            "service",
            {"credential_type": "env_var"},
            "OAuth service, registration, or membership changed",
        ),
        (
            "service",
            {"oauth_server_url": "https://other.example/mcp"},
            "OAuth service, registration, or membership changed",
        ),
        (
            "service",
            {"organization_id": "other"},
            "OAuth service, registration, or membership changed",
        ),
        ("registration", None, "OAuth service, registration, or membership changed"),
        (
            "registration",
            {"token_endpoint": "http://auth.example/token"},
            "OAuth service, registration, or membership changed",
        ),
        (
            "registration",
            {"client_id": "changed"},
            "OAuth service, registration, or membership changed",
        ),
        ("activation", None, "OAuth service is inactive"),
    ],
)
def test_create_rechecks_live_authorization_before_writing(
    grant_state: McpOAuthAuthorizationState,
    row: str,
    changes: dict | None,
    message: str,
) -> None:
    """A callback snapshot never authorizes changed, deleted or inactive rows."""
    grant_state.organization_credential = True
    if row == "service" and changes and "organization_id" in changes:
        other = Organization.objects.create(name="Other", slug="other")
        changes = {"organization_id": other.id}
    rows = {
        "membership": Membership.objects.filter(user=grant_state.user),
        "user": get_user_model().objects.filter(pk=grant_state.user_id),
        "service": CredentialService.objects.filter(pk=grant_state.service_id),
        "registration": McpOAuthClientRegistration.objects.filter(
            pk=grant_state.registration_id
        ),
        "activation": OrgCredentialServiceActivation.objects.filter(
            credential_service_id=grant_state.service_id
        ),
    }
    if changes is None:
        rows[row].delete()
    else:
        rows[row].update(**changes)

    with pytest.raises(ValueError, match=f"^{message}$"):
        OAuthGrantService().persist(
            state=grant_state, encrypted_value="encrypted-grant"
        )
    assert not Credential.objects.filter(service_id=grant_state.service_id).exists()


@pytest.fixture
def reconnect_state(
    grant_state: McpOAuthAuthorizationState,
) -> McpOAuthAuthorizationState:
    """An explicit personal reconnect whose existing secret must survive failure."""
    grant_state.credential = Credential.objects.create(
        user=grant_state.user,
        service=grant_state.service,
        name="Original name",
        encrypted_value="original-secret",
        oauth_server_url=grant_state.server_url,
        created_by=grant_state.user,
    )
    grant_state.reconnect_existing = True
    grant_state.credential_server_url_snapshot = grant_state.server_url
    return grant_state


@pytest.mark.parametrize("organization_credential", [False, True])
@pytest.mark.parametrize("name", ["", "Renamed grant"])
def test_reconnect_updates_only_the_explicit_target_even_if_service_is_inactive(
    reconnect_state: McpOAuthAuthorizationState,
    name: str,
    organization_credential: bool,
) -> None:
    """Reconnect preserves identity and ownership and needs no fresh activation."""
    state = reconnect_state
    state.credential_name = name
    state.organization_credential = organization_credential
    if organization_credential:
        Credential.objects.filter(pk=state.credential_id).update(
            user=None, organization=state.organization
        )
    OrgCredentialServiceActivation.objects.filter(
        credential_service_id=state.service_id
    ).delete()

    credential_id = OAuthGrantService().persist(
        state=state, encrypted_value="replacement-secret"
    )

    assert credential_id == state.credential_id
    credential = Credential.objects.get(pk=credential_id)
    assert credential.name == (name or "Original name")
    assert credential.user_id == (None if organization_credential else state.user_id)
    assert credential.organization_id == (
        state.organization_id if organization_credential else None
    )
    assert credential.encrypted_value == "replacement-secret"
    assert credential.oauth_resource == state.resource
    assert credential.oauth_registration_id == state.registration_id
    assert credential.oauth_status == "connected"
    assert Credential.objects.filter(service_id=state.service_id).count() == 1


@pytest.mark.parametrize("change", ["missing", "deleted", "owner", "service", "url"])
def test_reconnect_rejects_changed_targets_without_replacing_secrets(
    reconnect_state: McpOAuthAuthorizationState, change: str
) -> None:
    """Reconnect cannot adopt a different credential or recreate a deleted one."""
    state = reconnect_state
    target = Credential.objects.filter(pk=state.credential_id)
    if change == "missing":
        state.credential_id = None
    elif change == "deleted":
        target.delete()
    elif change == "owner":
        target.update(user=None, organization=state.organization)
    elif change == "service":
        other = CredentialService.objects.create(name="Other", slug="other")
        target.update(service=other)
    else:
        target.update(oauth_server_url="https://other.example/mcp")

    with pytest.raises(
        ValueError, match="^OAuth reconnect target was deleted or changed$"
    ):
        OAuthGrantService().persist(state=state, encrypted_value="replacement-secret")
    assert not Credential.objects.filter(encrypted_value="replacement-secret").exists()


@pytest.mark.parametrize("reconnect", [False, True])
def test_failed_grant_write_rolls_back_all_changes(
    reconnect_state: McpOAuthAuthorizationState,
    monkeypatch: pytest.MonkeyPatch,
    reconnect: bool,
) -> None:
    """A failed create or reconnect cannot leave a partial credential update."""
    state = reconnect_state
    state.reconnect_existing = reconnect
    before = list(Credential.objects.values())
    original = Credential.save

    def fail_after_write(credential: Credential, *args: Any, **kwargs: Any) -> None:
        original(credential, *args, **kwargs)
        raise RuntimeError("storage failure after write")

    monkeypatch.setattr(Credential, "save", fail_after_write)
    with pytest.raises(RuntimeError, match="storage failure after write"):
        OAuthGrantService().persist(state=state, encrypted_value="replacement-secret")
    assert list(Credential.objects.values()) == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("reconnect", [False, True])
def test_authorization_rows_are_locked_in_one_transaction_before_writing(
    reconnect_state: McpOAuthAuthorizationState,
    monkeypatch: pytest.MonkeyPatch,
    reconnect: bool,
) -> None:
    """Finalization retains its lock order and transaction across context boundaries."""
    state = reconnect_state
    state.reconnect_existing = reconnect
    assert not connection.in_atomic_block
    reads = []

    def checked_read(name: str, read: Callable[..., Any]) -> Callable[..., Any]:
        def locked(*args: Any, **kwargs: Any) -> Any:
            assert connection.in_atomic_block
            assert kwargs.get("lock") is True
            reads.append(name)
            return read(*args, **kwargs)

        return locked

    for name in (
        "get_service",
        "get_registration",
        "get_reconnect_target",
        "service_active_for_org",
    ):
        monkeypatch.setattr(
            McpOAuthRepository,
            name,
            staticmethod(checked_read(name, getattr(McpOAuthRepository, name))),
        )
    monkeypatch.setattr(
        OrganizationService,
        "get_active_membership",
        checked_read("membership", OrganizationService.get_active_membership),
    )

    OAuthGrantService().persist(state=state, encrypted_value="replacement-secret")

    assert not connection.in_atomic_block
    assert reads == [
        "get_service",
        "get_registration",
        "membership",
        "get_reconnect_target" if reconnect else "service_active_for_org",
    ]


@pytest.mark.parametrize("changed_row", ["membership", "registration", "activation"])
def test_callback_rechecks_authorization_after_the_token_exchange(
    grant_state: McpOAuthAuthorizationState,
    monkeypatch: pytest.MonkeyPatch,
    changed_row: str,
) -> None:
    """Commit-time rejection retains the callback's authentication error contract."""
    state = grant_state
    state.encrypted_code_verifier = encrypt_value("verifier")
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._consume_callback_state", lambda *_args: state
    )

    def exchange(*_args: Any, **_kwargs: Any) -> dict:
        if changed_row == "membership":
            Membership.objects.filter(user=state.user).delete()
        elif changed_row == "registration":
            McpOAuthClientRegistration.objects.filter(pk=state.registration_id).update(
                client_id="changed-client"
            )
        else:
            OrgCredentialServiceActivation.objects.filter(
                credential_service_id=state.service_id
            ).delete()
        return {"access_token": "access", "refresh_token": "refresh"}

    monkeypatch.setattr("apps.credentials.mcp_oauth._post_token", exchange)
    expected = (
        "OAuth service is inactive"
        if changed_row == "activation"
        else "OAuth service, registration, or membership changed"
    )
    with pytest.raises(AuthenticationError, match=f"^{expected}$") as error:
        CredentialOAuthSvc().complete_authorization(
            raw_state="state", binding="binding", code="code"
        )
    assert error.value.code == "authentication_error"
    assert not Credential.objects.filter(service_id=state.service_id).exists()
