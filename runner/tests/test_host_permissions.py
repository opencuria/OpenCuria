"""Workspace storage on the runner host is private to the runner and QEMU."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import RunnerSettings
from src.runtime.host_permissions import (
    DIRECTORY_MODE,
    FILE_MODE,
    ensure_directory,
    restrict_tree,
)


def _mode(path: Path) -> int:
    return path.lstat().st_mode & 0o7777


def test_ensure_directory_ignores_the_umask_and_keeps_existing_parents(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o755)
    target = tmp_path / "data" / "disks"
    previous = os.umask(0o077)
    try:
        ensure_directory(target)
    finally:
        os.umask(previous)
    assert _mode(target) == _mode(target.parent) == DIRECTORY_MODE
    assert _mode(tmp_path) == 0o755


def test_restrict_tree_removes_group_and_other_access(tmp_path: Path) -> None:
    disk = tmp_path / "workspace.qcow2"
    disk.write_bytes(b"sk-ant-secret")
    disk.chmod(0o644)
    script = tmp_path / "windows" / "setup.cmd"
    script.parent.mkdir(mode=0o755)
    script.parent.chmod(0o755)
    script.write_text("x")
    script.chmod(0o775)
    stage = tmp_path / ".publication-1"
    stage.mkdir(mode=0o700)
    private = tmp_path / "hostkey"
    private.write_text("key")
    private.chmod(0o600)

    assert restrict_tree(tmp_path) == 3
    assert _mode(disk) == FILE_MODE
    assert _mode(script) == 0o700
    assert _mode(script.parent) == DIRECTORY_MODE
    assert _mode(stage) == 0o700
    assert _mode(private) == 0o600
    assert restrict_tree(tmp_path) == 0


def test_restrict_tree_leaves_links_and_special_files_alone(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("not storage")
    outside.chmod(0o644)
    root = tmp_path / "storage"
    root.mkdir()
    (root / "link").symlink_to(outside)
    (root / "dir-link").symlink_to(tmp_path)
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(str(root / "control.sock"))
        os.chmod(root / "control.sock", 0o666)
        assert restrict_tree(root) == 0
        assert _mode(root / "control.sock") == 0o666
    finally:
        listener.close()
    assert _mode(outside) == 0o644


def test_restrict_tree_reports_but_survives_foreign_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = tmp_path / "running.qcow2"
    disk.write_bytes(b"x")
    disk.chmod(0o644)
    chmod = Path.chmod

    def denied(path: Path, mode: int) -> None:
        if path == disk:
            raise PermissionError("owned by libvirt-qemu")
        chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", denied)
    assert restrict_tree(tmp_path) == 0


def test_runner_start_closes_storage_written_by_older_runners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.runtime.qemu_runtime import QemuRuntime

    settings = RunnerSettings(
        _env_file=None,
        qemu_disk_dir=str(tmp_path / "disks"),
        qemu_snapshot_dir=str(tmp_path / "snapshots"),
        qemu_image_cache_dir=str(tmp_path / "images"),
        qemu_ssh_key_path=str(tmp_path / "ssh" / "runner-key"),
    )
    build_dir = tmp_path / "base-images"
    legacy = {
        tmp_path / "disks" / "ws.qcow2": 0o644,
        tmp_path / "disks" / "ws-cloud-init.iso": 0o644,
        tmp_path / "snapshots" / "capture.qcow2": 0o644,
        tmp_path / "images" / "windows" / "base.qcow2": 0o644,
        build_dir / "image.qcow2": 0o644,
    }
    for path, mode in legacy.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o755)
        path.write_bytes(b"tenant data")
        path.chmod(mode)
    monkeypatch.setattr(QemuRuntime, "_ensure_ssh_keypair", MagicMock())
    monkeypatch.setattr(QemuRuntime, "_build_image_dir", build_dir)

    QemuRuntime(settings)

    for path in legacy:
        assert _mode(path) == FILE_MODE, path
        assert _mode(path.parent) == DIRECTORY_MODE, path.parent
