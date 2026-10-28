"""Repository layer for credentials and OAuth persistence."""

from __future__ import annotations

import hmac
import uuid
from contextlib import AbstractContextManager

from django.db import IntegrityError, connection, transaction
from django.db.models import F, Q, QuerySet, Value
from django.db.models.functions import Concat
from django.utils import timezone

from common.exceptions import ConflictError

from .models import (
    Credential,
    CredentialService,
    McpOAuthAuthorizationState,
    McpOAuthClientRegistration,
)


def _lock_queryset_self(query: QuerySet) -> QuerySet:
    """Lock only the queried row when joined nullable relations are present."""
    if connection.features.has_select_for_update_of:
        return query.select_for_update(of=("self",))
    return query.select_for_update()


class CredentialServiceRepository:
    """Data access for global and organization-owned service definitions."""

    @staticmethod
    def list_all() -> QuerySet[CredentialService]:
        return CredentialService.objects.exclude(credential_type="provider_secret")

    @staticmethod
    def list_visible_to_org(org_id: uuid.UUID) -> QuerySet[CredentialService]:
        return (
            CredentialService.objects.filter(
                Q(organization__isnull=True) | Q(organization_id=org_id)
            )
            .exclude(credential_type="provider_secret")
            .order_by("name")
        )

    @staticmethod
    def list_org_owned(org_id: uuid.UUID) -> QuerySet[CredentialService]:
        return CredentialService.objects.filter(organization_id=org_id).exclude(
            credential_type="provider_secret"
        )

    @staticmethod
    def get_by_id(service_id: uuid.UUID) -> CredentialService | None:
        return (
            CredentialService.objects.filter(id=service_id)
            .exclude(credential_type="provider_secret")
            .first()
        )

    @staticmethod
    def get_visible_by_id(
        service_id: uuid.UUID, org_id: uuid.UUID
    ) -> CredentialService | None:
        return (
            CredentialServiceRepository.list_visible_to_org(org_id)
            .filter(id=service_id)
            .first()
        )

    @staticmethod
    def get_by_slug(slug: str) -> CredentialService | None:
        return (
            CredentialService.objects.exclude(credential_type="provider_secret")
            .filter(slug=slug)
            .first()
        )

    @staticmethod
    def get_global_by_slug(slug: str) -> CredentialService | None:
        return (
            CredentialService.objects.exclude(credential_type="provider_secret")
            .filter(slug=slug, organization__isnull=True)
            .first()
        )

    @staticmethod
    def get_org_by_slug(slug: str, org_id: uuid.UUID) -> CredentialService | None:
        return (
            CredentialService.objects.exclude(credential_type="provider_secret")
            .filter(slug=slug, organization_id=org_id)
            .first()
        )

    @staticmethod
    def create_internal_provider_secret_service(
        *, organization_id: uuid.UUID, provider: str
    ) -> CredentialService:
        """Create/get the internal provider service without adopting a collision."""
        slug = f"provider-account-{provider}"
        try:
            with transaction.atomic():
                service, _created = CredentialService.objects.get_or_create(
                    organization_id=organization_id,
                    slug=slug,
                    defaults={
                        "name": f"Provider account ({provider})",
                        "description": "Internal provider-account secret storage.",
                        "credential_type": "provider_secret",
                        "env_var_name": "",
                        "target_path": "",
                        "label": "Provider account",
                    },
                )
        except IntegrityError:
            service = CredentialService.objects.get(
                organization_id=organization_id, slug=slug
            )
        if service.credential_type != "provider_secret":
            raise ConflictError("Reserved provider-account service slug is occupied")
        return service

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
        oauth_server_url: str = "",
    ) -> CredentialService:
        return CredentialService.objects.create(
            name=name,
            slug=slug,
            description=description,
            credential_type=credential_type,
            env_var_name=env_var_name,
            target_path=target_path,
            label=label,
            organization_id=organization_id,
            oauth_server_url=oauth_server_url,
        )


class CredentialRepository:
    """Data access for credentials."""

    @staticmethod
    def list_for_user_in_org(user_id: int, org_id: uuid.UUID) -> QuerySet[Credential]:
        return (
            Credential.objects.filter(Q(user_id=user_id) | Q(organization_id=org_id))
            .filter(
                Q(service__organization__isnull=True)
                | Q(service__organization_id=org_id)
            )
            .exclude(service__credential_type="provider_secret")
            .select_related("service", "user", "organization", "oauth_registration")
        )

    @staticmethod
    def ids_for_workspaces(
        workspace_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        """Read ordered ordinary attachment IDs without loading secret values."""
        grouped: dict[uuid.UUID, list[uuid.UUID]] = {
            workspace_id: [] for workspace_id in workspace_ids
        }
        if not grouped:
            return grouped
        rows = (
            Credential.objects.filter(workspaces__id__in=grouped)
            .exclude(service__credential_type="provider_secret")
            .values_list("workspaces__id", "id")
        )
        for workspace_id, credential_id in rows:
            grouped[workspace_id].append(credential_id)
        return grouped

    @staticmethod
    def list_for_workspace(
        workspace_id: uuid.UUID, *, include_internal: bool = False
    ) -> list[Credential]:
        """Load ordered attachments, optionally including rows for internal audits."""
        credentials = (
            Credential.objects.filter(workspaces__id=workspace_id)
            .select_related("service", "oauth_registration", "user", "organization")
            .order_by("-created_at")
        )
        if not include_internal:
            credentials = credentials.exclude(
                service__credential_type="provider_secret"
            )
        return list(credentials)

    @staticmethod
    def get_by_id(credential_id: uuid.UUID) -> Credential | None:
        return (
            Credential.objects.filter(id=credential_id)
            .exclude(service__credential_type="provider_secret")
            .select_related("service", "user", "organization", "oauth_registration")
            .first()
        )

    @staticmethod
    def create_internal_provider_secret(
        *,
        organization_id: uuid.UUID,
        user_id: int | None,
        service: CredentialService,
        name: str,
        encrypted_value: str,
        created_by,
    ) -> Credential:
        """Create a hidden, scope-matched provider secret."""
        return Credential.objects.create(
            organization_id=organization_id if user_id is None else None,
            user_id=user_id,
            service=service,
            name=name,
            encrypted_value=encrypted_value,
            created_by=created_by,
        )

    @staticmethod
    def _bound_provider_secrets() -> QuerySet[Credential]:
        """Match each secret to its live account, provider and ownership scope.

        Aliases keep all account predicates on the same reverse-relation join,
        including when callers add further filters to the queryset.
        """
        return (
            Credential.objects.alias(
                bound_account_id=F("provider_accounts__id"),
                bound_org_id=F("provider_accounts__organization_id"),
                bound_user_id=F("provider_accounts__user_id"),
                bound_provider=F("provider_accounts__provider"),
                bound_deleted_at=F("provider_accounts__deleted_at"),
            )
            .filter(
                service__credential_type="provider_secret",
                service__slug=Concat(Value("provider-account-"), F("bound_provider")),
                service__organization_id=F("bound_org_id"),
                bound_deleted_at__isnull=True,
            )
            .filter(
                Q(
                    bound_user_id__isnull=True,
                    user_id__isnull=True,
                    organization_id=F("bound_org_id"),
                )
                | Q(
                    bound_user_id__isnull=False,
                    user_id=F("bound_user_id"),
                    organization_id__isnull=True,
                )
            )
        )

    @staticmethod
    def get_internal_provider_secret(
        credential_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: int | None,
        provider: str,
        account_id: uuid.UUID,
    ) -> Credential | None:
        """Fetch only a secret bound to the exact provider account and scope."""
        return (
            CredentialRepository._bound_provider_secrets()
            .filter(
                id=credential_id,
                bound_account_id=account_id,
                bound_org_id=organization_id,
                bound_user_id=user_id,
                bound_provider=provider,
            )
            .select_related("service")
            .first()
        )

    @staticmethod
    def get_bound_provider_secrets(
        *, organization_id: uuid.UUID, user_id: int, account_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Read encrypted values for visible, correctly bound accounts in one query."""
        if not account_ids:
            return {}
        query = (
            CredentialRepository._bound_provider_secrets()
            .filter(bound_account_id__in=account_ids, bound_org_id=organization_id)
            .filter(Q(bound_user_id__isnull=True) | Q(bound_user_id=user_id))
            .annotate(account_id=F("bound_account_id"))
        )
        return dict(query.values_list("account_id", "encrypted_value"))

    @staticmethod
    def reassign_internal_provider_secret(
        credential_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        previous_user_id: int | None,
        user_id: int | None,
        provider: str,
        account_id: uuid.UUID,
    ) -> bool:
        """Move a bound provider secret to match an account's new scope."""
        credential = Credential.objects.filter(
            id=credential_id,
            service__credential_type="provider_secret",
            service__slug=f"provider-account-{provider}",
            service__organization_id=organization_id,
            organization_id=organization_id if previous_user_id is None else None,
            user_id=previous_user_id,
            provider_accounts__id=account_id,
            provider_accounts__organization_id=organization_id,
            provider_accounts__user_id=previous_user_id,
            provider_accounts__provider=provider,
            provider_accounts__deleted_at__isnull=True,
        ).first()
        if credential is None:
            return False
        credential.organization_id = organization_id if user_id is None else None
        credential.user_id = user_id
        credential.save(update_fields=["organization_id", "user_id", "updated_at"])
        return True

    @staticmethod
    def update_internal_provider_secret(
        credential_id: uuid.UUID,
        encrypted_value: str,
        *,
        organization_id: uuid.UUID,
        user_id: int | None,
        provider: str,
        account_id: uuid.UUID,
    ) -> bool:
        """Replace encrypted JSON only for a correctly bound provider secret."""
        return (
            Credential.objects.filter(
                id=credential_id,
                service__credential_type="provider_secret",
                service__slug=f"provider-account-{provider}",
                service__organization_id=organization_id,
                organization_id=organization_id if user_id is None else None,
                user_id=user_id,
                provider_accounts__id=account_id,
                provider_accounts__organization_id=organization_id,
                provider_accounts__user_id=user_id,
                provider_accounts__provider=provider,
                provider_accounts__deleted_at__isnull=True,
            ).update(encrypted_value=encrypted_value, updated_at=timezone.now())
            == 1
        )

    @staticmethod
    def delete_internal_provider_secret(
        credential_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        user_id: int | None,
        provider: str,
        account_id: uuid.UUID,
    ) -> None:
        """Delete one internal secret only through its live owning account."""
        Credential.objects.filter(
            id=credential_id,
            service__credential_type="provider_secret",
            service__slug=f"provider-account-{provider}",
            service__organization_id=organization_id,
            organization_id=organization_id if user_id is None else None,
            user_id=user_id,
            provider_accounts__id=account_id,
            provider_accounts__organization_id=organization_id,
            provider_accounts__user_id=user_id,
            provider_accounts__provider=provider,
            provider_accounts__deleted_at__isnull=True,
        ).delete()

    @staticmethod
    def get_many_internal_provider_secrets(
        credential_ids: list[uuid.UUID],
    ) -> list[Credential]:
        """Detect server-only provider secrets in untrusted public ID input."""
        return list(
            Credential.objects.filter(
                id__in=credential_ids,
                service__credential_type="provider_secret",
            ).only("id")
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
        return Credential.objects.create(
            user=user,
            service=service,
            name=name,
            encrypted_value=encrypted_value,
            public_key=public_key,
            created_by=user,
        )

    @staticmethod
    def create_oauth(
        *,
        user,
        organization,
        service: CredentialService,
        name: str,
        server_url: str,
        resource: str,
        registration,
        encrypted_value: str,
        created_by,
    ) -> Credential:
        """Persist a fresh service-scoped OAuth grant as its own credential."""
        return Credential.objects.create(
            user=user,
            organization=organization,
            service=service,
            name=name,
            encrypted_value=encrypted_value,
            created_by=created_by,
            oauth_server_url=server_url,
            oauth_resource=resource,
            oauth_registration=registration,
            oauth_status="connected",
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
        fields = ["updated_at"]
        if name is not None:
            credential.name = name
            fields.append("name")
        if encrypted_value is not None:
            credential.encrypted_value = encrypted_value
            fields.append("encrypted_value")
        credential.save(update_fields=fields)
        return credential

    @staticmethod
    def delete(credential_id: uuid.UUID) -> int:
        """Delete an ordinary credential; internal provider secrets stay server-only."""
        count, _ = (
            Credential.objects.filter(id=credential_id)
            .exclude(service__credential_type="provider_secret")
            .delete()
        )
        return count

    @staticmethod
    def get_many_by_ids(
        credential_ids: list[uuid.UUID], *, org_id: uuid.UUID, user_id: int
    ) -> list[Credential]:
        return list(
            Credential.objects.filter(id__in=credential_ids)
            .filter(Q(organization_id=org_id) | Q(user_id=user_id))
            .exclude(service__credential_type="provider_secret")
            .select_related("service", "oauth_registration")
        )

    @staticmethod
    def exists_for_service_ids(service_ids: list[uuid.UUID]) -> bool:
        return (
            bool(service_ids)
            and Credential.objects.filter(service_id__in=service_ids)
            .exclude(service__credential_type="provider_secret")
            .exists()
        )

    @staticmethod
    def service_ids_with_credentials(service_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        if not service_ids:
            return set()
        return set(
            Credential.objects.filter(service_id__in=service_ids)
            .exclude(service__credential_type="provider_secret")
            .values_list("service_id", flat=True)
        )

    @staticmethod
    def credential_ids_for_services(service_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        if not service_ids:
            return set()
        return set(
            Credential.objects.filter(service_id__in=service_ids)
            .exclude(service__credential_type="provider_secret")
            .values_list("id", flat=True)
        )

    @staticmethod
    def org_service_ids_with_credentials(
        org_id: uuid.UUID, service_ids: list[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not service_ids:
            return set()
        return set(
            Credential.objects.filter(
                organization_id=org_id, service_id__in=service_ids
            )
            .exclude(service__credential_type="provider_secret")
            .values_list("service_id", flat=True)
        )

    @staticmethod
    def oauth_candidates(
        *,
        credential_ids,
        service_id: uuid.UUID,
        server_url: str,
        org_id: uuid.UUID,
        owner_id: int,
    ):
        """Return attached owned OAuth credentials for a fixed service endpoint."""
        return list(
            Credential.objects.filter(
                id__in=credential_ids,
                service_id=service_id,
                oauth_server_url=server_url,
                oauth_status="connected",
            )
            .filter(
                Q(service__organization__isnull=True)
                | Q(service__organization_id=org_id)
            )
            .filter(Q(user_id=owner_id) | Q(organization_id=org_id))
            .exclude(service__credential_type="provider_secret")
            .select_related("service", "oauth_registration", "created_by")
        )


class OrgCredentialServiceActivationRepository:
    """Data access for organization service activation rows."""

    @staticmethod
    def activated_service_ids(org_id: uuid.UUID) -> set[uuid.UUID]:
        from .models import OrgCredentialServiceActivation

        return set(
            OrgCredentialServiceActivation.objects.filter(
                organization_id=org_id
            ).values_list("credential_service_id", flat=True)
        )

    @staticmethod
    def ensure_activated(org_id: uuid.UUID, service_ids: list[uuid.UUID]) -> None:
        from .models import OrgCredentialServiceActivation

        if service_ids:
            OrgCredentialServiceActivation.objects.bulk_create(
                [
                    OrgCredentialServiceActivation(
                        organization_id=org_id, credential_service_id=sid
                    )
                    for sid in service_ids
                ],
                ignore_conflicts=True,
            )

    @staticmethod
    def activate_all_global(org_id: uuid.UUID) -> None:
        """Activate every global service for the org, provider secrets included."""
        OrgCredentialServiceActivationRepository.ensure_activated(
            org_id,
            list(
                CredentialService.objects.filter(organization__isnull=True)
                .values_list("id", flat=True)
            ),
        )

    @staticmethod
    def active_in_org(service_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        """Return whether this org has activated the given service."""
        from .models import OrgCredentialServiceActivation

        return OrgCredentialServiceActivation.objects.filter(
            organization_id=org_id, credential_service_id=service_id
        ).exists()

    @staticmethod
    def deactivate(org_id: uuid.UUID, service_id: uuid.UUID) -> None:
        from .models import OrgCredentialServiceActivation

        OrgCredentialServiceActivation.objects.filter(
            organization_id=org_id, credential_service_id=service_id
        ).delete()


class McpOAuthRepository:
    """Repository access for OAuth services, registration, state, and grants."""

    @staticmethod
    def grant_transaction() -> AbstractContextManager[None]:
        """Keep grant authorization reads, row locks and writes in one transaction."""
        return transaction.atomic()

    @staticmethod
    def get_service(service_id: uuid.UUID, *, lock: bool = False):
        query = CredentialService.objects.all()
        if lock:
            query = query.select_for_update()
        return query.filter(pk=service_id).first()

    @staticmethod
    def get_registration(registration_id: uuid.UUID, *, lock: bool = False):
        query = McpOAuthClientRegistration.objects.all()
        if lock:
            query = query.select_for_update()
        return query.filter(pk=registration_id).first()

    @staticmethod
    def registration(server_url: str, callback_url: str, issuer: str):
        return McpOAuthClientRegistration.objects.filter(
            server_url=server_url, callback_url=callback_url, issuer=issuer
        ).first()

    @staticmethod
    def save_registration(**values):
        return McpOAuthClientRegistration.objects.get_or_create(
            server_url=values["server_url"],
            callback_url=values["callback_url"],
            issuer=values["issuer"],
            defaults=values,
        )[0]

    @staticmethod
    def create_state(**values):
        return McpOAuthAuthorizationState.objects.create(**values)

    @staticmethod
    def consume_state(*, state_hash: str, now):
        return (
            _lock_queryset_self(McpOAuthAuthorizationState.objects.all())
            .select_related(
                "user", "organization", "service", "registration", "credential"
            )
            .filter(state_hash=state_hash, consumed_at__isnull=True, expires_at__gt=now)
            .first()
        )

    @staticmethod
    def mark_state_consumed(state: McpOAuthAuthorizationState, consumed_at) -> None:
        state.consumed_at = consumed_at
        state.save(update_fields=["consumed_at"])

    @staticmethod
    def consume_state_and_match_binding(*, state_hash: str, binding_hash: str, now):
        """Consume once and compare only the supplied browser cookie hash."""
        state = None
        matched = False
        with transaction.atomic():
            state = McpOAuthRepository.consume_state(state_hash=state_hash, now=now)
            if state is None:
                return None, False
            matched = hmac.compare_digest(state.browser_binding_hash, binding_hash)
            McpOAuthRepository.mark_state_consumed(state, now)
        return state, matched

    @staticmethod
    def service_visible_to_org(service_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        return (
            CredentialServiceRepository.list_visible_to_org(org_id)
            .filter(id=service_id)
            .exists()
        )

    @staticmethod
    def service_active_for_org(
        service_id: uuid.UUID, org_id: uuid.UUID, *, lock: bool = False
    ) -> bool:
        from .models import OrgCredentialServiceActivation

        query = OrgCredentialServiceActivation.objects.filter(
            credential_service_id=service_id, organization_id=org_id
        )
        if lock:
            query = query.select_for_update()
        return query.exists()

    @staticmethod
    def get_credential(credential_id: uuid.UUID, *, lock: bool = False):
        query = Credential.objects.select_related(
            "service", "oauth_registration", "user", "organization"
        )
        if lock:
            query = _lock_queryset_self(query)
        return query.filter(
            pk=credential_id, service__credential_type="mcp_oauth"
        ).first()

    @staticmethod
    def get_reconnect_target(credential_id: uuid.UUID, *, lock: bool = False):
        query = Credential.objects.select_related(
            "service", "oauth_registration", "user", "organization"
        )
        if lock:
            query = _lock_queryset_self(query)
        return query.filter(pk=credential_id).first()

    @staticmethod
    def create_grant(
        *,
        user_id: int | None,
        organization_id: uuid.UUID | None,
        service: CredentialService,
        name: str,
        server_url: str,
        resource: str,
        registration: McpOAuthClientRegistration,
        encrypted_value: str,
        created_by_id: int,
    ) -> Credential:
        """Insert a service-bound grant without resolving owner relationships."""
        return Credential.objects.create(
            user_id=user_id,
            organization_id=organization_id,
            service=service,
            name=name,
            encrypted_value=encrypted_value,
            created_by_id=created_by_id,
            oauth_server_url=server_url,
            oauth_resource=resource,
            oauth_registration=registration,
            oauth_status="connected",
        )

    @staticmethod
    def update_locked_grant(credential: Credential, **values) -> None:
        for key, value in values.items():
            setattr(credential, key, value)
        credential.save(update_fields=[*values, "updated_at"])

    @staticmethod
    def update_oauth(credential_id: uuid.UUID, **values) -> int:
        return Credential.objects.filter(
            pk=credential_id, service__credential_type="mcp_oauth"
        ).update(**values)
