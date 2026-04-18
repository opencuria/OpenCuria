"""
Service layer for the credentials app.

All business logic related to credential management lives here.
"""

from __future__ import annotations

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
    files: list["ResolvedCredentialFile"] = field(default_factory=list)
    ssh_keys: list[str] = field(default_factory=list)
    credentials: list = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedCredentialFile:
    """A credential file that should exist during workspace operations."""

    target_path: str
    content: str
    mode: int = 0o600


class CredentialServiceSvc:
    """Business logic for the credential service catalog (read-only via API)."""

    def __init__(self) -> None:
        self.services = CredentialServiceRepository

    def list_services(self):
        """Return all credential services."""
        return self.services.list_all()

    def get_service(self, service_id: uuid.UUID):
        """Return a single credential service or raise."""
        svc = self.services.get_by_id(service_id)
        if svc is None:
            raise NotFoundError("CredentialService", str(service_id))
        return svc

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
    ):
        """Create a credential service with validation."""
        name = name.strip()
        if not name:
            raise ValueError("Name is required")

        normalized_slug = slugify(slug.strip() if slug.strip() else name)
        if not normalized_slug:
            raise ValueError("Slug cannot be empty")
        if self.services.get_by_slug(normalized_slug):
            raise ValueError(f"Credential service slug '{normalized_slug}' already exists")

        if credential_type not in CredentialType.values:
            raise ValueError("Invalid credential type")

        cleaned_env = env_var_name.strip().upper()
        cleaned_target_path = target_path.strip()
        if credential_type == CredentialType.ENV:
            if not cleaned_env:
                raise ValueError("env_var_name is required for env credentials")
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", cleaned_env):
                raise ValueError("env_var_name must be a valid environment variable name")
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

        if any(part in {"", ".", ".."} for part in PurePosixPath(path_without_home).parts):
            raise ValueError("target_path must not contain empty, '.' or '..' segments")


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
    ):
        """Create a personal credential owned by the user."""
        service = self._get_service_or_raise(service_id)

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
        service = self._get_service_or_raise(service_id)

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
        """
        cred = self.credentials.get_by_id(credential_id)
        if cred is None or not self._is_visible(cred, user=user, org_id=org_id):
            raise NotFoundError("Credential", str(credential_id))

        self._assert_can_edit(cred, user=user, org_id=org_id, is_admin=is_admin)
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

        self.assert_unique_workspace_credentials(credentials)
        return self._build_resolved_credentials(credentials)

    def resolve_workspace_credentials(self, workspace) -> ResolvedCredentials:
        """Resolve the credentials explicitly attached to a workspace."""
        credentials = list(workspace.credentials.all().select_related("service"))
        if not credentials:
            return ResolvedCredentials()

        return self._build_resolved_credentials(credentials)

    def _build_resolved_credentials(self, credentials: list) -> ResolvedCredentials:
        """Convert credential model instances into decrypted workspace values."""
        result = ResolvedCredentials(credentials=list(credentials))

        for cred in credentials:
            svc = cred.service
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

    def _get_service_or_raise(self, service_id: uuid.UUID):
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
