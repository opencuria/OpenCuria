"""OAuth grant authorization policy over transaction-scoped repository operations."""

from __future__ import annotations

import uuid

from apps.organizations.services import OrganizationService

from .models import Credential, McpOAuthAuthorizationState
from .oauth_metadata import registration_fingerprint, registration_metadata_safe
from .repositories import McpOAuthRepository


class OAuthGrantService:
    """Recheck live authorization and finalize a new grant or explicit reconnect."""

    def __init__(
        self,
        *,
        repository: type[McpOAuthRepository] | None = None,
        organizations: OrganizationService | None = None,
    ) -> None:
        """Inject grant persistence and organization authorization boundaries."""
        self.repository = repository or McpOAuthRepository
        self.organizations = organizations or OrganizationService()

    @staticmethod
    def owner_matches_state(
        state: McpOAuthAuthorizationState, credential: Credential
    ) -> bool:
        """Return whether a reconnect target still belongs to the requested owner."""
        if state.organization_credential:
            return (
                credential.organization_id == state.organization_id
                and credential.user_id is None
            )
        return (
            credential.user_id == state.user_id and credential.organization_id is None
        )

    def persist(
        self, *, state: McpOAuthAuthorizationState, encrypted_value: str
    ) -> uuid.UUID:
        """Atomically recheck service, registration and owner before writing a grant."""
        with self.repository.grant_transaction():
            service = self.repository.get_service(state.service_id, lock=True)
            registration = self.repository.get_registration(
                state.registration_id, lock=True
            )
            membership = self.organizations.get_active_membership(
                state.user_id, state.organization_id, lock=True
            )

            if (
                service is None
                or registration is None
                or membership is None
                or (state.organization_credential and membership.role != "admin")
                or service.credential_type != "mcp_oauth"
                or service.oauth_server_url != state.server_url
                or not self.repository.service_visible_to_org(
                    state.service_id, state.organization_id
                )
                or not registration_metadata_safe(
                    registration,
                    expected_url=state.server_url,
                    expected_issuer=state.issuer,
                )
                or registration_fingerprint(registration)
                != state.registration_fingerprint
            ):
                raise ValueError("OAuth service, registration, or membership changed")
            if state.reconnect_existing:
                target = (
                    self.repository.get_reconnect_target(state.credential_id, lock=True)
                    if state.credential_id
                    else None
                )
                if (
                    target is None
                    or target.service_id != state.service_id
                    or target.oauth_server_url != state.credential_server_url_snapshot
                    or not self.owner_matches_state(state, target)
                    or not self.repository.service_visible_to_org(
                        target.service_id, state.organization_id
                    )
                ):
                    raise ValueError("OAuth reconnect target was deleted or changed")
                self.repository.update_locked_grant(
                    target,
                    name=state.credential_name or target.name,
                    oauth_server_url=state.server_url,
                    oauth_resource=state.resource,
                    oauth_registration=registration,
                    oauth_status="connected",
                    encrypted_value=encrypted_value,
                )
                return target.id
            if not self.repository.service_active_for_org(
                state.service_id, state.organization_id, lock=True
            ):
                raise ValueError("OAuth service is inactive")
            return self.repository.create_grant(
                user_id=None if state.organization_credential else state.user_id,
                organization_id=state.organization_id
                if state.organization_credential
                else None,
                service=service,
                name=state.credential_name or f"{service.name} OAuth",
                server_url=state.server_url,
                resource=state.resource,
                registration=registration,
                encrypted_value=encrypted_value,
                created_by_id=state.user_id,
            ).id
