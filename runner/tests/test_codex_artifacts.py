"""Pinned standalone Codex packages: provisioning, hostile archives, local installs."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.runtime import artifacts
from src.runtime import codex_artifact as codex
from src.runtime.artifacts import ArtifactProvisionError, RuntimeArtifactManager

TARGET = "x86_64-unknown-linux-musl"
REAL_PACKAGE = Path(
    os.environ.get(
        "OPENCURIA_TEST_CODEX_PACKAGE",
        Path(__file__).resolve().parents[2]
        / ".opencuria/codex-test-runtime/package.tar.gz",
    )
)


def package_bytes(*, target=TARGET, extra=None, omit=None):
    metadata = {
        "layoutVersion": 1,
        "version": codex.VERSION,
        "target": target,
        "variant": "codex-app-server",
        "entrypoint": "bin/codex-app-server",
        "resourcesDir": "codex-resources",
        "pathDir": "codex-path",
    }
    files = {
        name: b"#!/bin/sh\nprintf 'codex-app-server 0.163.0\\n'\n"
        for name in codex.FILES
    }
    files["codex-package.json"] = json.dumps(metadata).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in sorted(codex.DIRECTORIES):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        for name, data in sorted(files.items()):
            if name == omit:
                continue
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b"x" * min(extra.size, 100)))
    return output.getvalue()


class LocalPackageRuntime:
    """Execute the real guest installer and transport wrapper using local files."""

    def __init__(self):
        self.uploads = []
        self.commands = []
        self.upload_gate = None
        self.upload_entered = asyncio.Event()
        self.cleaned = asyncio.Event()

    async def exec_command_wait(self, instance, command):
        self.commands.append(command)
        if "uname -s" in command[2]:
            return 0, "linux-x64\n"
        if command[:2] == ["sh", "-c"]:
            self.cleaned.set()
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _error = await proc.communicate()
        return proc.returncode, output.decode()

    async def put_archive(self, instance, path, data):
        assert path == "/tmp"
        self.upload_entered.set()
        if self.upload_gate:
            await self.upload_gate.wait()
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            members = archive.getmembers()
            assert len(members) == 1 and members[0].isfile()
            assert "/" not in members[0].name
            source = Path(path) / members[0].name
            self.uploads.append(source)
            with source.open("xb") as output:
                shutil.copyfileobj(archive.extractfile(members[0]), output)


@pytest.fixture
def setup_codex(tmp_path, monkeypatch):
    payload = package_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(codex.PINS, TARGET, (digest, len(payload)))
    manifest = json.loads(artifacts.ARTIFACT_MANIFEST_PATH.read_text())
    manifest["artifacts"]["codex-agent"] = codex.manifest_entry()
    manifest["artifacts"]["claude-agent"]["manifest_sha256"] = "x"
    canonical_hash = hashlib.sha256(
        (json.dumps(manifest, indent=2) + "\n").encode()
    ).hexdigest()
    monkeypatch.setattr(artifacts, "CLAUDE_ARTIFACT_SPEC_SHA256", canonical_hash)
    manifest["artifacts"]["claude-agent"]["manifest_sha256"] = canonical_hash
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    runtime = LocalPackageRuntime()
    info = SimpleNamespace(instance_id="local-guest")
    manager = RuntimeArtifactManager(
        runtime,
        lambda _: info,
        lambda _: runtime,
        tmp_path / "state",
        manifest_path=manifest_path,
        guest_runtime_root=str(tmp_path / "guest" / "runtimes"),
    )

    async def download(url, size, sha, destination):
        assert url == codex.manifest_entry()["platforms"]["linux-x64"]["url"]
        assert (sha, size) == (digest, len(payload))
        destination.write_bytes(payload)

    manager._download = AsyncMock(side_effect=download)
    return manager, runtime, info


@pytest.mark.asyncio
async def test_codex_package_install_cache_verify_and_poison_rejection(setup_codex):
    manager, runtime, _ = setup_codex
    workspace = uuid.uuid4()
    result = await manager.ensure_runtime_artifact(workspace, "codex-agent", "0.163.0")
    assert result == {
        "ok": True,
        "path": f"{manager._guest_runtime_root}/codex-agent/0.163.0/bin/codex-app-server",
        "version": "0.163.0",
        "platform": "linux-x64",
    }
    installed = Path(result["path"]).parent.parent
    assert all(not source.exists() for source in runtime.uploads)
    assert (
        await manager.ensure_runtime_artifact(workspace, "codex-agent", "0.163.0")
        == result
    )
    manager._download.assert_awaited_once()
    assert len(list(installed.rglob("*"))) == len(codex.FILES | codex.DIRECTORIES)
    hashes = codex.inspect_package(
        next(manager._cache_root.rglob("package.tar.gz")), TARGET
    )
    codex.verify_tree(installed, hashes)
    helper = installed / "codex-path/rg"
    helper.chmod(0o755)
    helper.write_text("poisoned executable")
    helper.chmod(0o555)
    with pytest.raises(ArtifactProvisionError):
        await manager.ensure_runtime_artifact(workspace, "codex-agent", "0.163.0")
    assert helper.read_text() == "poisoned executable"  # Never silently replace.


@pytest.mark.asyncio
async def test_codex_corrupt_cache_redownloaded_and_symlink_not_followed(
    setup_codex, tmp_path
):
    manager, _, _ = setup_codex
    await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")
    cached = next(manager._cache_root.rglob("package.tar.gz"))
    cached.write_bytes(b"corrupt")
    await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")
    target = tmp_path / "untouched"
    target.write_bytes(b"keep")
    cached.unlink()
    cached.symlink_to(target)
    await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")
    assert not cached.is_symlink()
    assert target.read_bytes() == b"keep"
    assert manager._download.await_count == 3


@pytest.mark.asyncio
async def test_codex_guest_config_path_and_artifact_symlinks_rejected(
    setup_codex, tmp_path
):
    manager, _, _ = setup_codex
    guest = Path(manager._guest_runtime_root)
    guest.parent.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    guest.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ArtifactProvisionError):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")
    assert not list(elsewhere.iterdir())
    guest.unlink()
    result = await manager.ensure_runtime_artifact(
        uuid.uuid4(), "codex-agent", "0.163.0"
    )
    helper = Path(result["path"]).parent.parent / "codex-path/rg"
    helper.parent.chmod(0o755)
    helper.unlink()
    helper.symlink_to("/bin/sh")
    with pytest.raises(ArtifactProvisionError):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")


@pytest.mark.asyncio
async def test_codex_cache_directory_symlink_rejected(setup_codex, tmp_path):
    manager, _, _ = setup_codex
    manager._state_dir.mkdir()
    manager._cache_root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ArtifactProvisionError, match="unsafe"):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")
    manager._download.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_rejects_mutable_version_and_recreated_guest(setup_codex):
    manager, runtime, info = setup_codex
    with pytest.raises(ValueError, match="Unapproved"):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "latest")
    original = runtime.exec_command_wait

    async def changed(instance, command):
        result = await original(instance, command)
        if command[0] == "python3":
            info.instance_id = "new-guest"
        return result

    runtime.exec_command_wait = changed
    with pytest.raises(ArtifactProvisionError, match="Workspace changed"):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")


@pytest.mark.asyncio
async def test_codex_cancelled_upload_settles_and_cleans(setup_codex):
    manager, runtime, _ = setup_codex
    runtime.upload_gate = asyncio.Event()
    task = asyncio.create_task(
        manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")
    )
    await asyncio.wait_for(runtime.upload_entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime.upload_gate.set()
    await asyncio.wait_for(runtime.cleaned.wait(), 2)
    for _ in range(100):
        if not manager._background_guest_tasks:
            break
        await asyncio.sleep(0.01)
    assert not manager._background_guest_tasks
    assert not any(p.exists() for p in runtime.uploads)


@pytest.mark.parametrize("key", ["version", "platforms"])
def test_codex_manifest_poison_rejected(tmp_path, key):
    manifest = json.loads(artifacts.ARTIFACT_MANIFEST_PATH.read_text())
    manifest["artifacts"]["codex-agent"][key] = "poison"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="integrity"):
        RuntimeArtifactManager(
            {}, lambda _: None, lambda _: None, tmp_path / "state", manifest_path=path
        )


@pytest.mark.parametrize(
    "platform,target",
    [
        ("linux-x64", TARGET),
        ("linux-x64-musl", TARGET),
        ("linux-arm64", "aarch64-unknown-linux-musl"),
        ("linux-arm64-musl", "aarch64-unknown-linux-musl"),
    ],
)
def test_codex_official_platform_pins(platform, target):
    entry = codex.manifest_entry()["platforms"][platform]
    assert entry["url"].endswith(
        f"rust-v0.163.0/codex-app-server-package-{target}.tar.gz"
    )
    assert (entry["sha256"], entry["size"]) == codex.PINS[target]
    assert entry["size"] < codex.MAX_ARCHIVE_SIZE


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("bin/../escape", tarfile.REGTYPE),
        ("bin/codex-app-server", tarfile.REGTYPE),
        ("extra", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("link", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
        ("fifo", tarfile.FIFOTYPE),
    ],
)
def test_codex_rejects_hostile_archive_entries(tmp_path, name, kind):
    member = tarfile.TarInfo(name)
    member.type = kind
    member.size = 1 if kind == tarfile.REGTYPE else 0
    member.linkname = (
        "/etc/passwd" if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
    )
    path = tmp_path / "package.tar.gz"
    path.write_bytes(package_bytes(extra=member))
    with pytest.raises(ValueError):
        codex.inspect_package(path, TARGET)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("change", ["target", "missing", "size", "metadata"])
def test_codex_rejects_wrong_layout_target_and_bombs(tmp_path, monkeypatch, change):
    kwargs = {"target": "aarch64-unknown-linux-musl"} if change == "target" else {}
    if change == "missing":
        kwargs["omit"] = "codex-resources/bwrap"
    if change == "size":
        monkeypatch.setattr(codex, "MAX_PACKAGE_SIZE", 10)
    payload = package_bytes(**kwargs)
    if change == "metadata":
        # A regular helper cannot replace the mandatory package metadata.
        payload = package_bytes(omit="codex-package.json")
    path = tmp_path / "package.tar.gz"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        codex.inspect_package(path, TARGET)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not REAL_PACKAGE.is_file(), reason="official package not available locally"
)
async def test_real_codex_package_local_installer_contract(tmp_path):
    """Use all actual pinned release bytes, not a lone binary or mocked installer."""
    digest, size = codex.PINS[TARGET]
    assert REAL_PACKAGE.stat().st_size == size
    assert await asyncio.to_thread(codex.sha_file, REAL_PACKAGE) == digest
    runtime = LocalPackageRuntime()
    info = SimpleNamespace(instance_id="real-local-guest")
    manager = RuntimeArtifactManager(
        runtime,
        lambda _: info,
        lambda _: runtime,
        tmp_path / "state",
        guest_runtime_root=str(tmp_path / "real-guest"),
    )

    async def download(url, expected_size, expected_digest, destination):
        await asyncio.to_thread(shutil.copyfile, REAL_PACKAGE, destination)

    manager._download = AsyncMock(side_effect=download)
    result = await manager.ensure_runtime_artifact(
        uuid.uuid4(), "codex-agent", "0.163.0"
    )
    assert Path(result["path"]).is_file()
    installed = Path(result["path"]).parent.parent
    codex.verify_tree(
        installed, await asyncio.to_thread(codex.inspect_package, REAL_PACKAGE, TARGET)
    )
    # The installed standalone package also speaks the official protocol under
    # the same managed isolation used by QEMU, with no auth env.
    from src.runtime.managed_process import isolated_agent_env_preamble, managed_argv

    from tests.test_managed_streams import current_token

    config_root = str(tmp_path / "codex-config")
    config_dir = Path(config_root) / str(uuid.uuid4()) / "config"
    config_dir.mkdir(parents=True)
    home = Path("/tmp/opencuria-agent-home-" + uuid.uuid4().hex)
    env = {"CODEX_HOME": str(config_dir)}
    argv = managed_argv(
        str(tmp_path / "managed-control"),
        str(tmp_path),
        env,
        [result["path"]],
        expected_token=current_token(),
        isolated_env=True,
        isolated_home=str(home),
        config_root=config_root,
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        initialize = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "runner_contract_test", "version": "1.0"},
            },
        }
        process.stdin.write(isolated_agent_env_preamble(env, config_root=config_root))
        process.stdin.write((json.dumps(initialize) + "\n").encode())
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 10)
            assert line, "official app-server exited before initialization"
            message = json.loads(line)
            if message.get("id") == 1:
                assert "result" in message and "error" not in message
                break
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 10) == 0
    finally:
        if process.returncode is None:
            from src.runtime.managed_process import managed_close_argv

            close = await asyncio.create_subprocess_exec(
                *managed_close_argv(str(tmp_path / "managed-control"))
            )
            await close.wait()
            await process.wait()
    assert not home.exists()
    # Non-entrypoint bytes are part of the immutable artifact, too.
    helper = installed / "bin/codex-code-mode-host"
    helper.chmod(0o755)
    with helper.open("r+b") as output:
        output.seek(-1, os.SEEK_END)
        output.write(b"\x01")
    helper.chmod(0o555)
    with pytest.raises(ArtifactProvisionError):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")
    assert all(not source.exists() for source in runtime.uploads)


class DownloadResponse:
    def __init__(self, status, *, headers=None, payload=b"release"):
        self.status = status
        self.headers = headers or {}
        self.payload = payload
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def iter_chunked(self, _size):
        yield self.payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["digest", "size", "oversize", "redirect", "source"]
)
async def test_codex_download_trust_caps_and_atomic_failure(
    setup_codex, tmp_path, monkeypatch, failure
):
    manager, _, _ = setup_codex
    url = codex.manifest_entry()["platforms"]["linux-x64"]["url"]
    destination = tmp_path / "download.tar.gz"
    payload = b"release"
    size = len(payload)
    digest = hashlib.sha256(payload).hexdigest()
    responses = [DownloadResponse(200, payload=payload)]
    if failure == "digest":
        digest = "0" * 64
    elif failure == "size":
        size -= 1
    elif failure == "oversize":
        size = codex.MAX_ARCHIVE_SIZE + 1
    elif failure == "redirect":
        responses = [
            DownloadResponse(
                302, headers={"Location": "https://attacker.invalid/asset"}
            )
        ]
    elif failure == "source":
        url = (
            "https://github.com/attacker/codex/releases/download/latest/package.tar.gz"
        )

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def get(self, url, *, allow_redirects):
            assert not allow_redirects
            return responses.pop(0)

    monkeypatch.setattr(artifacts.aiohttp, "ClientSession", Session)
    with pytest.raises(ArtifactProvisionError):
        await RuntimeArtifactManager._download(manager, url, size, digest, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".download-*"))


@pytest.mark.asyncio
async def test_codex_download_official_redirect_and_verified_atomic_publish(
    setup_codex, tmp_path, monkeypatch
):
    manager, _, _ = setup_codex
    url = codex.manifest_entry()["platforms"]["linux-x64"]["url"]
    asset = "https://release-assets.githubusercontent.com/github-production-release-asset/asset?signature=pinned"
    payload = b"verified official asset fixture"
    responses = [
        DownloadResponse(302, headers={"Location": asset}),
        DownloadResponse(200, payload=payload),
    ]
    seen = []

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def get(self, source, *, allow_redirects):
            assert not allow_redirects
            seen.append(source)
            return responses.pop(0)

    monkeypatch.setattr(artifacts.aiohttp, "ClientSession", Session)
    destination = tmp_path / "download.tar.gz"
    await RuntimeArtifactManager._download(
        manager, url, len(payload), hashlib.sha256(payload).hexdigest(), destination
    )
    assert seen == [url, asset]
    assert destination.read_bytes() == payload
    assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_cancelled_guest_operation_is_settled_without_orphan_task(setup_codex):
    manager, _, _ = setup_codex
    entered, finish = asyncio.Event(), asyncio.Event()

    async def operation():
        entered.set()
        await finish.wait()
        return 0, "0.163.0"

    task = asyncio.create_task(manager._complete_guest_operation(operation(), 2))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager._background_guest_tasks
    finish.set()
    for _ in range(100):
        if not manager._background_guest_tasks:
            break
        await asyncio.sleep(0.01)
    assert not manager._background_guest_tasks


@pytest.mark.parametrize("existing", ["empty", "poisoned"])
def test_codex_atomic_publication_never_replaces_existing_directory(tmp_path, existing):
    final = tmp_path / "final"
    final.mkdir()
    if existing == "poisoned":
        (final / "poison").write_text("keep")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new").write_text("new")
    with pytest.raises(ValueError, match="already exists"):
        codex.publish_directory(stage, final)
    assert stage.exists()
    assert not (final / "new").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "2.1.293"),
        ("signature_sha256", "0" * 64),
        ("release_manifest_sha256", "0" * 64),
        ("signing_fingerprint", "attacker"),
    ],
)
def test_adding_codex_does_not_relax_claude_manifest_trust(tmp_path, field, value):
    manifest = json.loads(artifacts.ARTIFACT_MANIFEST_PATH.read_text())
    manifest["artifacts"]["claude-agent"][field] = value
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="integrity"):
        RuntimeArtifactManager(
            {},
            lambda _: None,
            lambda _: None,
            tmp_path / "state",
            manifest_path=path,
        )


@pytest.mark.asyncio
async def test_codex_rejects_extra_installed_entries_and_hardlinks(
    setup_codex, tmp_path
):
    manager, _, _ = setup_codex
    result = await manager.ensure_runtime_artifact(
        uuid.uuid4(), "codex-agent", "0.163.0"
    )
    installed = Path(result["path"]).parent.parent
    installed.chmod(0o755)
    extra = installed / "extra"
    extra.write_text("unexpected")
    installed.chmod(0o555)
    with pytest.raises(ArtifactProvisionError):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")
    installed.chmod(0o755)
    extra.unlink()
    installed.chmod(0o555)
    os.link(installed / "codex-path/rg", tmp_path / "unexpected-hardlink")
    with pytest.raises(ArtifactProvisionError):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "codex-agent", "0.163.0")
