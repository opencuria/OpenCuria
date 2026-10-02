"""Repository layer for credentials and OAuth persistence."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid

from django.db import transaction
from django.db.models import Q, QuerySet

from .models import (
    Credential,
    CredentialService,
    McpOAuthAuthorizationState,
    McpOAuthClientRegistration,
)


class CredentialServiceRepository:
    """Data access for global and organization-owned service definitions."""

    @staticmethod
    def list_all() -> QuerySet[CredentialService]:
        return CredentialService.objects.all()

    @staticmethod
    def list_visible_to_org(org_id: uuid.UUID) -> QuerySet[CredentialService]:
        return CredentialService.objects.filter(
            Q(organization__isnull=True) | Q(organization_id=org_id)
        ).order_by("name")

    @staticmethod
    def list_org_owned(org_id: uuid.UUID) -> QuerySet[CredentialService]:
        return CredentialService.objects.filter(organization_id=org_id)

    @staticmethod
    def get_by_id(service_id: uuid.UUID) -> CredentialService | None:
        return CredentialService.objects.filter(id=service_id).first()

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
        return CredentialService.objects.filter(slug=slug).first()

    @staticmethod
    def get_global_by_slug(slug: str) -> CredentialService | None:
        return CredentialService.objects.filter(
            slug=slug, organization__isnull=True
        ).first()

    @staticmethod
    def get_org_by_slug(slug: str, org_id: uuid.UUID) -> CredentialService | None:
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
            .filter(Q(service__organization__isnull=True) | Q(service__organization_id=org_id))
            .select_related("service", "user", "organization", "oauth_registration")
        )

    @staticmethod
    def ids_for_workspace(workspace_id: uuid.UUID) -> list[uuid.UUID]:
        """Return IDs attached to one workspace, without loading secret fields."""
        return list(
            Credential.objects.filter(workspaces__id=workspace_id).values_list(
                "id", flat=True
            )
        )

    @staticmethod
    def list_for_workspace(workspace_id: uuid.UUID) -> list[Credential]:
        """Return workspace attachments with service and OAuth metadata loaded."""
        return list(
            Credential.objects.filter(workspaces__id=workspace_id)
            .select_related("service", "oauth_registration", "user", "organization")
            .order_by("-created_at")
        )

    @staticmethod
    def get_by_id(credential_id: uuid.UUID) -> Credential | None:
        return (
            Credential.objects.filter(id=credential_id)
            .select_related("service", "user", "organization", "oauth_registration")
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
        count, _ = Credential.objects.filter(id=credential_id).delete()
        return count

    @staticmethod
    def get_many_by_ids(
        credential_ids: list[uuid.UUID], *, org_id: uuid.UUID, user_id: int
    ) -> list[Credential]:
        return list(
            Credential.objects.filter(id__in=credential_ids)
            .filter(Q(organization_id=org_id) | Q(user_id=user_id))
            .select_related("service", "oauth_registration")
        )

    @staticmethod
    def exists_for_service_ids(service_ids: list[uuid.UUID]) -> bool:
        return (
            bool(service_ids)
            and Credential.objects.filter(service_id__in=service_ids).exists()
        )

    @staticmethod
    def service_ids_with_credentials(service_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        if not service_ids:
            return set()
        return set(
            Credential.objects.filter(service_id__in=service_ids).values_list(
                "service_id", flat=True
            )
        )

    @staticmethod
    def credential_ids_for_services(service_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        if not service_ids:
            return set()
        return set(
            Credential.objects.filter(service_id__in=service_ids).values_list(
                "id", flat=True
            )
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
            ).values_list("service_id", flat=True)
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
            McpOAuthAuthorizationState.objects.select_for_update()
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
    def _registration_fingerprint(registration) -> str:
        values = (
            registration.server_url,
            registration.issuer,
            registration.client_id,
            registration.authorization_endpoint,
            registration.token_endpoint,
            registration.registration_endpoint,
            registration.token_endpoint_auth_method,
            registration.encrypted_client_secret,
        )
        return hashlib.sha256("\\0".join(values).encode()).hexdigest()

    @staticmethod
    def _safe_https_url(value) -> bool:
        from urllib.parse import urlsplit

        if not isinstance(value, str) or not value:
            return False
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError):
            return False
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

    @staticmethod
    def _resource_matches(endpoint: str, resource: str) -> bool:
        from urllib.parse import urlsplit

        if not McpOAuthRepository._safe_https_url(
            endpoint
        ) or not McpOAuthRepository._safe_https_url(resource):
            return False
        root, candidate = urlsplit(endpoint), urlsplit(resource)
        if root.hostname.lower() != candidate.hostname.lower() or (
            root.port or 443
        ) != (candidate.port or 443):
            return False
        root_path = (root.path or "/").rstrip("/") or "/"
        resource_path = (candidate.path or "/").rstrip("/") or "/"
        return root_path == resource_path or root_path.startswith(
            resource_path.rstrip("/") + "/"
        )

    @staticmethod
    def grant_metadata(
        credential: Credential, *, expected_url: str | None = None
    ) -> dict | None:
        """Validate registration and grant envelope; keep grant material internal."""
        import json
        from datetime import datetime

        from django.utils import timezone

        from common.utils import decrypt_value

        try:
            endpoint = credential.service.oauth_server_url
            registration = credential.oauth_registration
            if (
                not isinstance(endpoint, str)
                or not endpoint
                or credential.oauth_server_url != endpoint
                or (expected_url is not None and endpoint != expected_url)
                or registration is None
                or registration.server_url != endpoint
                or not McpOAuthRepository._safe_https_url(registration.issuer)
                or not McpOAuthRepository._safe_https_url(
                    registration.authorization_endpoint
                )
                or not McpOAuthRepository._safe_https_url(registration.token_endpoint)
                or (
                    registration.registration_endpoint
                    and not McpOAuthRepository._safe_https_url(
                        registration.registration_endpoint
                    )
                )
                or not isinstance(registration.client_id, str)
                or not registration.client_id
                or registration.token_endpoint_auth_method
                not in {"none", "client_secret_basic", "client_secret_post"}
                or not isinstance(registration.encrypted_client_secret, str)
                or (
                    registration.token_endpoint_auth_method == "none"
                    and bool(registration.encrypted_client_secret)
                )
                or (
                    registration.token_endpoint_auth_method != "none"
                    and not registration.encrypted_client_secret
                )
                or not isinstance(registration.scopes_supported, list)
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
                or not McpOAuthRepository._resource_matches(endpoint, resource)
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

    @staticmethod
    def get_credential(credential_id: uuid.UUID, *, lock: bool = False):
        query = Credential.objects.select_related(
            "service", "oauth_registration", "user", "organization"
        )
        if lock:
            query = query.select_for_update()
        return query.filter(
            pk=credential_id, service__credential_type="mcp_oauth"
        ).first()

    @staticmethod
    def get_reconnect_target(credential_id: uuid.UUID, *, lock: bool = False):
        query = Credential.objects.select_related(
            "service", "oauth_registration", "user", "organization"
        )
        if lock:
            query = query.select_for_update()
        return query.filter(pk=credential_id).first()

    @staticmethod
    def owner_matches_state(state, credential: Credential) -> bool:
        if state.organization_credential:
            return (
                credential.organization_id == state.organization_id
                and credential.user_id is None
            )
        return (
            credential.user_id == state.user_id and credential.organization_id is None
        )

    @staticmethod
    def create_grant(
        *,
        user,
        organization,
        service: CredentialService,
        name: str,
        server_url: str,
        resource: str,
        registration: McpOAuthClientRegistration,
        encrypted_value: str,
        created_by,
    ) -> Credential:
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
    def update_locked_grant(credential: Credential, **values) -> None:
        for key, value in values.items():
            setattr(credential, key, value)
        credential.save(update_fields=[*values, "updated_at"])

    @staticmethod
    def persist_authorization_grant(*, state, encrypted_value: str) -> uuid.UUID:
        """Atomically recheck service/registration/owner state and persist grant."""
        from apps.organizations.repositories import MembershipRepository

        with transaction.atomic():
            service = McpOAuthRepository.get_service(state.service_id, lock=True)
            registration = McpOAuthRepository.get_registration(
                state.registration_id, lock=True
            )
            membership = MembershipRepository.get_for_user_and_org(
                state.user_id, state.organization_id, lock=True
            )
            from apps.credentials.mcp_oauth import _registration_fingerprint

            if (
                service is None
                or registration is None
                or membership is None
                or not membership.user.is_active
                or (state.organization_credential and membership.role != "admin")
                or service.credential_type != "mcp_oauth"
                or service.oauth_server_url != state.server_url
                or not McpOAuthRepository.service_visible_to_org(
                    state.service_id, state.organization_id
                )
                or not McpOAuthRepository._registration_valid(
                    registration, state.server_url, state.issuer
                )
                or _registration_fingerprint(registration)
                != state.registration_fingerprint
            ):
                raise ValueError("OAuth service, registration, or membership changed")
            if state.reconnect_existing:
                target = (
                    McpOAuthRepository.get_reconnect_target(
                        state.credential_id, lock=True
                    )
                    if state.credential_id
                    else None
                )
                if (
                    target is None
                    or target.service_id != state.service_id
                    or target.oauth_server_url != state.credential_server_url_snapshot
                    or not McpOAuthRepository.owner_matches_state(state, target)
                    or not McpOAuthRepository.service_visible_to_org(
                        target.service_id, state.organization_id
                    )
                ):
                    raise ValueError("OAuth reconnect target was deleted or changed")
                target.name = state.credential_name or target.name
                target.oauth_server_url = state.server_url
                target.oauth_resource = state.resource
                target.oauth_registration = registration
                target.oauth_status = "connected"
                target.encrypted_value = encrypted_value
                target.save(
                    update_fields=[
                        "name",
                        "oauth_server_url",
                        "oauth_resource",
                        "oauth_registration",
                        "oauth_status",
                        "encrypted_value",
                        "updated_at",
                    ]
                )
                return target.id
            if not McpOAuthRepository.service_active_for_org(
                state.service_id, state.organization_id, lock=True
            ):
                raise ValueError("OAuth service is inactive")
            return McpOAuthRepository.create_grant(
                user=None if state.organization_credential else state.user,
                organization=state.organization
                if state.organization_credential
                else None,
                service=service,
                name=state.credential_name or f"{service.name} OAuth",
                server_url=state.server_url,
                resource=state.resource,
                registration=registration,
                encrypted_value=encrypted_value,
                created_by=state.user,
            ).id

    @staticmethod
    def _registration_valid(registration, endpoint: str, issuer: str) -> bool:
        return bool(
            registration.server_url == endpoint
            and registration.issuer == issuer
            and McpOAuthRepository._safe_https_url(registration.issuer)
            and McpOAuthRepository._safe_https_url(registration.authorization_endpoint)
            and McpOAuthRepository._safe_https_url(registration.token_endpoint)
            and (
                not registration.registration_endpoint
                or McpOAuthRepository._safe_https_url(
                    registration.registration_endpoint
                )
            )
            and isinstance(registration.client_id, str)
            and bool(registration.client_id)
            and isinstance(registration.encrypted_client_secret, str)
            and registration.token_endpoint_auth_method
            in {"none", "client_secret_basic", "client_secret_post"}
            and (
                (
                    registration.token_endpoint_auth_method == "none"
                    and not registration.encrypted_client_secret
                )
                or (
                    registration.token_endpoint_auth_method != "none"
                    and bool(registration.encrypted_client_secret)
                )
            )
            and isinstance(registration.scopes_supported, list)
        )

    @staticmethod
    def update_oauth(credential_id: uuid.UUID, **values) -> int:
        return Credential.objects.filter(
            pk=credential_id, service__credential_type="mcp_oauth"
        ).update(**values)
