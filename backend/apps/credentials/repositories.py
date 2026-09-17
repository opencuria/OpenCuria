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
    """Data access for CredentialService (global + org-owned) records."""

    @staticmethod
    def list_all() -> QuerySet[CredentialService]:
        """Return all credential services ordered by name (legacy helper)."""
        return CredentialService.objects.all()

    @staticmethod
    def list_visible_to_org(org_id: uuid.UUID) -> QuerySet[CredentialService]:
        """Return global + org-owned services visible in the given org."""
        return CredentialService.objects.filter(
            Q(organization__isnull=True) | Q(organization_id=org_id)
        ).order_by("name")

    @staticmethod
    def list_org_owned(org_id: uuid.UUID) -> QuerySet[CredentialService]:
        """Return org-owned services of the given org."""
        return CredentialService.objects.filter(organization_id=org_id)

    @staticmethod
    def get_by_id(service_id: uuid.UUID) -> CredentialService | None:
        """Fetch a credential service by ID."""
        return CredentialService.objects.filter(id=service_id).first()

    @staticmethod
    def get_visible_by_id(
        service_id: uuid.UUID, org_id: uuid.UUID
    ) -> CredentialService | None:
        """Fetch a service visible in the org (global or org-owned)."""
        return (
            CredentialService.objects.filter(id=service_id)
            .filter(Q(organization__isnull=True) | Q(organization_id=org_id))
            .first()
        )

    @staticmethod
    def get_by_slug(slug: str) -> CredentialService | None:
        """Fetch a credential service by slug (legacy helper)."""
        return CredentialService.objects.filter(slug=slug).first()

    @staticmethod
    def get_global_by_slug(slug: str) -> CredentialService | None:
        """Fetch a global service by slug."""
        return CredentialService.objects.filter(
            slug=slug, organization__isnull=True
        ).first()

    @staticmethod
    def get_org_by_slug(slug: str, org_id: uuid.UUID) -> CredentialService | None:
        """Fetch an org-owned service of this org by slug."""
        return CredentialService.objects.filter(
            slug=slug, organization_id=org_id
        ).first()

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
        organization_id: uuid.UUID | None = None,
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
            organization_id=organization_id,
        )

    @staticmethod
    def delete_org_services(
        org_id: uuid.UUID, service_ids: list[uuid.UUID]
    ) -> int:
        """Delete org-owned service definitions of one org."""
        if not service_ids:
            return 0
        count, _ = CredentialService.objects.filter(
            id__in=service_ids, organization_id=org_id
        ).delete()
        return count


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

    @staticmethod
    def exists_for_service_ids(service_ids: list[uuid.UUID]) -> bool:
        """Return True if any credential references the given services."""
        if not service_ids:
            return False
        return Credential.objects.filter(service_id__in=service_ids).exists()

    @staticmethod
    def service_ids_with_credentials(
        service_ids: list[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Return the subset of service IDs still referenced by credentials."""
        if not service_ids:
            return set()
        return set(
            Credential.objects.filter(service_id__in=service_ids).values_list(
                "service_id", flat=True
            )
        )

    @staticmethod
    def org_service_ids_with_credentials(
        org_id: uuid.UUID, service_ids: list[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Return org-credential service IDs (no secret values inspected)."""
        if not service_ids:
            return set()
        return set(
            Credential.objects.filter(
                organization_id=org_id, service_id__in=service_ids
            ).values_list("service_id", flat=True)
        )

class OrgCredentialServiceActivationRepository:
    """Data access for OrgCredentialServiceActivation records."""

    @staticmethod
    def activated_service_ids(org_id: uuid.UUID) -> set[uuid.UUID]:
        """Return activated service IDs for the org."""
        from .models import OrgCredentialServiceActivation

        return set(
            OrgCredentialServiceActivation.objects.filter(
                organization_id=org_id
            ).values_list("credential_service_id", flat=True)
        )

    @staticmethod
    def ensure_activated(org_id: uuid.UUID, service_ids: list[uuid.UUID]) -> None:
        """Activate services for the org (idempotent)."""
        from .models import OrgCredentialServiceActivation

        if not service_ids:
            return
        OrgCredentialServiceActivation.objects.bulk_create(
            [
                OrgCredentialServiceActivation(
                    organization_id=org_id,
                    credential_service_id=service_id,
                )
                for service_id in service_ids
            ],
            ignore_conflicts=True,
        )

    @staticmethod
    def deactivate(org_id: uuid.UUID, service_id: uuid.UUID) -> None:
        """Deactivate a service for the org."""
        from .models import OrgCredentialServiceActivation

        OrgCredentialServiceActivation.objects.filter(
            organization_id=org_id,
            credential_service_id=service_id,
        ).delete()
