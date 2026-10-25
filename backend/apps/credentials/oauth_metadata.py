"""Integrity rules for OAuth endpoints, registrations and persisted grant metadata.

Callers supply loaded records. Protocol workflows and locked persistence share
these rules without importing each other or reaching the database here.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlsplit

from django.utils import timezone

from common.utils import decrypt_value

if TYPE_CHECKING:
    from .models import Credential, McpOAuthClientRegistration


def validate_oauth_server_url(value: str) -> str:
    """Validate a fixed public HTTPS MCP URL without plugin dependencies."""
    if not isinstance(value, str):
        raise ValueError("OAuth MCP endpoint must be an HTTPS URL")
    url = value.strip()
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("OAuth MCP endpoint has an invalid port") from None
    if (
        not url
        or any(ord(char) < 0x21 for char in url)
        or parsed.scheme.lower() != "https"
        or not parsed.hostname
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or "@" in parsed.netloc
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(
            "OAuth MCP endpoint must be an HTTPS URL without query, "
            "userinfo, or fragment"
        )
    return url


def is_safe_registration_url(value: str) -> bool:
    """Check persisted registration URLs without DNS or network access."""
    try:
        parsed = urlparse(value)
        port = parsed.port
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.netloc
            and not parsed.username
            and not parsed.password
            and "@" not in parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and (port is None or 1 <= port <= 65535)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def registration_metadata_safe(
    registration: McpOAuthClientRegistration,
    *,
    expected_url: str,
    expected_issuer: str | None = None,
    expected_callback: str | None = None,
) -> bool:
    """Validate persisted provider endpoints and token authentication metadata."""
    try:
        method = registration.token_endpoint_auth_method
        secret = registration.encrypted_client_secret
        return bool(
            registration.server_url == expected_url
            and (
                expected_callback is None
                or registration.callback_url == expected_callback
            )
            and isinstance(registration.issuer, str)
            and (expected_issuer is None or registration.issuer == expected_issuer)
            and is_safe_registration_url(registration.issuer)
            and is_safe_registration_url(registration.authorization_endpoint)
            and is_safe_registration_url(registration.token_endpoint)
            and (
                not registration.registration_endpoint
                or is_safe_registration_url(registration.registration_endpoint)
            )
            and isinstance(registration.client_id, str)
            and registration.client_id
            and method in {"none", "client_secret_basic", "client_secret_post"}
            and isinstance(secret, str)
            and (
                (method == "none" and not secret) or (method != "none" and bool(secret))
            )
            and isinstance(registration.scopes_supported, list)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def registration_fingerprint(registration: McpOAuthClientRegistration) -> str:
    """Hash security-sensitive registration metadata for callback integrity."""
    values = [
        registration.server_url,
        registration.issuer,
        registration.client_id,
        registration.authorization_endpoint,
        registration.token_endpoint,
        registration.registration_endpoint,
        registration.token_endpoint_auth_method,
        registration.encrypted_client_secret,
    ]
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def _grant_resource_matches(endpoint: str, resource: str) -> bool:
    """Check a persisted grant resource against its configured MCP endpoint."""
    if not is_safe_registration_url(endpoint) or not is_safe_registration_url(resource):
        return False
    root, candidate = urlsplit(endpoint), urlsplit(resource)
    if root.hostname.lower() != candidate.hostname.lower() or (root.port or 443) != (
        candidate.port or 443
    ):
        return False
    root_path = (root.path or "/").rstrip("/") or "/"
    resource_path = (candidate.path or "/").rstrip("/") or "/"
    return root_path == resource_path or root_path.startswith(
        resource_path.rstrip("/") + "/"
    )


def grant_metadata(
    credential: Credential, *, expected_url: str | None = None
) -> dict | None:
    """Validate registration and grant envelope; keep grant material internal."""
    try:
        endpoint = credential.service.oauth_server_url
        registration = credential.oauth_registration
        if (
            not isinstance(endpoint, str)
            or not endpoint
            or credential.oauth_server_url != endpoint
            or (expected_url is not None and endpoint != expected_url)
            or registration is None
            or not registration_metadata_safe(registration, expected_url=endpoint)
        ):
            return None
        grant = json.loads(decrypt_value(credential.encrypted_value))
        if not isinstance(grant, dict):
            return None
        access, refresh = grant.get("access_token"), grant.get("refresh_token", "")
        resource, server_url = grant.get("resource"), grant.get("server_url")
        expiry_text, identity = (
            grant.get("expires_at", ""),
            grant.get("identity", {}),
        )
        token_type = grant.get("token_type", "Bearer")
        scope = grant.get("scope", "")
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not isinstance(expiry_text, str)
            or not isinstance(identity, dict)
            or any(not isinstance(value, str) for value in identity.values())
            or token_type.lower() != "bearer"
            or not isinstance(scope, str)
            or not isinstance(resource, str)
            or server_url != endpoint
            or resource != credential.oauth_resource
            or not _grant_resource_matches(endpoint, resource)
            or grant.get("service_id") != str(credential.service_id)
            or grant.get("registration_id") != str(registration.id)
        ):
            return None
        if len(scope) > 2048 or any(
            not re.fullmatch(r"[\x21\x23-\x5B\x5D-\x7E]+", part)
            for part in scope.split()
        ):
            return None
        expiry = datetime.fromisoformat(expiry_text) if expiry_text else None
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.get_current_timezone())
        if expiry is not None and expiry <= timezone.now() and not refresh:
            return None
        grant["_expires_at"] = expiry
        return grant
    except Exception:
        return None
