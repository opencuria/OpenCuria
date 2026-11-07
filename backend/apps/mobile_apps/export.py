"""Validate an ``npx expo export`` build and turn it into a publishable app.

Pure functions over the files read from the workspace: no I/O, no ORM. Every
error message tells the agent what to change in its project or build.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from .runtime import AppRuntime, dependency_warnings

PLATFORMS = ("android", "ios")
LABELS = {"android": "Android", "ios": "iOS"}
#: Upper bound for one published version (all platforms, bundles and assets).
MAX_VERSION_BYTES = 100 * 1024 * 1024
MAX_ASSETS_PER_PLATFORM = 4000
MAX_ICON_BYTES = 2 * 1024 * 1024
MAX_CONFIG_BYTES = 256 * 1024
MAX_NOTES_CHARS = 2000

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ASSET_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EXTENSION = re.compile(r"^[A-Za-z0-9]{0,16}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ExportError(ValueError):
    """The build cannot be published; the message says how to fix it."""


@dataclass(frozen=True)
class ExportFiles:
    """Raw files of an Expo project's build, as read from the workspace."""

    #: Public Expo config (``npx expo config --type public --json``).
    config: dict[str, Any]
    #: Build output directory relative to the project root, e.g. ``dist``.
    output_dir: str
    #: File contents keyed by POSIX path relative to the project root.
    files: dict[str, bytes]


@dataclass(frozen=True)
class ExportAsset:
    """One asset file the bundle references by its Metro hash."""

    key: str
    extension: str
    content: bytes


@dataclass(frozen=True)
class PlatformExport:
    """Launch bundle and assets for one platform."""

    bundle: bytes
    assets: tuple[ExportAsset, ...]

    @property
    def size(self) -> int:
        """Total bytes of the bundle and its assets."""
        return len(self.bundle) + sum(len(asset.content) for asset in self.assets)


@dataclass(frozen=True)
class AppExport:
    """A validated build, ready to be stored as a new app version."""

    slug: str
    name: str
    version_name: str
    sdk_version: str
    config: dict[str, Any]
    platforms: dict[str, PlatformExport]
    icon: bytes | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def relative_path(path: str) -> str:
    """Normalize a build-relative path; reject absolute or escaping ones."""
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise ExportError(f"Invalid path in the build: {path!r}.")
    normalized = posixpath.normpath(path)
    if normalized.startswith(("/", "../")) or normalized in {".", ".."}:
        raise ExportError(f"Path {path!r} leaves the project directory.")
    return normalized


def _metadata(files: ExportFiles) -> dict[str, Any]:
    path = posixpath.join(files.output_dir, "metadata.json")
    raw = files.files.get(path)
    if raw is None:
        raise ExportError(
            f"No {path} found. Build the app first: `npx expo export --platform "
            f"android --platform ios --output-dir {files.output_dir}`."
        )
    try:
        metadata = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"{path} is not valid JSON.") from exc
    file_metadata = metadata.get("fileMetadata") if isinstance(metadata, dict) else None
    if not isinstance(file_metadata, dict):
        raise ExportError(
            f"{path} has no fileMetadata; rebuild with `npx expo export`."
        )
    return file_metadata


def _platform(files: ExportFiles, platform: str, entry: Any) -> PlatformExport:
    if not isinstance(entry, dict) or not isinstance(entry.get("bundle"), str):
        raise ExportError(f"metadata.json lists no {platform} bundle.")
    bundle_path = relative_path(posixpath.join(files.output_dir, entry["bundle"]))
    bundle = files.files.get(bundle_path)
    if not bundle:
        raise ExportError(f"The {platform} bundle {bundle_path} is missing or empty.")
    raw_assets = entry.get("assets") or []
    if not isinstance(raw_assets, list):
        raise ExportError(f"metadata.json has invalid {platform} assets.")
    if len(raw_assets) > MAX_ASSETS_PER_PLATFORM:
        raise ExportError(
            f"The {platform} build has {len(raw_assets)} assets; at most "
            f"{MAX_ASSETS_PER_PLATFORM} are supported."
        )
    assets: dict[str, ExportAsset] = {}
    for item in raw_assets:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ExportError(f"metadata.json has an invalid {platform} asset.")
        path = relative_path(posixpath.join(files.output_dir, item["path"]))
        key = posixpath.basename(path)
        extension = str(item.get("ext") or "")
        if not _ASSET_KEY.match(key) or not _EXTENSION.match(extension):
            raise ExportError(f"Unexpected asset file name {item['path']!r}.")
        content = files.files.get(path)
        if content is None:
            raise ExportError(f"Asset {path} listed in metadata.json is missing.")
        assets[key] = ExportAsset(key=key, extension=extension, content=content)
    return PlatformExport(bundle=bundle, assets=tuple(assets.values()))


def _config_string(config: dict[str, Any], key: str, default: str = "") -> str:
    value = config.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _icon(files: ExportFiles, config: dict[str, Any]) -> bytes | None:
    icon = config.get("icon")
    if not isinstance(icon, str) or not icon or "://" in icon:
        return None
    content = files.files.get(relative_path(icon.removeprefix("./")))
    if not content or len(content) > MAX_ICON_BYTES:
        return None
    return content if content.startswith(_PNG_SIGNATURE) else None


def parse_export(
    files: ExportFiles, runtime: AppRuntime, dependencies: dict[str, str]
) -> AppExport:
    """Validate an export against the runtime and return the app to store.

    Raises:
        ExportError: When the build cannot run in the OpenCuria app.
    """
    config = files.config
    if not isinstance(config, dict) or not config:
        raise ExportError("The Expo config is empty.")
    if len(json.dumps(config)) > MAX_CONFIG_BYTES:
        raise ExportError("The public Expo config is larger than 256 KiB.")
    sdk_version = _config_string(config, "sdkVersion")
    if not sdk_version:
        raise ExportError("The Expo config has no sdkVersion; is expo installed?")
    if sdk_version.split(".", 1)[0] != str(runtime.sdk_major):
        raise ExportError(
            f"The app uses Expo SDK {sdk_version}, the OpenCuria app runs SDK "
            f"{runtime.sdk_version}. Create the project with `npx create-expo-app "
            f"--template blank-typescript@sdk-{runtime.sdk_major}` or align its "
            f"dependencies with `npx expo install expo@^{runtime.sdk_version} "
            "--fix`, then export again."
        )
    slug = _config_string(config, "slug")
    if not _SLUG.match(slug):
        raise ExportError(
            "The Expo config needs a slug of letters, digits, '.', '_' or '-'."
        )
    name = _config_string(config, "name", slug)[:255] or slug
    version_name = _config_string(config, "version", "1.0.0")[:64] or "1.0.0"

    file_metadata = _metadata(files)
    platforms = {
        platform: _platform(files, platform, file_metadata[platform])
        for platform in PLATFORMS
        if platform in file_metadata
    }
    if not platforms:
        raise ExportError(
            "The build contains neither an Android nor an iOS bundle. Export with "
            "`--platform android --platform ios`."
        )
    total = sum(platform.size for platform in platforms.values())
    if total > MAX_VERSION_BYTES:
        raise ExportError(
            f"The build is {total // (1024 * 1024)} MiB; at most "
            f"{MAX_VERSION_BYTES // (1024 * 1024)} MiB can be published."
        )
    warnings = dependency_warnings(dependencies, runtime)
    missing = [LABELS[p] for p in PLATFORMS if p not in platforms]
    if missing:
        names = " or ".join(missing)
        warnings.insert(
            0, f"No {names} bundle: the app cannot be installed on {names} devices."
        )
    return AppExport(
        slug=slug,
        name=name,
        version_name=version_name,
        sdk_version=sdk_version,
        config=config,
        platforms=platforms,
        icon=_icon(files, config),
        warnings=tuple(warnings),
    )


def package_dependencies(raw: bytes | None) -> dict[str, str]:
    """Return ``dependencies`` of a package.json, or ``{}`` when unreadable."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(deps, dict):
        return {}
    return {str(k): v for k, v in deps.items() if isinstance(v, str)}
