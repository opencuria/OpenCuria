"""Persistence for Windows licensing; all ORM access of the app lives here.

Admission runs inside the runners app's locked lifecycle transactions and
admin edits inside :meth:`WindowsLicensePolicyRepository.locked`. Both lock
the organization's policy row first and key rows second, so every seat count
and change in one organization is serialized (lock order: runner → workspace
graph → policy row → key rows).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.db import connection, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet

from apps.runners.enums import TaskStatus, TaskType, WorkspaceStatus
from apps.runners.guest_os import WINDOWS
from apps.runners.models import Workspace, WorkspaceRecreateRequest

from .models import (
    WindowsActivationStatus,
    WindowsLicenseKind,
    WindowsLicensePolicy,
    WindowsProductKey,
    WindowsUserLicense,
    WindowsWorkspaceLicense,
)


def _windows_workspaces(organization_id: uuid.UUID) -> QuerySet[Workspace]:
    """Workspaces of the organization whose image is a Windows guest."""
    return Workspace.objects.filter(
        runner__organization_id=organization_id,
        base_image_instance__guest_os=WINDOWS,
    )


def _running() -> Q:
    """A workspace counts as running while it is created, runs or starts.

    Starting covers an unresolved start (resume) operation and an unfinished
    reset/update/capture replacement, which boots the workspace again.
    """
    live_recreate = WorkspaceRecreateRequest.objects.filter(
        workspace_id=OuterRef("pk")
    ).exclude(phase__in=["completed", "failed"])
    return (
        Q(status__in=[WorkspaceStatus.CREATING, WorkspaceStatus.RUNNING])
        | Q(
            current_task__type=TaskType.RESUME_WORKSPACE,
            current_task__status__in=[TaskStatus.PENDING, TaskStatus.IN_PROGRESS],
        )
        | Q(Exists(live_recreate))
    )


class WindowsLicensePolicyRepository:
    """Organization license settings, created lazily with defaults."""

    @staticmethod
    def current(organization_id: uuid.UUID) -> WindowsLicensePolicy:
        """Return the stored policy, or unsaved defaults when none exists yet."""
        policy = WindowsLicensePolicy.objects.filter(
            organization_id=organization_id
        ).first()
        return policy or WindowsLicensePolicy(organization_id=organization_id)

    @staticmethod
    def lock(organization_id: uuid.UUID) -> WindowsLicensePolicy:
        """Create the policy if needed and row-lock it in the caller's transaction.

        SQLite has no row locks; a no-op write takes its writer lock instead.
        """
        policy, _ = WindowsLicensePolicy.objects.get_or_create(
            organization_id=organization_id
        )
        if connection.vendor == "sqlite":
            WindowsLicensePolicy.objects.filter(pk=policy.pk).update(mode=F("mode"))
        return WindowsLicensePolicy.objects.select_for_update().get(pk=policy.pk)

    @staticmethod
    @contextmanager
    def locked(organization_id: uuid.UUID) -> Iterator[WindowsLicensePolicy]:
        """Hold the policy lock for an admin edit until it commits or rolls back."""
        with transaction.atomic():
            yield WindowsLicensePolicyRepository.lock(organization_id)

    @staticmethod
    def update(policy: WindowsLicensePolicy, **fields: Any) -> WindowsLicensePolicy:
        """Persist validated policy fields."""
        for name, value in fields.items():
            setattr(policy, name, value)
        policy.save(update_fields=[*fields, "updated_at"])
        return policy


class WindowsProductKeyRepository:
    """Product keys of an organization and their seat usage."""

    @staticmethod
    def list_for_org(organization_id: uuid.UUID) -> list[WindowsProductKey]:
        """Return keys oldest first, annotated with ``used_seats``."""
        return list(
            WindowsProductKey.objects.filter(organization_id=organization_id)
            .annotate(used_seats=Count("assignments"))
            .order_by("created_at", "id")
        )

    @staticmethod
    def get_for_org(
        key_id: uuid.UUID, organization_id: uuid.UUID, *, lock: bool = False
    ) -> WindowsProductKey | None:
        """Return one key of the organization, optionally row-locked."""
        query = WindowsProductKey.objects.filter(
            pk=key_id, organization_id=organization_id
        )
        if lock:
            query = query.select_for_update()
        return query.first()

    @staticmethod
    def first_with_free_seat(
        organization_id: uuid.UUID, *, lock: bool
    ) -> WindowsProductKey | None:
        """Return the oldest key (created_at, id) with a free seat."""
        query = WindowsProductKey.objects.filter(
            organization_id=organization_id
        ).order_by("created_at", "id")
        if lock:
            query = query.select_for_update()
        keys = list(query)
        used = WindowsProductKeyRepository.used_seats_many([key.id for key in keys])
        return next((key for key in keys if used.get(key.id, 0) < key.seats), None)

    @staticmethod
    def used_seats_many(key_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Return the number of workspaces holding a seat, per key."""
        if not key_ids:
            return {}
        rows = (
            WindowsWorkspaceLicense.objects.filter(product_key_id__in=key_ids)
            .values("product_key_id")
            .annotate(used=Count("pk"))
        )
        return {row["product_key_id"]: row["used"] for row in rows}

    @staticmethod
    def used_seats(key_id: uuid.UUID) -> int:
        """Return the number of workspaces holding a seat of one key."""
        return WindowsWorkspaceLicense.objects.filter(product_key_id=key_id).count()

    @staticmethod
    def fingerprint_exists(organization_id: uuid.UUID, fingerprint: str) -> bool:
        """Whether the organization already stores a key with this fingerprint."""
        return WindowsProductKey.objects.filter(
            organization_id=organization_id, fingerprint=fingerprint
        ).exists()

    @staticmethod
    def create(**fields: Any) -> WindowsProductKey:
        """Persist a new key (the caller encrypts it)."""
        return WindowsProductKey.objects.create(**fields)

    @staticmethod
    def update(key: WindowsProductKey, **fields: Any) -> WindowsProductKey:
        """Persist validated label/seat changes."""
        for name, value in fields.items():
            setattr(key, name, value)
        if fields:
            key.save(update_fields=list(fields))
        return key

    @staticmethod
    def delete(key: WindowsProductKey) -> None:
        """Delete a key without assigned seats."""
        key.delete()


class WindowsUserLicenseRepository:
    """Users licensed for Windows in an organization (per-user model)."""

    @staticmethod
    def has(organization_id: uuid.UUID, user_id: Any) -> bool:
        """Whether the user holds a Windows license in the organization."""
        return WindowsUserLicense.objects.filter(
            organization_id=organization_id, user_id=user_id
        ).exists()

    @staticmethod
    def count(organization_id: uuid.UUID) -> int:
        """Return the number of granted user licenses."""
        return WindowsUserLicense.objects.filter(
            organization_id=organization_id
        ).count()

    @staticmethod
    def licensed_users(organization_id: uuid.UUID) -> list[Any]:
        """Return the licensed users of the organization."""
        return [
            grant.user
            for grant in WindowsUserLicense.objects.filter(
                organization_id=organization_id
            ).select_related("user")
        ]

    @staticmethod
    def grant(organization_id: uuid.UUID, user_id: Any, *, created_by: Any) -> None:
        """Grant a license; granting twice keeps the first grant."""
        WindowsUserLicense.objects.get_or_create(
            organization_id=organization_id,
            user_id=user_id,
            defaults={"created_by": created_by},
        )

    @staticmethod
    def revoke(organization_id: uuid.UUID, user_id: Any) -> bool:
        """Revoke a license; return whether one existed."""
        deleted, _ = WindowsUserLicense.objects.filter(
            organization_id=organization_id, user_id=user_id
        ).delete()
        return bool(deleted)


class WindowsWorkspaceLicenseRepository:
    """The license rows of Windows workspaces."""

    @staticmethod
    def get(
        workspace_id: uuid.UUID, *, lock: bool = False
    ) -> WindowsWorkspaceLicense | None:
        """Return a workspace's license with its key, optionally row-locked."""
        query = WindowsWorkspaceLicense.objects.filter(workspace_id=workspace_id)
        if lock:
            if connection.features.has_select_for_update_of:
                query = query.select_for_update(of=("self",))
            else:
                query = query.select_for_update()
        return query.select_related("product_key").first()

    @staticmethod
    def assign(
        workspace_id: uuid.UUID, *, kind: str, product_key_id: uuid.UUID | None
    ) -> WindowsWorkspaceLicense:
        """Store the admitted license; a changed license awaits a new activation."""
        row = WindowsWorkspaceLicense.objects.filter(workspace_id=workspace_id).first()
        if row is None:
            return WindowsWorkspaceLicense.objects.create(
                workspace_id=workspace_id, kind=kind, product_key_id=product_key_id
            )
        if row.kind == kind and row.product_key_id == product_key_id:
            return row
        row.kind = kind
        row.product_key_id = product_key_id
        row.activation_status = WindowsActivationStatus.PENDING
        row.activation_code = ""
        row.checked_at = None
        row.save(
            update_fields=[
                "kind",
                "product_key",
                "activation_status",
                "activation_code",
                "checked_at",
                "updated_at",
            ]
        )
        return row

    @staticmethod
    def count_running(
        organization_id: uuid.UUID, *, exclude_workspace_id: uuid.UUID | None
    ) -> int:
        """Count the organization's running Windows workspaces (see ``_running``).

        Workspaces without a license row (created before licensing) count too.
        """
        query = _windows_workspaces(organization_id).filter(_running())
        if exclude_workspace_id is not None:
            query = query.exclude(pk=exclude_workspace_id)
        return query.count()

    @staticmethod
    def usage(organization_id: uuid.UUID) -> dict[str, int]:
        """Count live Windows workspaces, running ones and each license kind."""
        live = _windows_workspaces(organization_id).exclude(
            status=WorkspaceStatus.DELETED
        )
        kinds = dict(
            WindowsWorkspaceLicense.objects.filter(
                workspace__runner__organization_id=organization_id
            )
            .exclude(workspace__status=WorkspaceStatus.DELETED)
            .values_list("kind")
            .annotate(count=Count("pk"))
        )
        return {
            "windows_workspaces": live.count(),
            "running": live.filter(_running()).count(),
            **{kind.value: kinds.get(kind.value, 0) for kind in WindowsLicenseKind},
        }

    @staticmethod
    def assignments(organization_id: uuid.UUID) -> list[WindowsWorkspaceLicense]:
        """Return the organization's key seats with the workspace owner loaded."""
        return list(
            WindowsWorkspaceLicense.objects.filter(
                product_key__organization_id=organization_id
            )
            .select_related("workspace__created_by")
            .order_by("created_at", "workspace_id")
        )
