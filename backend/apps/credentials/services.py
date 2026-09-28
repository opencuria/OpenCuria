"""
Service layer for the credentials app.

All business logic related to credential management lives here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from django.utils.text import slugify

from common.exceptions import AuthenticationError, ConflictError, NotFoundError
from common.utils import decrypt_value, encrypt_value, generate_ssh_keypair

from .enums import CredentialType
from .repositories import CredentialRepository, CredentialServiceRepository

logger = logging.getLogger(__name__)


@dataclass
class ResolvedCredentials:
    """Result of resolving a set of credential IDs."""

    env_vars: dict[str, str] = field(default_factory=dict)
    files: list[ResolvedCredentialFile] = field(default_factory=list)
    ssh_keys: list[str] = field(default_factory=list)
    credentials: list = field(default_factory=list)
    oauth_credentials: list[uuid.UUID] = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class ResolvedCredentialFile:
    """A credential file that should exist during workspace operations."""

    target_path: str
    content: str
    mode: int = 0o600


class CredentialServiceSvc:
    """Business logic for the credential service catalog."""

    def __init__(self) -> None:
        self.services = CredentialServiceRepository

    def list_services(self, *, org_id: uuid.UUID | None = None):
        """Return credential services (org-safe: global + own org)."""
        services = (
            self.services.list_all()
            if org_id is None
            else self.services.list_visible_to_org(org_id)
        )
        return services.exclude(credential_type=CredentialType.MCP_OAUTH)

    def get_service(self, service_id: uuid.UUID, *, org_id: uuid.UUID | None = None):
        """Return a single credential service or raise."""
        if org_id is not None:
            svc = self.services.get_visible_by_id(service_id, org_id)
        else:
            svc = self.services.get_by_id(service_id)
        if svc is None:
            raise NotFoundError("CredentialService", str(service_id))
        return svc

    @staticmethod
    def plugin_oauth_service_slug(plugin_slug: str, requirement_key: str) -> str:
        """Return a deterministic slug unique to one OAuth requirement."""
        base = f"{plugin_slug.lower()}-{requirement_key.lower()}-oauth"
        if len(base) <= 255 and requirement_key == requirement_key.lower():
            return base
        digest = hashlib.sha256(
            f"{plugin_slug}\0{requirement_key}".encode()
        ).hexdigest()[:12]
        suffix = f"-{digest}"
        return f"{base[: 255 - len(suffix)].rstrip('-_')}{suffix}"

    @staticmethod
    def create_plugin_oauth_service(
        *,
        name: str,
        slug: str,
        description: str,
        organization_id: uuid.UUID,
        oauth_plugin_slug: str,
        oauth_requirement_key: str,
    ):
        """Create an isolated OAuth credential service for a plugin requirement."""
        clean_name = (name or "").strip()
        normalized_slug = (slug or clean_name).strip().lower()
        if not clean_name or not normalized_slug:
            raise ValueError("OAuth service name and slug are required")
        if not re.fullmatch(r"[a-z0-9_-]+", normalized_slug):
            raise ValueError("OAuth service slug must be URL-safe")
        expected_slug = CredentialServiceSvc.plugin_oauth_service_slug(
            oauth_plugin_slug, oauth_requirement_key
        )
        if normalized_slug != expected_slug:
            raise ValueError("OAuth service slug must identify its plugin requirement")
        if CredentialServiceRepository.get_org_by_slug(
            normalized_slug, organization_id
        ):
            raise ValueError(
                f"Credential service slug '{normalized_slug}' already exists "
                "in this organization"
            )
        service = CredentialServiceRepository.create(
            name=clean_name,
            slug=normalized_slug,
            description=(description or "").strip(),
            credential_type=CredentialType.MCP_OAUTH,
            env_var_name="",
            target_path="",
            label="Connect",
            organization_id=organization_id,
        )
        service.plugin_owned = True
        service.oauth_plugin_slug = oauth_plugin_slug
        service.oauth_requirement_key = oauth_requirement_key
        service.save(
            update_fields=[
                "plugin_owned",
                "oauth_plugin_slug",
                "oauth_requirement_key",
            ]
        )
        return service

    def create_service(
        self,
        *,
        name: str,
        slug: str,
        description: str,
        credential_type: str,
        env_var_name: str,
        target_path: str,
        label: str,
        organization_id: uuid.UUID | None = None,
    ):
        """Create a credential service with validation.

        Global services (organization_id None) enforce a globally unique
        slug; org-owned services enforce uniqueness per org + slug.
        """
        name = name.strip()
        if not name:
            raise ValueError("Name is required")

        normalized_slug = slugify(slug.strip() if slug.strip() else name)
        if not normalized_slug:
            raise ValueError("Slug cannot be empty")
        if organization_id is None:
            if self.services.get_global_by_slug(normalized_slug):
                raise ValueError(
                    f"Credential service slug '{normalized_slug}' already exists"
                )
        elif self.services.get_org_by_slug(normalized_slug, organization_id):
            raise ValueError(
                f"Credential service slug '{normalized_slug}' already exists "
                "in this organization"
            )

        if credential_type not in CredentialType.values:
            raise ValueError("Invalid credential type")
        if credential_type == CredentialType.MCP_OAUTH:
            raise ValueError(
                "MCP OAuth services can only be defined by an OAuth plugin"
            )

        cleaned_env = env_var_name.strip().upper()
        cleaned_target_path = target_path.strip()
        if credential_type == CredentialType.ENV:
            if not cleaned_env:
                raise ValueError("env_var_name is required for env credentials")
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", cleaned_env):
                raise ValueError(
                    "env_var_name must be a valid environment variable name"
                )
            cleaned_target_path = ""
        elif credential_type == CredentialType.FILE:
            cleaned_env = ""
            if not cleaned_target_path:
                raise ValueError("target_path is required for file credentials")
            self._validate_target_path(cleaned_target_path)
        else:
            cleaned_env = ""
            cleaned_target_path = ""

        return self.services.create(
            name=name,
            slug=normalized_slug,
            description=description.strip(),
            credential_type=credential_type,
            env_var_name=cleaned_env,
            target_path=cleaned_target_path,
            label=label.strip(),
            organization_id=organization_id,
        )

    def _validate_target_path(self, target_path: str) -> None:
        """Validate a workspace file target path."""

        if "\x00" in target_path:
            raise ValueError("target_path must not contain null bytes")

        normalized = target_path.strip()
        if not normalized:
            raise ValueError("target_path is required for file credentials")
        if normalized in {"~", "${HOME}", "/"} or normalized.endswith("/"):
            raise ValueError("target_path must point to a file path")

        path_without_home = normalized
        if normalized.startswith("~/"):
            path_without_home = normalized[2:]
        elif normalized.startswith("${HOME}/"):
            path_without_home = normalized[len("${HOME}/") :]
        elif normalized.startswith("/"):
            path_without_home = normalized[1:]

        if any(
            part in {"", ".", ".."} for part in PurePosixPath(path_without_home).parts
        ):
            raise ValueError("target_path must not contain empty, '.' or '..' segments")


class OrgCredentialServiceActivationSvc:
    """Business logic for org credential-service activations."""

    def __init__(self) -> None:
        from .repositories import OrgCredentialServiceActivationRepository

        self.activations = OrgCredentialServiceActivationRepository

    def activated_service_ids(self, org_id: uuid.UUID) -> set[uuid.UUID]:
        """Return activated service IDs for the org."""
        return self.activations.activated_service_ids(org_id)

    def set_activation(self, *, org_id: uuid.UUID, service, active: bool) -> bool:
        """Activate/deactivate a service for the org. Returns new state.

        Deactivation is blocked (409
        ``plugin_service_activation_in_use``) while any *org-enabled*
        plugin of this org references the service in its credential
        requirements — the service-activation switch only gates UI
        availability, but silently revoking it under an enabled plugin
        would break the documented auto-activated setup. Reactivation
        always succeeds (idempotent).
        """
        if active:
            self.activations.ensure_activated(org_id, [service.id])
            return True
        if self._blocks_deactivation(org_id, service.id):
            raise ConflictError(
                "Credential service cannot be deactivated while org-enabled "
                "plugins require it",
                code="plugin_service_activation_in_use",
            )
        self.activations.deactivate(org_id, service.id)
        return False

    @staticmethod
    def _blocks_deactivation(org_id: uuid.UUID, service_id: uuid.UUID) -> bool:
        """Return True when an org-enabled plugin requires *service_id*."""
        from apps.plugins.repositories import (
            OrgPluginActivationRepository,
            PluginCredentialRequirementRepository,
        )

        org_plugin_ids = set(OrgPluginActivationRepository.enabled_plugin_ids(org_id))
        if not org_plugin_ids:
            return False
        return PluginCredentialRequirementRepository.requirements_exist_for_plugins(
            org_plugin_ids, service_id
        )


class CredentialSvc:
    """Business logic for credential CRUD and resolution.

    Credentials are owned by either a user (personal) or an organization.
    - Personal credentials: visible across all of a user's organizations;
      only the owner may edit/delete.
    - Org credentials: visible to all org members; only admins may edit/delete.
    """

    def __init__(self) -> None:
        self.credentials = CredentialRepository
        self.service_repo = CredentialServiceRepository

    # -- Queries ------------------------------------------------------------

    def list_credentials(self, user, org_id: uuid.UUID):
        """Return all credentials visible to the user in the given org.

        Includes personal credentials owned by the user and org credentials
        belonging to the organization.
        """
        return list(self.credentials.list_for_user_in_org(user.id, org_id))

    # -- Create -------------------------------------------------------------

    def create_personal_credential(
        self,
        *,
        service_id: uuid.UUID,
        name: str | None,
        value: str | None,
        user,
        org_id: uuid.UUID | None = None,
    ):
        """Create a personal credential owned by the user.

        When ``org_id`` is given, the service must be visible in that org
        (global or org-owned); otherwise any existing service resolves.
        """
        service = self._get_service_or_raise(service_id, org_id=org_id)
        if service.credential_type == CredentialType.MCP_OAUTH:
            raise ValueError("MCP OAuth credentials must be created via OAuth connect")

        if not name:
            name = f"{service.name} Credential"

        encrypted, public_key = self._encrypt_for_service(service, value)

        credential = self.credentials.create_personal(
            user=user,
            service=service,
            name=name,
            encrypted_value=encrypted,
            public_key=public_key,
        )
        logger.info(
            "Personal credential created: %s (service=%s, user=%s)",
            credential.id,
            service.name,
            user.id,
        )
        return credential

    def create_org_credential(
        self,
        *,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        name: str | None,
        value: str | None,
        user,
    ):
        """Create an org-scoped credential. Caller must verify admin role."""
        service = self._get_service_or_raise(service_id, org_id=organization_id)
        if service.credential_type == CredentialType.MCP_OAUTH:
            raise ValueError("MCP OAuth credentials must be created via OAuth connect")

        if not name:
            name = f"{service.name} Credential"

        encrypted, public_key = self._encrypt_for_service(service, value)

        credential = self.credentials.create_org(
            organization_id=organization_id,
            service=service,
            name=name,
            encrypted_value=encrypted,
            public_key=public_key,
            created_by=user,
        )
        logger.info(
            "Org credential created: %s (service=%s, org=%s)",
            credential.id,
            service.name,
            organization_id,
        )
        return credential

    # -- Read ---------------------------------------------------------------

    def get_public_key(
        self,
        credential_id: uuid.UUID,
        *,
        org_id: uuid.UUID,
        user,
    ) -> str:
        """Return the public key for an SSH credential.

        Raises NotFoundError if the credential does not exist, is not
        visible to the user, or has no public key.
        """
        cred = self.credentials.get_by_id(credential_id)
        if cred is None or not self._is_visible(cred, user=user, org_id=org_id):
            raise NotFoundError("Credential", str(credential_id))
        if not cred.public_key:
            raise NotFoundError("PublicKey", str(credential_id))
        return cred.public_key

    # -- Update -------------------------------------------------------------

    def update_credential(
        self,
        *,
        credential_id: uuid.UUID,
        org_id: uuid.UUID,
        user,
        is_admin: bool,
        name: str | None = None,
        value: str | None = None,
    ):
        """Update a credential's name and/or value.

        Ownership rules:
        - Personal credential: only the owner may edit.
        - Org credential: only org admins may edit.
        """
        cred = self.credentials.get_by_id(credential_id)
        if cred is None or not self._is_visible(cred, user=user, org_id=org_id):
            raise NotFoundError("Credential", str(credential_id))

        self._assert_can_edit(cred, user=user, org_id=org_id, is_admin=is_admin)

        if (
            cred.service.credential_type == CredentialType.MCP_OAUTH
            and value is not None
        ):
            raise ValueError("MCP OAuth tokens can only be updated via OAuth refresh")
        encrypted = encrypt_value(value) if value else None
        return self.credentials.update(cred, name=name, encrypted_value=encrypted)

    # -- Delete -------------------------------------------------------------

    def delete_credential(
        self,
        credential_id: uuid.UUID,
        *,
        org_id: uuid.UUID,
        user,
        is_admin: bool,
    ) -> None:
        """Delete a credential.

        Ownership rules:
        - Personal credential: only the owner may delete.
        - Org credential: only org admins may delete.

        Deletion is blocked (409 ``plugin_credentials_in_use``) when the
        credential is required for an *effective* plugin activation of
        any workspace it is attached to — deleting would silently break
        the runtime. Re-attach-free personal credentials and unused
        credentials delete normally. Updating the value (PATCH) never
        blocks: the attachment stays intact.
        """
        from apps.plugins.services import PluginService as _PluginService

        cred = self.credentials.get_by_id(credential_id)
        if cred is None or not self._is_visible(cred, user=user, org_id=org_id):
            raise NotFoundError("Credential", str(credential_id))

        self._assert_can_edit(cred, user=user, org_id=org_id, is_admin=is_admin)
        if cred.service.credential_type == CredentialType.MCP_OAUTH:
            raise ConflictError(
                "MCP OAuth credentials can only be removed via OAuth disconnect",
                code="oauth_flow_required",
            )

        gaps = _PluginService().blocking_plugin_gaps_for_credential(cred, org_id=org_id)
        if gaps:
            raise ConflictError(
                "Cannot delete credential required by active workspace "
                "plugins: "
                + ", ".join(
                    f"{g['workspace_id']}:{g['plugin_id']}:{g['key']}" for g in gaps
                ),
                code="plugin_credentials_in_use",
            )
        self.credentials.delete(credential_id)
        logger.info("Credential deleted: %s", credential_id)

    # -- Resolution ---------------------------------------------------------

    def resolve_credentials(
        self,
        credential_ids: list[uuid.UUID],
        *,
        org_id: uuid.UUID,
        user,
    ) -> ResolvedCredentials:
        """Resolve a list of credential IDs into env vars and SSH keys.

        Decrypts each credential and categorises it by type:
        - env credentials become {env_var_name: plaintext} entries.
        - file credentials become target-path/content pairs.
        - ssh_key credentials become decrypted private keys in a list.

        Resolves credentials that are visible to the user in the org
        (personal credentials owned by the user OR org credentials).

        Raises NotFoundError if any credential is not found or not visible.
        """
        if not credential_ids:
            return ResolvedCredentials()

        credentials = self.credentials.get_many_by_ids(
            credential_ids,
            org_id=org_id,
            user_id=user.id,
        )

        found_ids = {c.id for c in credentials}
        missing = set(credential_ids) - found_ids
        if missing:
            raise NotFoundError(
                "Credential",
                ", ".join(str(m) for m in missing),
            )

        self._validate_oauth_bindings(credentials, user=user, org_id=org_id)
        owner_keys = [
            (credential.service_id, credential.oauth_server_id)
            if credential.service.credential_type == CredentialType.MCP_OAUTH
            else (credential.service_id, None)
            for credential in credentials
        ]
        if len(owner_keys) != len(set(owner_keys)):
            raise ConflictError(
                "Only one credential per credential service can be attached "
                "to a workspace"
            )
        normal_credentials = [
            credential
            for credential in credentials
            if credential.service.credential_type != CredentialType.MCP_OAUTH
        ]
        self.assert_unique_workspace_credentials(normal_credentials)
        resolved = self._build_resolved_credentials(normal_credentials)
        resolved.credentials = credentials
        resolved.oauth_credentials = [
            credential.id
            for credential in credentials
            if credential.service.credential_type == CredentialType.MCP_OAUTH
        ]
        return resolved

    def resolve_workspace_credentials(self, workspace) -> ResolvedCredentials:
        """Resolve the credentials explicitly attached to a workspace."""
        credentials = list(workspace.credentials.all().select_related("service"))
        if not credentials:
            return ResolvedCredentials()
        self._validate_oauth_bindings(
            credentials,
            user=workspace.created_by,
            org_id=workspace.runner.organization_id,
        )
        normal_credentials = [
            credential
            for credential in credentials
            if credential.service.credential_type != CredentialType.MCP_OAUTH
        ]
        self.assert_unique_workspace_credentials(normal_credentials)
        resolved = self._build_resolved_credentials(normal_credentials)
        resolved.credentials = credentials
        resolved.oauth_credentials = [
            credential.id
            for credential in credentials
            if credential.service.credential_type == CredentialType.MCP_OAUTH
        ]
        return resolved

    def _validate_oauth_bindings(self, credentials, *, user, org_id):
        """Fail closed when an OAuth credential is not bound to a visible plugin."""
        from django.db.models import Q

        from apps.plugins.models import PluginMcpServer
        from apps.plugins.services import validate_oauth_server_url

        if org_id is None or user is None:
            raise ConflictError("Workspace owner and organization are required")

        for credential in credentials:
            if credential.service.credential_type != CredentialType.MCP_OAUTH:
                continue
            if (
                not credential.oauth_server_id
                or not credential.oauth_server_url
                or not credential.oauth_resource
                or not credential.oauth_registration_id
            ):
                raise ConflictError("MCP OAuth credential binding is invalid")
            try:
                normalized_url = validate_oauth_server_url(credential.oauth_server_url)
                if normalized_url != credential.oauth_server_url:
                    raise ValueError
            except ValueError:
                raise ConflictError(
                    "MCP OAuth credential server URL is invalid"
                ) from None
            if credential.oauth_status not in {
                "connected",
                "reconnect_required",
                "disconnected",
            }:
                raise ConflictError("MCP OAuth credential status is invalid")
            if (
                credential.oauth_status == "connected"
                and not credential.encrypted_value
            ):
                raise ConflictError("MCP OAuth credential requires reconnect")
            if credential.service.organization_id not in {None, org_id}:
                raise NotFoundError("Credential", str(credential.id))
            if credential.user_id is not None and credential.user_id != user.id:
                raise NotFoundError("Credential", str(credential.id))
            if (
                credential.organization_id is not None
                and credential.organization_id != org_id
            ):
                raise NotFoundError("Credential", str(credential.id))

            servers = PluginMcpServer.objects.filter(
                plugin__slug=credential.service.oauth_plugin_slug,
                id=credential.oauth_server_id,
                url=credential.oauth_server_url,
                auth_type="oauth",
                oauth_requirement_key=credential.service.oauth_requirement_key,
                plugin__credential_requirements__credential_service_id=credential.service_id,
                plugin__credential_requirements__required=True,
            ).filter(
                Q(plugin__organization__isnull=True) | Q(plugin__organization_id=org_id)
            )
            server = next(
                (
                    candidate
                    for candidate in servers
                    if candidate.plugin.credential_requirements.filter(
                        key=candidate.oauth_requirement_key,
                        credential_service_id=credential.service_id,
                        required=True,
                    ).exists()
                ),
                None,
            )
            if server is None:
                raise ConflictError(
                    "MCP OAuth credential is not bound to a visible plugin server"
                )
            if (
                credential.service.oauth_plugin_slug
                and server.plugin.slug != credential.service.oauth_plugin_slug
            ):
                raise ConflictError(
                    "MCP OAuth credential service belongs to another plugin"
                )
            if credential.oauth_status == "connected":
                try:
                    data = json.loads(decrypt_value(credential.encrypted_value))
                except Exception:
                    raise ConflictError(
                        "MCP OAuth credential requires reconnect"
                    ) from None
                if (
                    data.get("server_id") != str(server.id)
                    or data.get("server_url") != server.url
                    or data.get("resource") != credential.oauth_resource
                    or data.get("registration_id")
                    != str(credential.oauth_registration_id)
                ):
                    raise ConflictError("MCP OAuth credential binding is inconsistent")

    def _build_resolved_credentials(self, credentials: list) -> ResolvedCredentials:
        """Convert credential model instances into decrypted workspace values."""
        result = ResolvedCredentials(credentials=list(credentials))

        for cred in credentials:
            svc = cred.service
            if svc.credential_type == CredentialType.MCP_OAUTH:
                result.oauth_credentials.append(cred.id)
                continue
            plaintext = decrypt_value(cred.encrypted_value)
            if svc.credential_type == CredentialType.SSH_KEY:
                result.ssh_keys.append(plaintext)
            elif svc.credential_type == CredentialType.FILE and svc.target_path:
                result.files.append(
                    ResolvedCredentialFile(
                        target_path=svc.target_path,
                        content=plaintext,
                    )
                )
            elif svc.credential_type == CredentialType.ENV and svc.env_var_name:
                result.env_vars[svc.env_var_name] = plaintext

        return result

    # -- Internal helpers ---------------------------------------------------

    def assert_unique_workspace_credentials(self, credentials: list) -> None:
        """Reject attaching multiple credentials from the same service."""
        seen_service_ids: set[uuid.UUID] = set()

        for credential in credentials:
            if credential.service_id in seen_service_ids:
                raise ConflictError(
                    "Only one credential per credential service can be attached "
                    f"to a workspace. Conflicting service: {credential.service.name}."
                )
            seen_service_ids.add(credential.service_id)

    def _get_service_or_raise(self, service_id: uuid.UUID, *, org_id=None):
        if org_id is not None:
            service = self.service_repo.get_visible_by_id(service_id, org_id)
        else:
            service = self.service_repo.get_by_id(service_id)
        if service is None:
            raise NotFoundError("CredentialService", str(service_id))
        return service

    def _encrypt_for_service(self, service, value: str | None) -> tuple[str, str]:
        """Return (encrypted_value, public_key) for the given service type."""
        if service.credential_type == CredentialType.SSH_KEY:
            private_pem, public_openssh = generate_ssh_keypair()
            return encrypt_value(private_pem), public_openssh
        else:
            if not value:
                raise ValueError("A value is required for non-SSH credentials.")
            return encrypt_value(value), ""

    def _is_visible(self, credential, *, user, org_id: uuid.UUID) -> bool:
        """Return True if the credential is visible to the user in this org."""
        if credential.user_id is not None:
            return credential.user_id == user.id
        return credential.organization_id == org_id

    def _assert_can_edit(
        self, credential, *, user, org_id: uuid.UUID, is_admin: bool
    ) -> None:
        """Raise AuthenticationError if the user cannot edit this credential."""
        if credential.user_id is not None:
            # Personal credential — only the owner may edit
            if credential.user_id != user.id:
                raise AuthenticationError("You do not own this credential")
        else:
            # Org credential — must be admin of the owning org
            if credential.organization_id != org_id:
                raise NotFoundError("Credential", str(credential.id))
            if not is_admin:
                raise AuthenticationError(
                    "Only org admins can edit organization credentials"
                )
