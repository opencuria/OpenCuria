"""Windows installation media: pinned inputs, administrator ISO and setup media."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime import windows_media
from src.runtime.diagnostics import WINDOWS_MEDIA_UNAVAILABLE, OperationRejectedError
from src.runtime.windows_media import (
    PinnedFile,
    WindowsMedia,
    render_autounattend,
    windows_edition,
)


def _media(tmp_path: Path, **settings) -> WindowsMedia:
    return WindowsMedia(SimpleNamespace(qemu_image_cache_dir=str(tmp_path), **settings))


def test_editions_from_base_distros() -> None:
    assert windows_edition("windows:11") == "pro"
    assert windows_edition(" Windows:11-Enterprise ") == "enterprise"
    assert windows_edition("windows:11-pro-workstations") == "pro-workstations"
    for invalid in ("windows:10", "windows:11-home", "ubuntu:24.04"):
        with pytest.raises(OperationRejectedError) as rejected:
            windows_edition(invalid)
        assert rejected.value.code == WINDOWS_MEDIA_UNAVAILABLE


def test_answer_file_selects_the_edition_without_activating() -> None:
    answers = render_autounattend("enterprise")
    assert "<Value>Windows 11 Enterprise</Value>" in answers
    assert "XGVPP-NMH47-7TTHJ-W3FW7-8HV2C" in answers
    assert "{" not in answers
    # The system partition is last so it can always grow with the disk.
    assert answers.count("<CreatePartition ") == 3
    assert "PreventDeviceEncryption" in answers


def test_missing_iso_is_reported(tmp_path) -> None:
    for settings in ({}, {"windows_iso": ""}, {"windows_iso": str(tmp_path / "x.iso")}):
        with pytest.raises(OperationRejectedError) as rejected:
            _media(tmp_path, **settings).resolve_iso()
        assert rejected.value.code == WINDOWS_MEDIA_UNAVAILABLE


def test_local_iso_is_hashed_once_and_checked(tmp_path) -> None:
    iso = tmp_path / "win.iso"
    iso.write_bytes(b"windows")
    digest = hashlib.sha256(b"windows").hexdigest()
    media = _media(tmp_path / "cache", windows_iso=str(iso), windows_iso_sha256=digest)
    assert media.resolve_iso() == (iso, digest)
    # The cached digest is reused for an unchanged file.
    assert media._iso_digest(iso) == digest

    wrong = _media(tmp_path / "cache", windows_iso=str(iso), windows_iso_sha256="0" * 64)
    with pytest.raises(OperationRejectedError, match="checksum"):
        wrong.resolve_iso()


def test_pinned_files_are_verified_and_replaced_when_corrupt(tmp_path, monkeypatch) -> None:
    content = b"component"
    pin = PinnedFile("c.bin", "https://example.invalid/c.bin",
                     hashlib.sha256(content).hexdigest(), len(content))
    media = _media(tmp_path)
    downloads = []

    def download(url, target, *, size, sha256):
        downloads.append(url)
        target.write_bytes(content)
        media._verify(target, size=size, sha256=sha256)

    monkeypatch.setattr(media, "_download", download)
    assert media.ensure_pinned(pin).read_bytes() == content
    assert media.ensure_pinned(pin).read_bytes() == content
    assert len(downloads) == 1

    (media.cache_dir / "c.bin").write_bytes(b"tampered")
    media.ensure_pinned(pin)
    assert len(downloads) == 2

    def failing(url, target, *, size, sha256):
        raise OSError("offline")

    (media.cache_dir / "c.bin").unlink()
    monkeypatch.setattr(media, "_download", failing)
    with pytest.raises(OperationRejectedError) as rejected:
        media.ensure_pinned(pin)
    assert rejected.value.code == WINDOWS_MEDIA_UNAVAILABLE


def test_base_cache_key_follows_edition_iso_and_assets(tmp_path) -> None:
    media = _media(tmp_path)
    pro = media.base_path("pro", "a" * 64)
    assert pro == media.base_path("pro", "a" * 64)
    assert pro != media.base_path("enterprise", "a" * 64)
    assert pro != media.base_path("pro", "b" * 64)

    assert not media.base_ready(pro)
    pro.parent.mkdir(parents=True, exist_ok=True)
    pro.write_bytes(b"disk")
    media.record_base(pro, {"edition": "pro"})
    assert media.base_ready(pro)
    pro.write_bytes(b"changed disk")
    assert not media.base_ready(pro)


@pytest.mark.skipif(
    not (shutil.which("genisoimage") or shutil.which("mkisofs")) or not shutil.which("isoinfo"),
    reason="ISO tools are not installed",
)
def test_setup_media_layout(tmp_path, monkeypatch) -> None:
    media = _media(tmp_path / "cache")

    def pinned(pin):
        path = tmp_path / pin.name
        path.write_bytes(b"installer")
        return path

    monkeypatch.setattr(media, "ensure_pinned", pinned)
    iso = media.build_setup_iso("pro", tmp_path / "setup.iso")
    listing = subprocess.run(
        ["isoinfo", "-J", "-f", "-i", str(iso)], capture_output=True, text=True, check=True
    ).stdout.split()
    assert "/autounattend.xml" in listing
    for name in windows_media.SETUP_SCRIPTS:
        assert f"/opencuria/{name}" in listing
    assert f"/installers/{windows_media.MSYS2.name}" in listing
    manifest = subprocess.run(
        ["isoinfo", "-J", "-x", "/opencuria/media.json", "-i", str(iso)],
        capture_output=True, check=True,
    ).stdout
    assert json.loads(manifest)["msys2_packages"] == list(windows_media.MSYS2_PACKAGES)
    assert not (tmp_path / "setup.iso.stage").exists()
