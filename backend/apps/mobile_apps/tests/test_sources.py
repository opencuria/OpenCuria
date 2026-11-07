"""Collecting an Expo build from a workspace through its runner."""

from __future__ import annotations

import uuid

import pytest

from apps.mobile_apps.export import ExportError
from apps.mobile_apps.sources import WorkspaceExportReader, unpack_archive

from .conftest import FakeAccessor, build_files, tarball

WORKSPACE = uuid.uuid4()


def reader_for(accessor: FakeAccessor) -> WorkspaceExportReader:
    async def factory(workspace_id: str):
        assert workspace_id == str(WORKSPACE)
        return accessor

    return WorkspaceExportReader(accessor_factory=factory)


async def test_reads_config_and_build_in_one_archive():
    files, package = build_files()
    archive = tarball({**files.files, "package.json": package})
    accessor = FakeAccessor(config=files.config, archive=archive)

    export, raw_package = await reader_for(accessor).read(WORKSPACE, "todo-app")

    assert export.config == files.config
    assert export.files["dist/metadata.json"] == files.files["dist/metadata.json"]
    assert raw_package == package
    config_cmd, tar_cmd, cleanup_cmd = accessor.commands
    assert config_cmd[:3] == ["npx", "--no-install", "expo"]
    archive_path = tar_cmd[2]
    assert tar_cmd[:2] == ["tar", "-czhf"] and archive_path.startswith("/tmp/")
    assert tar_cmd[-3:] == ["dist", "package.json", "assets/icon.png"]
    assert "/workspace/todo-app" in tar_cmd
    assert accessor.read_paths == [archive_path]
    assert cleanup_cmd == ["rm", "-f", archive_path]


async def test_config_failure_explains_and_skips_transfer():
    accessor = FakeAccessor(config_exit=1)
    with pytest.raises(ExportError, match="npx expo config` failed"):
        await reader_for(accessor).read(WORKSPACE, "/workspace/none")
    assert len(accessor.commands) == 1
    assert accessor.read_paths == []


async def test_escaping_output_dir_is_rejected():
    accessor = FakeAccessor(config={"slug": "x"})
    with pytest.raises(ExportError, match="leaves the project"):
        await reader_for(accessor).read(WORKSPACE, "app", "../../etc")


def test_unpack_skips_links_and_escapes():
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in {"./dist/a": b"a", "../evil": b"x"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("dist/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    assert unpack_archive(buffer.getvalue()) == {"dist/a": b"a"}
    with pytest.raises(ExportError, match="corrupt"):
        unpack_archive(b"not a tarball")


async def test_interrupted_transfer_is_retried_once_then_reported():
    from apps.harness.access.runner_accessor import RunnerAccessorError

    files, package = build_files()
    archive = tarball({**files.files, "package.json": package})
    flaky = FakeAccessor(config=files.config, archive=archive, read_failures=1)
    export, _ = await reader_for(flaky).read(WORKSPACE, "todo-app")
    assert export.config == files.config
    assert len(flaky.read_paths) == 2

    broken = FakeAccessor(config=files.config, archive=archive, read_failures=2)
    with pytest.raises(RunnerAccessorError):
        await reader_for(broken).read(WORKSPACE, "todo-app")
    assert broken.commands[-1][:2] == ["rm", "-f"]
