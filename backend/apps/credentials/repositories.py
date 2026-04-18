"""
Repository layer for the credentials app.

Encapsulates all database queries. Services never use the ORM directly.
"""

from __future__ import annotations

import uuid

from django.db.models import Q, QuerySet

from .models import Credential, CredentialService

# ---------------------------------------------------------------------------
# CredentialService Repository
# ---------------------------------------------------------------------------


class CredentialServiceRepository:
    """Data access for CredentialService (global catalog) records."""

    @staticmethod
    def list_all() -> QuerySet[CredentialService]:
        """Return all credential services ordered by name."""
        return CredentialService.objects.all()

    @staticmethod
    def get_by_id(service_id: uuid.UUID) -> CredentialService | None:
        """Fetch a credential service by ID."""
        return CredentialService.objects.filter(id=service_id).first()

    @staticmethod
    def get_by_slug(slug: str) -> CredentialService | None:
        """Fetch a credential service by slug."""
        return CredentialService.objects.filter(slug=slug).first()

    @staticmethod
    def create(
        *,
        name: str,
        slug: str,
        description: str,
        credential_type: str,
        env_var_name: str,
        target_path: str,
        label: str,
    ) -> CredentialService:
        """Create a credential service catalog entry."""
        return CredentialService.objects.create(
            name=name,
            slug=slug,
            description=description,
            credential_type=credential_type,
            env_var_name=env_var_name,
            target_path=target_path,
            label=label,
        )


# ---------------------------------------------------------------------------
# Credential Repository
# ---------------------------------------------------------------------------


class CredentialRepository:
    """Data access for Credential records."""

    @staticmethod
    def list_for_user_in_org(
        user_id: int, org_id: uuid.UUID
    ) -> QuerySet[Credential]:
        """Return all credentials visible to a user in an org:
        - Credentials personally owned by this user, OR
        - Credentials owned by this organization.
        """
        return (
            Credential.objects.filter(
                Q(user_id=user_id) | Q(organization_id=org_id)
            )
            .select_related("service", "user", "organization")
        )

    @staticmethod
    def get_by_id(credential_id: uuid.UUID) -> Credential | None:
        """Fetch a credential by ID with related objects."""
        return (
            Credential.objects.filter(id=credential_id)
            .select_related("service", "user", "organization")
            .first()
        )

    @staticmethod
    def create_personal(
        *,
        user,
        service: CredentialService,
        name: str,
        encrypted_value: str,
        public_key: str = "",
    ) -> Credential:
        """Create a new personal credential owned by the given user."""
        return Credential.objects.create(
            user=user,
            service=service,
            name=name,
            encrypted_value=encrypted_value,
            public_key=public_key,
            created_by=user,
        )

    @staticmethod
    def create_org(
        *,
        organization_id: uuid.UUID,
        service: CredentialService,
        name: str,
        encrypted_value: str,
        public_key: str = "",
        created_by,
    ) -> Credential:
        """Create a new org-scoped credential."""
        return Credential.objects.create(
            organization_id=organization_id,
            service=service,
            name=name,
            encrypted_value=encrypted_value,
            public_key=public_key,
            created_by=created_by,
        )

    @staticmethod
    def update(
        credential: Credential,
        *,
        name: str | None = None,
        encrypted_value: str | None = None,
    ) -> Credential:
        """Update a credential's name and/or value."""
        update_fields = ["updated_at"]
        if name is not None:
            credential.name = name
            update_fields.append("name")
        if encrypted_value is not None:
            credential.encrypted_value = encrypted_value
            update_fields.append("encrypted_value")
        credential.save(update_fields=update_fields)
        return credential

    @staticmethod
    def delete(credential_id: uuid.UUID) -> int:
        """Delete a credential by ID. Returns number of rows deleted."""
        count, _ = Credential.objects.filter(id=credential_id).delete()
        return count

    @staticmethod
    def get_many_by_ids(
        credential_ids: list[uuid.UUID],
        *,
        org_id: uuid.UUID,
        user_id: int,
    ) -> list[Credential]:
        """Fetch multiple credentials by IDs visible to the user in the org.

        Returns credentials that are either:
        - org credentials belonging to this organization, OR
        - personal credentials owned by this user.
        """
        return list(
            Credential.objects.filter(
                id__in=credential_ids,
            )
            .filter(Q(organization_id=org_id) | Q(user_id=user_id))
            .select_related("service")
        )
