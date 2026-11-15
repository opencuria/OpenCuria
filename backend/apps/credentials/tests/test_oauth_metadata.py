"""Characterize persisted OAuth integrity rules without database or HTTP access."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.credentials.oauth_metadata import (
    grant_metadata,
    registration_fingerprint,
    registration_metadata_safe,
    validate_oauth_server_url,
)
from common.utils import encrypt_value

ENDPOINT = "https://mcp.example/tenant/mcp"
ISSUER = "https://auth.example/tenant"
CALLBACK = "https://app.example/api/v1/mcp-oauth/callback/"


@pytest.fixture
def registration() -> SimpleNamespace:
    """Registration fields already loaded by the persistence boundary."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        server_url=ENDPOINT,
        issuer=ISSUER,
        callback_url=CALLBACK,
        client_id="client",
        authorization_endpoint=f"{ISSUER}/authorize",
        token_endpoint=f"{ISSUER}/token",
        registration_endpoint=f"{ISSUER}/register",
        token_endpoint_auth_method="none",
        encrypted_client_secret="",
        scopes_supported=["read", "write"],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("server_url", "https://other.example/mcp"),
        ("issuer", "https://other.example/tenant"),
        ("issuer", "http://auth.example"),
        ("authorization_endpoint", "https://user:secret@auth.example/authorize"),
        ("authorization_endpoint", "https://auth.example/authorize?scope=read"),
        ("token_endpoint", "https://auth.example:0/token"),
        ("token_endpoint", "https://auth.example:65536/token"),
        ("token_endpoint", "https://auth.example:bad/token"),
        ("token_endpoint", "https://auth.example/token#fragment"),
        ("token_endpoint", "http://auth.example/token"),
        ("registration_endpoint", "http://auth.example/register"),
        ("client_id", ""),
        ("client_id", None),
        ("token_endpoint_auth_method", "unsupported"),
        ("encrypted_client_secret", "unexpected-secret"),
        ("encrypted_client_secret", None),
        ("scopes_supported", "read write"),
    ],
)
def test_registration_tampering_is_rejected_by_callback_and_commit(
    registration: SimpleNamespace, field: str, value: object
) -> None:
    """Preflight and locked persistence agree on stored registration integrity."""
    setattr(registration, field, value)
    assert not registration_metadata_safe(
        registration, expected_url=ENDPOINT, expected_issuer=ISSUER
    )


@pytest.mark.parametrize(
    ("method", "secret", "valid"),
    [
        ("none", "", True),
        ("none", "encrypted-secret", False),
        ("client_secret_basic", "encrypted-secret", True),
        ("client_secret_post", "encrypted-secret", True),
        ("client_secret_basic", "", False),
        ("client_secret_post", "", False),
    ],
)
def test_registration_authentication_methods(
    registration: SimpleNamespace, method: str, secret: str, valid: bool
) -> None:
    """Confidential clients require secrets; public clients must have none."""
    registration.token_endpoint_auth_method = method
    registration.encrypted_client_secret = secret
    assert registration_metadata_safe(registration, expected_url=ENDPOINT) is valid


def test_callback_binding_and_fingerprint_contract(
    registration: SimpleNamespace,
) -> None:
    """Callback binding is optional; only the existing fingerprint fields are hashed."""
    assert registration_metadata_safe(
        registration, expected_url=ENDPOINT, expected_callback=CALLBACK
    )
    assert not registration_metadata_safe(
        registration, expected_url=ENDPOINT, expected_callback="https://other.example/"
    )
    original = registration_fingerprint(registration)
    assert (
        original == "9d3a13e16ec8df923500613bf2d402b3dc576e31672e1c05881e02c485462b78"
    )
    registration.callback_url = "https://other.example/"
    registration.scopes_supported = []
    assert registration_fingerprint(registration) == original
    for field in (
        "server_url",
        "issuer",
        "client_id",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "token_endpoint_auth_method",
        "encrypted_client_secret",
    ):
        value = getattr(registration, field)
        setattr(registration, field, value + "changed")
        assert registration_fingerprint(registration) != original
        setattr(registration, field, value)


@pytest.fixture
def grant_record(registration: SimpleNamespace) -> tuple[SimpleNamespace, dict]:
    """A bound encrypted bearer grant and the corresponding loaded credential."""
    service_id = uuid.uuid4()
    credential = SimpleNamespace(
        service=SimpleNamespace(oauth_server_url=ENDPOINT),
        service_id=service_id,
        oauth_server_url=ENDPOINT,
        oauth_resource="https://mcp.example/tenant",
        oauth_registration=registration,
        encrypted_value="",
    )
    payload = {
        "access_token": "test-access",
        "refresh_token": "test-refresh",
        "expires_at": "2099-01-02T03:04:05+00:00",
        "resource": credential.oauth_resource,
        "server_url": ENDPOINT,
        "service_id": str(service_id),
        "registration_id": str(registration.id),
        "identity": {"workspace_id": "test-workspace"},
        "scope": "read write",
        "token_type": "Bearer",
    }
    return credential, payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", ""),
        ("access_token", None),
        ("refresh_token", None),
        ("expires_at", 123),
        ("expires_at", "not-a-date"),
        ("identity", []),
        ("identity", {"workspace_id": 123}),
        ("token_type", "Basic"),
        ("token_type", None),
        ("scope", None),
        ("scope", 'read "write"'),
        ("scope", "a" * 2049),
        ("resource", "https://other.example/tenant"),
        ("server_url", "https://other.example/mcp"),
        ("service_id", "another-service"),
        ("registration_id", "another-client"),
    ],
)
def test_invalid_grant_envelopes_are_unavailable(
    grant_record: tuple[SimpleNamespace, dict], field: str, value: object
) -> None:
    """Malformed or differently bound tokens never become usable credentials."""
    credential, payload = grant_record
    payload[field] = value
    credential.encrypted_value = encrypt_value(json.dumps(payload))
    assert grant_metadata(credential) is None


def test_grant_metadata_keeps_expiry_and_binding_rules(
    grant_record: tuple[SimpleNamespace, dict],
) -> None:
    """Expired grants need refresh; naive timestamps retain the configured timezone."""
    credential, payload = grant_record
    expiry = (timezone.now() - timedelta(minutes=1)).replace(tzinfo=None)
    payload["expires_at"] = expiry.isoformat()
    credential.encrypted_value = encrypt_value(json.dumps(payload))
    result = grant_metadata(credential, expected_url=ENDPOINT)
    assert result == {
        **payload,
        "_expires_at": expiry.replace(tzinfo=timezone.get_current_timezone()),
    }
    assert grant_metadata(credential, expected_url="https://other.example/mcp") is None
    payload["refresh_token"] = ""
    credential.encrypted_value = encrypt_value(json.dumps(payload))
    assert grant_metadata(credential) is None
    payload["expires_at"] = ""
    credential.encrypted_value = encrypt_value(json.dumps(payload))
    assert grant_metadata(credential) == {
        **payload,
        "_expires_at": None,
    }
    credential.oauth_server_url = "https://other.example/mcp"
    assert grant_metadata(credential) is None


@pytest.mark.parametrize(
    ("resource", "valid"),
    [
        (ENDPOINT, True),
        ("https://MCP.example:443/tenant/", True),
        ("https://mcp.example/", True),
        ("https://mcp.example/tenant/m", False),
        ("https://mcp.example/tenant/mcp/child", False),
        ("https://mcp.example:444/tenant", False),
        ("https://mcp.example/tenant;v=1", False),
        ("https://mcp.example/tenant?scope=read", False),
        ("https://mcp.example/tenant#fragment", False),
        ("https://user@mcp.example/tenant", False),
        ("http://mcp.example/tenant", False),
    ],
)
def test_grant_resource_origin_and_path_boundaries(
    grant_record: tuple[SimpleNamespace, dict], resource: str, valid: bool
) -> None:
    """Stored resource identifiers cover exact paths or ancestors on one origin."""
    credential, payload = grant_record
    credential.oauth_resource = payload["resource"] = resource
    credential.encrypted_value = encrypt_value(json.dumps(payload))
    assert (grant_metadata(credential) is not None) is valid


@pytest.mark.parametrize("encrypted_value", ["", "invalid-ciphertext"])
def test_unreadable_grants_are_unavailable(
    grant_record: tuple[SimpleNamespace, dict], encrypted_value: str
) -> None:
    """Unreadable stored data fails closed without exposing decryption errors."""
    credential, _payload = grant_record
    credential.encrypted_value = encrypted_value
    assert grant_metadata(credential) is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "http://mcp.example/mcp",
        "https://mcp.example/mcp?",
        "https://mcp.example/mcp#",
        "https://user@mcp.example/mcp",
        "https://mcp.example:0/mcp",
        "https://mcp.example:65536/mcp",
        "https://mcp.example:bad/mcp",
        "https://mcp.example/a b",
    ],
)
def test_invalid_configured_endpoints_are_rejected(value: object) -> None:
    """Configuration validation rejects ambiguous or non-HTTPS endpoints."""
    with pytest.raises(ValueError, match="OAuth MCP endpoint"):
        validate_oauth_server_url(value)


def test_configured_endpoint_normalization() -> None:
    """Configuration strips surrounding space but preserves path and explicit port."""
    assert (
        validate_oauth_server_url(" https://MCP.example:443/tenant/mcp;v=1/ ")
        == "https://MCP.example:443/tenant/mcp;v=1/"
    )
