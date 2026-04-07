"""
Optional external SSO integration (Keycloak-compatible OIDC).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

import jwt
from django.conf import settings

from common.exceptions import AuthenticationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SsoProviderConfig:
    """Normalized SSO provider configuration."""

    enabled: bool
    provider: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    client_id: str
    scope: str
    supports_pkce: bool = True


def get_sso_provider_config() -> SsoProviderConfig | None:
    """Return provider configuration when SSO is fully configured and enabled."""
    if not getattr(settings, "SSO_ENABLED", False):
        return None

    provider = getattr(settings, "SSO_PROVIDER", "keycloak")
    if provider != "keycloak":
        return None

    base_url = getattr(settings, "SSO_KEYCLOAK_BASE_URL", "")
    realm = getattr(settings, "SSO_KEYCLOAK_REALM", "")
    client_id = getattr(settings, "SSO_KEYCLOAK_CLIENT_ID", "")
    scope = getattr(settings, "SSO_KEYCLOAK_SCOPE", "openid email profile")

    if not (base_url and realm and client_id):
        return None

    issuer = f"{base_url}/realms/{realm}"
    return SsoProviderConfig(
        enabled=True,
        provider="keycloak",
        issuer=issuer,
        authorization_endpoint=f"{issuer}/protocol/openid-connect/auth",
        token_endpoint=f"{issuer}/protocol/openid-connect/token",
        jwks_uri=f"{issuer}/protocol/openid-connect/certs",
        client_id=client_id,
        scope=scope,
    )


class KeycloakSsoService:
    """Exchanges auth codes and provisions local users from OIDC claims."""

    def __init__(self, config: SsoProviderConfig | None = None) -> None:
        self._config = config or get_sso_provider_config()
        if self._config is None:
            raise AuthenticationError("SSO is not enabled or not configured")

    @property
    def config(self) -> SsoProviderConfig:
        """Expose immutable provider config."""
        return self._config

    def exchange_code_for_user(self, *, code: str, redirect_uri: str):
        """Exchange OIDC authorization code and return a local user."""
        id_token = self._exchange_code_for_id_token(code=code, redirect_uri=redirect_uri)
        claims = self._validate_id_token(id_token)
        return self._get_or_create_user(claims)

    def _exchange_code_for_id_token(self, *, code: str, redirect_uri: str) -> str:
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "code": code,
            "redirect_uri": redirect_uri,
        }

        client_secret = getattr(settings, "SSO_KEYCLOAK_CLIENT_SECRET", "")
        if client_secret:
            payload["client_secret"] = client_secret

        encoded = parse.urlencode(payload).encode("utf-8")
        req = request.Request(
            self.config.token_endpoint,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=10) as resp:
                token_payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8")
            logger.warning("SSO code exchange failed: status=%s body=%s", exc.code, details)
            raise AuthenticationError("SSO code exchange failed")
        except (error.URLError, TimeoutError) as exc:
            logger.warning("SSO code exchange network error: %s", exc)
            raise AuthenticationError("SSO provider is unreachable")

        id_token = token_payload.get("id_token")
        if not id_token:
            raise AuthenticationError("SSO response did not include an ID token")
        return str(id_token)

    def _validate_id_token(self, id_token: str) -> dict[str, Any]:
        try:
            jwk_client = jwt.PyJWKClient(self.config.jwks_uri)
            signing_key = jwk_client.get_signing_key_from_jwt(id_token)
            payload = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.config.client_id,
                issuer=self.config.issuer,
            )
        except jwt.PyJWTError as exc:
            logger.warning("Invalid SSO ID token: %s", exc)
            raise AuthenticationError("Invalid SSO token")

        return payload

    @staticmethod
    def _get_or_create_user(claims: dict[str, Any]):
        from apps.accounts.models import User

        email = claims.get("email")
        if not email:
            raise AuthenticationError("SSO token did not include an email claim")

        defaults = {
            "username": email,
            "first_name": claims.get("given_name", ""),
            "last_name": claims.get("family_name", ""),
        }
        user, created = User.objects.get_or_create(email=email, defaults=defaults)
        if not created:
            update_fields: list[str] = []
            first_name = claims.get("given_name", "")
            last_name = claims.get("family_name", "")
            if first_name and user.first_name != first_name:
                user.first_name = first_name
                update_fields.append("first_name")
            if last_name and user.last_name != last_name:
                user.last_name = last_name
                update_fields.append("last_name")
            if update_fields:
                user.save(update_fields=update_fields)

        if not user.is_active:
            raise AuthenticationError("Account is disabled")
        return user
