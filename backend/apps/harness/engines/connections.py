"""Personal, organization-contextual Claude engine authorization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from apps.credentials.repositories import CredentialServiceRepository
from apps.credentials.services import CredentialSvc
from apps.organizations.repositories import MembershipRepository
from common.exceptions import NotFoundError
from common.utils import decrypt_value

from ..models import HarnessConnection
from .repositories import HarnessConnectionRepository

AUTH_TYPE_TO_SERVICE_SLUG = {
    HarnessConnection.AuthType.API_TOKEN: "claude-agent-api-token",
    HarnessConnection.AuthType.SUBSCRIPTION_TOKEN: "claude-agent-subscription-token",
}
AUTH_TYPE_TO_ENV = {
    HarnessConnection.AuthType.API_TOKEN: "ANTHROPIC_API_KEY",
    HarnessConnection.AuthType.SUBSCRIPTION_TOKEN: "CLAUDE_CODE_OAUTH_TOKEN",
}


@dataclass(frozen=True)
class AuthConfiguration:
    """Server-only auth material and mode for a single Claude run."""

    auth_type: str
    token: str = field(repr=False)
    label: str = ""
    connection_id: uuid.UUID | None = None

    @property
    def secret_env(self) -> dict[str, str]:
        """Return the single narrowly-scoped CLI credential variable."""
        env_name = AUTH_TYPE_TO_ENV.get(self.auth_type)
        if env_name is None:
            raise ValueError("Unsupported Claude authentication type")
        return {env_name: self.token}


class EngineConnectionService:
    """Manage personal Claude credentials using the shared credential store."""

    def __init__(
        self,
        repository: type[HarnessConnectionRepository] | None = None,
        credential_service: CredentialSvc | None = None,
    ) -> None:
        self.repository = repository or HarnessConnectionRepository
        self.credentials = credential_service or CredentialSvc()

    @staticmethod
    def _normalize_auth_type(auth_type: str) -> str:
        """Validate auth type and return its canonical value."""
        value = (auth_type or "").strip().lower()
        if value not in AUTH_TYPE_TO_SERVICE_SLUG:
            raise ValueError("auth_type must be api_token or subscription_token")
        return value

    @staticmethod
    def _assert_member(organization_id: uuid.UUID, user_id: object) -> None:
        """Require active membership before exposing or changing credentials."""
        membership = MembershipRepository.get_for_user_and_org(user_id, organization_id)
        if membership is None or not membership.user.is_active:
            raise NotFoundError("Organization", str(organization_id))

    def list_connections(
        self, organization_id: uuid.UUID, user_id: object
    ) -> list[dict[str, Any]]:
        """List only safe metadata for the current user's engine connections."""
        self._assert_member(organization_id, user_id)
        return [
            {
                "id": str(row.id),
                "auth_type": row.auth_type,
                "label": row.label,
                "connected": row.credential_id is not None,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in self.repository.list_by_org_user(organization_id, user_id)
        ]

    def save_connection(
        self,
        *,
        organization_id: uuid.UUID,
        user,
        auth_type: str,
        token: str = "",
        label: str = "",
        connection_id: uuid.UUID | str | None = None,
    ) -> HarnessConnection:
        """Create/update one personal connection; empty token preserves same mode.

        A caller changing authentication modes must supply a new token. The
        token itself is persisted only by ``CredentialSvc`` as Fernet data.
        """
        self._assert_member(organization_id, user.id)
        normalized_type = self._normalize_auth_type(auth_type)
        if not isinstance(token, str):
            raise ValueError("token must be a string")
        if len(token) > 8192:
            raise ValueError("token exceeds the 8192 character limit")
        if token.strip() and any(char.isspace() for char in token):
            raise ValueError("token must not contain whitespace")
        if "\x00" in token:
            raise ValueError("token must not contain control characters")
        secret = token.strip()
        if len(secret) > 8192:
            raise ValueError("token exceeds the 8192 character limit")
        cleaned_label = (label or "").strip()[:255] or "Claude"
        existing = self.repository.get_by_org_user(organization_id, user.id)
        if connection_id is not None:
            try:
                requested_id = uuid.UUID(str(connection_id))
            except (TypeError, ValueError, AttributeError):
                raise NotFoundError("HarnessConnection", str(connection_id)) from None
            if existing is None or existing.id != requested_id:
                raise NotFoundError("HarnessConnection", str(connection_id))

        if existing is not None and existing.credential_id is not None:
            self._assert_credential_binding(existing, existing.auth_type, user.id)
        if existing is not None and not secret and existing.credential_id is None:
            raise ValueError("A token is required to reconnect Claude")
        if existing is not None and not secret and existing.auth_type != normalized_type:
            raise ValueError("A token is required when changing auth_type")

        if not secret:
            if existing is None or existing.credential_id is None:
                raise ValueError("A token is required to connect Claude")
            if existing.auth_type != normalized_type:
                raise ValueError("A token is required when changing auth_type")
            existing.label = cleaned_label
            return self.repository.update(existing, label=cleaned_label)

        service_slug = AUTH_TYPE_TO_SERVICE_SLUG[normalized_type]
        service = CredentialServiceRepository.get_global_by_slug(service_slug)
        if service is None:
            raise NotFoundError("CredentialService", service_slug)

        with transaction.atomic():
            old_credential = None
            if existing is not None and existing.credential_id is not None:
                if existing.auth_type == normalized_type:
                    self.credentials.update_credential(
                        credential_id=existing.credential_id,
                        org_id=organization_id,
                        user=user,
                        is_admin=False,
                        name=cleaned_label,
                        value=secret,
                    )
                    return self.repository.update(
                        existing,
                        auth_type=normalized_type,
                        label=cleaned_label,
                    )
                old_credential = existing.credential_id

            # CredentialSvc validates service visibility/activation and applies
            # the existing Fernet encryption and personal-ownership rules.
            new_credential = self.credentials.create_personal_credential(
                service_id=service.id,
                name=cleaned_label,
                value=secret,
                user=user,
                org_id=organization_id,
            )
            if old_credential is not None:
                self.credentials.delete_credential(
                    credential_id=old_credential,
                    org_id=organization_id,
                    user=user,
                    is_admin=False,
                )
            return self.repository.upsert(
                organization_id=organization_id,
                user_id=user.id,
                credential_id=new_credential.id,
                auth_type=normalized_type,
                label=cleaned_label,
            )

    def delete_connection(
        self,
        organization_id: uuid.UUID,
        user_id: object,
        connection_id: uuid.UUID | str | None = None,
    ) -> None:
        """Delete the user's Claude connection and its owned Credential."""
        self._assert_member(organization_id, user_id)
        existing = self.repository.get_by_org_user(organization_id, user_id)
        if existing is None:
            raise NotFoundError("HarnessConnection", str(connection_id or user_id))
        if connection_id is not None and str(existing.id) != str(connection_id):
            raise NotFoundError("HarnessConnection", str(connection_id))
        if existing.credential_id is not None:
            user = existing.user
            self.credentials.delete_credential(
                credential_id=existing.credential_id,
                org_id=organization_id,
                user=user,
                is_admin=False,
            )
        self.repository.delete(existing)

    @staticmethod
    def _assert_credential_binding(
        connection: HarnessConnection, auth_type: str, user_id: object
    ) -> None:
        """Reject dangling, foreign, or mode-mismatched credential links."""
        credential = connection.credential
        expected_slug = AUTH_TYPE_TO_SERVICE_SLUG.get(auth_type)
        if (
            credential is None
            or expected_slug is None
            or credential.user_id != user_id
            or credential.organization_id is not None
            or credential.service.organization_id is not None
            or credential.service.slug != expected_slug
        ):
            raise NotFoundError("HarnessConnection", str(connection.id))

    def resolve(
        self,
        organization_id: uuid.UUID,
        user_id: object,
        connection_id: uuid.UUID | str | None = None,
    ) -> AuthConfiguration:
        """Resolve an owned connection's secret for this run only.

        This method fails closed on a missing credential, mismatched auth type,
        non-personal ownership, inactive user, or foreign connection id.
        """
        self._assert_member(organization_id, user_id)
        if connection_id is not None:
            try:
                requested_id = uuid.UUID(str(connection_id))
            except (TypeError, ValueError, AttributeError):
                raise NotFoundError("HarnessConnection", str(connection_id)) from None
            existing = self.repository.get_by_id_for_owner(
                requested_id, organization_id=organization_id, user_id=user_id
            )
        else:
            existing = self.repository.get_by_org_user(organization_id, user_id)
        if existing is None or existing.credential_id is None:
            raise NotFoundError("HarnessConnection", str(connection_id or user_id))
        normalized_type = self._normalize_auth_type(existing.auth_type)
        self._assert_credential_binding(existing, normalized_type, user_id)
        credential = existing.credential
        token = decrypt_value(credential.encrypted_value)
        if not token:
            raise NotFoundError("HarnessConnection", str(existing.id))
        return AuthConfiguration(
            auth_type=normalized_type,
            token=token,
            label=existing.label,
            connection_id=existing.id,
        )
