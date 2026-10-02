"""
Service layer for the credentials app.

All business logic related to credential management lives here.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import structlog
from django.utils.text import slugify

from common.exceptions import AuthenticationError, ConflictError, NotFoundError
from common.utils import decrypt_value, encrypt_value, generate_ssh_keypair

from .enums import CredentialType
from .repositories import CredentialRepository, CredentialServiceRepository

logger = structlog.get_logger(__name__)


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
        """Return visible credential service definitions, including OAuth."""
        return (
            self.services.list_all()
            if org_id is None
            else self.services.list_visible_to_org(org_id)
        )

    def get_service(self, service_id: uuid.UUID, *, org_id: uuid.UUID | None = None):
        """Return a single credential service or raise."""
        if org_id is not None:
            svc = self.services.get_visible_by_id(service_id, org_id)
        else:
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
        organization_id: uuid.UUID | None = None,
        oauth_server_url: str = "",
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
            cleaned_server_url = self._validate_oauth_server_url(oauth_server_url)
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
                oauth_server_url=cleaned_server_url,
            )

        if oauth_server_url.strip():
            raise ValueError("oauth_server_url is only valid for MCP OAuth services")
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

    @staticmethod
    def _validate_oauth_server_url(value: str) -> str:
        """Validate a fixed public HTTPS MCP URL without plugin dependencies."""
        from urllib.parse import urlsplit

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
        self.activations.deactivate(org_id, service.id)
        return False


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
        self._assert_service_active(service, org_id)
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
        self._assert_service_active(service, organization_id)
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
            raise ValueError("MCP OAuth tokens can only be updated via OAuth connect")
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

        self._validate_credential_ownership(credentials, user=user, org_id=org_id)
        self._validate_oauth_bindings(credentials, user=user, org_id=org_id)
        owner_keys = [credential.service_id for credential in credentials]
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
        self.assert_unique_workspace_credentials(credentials)
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
        self._validate_credential_ownership(
            credentials,
            user=workspace.created_by,
            org_id=workspace.runner.organization_id,
        )
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
        self.assert_unique_workspace_credentials(credentials)
        resolved = self._build_resolved_credentials(normal_credentials)
        resolved.credentials = credentials
        resolved.oauth_credentials = [
            credential.id
            for credential in credentials
            if credential.service.credential_type == CredentialType.MCP_OAUTH
        ]
        return resolved

    def _validate_credential_ownership(self, credentials, *, user, org_id) -> None:
        """Fail closed on every credential owner and organization boundary."""
        if org_id is None or user is None:
            raise ConflictError("Workspace owner and organization are required")
        for credential in credentials:
            if credential.service.organization_id not in {None, org_id}:
                raise NotFoundError("Credential", str(credential.id))
            if credential.user_id is not None:
                if (
                    credential.user_id != user.id
                    or credential.organization_id is not None
                ):
                    raise NotFoundError("Credential", str(credential.id))
            elif credential.organization_id != org_id:
                raise NotFoundError("Credential", str(credential.id))

    def _validate_oauth_bindings(self, credentials, *, user, org_id):
        """Validate OAuth ownership and grant integrity without plugin lookups."""
        from .mcp_oauth import oauth_credential_status

        if org_id is None or user is None:
            raise ConflictError("Workspace owner and organization are required")
        for credential in credentials:
            if credential.service.credential_type != CredentialType.MCP_OAUTH:
                continue
            if (
                not credential.oauth_server_url
                or not credential.oauth_resource
                or not credential.oauth_registration_id
                or credential.oauth_server_url != credential.service.oauth_server_url
            ):
                raise ConflictError("MCP OAuth credential binding is invalid")
            if credential.oauth_status not in {
                "connected",
                "reconnect_required",
                "disconnected",
            }:
                raise ConflictError("MCP OAuth credential status is invalid")
            if credential.service.organization_id not in {None, org_id}:
                raise NotFoundError("Credential", str(credential.id))
            if credential.user_id is not None and credential.user_id != user.id:
                raise NotFoundError("Credential", str(credential.id))
            if (
                credential.organization_id is not None
                and credential.organization_id != org_id
            ):
                raise NotFoundError("Credential", str(credential.id))
            status = oauth_credential_status(credential)
            if credential.oauth_status == "connected" and not status["connected"]:
                raise ConflictError("MCP OAuth credential requires reconnect")

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

    @staticmethod
    def _assert_service_active(service, org_id: uuid.UUID | None) -> None:
        """Require visible org services to be activated before new credentials."""
        if org_id is None:
            return
        from .repositories import OrgCredentialServiceActivationRepository

        active_ids = OrgCredentialServiceActivationRepository.activated_service_ids(
            org_id
        )
        if service.id not in active_ids:
            raise ValueError("Credential service is inactive for this organization")

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
        if credential.service.organization_id not in {None, org_id}:
            return False
        if credential.user_id is not None:
            return credential.user_id == user.id and credential.organization_id is None
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


class CredentialOAuthSvc:
    """Orchestrate service-owned OAuth authorization and credential mutations."""

    def __init__(self) -> None:
        from .repositories import (
            CredentialRepository,
            CredentialServiceRepository,
            McpOAuthRepository,
            OrgCredentialServiceActivationRepository,
        )

        self.credentials = CredentialRepository
        self.services = CredentialServiceRepository
        self.oauth = McpOAuthRepository
        self.activations = OrgCredentialServiceActivationRepository

    def start_connection(
        self,
        *,
        user,
        organization,
        organization_credential: bool,
        request_scheme: str,
        request_host: str,
        service_id: uuid.UUID | None = None,
        credential_id: uuid.UUID | None = None,
        name: str = "",
    ) -> tuple[str, str, str]:
        """Start fresh authorization or reconnect one explicitly selected row."""
        from apps.organizations.repositories import MembershipRepository

        from .mcp_oauth import start_flow, validate_browser_request_origin

        if credential_id is None:
            membership = MembershipRepository.get_for_user_and_org(
                user.id, organization.id
            )
            if membership is None or not membership.user.is_active:
                raise AuthenticationError("Active organization membership required")
            if service_id is None:
                raise NotFoundError("CredentialService", "")
            service = self.services.get_visible_by_id(service_id, organization.id)
            if service is None or service.credential_type != CredentialType.MCP_OAUTH:
                raise NotFoundError("CredentialService", str(service_id))
            if service.id not in self.activations.activated_service_ids(
                organization.id
            ):
                raise ConflictError(
                    "Credential service is inactive for this organization",
                    code="service_inactive",
                )
            credential = None
        else:
            credential = self.oauth.get_reconnect_target(credential_id)
            if (
                credential is None
                or credential.service.credential_type != CredentialType.MCP_OAUTH
            ):
                raise NotFoundError("Credential", str(credential_id))
            service = self.services.get_visible_by_id(
                credential.service_id, organization.id
            )
            if service is None:
                raise NotFoundError("Credential", str(credential_id))
            if credential.user_id is not None:
                if credential.user_id != user.id:
                    raise NotFoundError("Credential", str(credential_id))
                organization_credential = False
            elif credential.organization_id == organization.id:
                organization_credential = True
            else:
                raise NotFoundError("Credential", str(credential_id))
        if credential_id is not None:
            membership = MembershipRepository.get_for_user_and_org(
                user.id, organization.id
            )
            if membership is None or not membership.user.is_active:
                raise AuthenticationError("Active organization membership required")
        if organization_credential and (
            membership is None or membership.role != "admin"
        ):
            raise AuthenticationError(
                "Admin role required for organization credentials"
            )
        validate_browser_request_origin(
            request_scheme=request_scheme, request_host=request_host
        )
        return start_flow(
            user=user,
            organization=organization,
            service=service,
            organization_credential=organization_credential,
            credential=credential,
            credential_name=(name or (credential.name if credential else "")).strip(),
        )

    def complete_authorization(
        self, *, raw_state: str, binding: str, code: str = "", error: str = ""
    ) -> uuid.UUID:
        """Validate and commit callbacks only while authorization rows are locked."""

        from django.db import transaction

        from apps.organizations.repositories import MembershipRepository
        from common.utils import decrypt_value

        from .mcp_oauth import (
            OAuthError,
            _consume_callback_state,
            _post_token,
            _registration_fingerprint,
            _registration_metadata_safe,
            _token_auth,
        )

        # Commit one-use state consumption independently so failed callbacks
        # cannot replay an authorization code.
        state = _consume_callback_state(raw_state, binding)
        with transaction.atomic():
            if error:
                raise OAuthError("Authorization was denied")
            if not isinstance(code, str) or not code or len(code) > 8192:
                raise OAuthError("OAuth callback is invalid")

            service = self.oauth.get_service(state.service_id, lock=True)
            registration = self.oauth.get_registration(state.registration_id, lock=True)
            membership = MembershipRepository.get_for_user_and_org(
                state.user_id, state.organization_id, lock=True
            )
            target = (
                self.oauth.get_reconnect_target(state.credential_id, lock=True)
                if state.reconnect_existing and state.credential_id
                else None
            )
            if (
                membership is None
                or not membership.user.is_active
                or (state.organization_credential and membership.role != "admin")
            ):
                raise AuthenticationError("OAuth authorization is no longer permitted")
            if (
                service is None
                or registration is None
                or service.credential_type != CredentialType.MCP_OAUTH
                or service.oauth_server_url != state.server_url
                or not self.oauth.service_visible_to_org(
                    state.service_id, state.organization_id
                )
                or not _registration_metadata_safe(
                    registration,
                    expected_url=state.server_url,
                    expected_issuer=state.issuer,
                    expected_callback=state.redirect_uri,
                )
                or _registration_fingerprint(registration)
                != state.registration_fingerprint
            ):
                raise AuthenticationError("OAuth endpoint or registration changed")
            if state.reconnect_existing:
                if (
                    target is None
                    or target.service_id != state.service_id
                    or target.oauth_server_url != state.credential_server_url_snapshot
                    or not self.oauth.owner_matches_state(state, target)
                    or not self.oauth.service_visible_to_org(
                        target.service_id, state.organization_id
                    )
                ):
                    raise NotFoundError("Credential", str(state.credential_id))
            elif not self.oauth.service_active_for_org(
                state.service_id, state.organization_id, lock=True
            ):
                raise ConflictError(
                    "OAuth service is inactive for this organization",
                    code="service_inactive",
                )

            secret = (
                decrypt_value(registration.encrypted_client_secret)
                if registration.encrypted_client_secret
                else ""
            )
            auth, post_secret = _token_auth(registration, secret)
            token = _post_token(
                registration.token_endpoint,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": state.redirect_uri,
                    "client_id": registration.client_id,
                    "code_verifier": decrypt_value(state.encrypted_code_verifier),
                    "resource": state.resource,
                },
                auth=auth,
                post_client_secret=post_secret,
            )
            from .mcp_oauth import _store_authorization_grant

            return _store_authorization_grant(state, token)

    def disconnect(
        self, *, credential_id: uuid.UUID, user, organization_id: uuid.UUID
    ) -> None:
        """Clear an owned OAuth grant without detaching existing workspace links."""
        from .mcp_oauth import disconnect_credential

        credential = self.credentials.get_by_id(credential_id)
        if (
            credential is None
            or credential.service.credential_type != CredentialType.MCP_OAUTH
            or credential.service.organization_id not in {None, organization_id}
        ):
            raise NotFoundError("Credential", str(credential_id))
        if credential.user_id is not None:
            if credential.user_id != user.id or credential.organization_id is not None:
                raise NotFoundError("Credential", str(credential_id))
        elif credential.organization_id != organization_id:
            raise NotFoundError("Credential", str(credential_id))
        elif not self._is_admin(user.id, organization_id):
            raise AuthenticationError("Admin role required")
        if not disconnect_credential(credential_id):
            raise NotFoundError("Credential", str(credential_id))

    @staticmethod
    def _user_is_authorized(state) -> bool:
        """Require active membership and current admin role for shared grants."""
        from apps.organizations.repositories import MembershipRepository

        membership = MembershipRepository.get_for_user_and_org(
            state.user_id, state.organization_id
        )
        return bool(
            membership
            and membership.user.is_active
            and (not state.organization_credential or membership.role == "admin")
        )

    @staticmethod
    def _is_admin(user_id: int, organization_id: uuid.UUID) -> bool:
        from apps.organizations.repositories import MembershipRepository

        membership = MembershipRepository.get_for_user_and_org(user_id, organization_id)
        return bool(
            membership and membership.user.is_active and membership.role == "admin"
        )
