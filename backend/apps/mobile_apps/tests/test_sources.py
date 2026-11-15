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
    config_cmd, tar_cmd, stat_cmd, cleanup_cmd = accessor.commands
    assert config_cmd[:3] == ["npx", "--no-install", "expo"]
    archive_path = tar_cmd[2]
    assert tar_cmd[:2] == ["tar", "-czhf"] and archive_path.startswith("/tmp/")
    assert tar_cmd[-3:] == ["dist", "package.json", "assets/icon.png"]
    assert "/workspace/todo-app" in tar_cmd
    assert stat_cmd == ["stat", "-c", "%s", "--", archive_path]
    assert accessor.read_paths == [archive_path]
    assert cleanup_cmd == ["rm", "-f", archive_path]


async def test_oversized_build_is_reported_before_the_transfer():
    from apps.harness.access.runner_accessor import HARNESS_READ_MAX_BYTES

    files, package = build_files()
    accessor = FakeAccessor(
        config=files.config,
        archive=tarball({**files.files, "package.json": package}),
        archive_size=HARNESS_READ_MAX_BYTES + 1,
    )
    with pytest.raises(ExportError, match="larger than 100 MiB compressed"):
        await reader_for(accessor).read(WORKSPACE, "todo-app")
    assert accessor.read_paths == []
    assert accessor.commands[-1][:2] == ["rm", "-f"]


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


def _archive(members: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_unpack_counts_skipped_members_against_the_size_limit(monkeypatch):
    from apps.mobile_apps import sources

    monkeypatch.setattr(sources, "MAX_UNPACKED_BYTES", 1000)
    # The escaping member is skipped but still decompressed.
    data = _archive({"dist/a": b"a", "../evil": b"x" * 2000})
    with pytest.raises(ExportError, match="larger than"):
        unpack_archive(data)


def test_unpack_bounds_decompressed_bytes(monkeypatch):
    from apps.mobile_apps import sources

    # Tar pads archives to 10 KiB records.
    monkeypatch.setattr(sources, "MAX_DECOMPRESSED_BYTES", 16 * 1024)
    small = {"dist/a": b"a" * 100}
    assert unpack_archive(_archive(small)) == small
    with pytest.raises(ExportError, match="larger than"):
        unpack_archive(_archive({"dist/a": b"\0" * 32 * 1024}))


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
