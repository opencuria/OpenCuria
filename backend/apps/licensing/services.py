"""Windows licensing: organization policy, workspace admission, activation.

License models (``WindowsLicensePolicy.mode``):

- ``per_device``: a Windows workspace holds one seat of one product key for
  its whole life (oldest key with a free seat first). The seat is freed only
  when the workspace deletion is confirmed, because the key stays installed
  in the guest until the VM is gone.
- ``per_user``: the workspace owner must hold a user license; the guest
  activates against KMS.
- ``unlicensed``: Windows runs without a product key or activation.

Admission runs whenever a Windows workspace boots (create, clone, start and
start retries). A workspace holding a key seat may always boot; otherwise the
current mode decides, so a mode change applies at the next boot and never
revokes held seats. Resets, updates and capture replacements keep the
workspace's license. The optional running cap applies in every mode. Linux
workspaces are never affected. Admission must run inside the caller's
lifecycle transaction, after the runner lock; it writes only after every
check passed.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from apps.organizations.services import OrganizationService
from apps.runners.enums import WorkspaceStatus
from apps.runners.guest_os import WINDOWS, normalize_guest_os
from common.exceptions import ConflictError, NotFoundError, ServiceError
from common.user_labels import owner_label
from common.utils import encrypt_value, hash_token

from .models import (
    WindowsLicenseKind,
    WindowsLicenseMode,
    WindowsLicensePolicy,
    WindowsWorkspaceLicense,
)
from .repositories import (
    WindowsLicensePolicyRepository,
    WindowsProductKeyRepository,
    WindowsUserLicenseRepository,
    WindowsWorkspaceLicenseRepository,
)

logger = structlog.get_logger(__name__)

_PRODUCT_KEY = re.compile(r"[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}")
_HOST_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")
MAX_SEATS = 100_000
MAX_LABEL_LENGTH = 255


class LicensingValidationError(ServiceError):
    """Invalid licensing input; adapters answer it with HTTP 400."""

    def __init__(self, message: str, code: str = "validation_error") -> None:
        super().__init__(message=message, code=code)


def normalize_product_key(value: str) -> str:
    """Return ``XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`` in upper case or raise."""
    key = re.sub(r"\s+", "", value or "").upper()
    if not _PRODUCT_KEY.fullmatch(key):
        raise LicensingValidationError(
            "Enter a product key as five groups of five letters or digits "
            "separated by hyphens.",
            code="windows_key_invalid",
        )
    return key


def normalize_kms_host(value: str | None) -> str:
    """Return a validated ``host``/``host:port`` (IPv6 as ``[addr]:port``)."""
    host = (value or "").strip()
    if not host:
        return ""
    port = None
    if host.startswith("["):
        address, _, rest = host[1:].partition("]")
        if rest:
            if not rest.startswith(":"):
                raise _invalid_kms_host()
            port = rest[1:]
        try:
            ipaddress.IPv6Address(address)
        except ValueError as exc:
            raise _invalid_kms_host() from exc
    else:
        name, separator, port_text = host.partition(":")
        if separator:
            port = port_text
        labels = name.rstrip(".").split(".")
        if len(name) > 253 or not all(_HOST_LABEL.fullmatch(p) for p in labels):
            raise _invalid_kms_host()
    if port is not None and not (port.isdigit() and 1 <= int(port) <= 65535):
        raise _invalid_kms_host()
    return host


def _invalid_kms_host() -> LicensingValidationError:
    return LicensingValidationError(
        "Enter the KMS host as a host name or IP address, optionally with :port."
    )


def is_windows(workspace: Any) -> bool:
    """Whether the workspace runs a Windows guest."""
    return normalize_guest_os(getattr(workspace, "guest_os", None)) == WINDOWS


@dataclass(frozen=True)
class WindowsProductKeyView:
    """A product key as admins see it (never the key itself)."""

    id: uuid.UUID
    label: str
    key_suffix: str
    seats: int
    used_seats: int
    created_at: datetime
    assignments: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class WindowsLicensingView:
    """The organization's Windows licensing state for its admins."""

    mode: str
    kms_host: str
    user_license_seats: int
    max_running: int | None
    product_keys: list[WindowsProductKeyView]
    members: list[dict[str, Any]]
    usage: dict[str, int]


class WindowsLicenseService:
    """Own Windows license policy, admission, delivery and admin edits."""

    def __init__(self) -> None:
        self.policies = WindowsLicensePolicyRepository
        self.keys = WindowsProductKeyRepository
        self.users = WindowsUserLicenseRepository
        self.workspaces = WindowsWorkspaceLicenseRepository
        self.organizations = OrganizationService()

    # -- Admission -------------------------------------------------------------

    def admit(self, workspace: Any) -> None:
        """Admit a Windows workspace to boot and store the license it boots with.

        Covers create, clone, start and start retries. The caller holds the
        runner and workspace locks of an open transaction.
        """
        if not is_windows(workspace):
            return
        organization_id = workspace.runner.organization_id
        policy = self.policies.lock(organization_id)
        held = self.workspaces.get(workspace.id, lock=True)
        self._admit_locked(workspace, policy, held)

    def admit_recreate(self, workspace: Any) -> None:
        """Admit a reset, update or capture replacement of the same workspace.

        The workspace identity stays, so it keeps its license row (an image
        line never changes its guest OS). Only a workspace that is not running
        now boots during the recreate and counts against the running cap. A
        Windows workspace without a row is admitted like a create.
        """
        if not is_windows(workspace):
            return
        organization_id = workspace.runner.organization_id
        policy = self.policies.lock(organization_id)
        held = self.workspaces.get(workspace.id, lock=True)
        if held is None:
            self._admit_locked(workspace, policy, held)
        elif workspace.status != WorkspaceStatus.RUNNING:
            self._check_running_cap(policy, organization_id, workspace.id)

    def _admit_locked(
        self,
        workspace: Any,
        policy: WindowsLicensePolicy,
        held: WindowsWorkspaceLicense | None,
    ) -> None:
        """Decide and store the license of a boot under the policy lock."""
        organization_id = workspace.runner.organization_id
        kind, key_id = self._decide(
            policy,
            organization_id=organization_id,
            owner_id=workspace.created_by_id,
            held=held,
            lock=True,
        )
        self._check_running_cap(policy, organization_id, workspace.id)
        self.workspaces.assign(workspace.id, kind=kind, product_key_id=key_id)
        logger.info(
            "windows_license_admitted",
            workspace_id=str(workspace.id),
            organization_id=str(organization_id),
            kind=kind,
            product_key_id=str(key_id) if key_id else None,
        )

    def preflight(
        self,
        *,
        organization_id: uuid.UUID,
        guest_os: str | None,
        owner_id: Any,
        workspace_id: uuid.UUID | None = None,
    ) -> None:
        """Reject an admission early, without locks or writes.

        Only :meth:`admit` inside the lifecycle transaction is authoritative.
        """
        if normalize_guest_os(guest_os) != WINDOWS:
            return
        policy = self.policies.current(organization_id)
        held = self.workspaces.get(workspace_id) if workspace_id else None
        self._decide(
            policy,
            organization_id=organization_id,
            owner_id=owner_id,
            held=held,
            lock=False,
        )
        self._check_running_cap(policy, organization_id, workspace_id)

    def preflight_workspace(self, workspace: Any) -> None:
        """Reject starting an existing workspace early (see :meth:`preflight`)."""
        self.preflight(
            organization_id=workspace.runner.organization_id,
            guest_os=getattr(workspace, "guest_os", None),
            owner_id=workspace.created_by_id,
            workspace_id=workspace.id,
        )

    def preflight_recreate(self, workspace: Any) -> None:
        """Reject a recreate early (see :meth:`admit_recreate`), without locks."""
        if not is_windows(workspace):
            return
        if self.workspaces.get(workspace.id) is None:
            self.preflight_workspace(workspace)
        elif workspace.status != WorkspaceStatus.RUNNING:
            organization_id = workspace.runner.organization_id
            self._check_running_cap(
                self.policies.current(organization_id), organization_id, workspace.id
            )

    def _decide(
        self,
        policy: WindowsLicensePolicy,
        *,
        organization_id: uuid.UUID,
        owner_id: Any,
        held: WindowsWorkspaceLicense | None,
        lock: bool,
    ) -> tuple[str, uuid.UUID | None]:
        """Return the license kind and key a workspace boots with, or raise."""
        if held is not None and held.kind == WindowsLicenseKind.DEVICE_KEY:
            return WindowsLicenseKind.DEVICE_KEY, held.product_key_id
        if policy.mode == WindowsLicenseMode.PER_DEVICE:
            key = self.keys.first_with_free_seat(organization_id, lock=lock)
            if key is None:
                raise ConflictError(
                    "All Windows licenses of this organization are in use.",
                    code="windows_license_unavailable",
                )
            return WindowsLicenseKind.DEVICE_KEY, key.id
        if policy.mode == WindowsLicenseMode.PER_USER:
            if owner_id is None or not self.users.has(organization_id, owner_id):
                raise ConflictError(
                    "Your account has no Windows license in this organization.",
                    code="windows_user_not_licensed",
                )
            return WindowsLicenseKind.USER, None
        return WindowsLicenseKind.UNLICENSED, None

    def _check_running_cap(
        self,
        policy: WindowsLicensePolicy,
        organization_id: uuid.UUID,
        workspace_id: uuid.UUID | None,
    ) -> None:
        """Reject a boot that would exceed the organization's running cap."""
        if policy.max_running is None:
            return
        running = self.workspaces.count_running(
            organization_id, exclude_workspace_id=workspace_id
        )
        if running + 1 > policy.max_running:
            raise ConflictError(
                "The maximum number of running Windows workspaces is reached.",
                code="windows_running_limit",
            )

    # -- Release ---------------------------------------------------------------

    def release_deleted(self) -> int:
        """Free licenses of workspaces already deleted (idempotent safety net)."""
        released = self.workspaces.release_deleted()
        if released:
            logger.info("windows_licenses_released", count=released)
        return released

    # -- Administration --------------------------------------------------------

    def licensing(self, *, org_id: uuid.UUID, user: Any) -> WindowsLicensingView:
        """Return the organization's licensing state (admins only)."""
        self.organizations.require_admin(user, org_id)
        return self._view(org_id)

    def update_policy(
        self, *, org_id: uuid.UUID, user: Any, changes: dict[str, Any]
    ) -> WindowsLicensingView:
        """Change mode, KMS host, user seats or running cap (admins only).

        Changing the mode never touches licenses workspaces already hold.
        """
        self.organizations.require_admin(user, org_id)
        values = self._policy_values(changes)
        with self.policies.locked(org_id) as policy:
            seats = values.get("user_license_seats")
            if seats is not None:
                granted = self.users.count(org_id)
                if seats < granted:
                    raise ConflictError(
                        f"{granted} members hold a Windows license. Revoke "
                        "licenses before reducing the seats below that.",
                        code="windows_user_seats_in_use",
                    )
            if values:
                self.policies.update(policy, **values)
        logger.info(
            "windows_license_policy_updated",
            organization_id=str(org_id),
            fields=sorted(values),
        )
        return self._view(org_id)

    def add_key(
        self,
        *,
        org_id: uuid.UUID,
        user: Any,
        product_key: str,
        label: str = "",
        seats: int = 1,
    ) -> WindowsProductKeyView:
        """Add a product key to the pool (admins only)."""
        self.organizations.require_admin(user, org_id)
        key = normalize_product_key(product_key)
        label = self._label(label)
        seats = self._seats(seats)
        fingerprint = hash_token(key)
        with self.policies.locked(org_id):
            if self.keys.fingerprint_exists(org_id, fingerprint):
                raise ConflictError(
                    "This product key is already in the pool.",
                    code="windows_key_duplicate",
                )
            created = self.keys.create(
                organization_id=org_id,
                label=label,
                encrypted_key=encrypt_value(key),
                key_suffix=key[-5:],
                fingerprint=fingerprint,
                seats=seats,
                created_by=user,
            )
        logger.info(
            "windows_product_key_added",
            organization_id=str(org_id),
            product_key_id=str(created.id),
            seats=seats,
        )
        return self._key_view(org_id, created.id)

    def update_key(
        self,
        *,
        org_id: uuid.UUID,
        user: Any,
        key_id: uuid.UUID,
        changes: dict[str, Any],
    ) -> WindowsProductKeyView:
        """Change a key's label or seats (admins only)."""
        self.organizations.require_admin(user, org_id)
        values: dict[str, Any] = {}
        if changes.get("label") is not None:
            values["label"] = self._label(changes["label"])
        if changes.get("seats") is not None:
            values["seats"] = self._seats(changes["seats"])
        with self.policies.locked(org_id):
            key = self.keys.get_for_org(key_id, org_id, lock=True)
            if key is None:
                raise NotFoundError("Product key")
            if "seats" in values:
                used = self.keys.used_seats(key.id)
                if values["seats"] < used:
                    raise ConflictError(
                        f"{used} workspaces use this key, so it needs at least "
                        f"{used} seats.",
                        code="windows_key_seats_in_use",
                    )
            self.keys.update(key, **values)
        return self._key_view(org_id, key_id)

    def delete_key(self, *, org_id: uuid.UUID, user: Any, key_id: uuid.UUID) -> None:
        """Delete a key without assigned seats (admins only)."""
        self.organizations.require_admin(user, org_id)
        with self.policies.locked(org_id):
            key = self.keys.get_for_org(key_id, org_id, lock=True)
            if key is None:
                raise NotFoundError("Product key")
            used = self.keys.used_seats(key.id)
            if used:
                raise ConflictError(
                    f"{used} workspace{'s' if used != 1 else ''} use this key. "
                    "It can be removed once they are deleted.",
                    code="windows_key_in_use",
                )
            self.keys.delete(key)
        logger.info(
            "windows_product_key_deleted",
            organization_id=str(org_id),
            product_key_id=str(key_id),
        )

    def grant_user(
        self, *, org_id: uuid.UUID, user: Any, user_id: Any
    ) -> WindowsLicensingView:
        """License an active member for Windows (admins only)."""
        self.organizations.require_admin(user, org_id)
        if self.organizations.get_active_membership(user_id, org_id) is None:
            raise NotFoundError("Member")
        with self.policies.locked(org_id) as policy:
            if not self.users.has(org_id, user_id):
                if self.users.count(org_id) >= policy.user_license_seats:
                    raise ConflictError(
                        "All Windows user licenses are assigned.",
                        code="windows_user_seats_exhausted",
                    )
                self.users.grant(org_id, user_id, created_by=user)
        return self._view(org_id)

    def revoke_user(
        self, *, org_id: uuid.UUID, user: Any, user_id: Any
    ) -> WindowsLicensingView:
        """Revoke a user license (admins only).

        Running workspaces keep running; their next start is refused.
        """
        self.organizations.require_admin(user, org_id)
        with self.policies.locked(org_id):
            self.users.revoke(org_id, user_id)
        return self._view(org_id)

    # -- Views and validation --------------------------------------------------

    def _view(self, org_id: uuid.UUID) -> WindowsLicensingView:
        """Compose the admin view: owner labels only, never workspace names."""
        policy = self.policies.current(org_id)
        licensed = {user.pk: user for user in self.users.licensed_users(org_id)}
        people = {
            membership.user.pk: membership.user
            for membership in self.organizations.list_active_members(org_id)
        }
        # License holders who left stay listed so admins can revoke them.
        people.update(licensed)
        members = sorted(
            (
                {
                    "user_id": str(person.pk),
                    "label": owner_label(person),
                    "licensed": person.pk in licensed,
                }
                for person in people.values()
            ),
            key=lambda member: (member["label"].lower(), member["user_id"]),
        )
        return WindowsLicensingView(
            mode=policy.mode,
            kms_host=policy.kms_host,
            user_license_seats=policy.user_license_seats,
            max_running=policy.max_running,
            product_keys=self._key_views(org_id),
            members=members,
            usage=self.workspaces.usage(org_id),
        )

    def _key_views(self, org_id: uuid.UUID) -> list[WindowsProductKeyView]:
        assignments: dict[uuid.UUID, list[dict[str, str]]] = {}
        for row in self.workspaces.assignments(org_id):
            assignments.setdefault(row.product_key_id, []).append(
                {
                    "owner_label": owner_label(row.workspace.created_by),
                    "activation_status": row.activation_status,
                }
            )
        return [
            WindowsProductKeyView(
                id=key.id,
                label=key.label,
                key_suffix=key.key_suffix,
                seats=key.seats,
                used_seats=key.used_seats,
                created_at=key.created_at,
                assignments=assignments.get(key.id, []),
            )
            for key in self.keys.list_for_org(org_id)
        ]

    def _key_view(self, org_id: uuid.UUID, key_id: uuid.UUID) -> WindowsProductKeyView:
        return next(view for view in self._key_views(org_id) if view.id == key_id)

    @staticmethod
    def _policy_values(changes: dict[str, Any]) -> dict[str, Any]:
        """Validate a partial policy update; ``max_running=None`` clears the cap."""
        values: dict[str, Any] = {}
        if changes.get("mode") is not None:
            if changes["mode"] not in WindowsLicenseMode.values:
                raise LicensingValidationError(
                    "mode must be one of: " + ", ".join(WindowsLicenseMode.values)
                )
            values["mode"] = changes["mode"]
        if "kms_host" in changes:
            values["kms_host"] = normalize_kms_host(changes["kms_host"])
        if changes.get("user_license_seats") is not None:
            seats = changes["user_license_seats"]
            if not 0 <= seats <= MAX_SEATS:
                raise LicensingValidationError(
                    f"user_license_seats must be between 0 and {MAX_SEATS}"
                )
            values["user_license_seats"] = seats
        if "max_running" in changes:
            cap = changes["max_running"]
            if cap is not None and not 0 <= cap <= MAX_SEATS:
                raise LicensingValidationError(
                    f"max_running must be between 0 and {MAX_SEATS}"
                )
            values["max_running"] = cap
        return values

    @staticmethod
    def _label(label: str | None) -> str:
        value = (label or "").strip()
        if len(value) > MAX_LABEL_LENGTH:
            raise LicensingValidationError(
                f"label must be at most {MAX_LABEL_LENGTH} characters"
            )
        return value

    @staticmethod
    def _seats(seats: int) -> int:
        if not 1 <= seats <= MAX_SEATS:
            raise LicensingValidationError(f"seats must be between 1 and {MAX_SEATS}")
        return seats
