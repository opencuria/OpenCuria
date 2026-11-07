"""Data access for published apps. All ORM and file storage access lives here."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Max, ProtectedError, Q, QuerySet

from .models import App, AppBlob, AppFile, AppVersion


class AppRepository:
    """Apps, versions, version files and their content-addressed blobs."""

    @staticmethod
    def _owned(organization_id: uuid.UUID, owner_id: int) -> QuerySet[App]:
        return App.objects.filter(organization_id=organization_id, owner_id=owner_id)

    @staticmethod
    def list_owned(organization_id: uuid.UUID, owner_id: int) -> list[App]:
        """Apps of one user in one organization, newest activity first."""
        return list(
            AppRepository._owned(organization_id, owner_id).select_related("workspace")
        )

    @staticmethod
    def get_owned(
        app_id: uuid.UUID, organization_id: uuid.UUID, owner_id: int
    ) -> App | None:
        """One app of the user."""
        return (
            AppRepository._owned(organization_id, owner_id)
            .filter(id=app_id)
            .select_related("workspace")
            .first()
        )

    @staticmethod
    def versions_by_app(
        app_ids: Iterable[uuid.UUID], *, latest_only: bool = False
    ) -> dict[uuid.UUID, list[AppVersion]]:
        """Versions per app, newest first; only the newest with *latest_only*."""
        grouped: dict[uuid.UUID, list[AppVersion]] = {}
        rows = (
            AppVersion.objects.filter(app_id__in=list(app_ids))
            .select_related("icon", "app")
            .order_by("app_id", "-number")
        )
        for version in rows:
            versions = grouped.setdefault(version.app_id, [])
            if not (latest_only and versions):
                versions.append(version)
        return grouped

    @staticmethod
    def get_version(app_id: uuid.UUID, version_id: uuid.UUID) -> AppVersion | None:
        """A version of *app_id* with its icon."""
        return (
            AppVersion.objects.filter(app_id=app_id, id=version_id)
            .select_related("icon", "app")
            .first()
        )

    @staticmethod
    def version_files(version_id: uuid.UUID, platform: str) -> list[AppFile]:
        """The bundle and assets of a version on *platform*."""
        return list(
            AppFile.objects.filter(version_id=version_id, platform=platform)
            .select_related("blob")
            .order_by("kind", "key")
        )

    @staticmethod
    def version_platforms(version_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, dict]:
        """Per version: platform -> total bytes of its bundle and assets."""
        sizes: dict[uuid.UUID, dict] = {}
        rows = AppFile.objects.filter(version_id__in=list(version_ids)).values_list(
            "version_id", "platform", "blob__size"
        )
        for version_id, platform, size in rows:
            per_version = sizes.setdefault(version_id, {})
            per_version[platform] = per_version.get(platform, 0) + size
        return sizes

    @staticmethod
    def blob_in_version(version_id: uuid.UUID, sha256: str) -> AppBlob | None:
        """A blob only when the version references it (file or icon)."""
        return (
            AppBlob.objects.filter(sha256=sha256)
            .filter(
                Q(
                    sha256__in=AppFile.objects.filter(version_id=version_id).values(
                        "blob"
                    )
                )
                | Q(sha256__in=AppVersion.objects.filter(id=version_id).values("icon"))
            )
            .first()
        )

    @staticmethod
    def ensure_blob(sha256: str, md5: str, content: bytes) -> AppBlob:
        """Return the blob for *content*, storing it on first use."""
        existing = AppBlob.objects.filter(sha256=sha256).first()
        if existing is not None and existing.file.storage.exists(existing.file.name):
            return existing
        blob = existing or AppBlob(sha256=sha256, md5=md5, size=len(content))
        blob.file.save(sha256, ContentFile(content), save=False)
        try:
            with transaction.atomic():
                blob.save()
        except IntegrityError:
            # A concurrent publish stored the same content first.
            blob.file.storage.delete(blob.file.name)
            return AppBlob.objects.get(sha256=sha256)
        return blob

    @staticmethod
    @transaction.atomic
    def create_version(
        *,
        organization_id: uuid.UUID,
        owner_id: int,
        workspace_id: uuid.UUID | None,
        slug: str,
        name: str,
        project_path: str,
        version_name: str,
        sdk_version: str,
        notes: str,
        expo_config: dict,
        icon: AppBlob | None,
        files: list[tuple[str, str, str, str, AppBlob]],
    ) -> AppVersion:
        """Create the app if needed and append the next version atomically.

        ``files`` holds ``(platform, kind, key, extension, blob)`` rows.
        """
        app, created = App.objects.select_for_update().get_or_create(
            organization_id=organization_id,
            owner_id=owner_id,
            slug=slug,
            defaults={
                "name": name,
                "workspace_id": workspace_id,
                "project_path": project_path,
            },
        )
        if not created:
            app.name = name
            app.workspace_id = workspace_id
            app.project_path = project_path
            app.save(update_fields=["name", "workspace", "project_path", "updated_at"])
        number = (app.versions.aggregate(top=Max("number"))["top"] or 0) + 1
        version = AppVersion.objects.create(
            app=app,
            number=number,
            version_name=version_name,
            sdk_version=sdk_version,
            notes=notes,
            expo_config=expo_config,
            icon=icon,
        )
        AppFile.objects.bulk_create(
            AppFile(
                version=version,
                platform=platform,
                kind=kind,
                key=key,
                extension=extension,
                blob=blob,
            )
            for platform, kind, key, extension, blob in files
        )
        return version

    @staticmethod
    def delete_versions_beyond(app_id: uuid.UUID, keep: int) -> int:
        """Delete all but the newest *keep* versions of an app."""
        stale = list(
            AppVersion.objects.filter(app_id=app_id)
            .order_by("-number")
            .values_list("id", flat=True)[keep:]
        )
        if not stale:
            return 0
        deleted, _ = AppVersion.objects.filter(id__in=stale).delete()
        return deleted

    @staticmethod
    def delete_app(app: App) -> None:
        """Delete an app with all versions and version files."""
        app.delete()

    @staticmethod
    def delete_unreferenced_blobs() -> int:
        """Remove blobs no version references any more, row and file."""
        referenced = AppFile.objects.values("blob")
        icons = AppVersion.objects.exclude(icon=None).values("icon")
        removed = 0
        for blob in AppBlob.objects.exclude(sha256__in=referenced).exclude(
            sha256__in=icons
        ):
            name, storage = blob.file.name, blob.file.storage
            try:
                with transaction.atomic():
                    blob.delete()
            except (IntegrityError, ProtectedError):
                continue  # A concurrent publish just started using it.
            transaction.on_commit(lambda n=name, s=storage: s.delete(n))
            removed += 1
        return removed
