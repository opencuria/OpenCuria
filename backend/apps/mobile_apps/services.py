"""Business logic for apps that workspace agents publish to their user."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from asgiref.sync import sync_to_async

from common.exceptions import NotFoundError

from .export import (
    MAX_NOTES_CHARS,
    PLATFORMS,
    AppExport,
    package_dependencies,
    parse_export,
)
from .models import App, AppBlob, AppFile, AppVersion
from .repositories import AppRepository
from .runtime import app_runtime
from .sources import WorkspaceExportReader

#: Published versions kept per app; older ones and their unshared files go.
VERSION_RETENTION = 5


@dataclass(frozen=True)
class PublishResult:
    """A stored version and what the agent should know about it."""

    version: AppVersion
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class AppListing:
    """An app with versions (newest first) and their download sizes."""

    app: App
    versions: list[AppVersion]
    #: Per version: platform -> total bytes of its bundle and assets.
    sizes: dict[uuid.UUID, dict[str, int]]

    @property
    def latest(self) -> AppVersion | None:
        """The version devices are offered."""
        return self.versions[0] if self.versions else None

    def platforms(self, version: AppVersion) -> dict[str, int]:
        """Download size per platform *version* supports."""
        return self.sizes.get(version.id, {})


@dataclass(frozen=True)
class ManifestFile:
    """A downloadable file of a version."""

    sha256: str
    md5: str
    size: int


@dataclass(frozen=True)
class ManifestAsset(ManifestFile):
    """An asset the bundle references by its Metro hash ``key``."""

    key: str
    extension: str


@dataclass(frozen=True)
class AppManifest:
    """Everything a device needs to install one version on one platform."""

    app: App
    version: AppVersion
    platform: str
    bundle: ManifestFile
    assets: list[ManifestAsset]


def _blob_file(blob: AppBlob) -> ManifestFile:
    return ManifestFile(sha256=blob.sha256, md5=blob.md5, size=blob.size)


class AppService:
    """Publishes, lists, serves and deletes a user's apps."""

    def __init__(
        self,
        repository: type[AppRepository] = AppRepository,
        reader: WorkspaceExportReader | None = None,
    ) -> None:
        self.apps = repository
        self.reader = reader or WorkspaceExportReader()

    # -- Publishing -------------------------------------------------------

    async def publish(
        self,
        *,
        owner_id: int,
        organization_id: uuid.UUID,
        workspace_id: uuid.UUID,
        project_path: str,
        output_dir: str = "dist",
        notes: str = "",
    ) -> PublishResult:
        """Read an exported Expo build from the workspace and add a version.

        The app is matched by its Expo slug for this owner; a new slug creates
        a new app.

        Raises:
            ExportError: When the build cannot run in the OpenCuria app.
            ServiceError: When the workspace or its runner is unavailable.
        """
        files, package_json = await self.reader.read(
            workspace_id, project_path, output_dir
        )
        export = parse_export(files, app_runtime(), package_dependencies(package_json))
        version = await sync_to_async(self._store)(
            export,
            owner_id=owner_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            project_path=project_path,
            notes=notes.strip()[:MAX_NOTES_CHARS],
        )
        return PublishResult(version=version, warnings=export.warnings)

    def _blob(self, content: bytes) -> AppBlob:
        return self.apps.ensure_blob(
            hashlib.sha256(content).hexdigest(),
            hashlib.md5(content, usedforsecurity=False).hexdigest(),
            content,
        )

    def _store(
        self,
        export: AppExport,
        *,
        owner_id: int,
        organization_id: uuid.UUID,
        workspace_id: uuid.UUID,
        project_path: str,
        notes: str,
    ) -> AppVersion:
        rows: list[tuple[str, str, str, str, AppBlob]] = []
        for platform, build in export.platforms.items():
            rows.append(
                (platform, AppFile.Kind.BUNDLE, "", "", self._blob(build.bundle))
            )
            rows.extend(
                (
                    platform,
                    AppFile.Kind.ASSET,
                    asset.key,
                    asset.extension,
                    self._blob(asset.content),
                )
                for asset in build.assets
            )
        version = self.apps.create_version(
            organization_id=organization_id,
            owner_id=owner_id,
            workspace_id=workspace_id,
            slug=export.slug,
            name=export.name,
            project_path=project_path,
            version_name=export.version_name,
            sdk_version=export.sdk_version,
            notes=notes,
            expo_config=export.config,
            icon=self._blob(export.icon) if export.icon else None,
            files=rows,
        )
        if self.apps.delete_versions_beyond(version.app_id, VERSION_RETENTION):
            self.apps.delete_unreferenced_blobs()
        return version

    # -- Reading ----------------------------------------------------------

    def _listings(self, apps: list[App], *, latest_only: bool) -> list[AppListing]:
        versions = self.apps.versions_by_app(
            (app.id for app in apps), latest_only=latest_only
        )
        sizes = self.apps.version_platforms(
            version.id for rows in versions.values() for version in rows
        )
        return [
            AppListing(app=app, versions=versions.get(app.id, []), sizes=sizes)
            for app in apps
        ]

    def list_apps(
        self, *, organization_id: uuid.UUID, owner_id: int
    ) -> list[AppListing]:
        """The user's apps with their newest version, recently published first."""
        apps = self.apps.list_owned(organization_id, owner_id)
        return self._listings(apps, latest_only=True)

    def get_app(
        self, app_id: uuid.UUID, *, organization_id: uuid.UUID, owner_id: int
    ) -> App:
        """One of the user's apps.

        Raises:
            NotFoundError: For unknown or foreign apps.
        """
        app = self.apps.get_owned(app_id, organization_id, owner_id)
        if app is None:
            raise NotFoundError("App", str(app_id))
        return app

    def app_detail(
        self, app_id: uuid.UUID, *, organization_id: uuid.UUID, owner_id: int
    ) -> AppListing:
        """One of the user's apps with all retained versions."""
        app = self.get_app(app_id, organization_id=organization_id, owner_id=owner_id)
        return self._listings([app], latest_only=False)[0]

    def version_sizes(self, version: AppVersion) -> dict[str, int]:
        """Download size per platform of one version."""
        return self.apps.version_platforms([version.id]).get(version.id, {})

    def _version(
        self,
        app_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        owner_id: int,
    ) -> tuple[App, AppVersion]:
        app = self.get_app(app_id, organization_id=organization_id, owner_id=owner_id)
        version = self.apps.get_version(app.id, version_id)
        if version is None:
            raise NotFoundError("App version", str(version_id))
        return app, version

    def manifest(
        self,
        app_id: uuid.UUID,
        version_id: uuid.UUID,
        platform: str,
        *,
        organization_id: uuid.UUID,
        owner_id: int,
    ) -> AppManifest:
        """The files a device downloads to install a version.

        Raises:
            NotFoundError: For foreign apps or a platform the build lacks.
            ValueError: For an unknown platform.
        """
        if platform not in PLATFORMS:
            raise ValueError(f"Unknown platform '{platform}'.")
        app, version = self._version(
            app_id, version_id, organization_id=organization_id, owner_id=owner_id
        )
        files = self.apps.version_files(version.id, platform)
        bundle = next((f for f in files if f.kind == AppFile.Kind.BUNDLE), None)
        if bundle is None:
            raise NotFoundError(f"{platform} build of version {version.number}")
        assets = [
            ManifestAsset(
                sha256=f.blob.sha256,
                md5=f.blob.md5,
                size=f.blob.size,
                key=f.key,
                extension=f.extension,
            )
            for f in files
            if f.kind == AppFile.Kind.ASSET
        ]
        return AppManifest(
            app=app,
            version=version,
            platform=platform,
            bundle=_blob_file(bundle.blob),
            assets=assets,
        )

    def open_file(
        self,
        app_id: uuid.UUID,
        version_id: uuid.UUID,
        sha256: str,
        *,
        organization_id: uuid.UUID,
        owner_id: int,
    ) -> AppBlob:
        """A file of one of the user's versions (bundle, asset or icon)."""
        _app, version = self._version(
            app_id, version_id, organization_id=organization_id, owner_id=owner_id
        )
        blob = self.apps.blob_in_version(version.id, sha256)
        if blob is None:
            raise NotFoundError("App file")
        return blob

    # -- Removing ---------------------------------------------------------

    def delete_app(
        self, app_id: uuid.UUID, *, organization_id: uuid.UUID, owner_id: int
    ) -> None:
        """Delete an app with every version; installed copies keep working."""
        app = self.get_app(app_id, organization_id=organization_id, owner_id=owner_id)
        self.apps.delete_app(app)
        self.apps.delete_unreferenced_blobs()
