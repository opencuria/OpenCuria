"""Validation of ``expo export`` builds against the app runtime."""

from __future__ import annotations

import pytest

from apps.mobile_apps.export import (
    ExportError,
    ExportFiles,
    package_dependencies,
    parse_export,
    relative_path,
)
from apps.mobile_apps.runtime import (
    AppRuntime,
    app_runtime,
    compatible,
    dependency_warnings,
    range_allows,
)

from .conftest import PNG, build_files

RUNTIME = AppRuntime(
    sdk_version="57.0.0",
    react_native_version="0.86.3",
    packages={"expo-camera": "57.0.6", "react-native": "0.86.3", "react": "19.2.3"},
)


def test_valid_build_becomes_an_app_export():
    files, package = build_files(assets={"logo.png": b"logo", "font.ttf": b"font"})

    export = parse_export(files, RUNTIME, package_dependencies(package))

    assert (export.slug, export.name, export.version_name) == ("todo", "Todo", "1.0.0")
    assert export.sdk_version == "57.0.0"
    assert set(export.platforms) == {"android", "ios"}
    android = export.platforms["android"]
    assert android.bundle == b"bundle-android"
    assert sorted(asset.extension for asset in android.assets) == ["png", "ttf"]
    assert export.icon == PNG
    assert export.warnings == ()


def test_other_sdk_is_rejected_with_a_fix():
    files, _ = build_files(sdk="58.0.0")
    with pytest.raises(ExportError, match="blank-typescript@sdk-57"):
        parse_export(files, RUNTIME, {})


def test_missing_build_tells_the_agent_to_export():
    files, _ = build_files()
    del files.files["dist/metadata.json"]
    with pytest.raises(ExportError, match="npx expo export"):
        parse_export(files, RUNTIME, {})


def test_missing_asset_and_bad_paths_are_rejected():
    files, _ = build_files()
    missing = ExportFiles(
        config=files.config,
        output_dir="dist",
        files={k: v for k, v in files.files.items() if "/assets/" not in k},
    )
    with pytest.raises(ExportError, match="is missing"):
        parse_export(missing, RUNTIME, {})
    for path in ("../etc/passwd", "/etc/passwd", "a\\b", ""):
        with pytest.raises(ExportError):
            relative_path(path)


def test_single_platform_build_warns():
    files, _ = build_files(ios_bundle=None)
    export = parse_export(files, RUNTIME, {})
    assert set(export.platforms) == {"android"}
    assert "No iOS bundle" in export.warnings[0]


def test_invalid_slug_and_icon_fallbacks():
    files, _ = build_files()
    files.config["slug"] = "has spaces"
    with pytest.raises(ExportError, match="slug"):
        parse_export(files, RUNTIME, {})
    files, _ = build_files()
    files.files["assets/icon.png"] = b"not a png"
    assert parse_export(files, RUNTIME, {}).icon is None
    for outside in ("../shared/icon.png", "/repo/assets/icon.png", "./"):
        files, _ = build_files()
        files.config["icon"] = outside
        assert parse_export(files, RUNTIME, {}).icon is None


@pytest.mark.parametrize(
    ("spec", "version", "expected"),
    [
        ("~57.0.2", "57.0.6", True),
        ("~57.0.2", "57.1.0", False),
        ("^4.5.0", "4.9.1", True),
        ("^4.5.0", "5.0.0", False),
        ("^0.10.1", "0.10.4", True),
        ("^0.10.1", "0.11.0", False),
        ("0.86.3", "0.86.3", True),
        ("0.86.2", "0.86.3", False),
        ("57.x", "57.3.1", True),
        ("*", "1.0.0", True),
        ("github:foo/bar", "1.0.0", None),
        (">=1 <2", "1.5.0", None),
    ],
)
def test_range_allows(spec, version, expected):
    assert range_allows(spec, version) is expected


@pytest.mark.parametrize(
    ("spec", "provided", "expected"),
    [
        ("~57.0.23", "57.0.22", True),
        ("^4.5.0", "4.5.1", True),
        ("4.5.1", "4.5.1", True),
        ("^4.5.0", "4.9.1", False),
        ("~58.0.1", "57.0.6", False),
        ("57.x", "57.0.6", True),
        ("latest", "1.0.0", True),
    ],
)
def test_patch_releases_are_compatible(spec, provided, expected):
    assert compatible(spec, provided) is expected


def test_dependency_warnings_name_the_fix():
    warnings = dependency_warnings(
        {
            "expo": "~58.0.0",
            "expo-camera": "~58.0.1",
            "react-native": "0.86.3",
            "react": "19.2.0",
            "react-native-ble-plx": "^3.0.0",
            "lodash": "^4.17.0",
        },
        RUNTIME,
    )
    assert len(warnings) == 2
    assert "npx expo install expo-camera" in warnings[0]
    assert warnings[1].startswith("react-native-ble-plx is not part")


def test_shipped_runtime_matches_the_mobile_app():
    runtime = app_runtime()
    assert runtime.sdk_major >= 57
    assert runtime.packages["react-native"] == runtime.react_native_version
    assert "expo-camera" in runtime.packages
    assert not any(name.startswith("opencuria-") for name in runtime.packages)
