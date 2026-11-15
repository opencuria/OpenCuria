import asyncio
import io
import os
import shlex
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from src.config import RunnerSettings
from src.runtime.base import WorkspaceConfig
from src.service import WorkspaceService
from src.services.credentials import (
    WORKSPACE_CREDENTIAL_DIR,
    WORKSPACE_CREDENTIAL_ENV_FILE,
)
from src.services.files import convert_archive_to_tar


@pytest.mark.parametrize("link_type", ["sym", "hard"])
def test_convert_archive_to_tar_rejects_links(link_type: str) -> None:
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w") as tar:
        info = tarfile.TarInfo(name="malicious-link")
        info.linkname = "/etc/passwd"
        info.type = tarfile.SYMTYPE if link_type == "sym" else tarfile.LNKTYPE
        tar.addfile(info)
    source.seek(0)

    with pytest.raises(ValueError, match="unsafe links"):
        convert_archive_to_tar(source.getvalue())


@pytest.mark.asyncio
@pytest.mark.parametrize("clone_exit_code", [0, 7, None])
async def test_create_workspace_clones_with_persistent_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone_exit_code: int | None
) -> None:
    env_path = tmp_path / "env.sh"
    clone_args = tmp_path / "clone-args"
    git = tmp_path / "git"
    git.write_text(
        "#!/bin/sh\n"
        'test "$GITHUB_TOKEN" = secret || exit 99\n'
        f'printf "%s\\n" "$@" >> {shlex.quote(str(clone_args))}\n'
        "echo clone-output\n"
        "echo clone-error >&2\n"
        + ("sleep 60\n" if clone_exit_code is None else f"exit {clone_exit_code}\n")
    )
    git.chmod(0o700)

    async def execute(
        instance_id: str,
        command: list[str],
        workdir: str,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        if "OPENCURIA_STDOUT" not in command[-1]:
            return 0, ""
        assert (instance_id, workdir) == ("instance-1", "/workspace")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        assert process.returncode is not None
        return process.returncode, output.decode()

    async def put_archive(instance_id: str, path: str, content: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(content)) as archive:
            env_path.write_bytes(archive.extractfile("env.sh").read())

    class DummyRuntime:
        def __init__(self) -> None:
            self.exec_command_wait = AsyncMock(side_effect=execute)
            self.put_archive = AsyncMock(side_effect=put_archive)

        async def create_workspace(self, config: WorkspaceConfig) -> str:
            return "instance-1"

    runtime = DummyRuntime()
    service = WorkspaceService(
        runtime,
        RunnerSettings(workspace_repo_clone_timeout=0.1),
    )
    service.harness._credential_env_file = str(env_path)
    log = Mock()
    log.bind.return_value = log
    monkeypatch.setattr("src.services.workspace_lifecycle.logger", log)
    repos = ["repo 'name'; touch injected", "another-repo"]

    workspace_id, credentials_present = await service.lifecycle.create_workspace(
        repos=repos,
        env_vars={"GITHUB_TOKEN": "secret", "PATH": f"{tmp_path}:{os.defpath}"},
        ssh_keys=[
            "-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----"
        ],
        base_image_path="/images/base.qcow2",
    )

    assert credentials_present is True
    runtime.put_archive.assert_awaited_once()
    with tarfile.open(
        fileobj=io.BytesIO(runtime.put_archive.await_args.args[2]), mode="r"
    ) as archive:
        env_file = archive.extractfile("env.sh").read().decode("utf-8")
        ssh_key = archive.extractfile("ssh/id_ed25519").read().decode("utf-8")
    assert "GITHUB_TOKEN=secret" in env_file
    assert "mock" in ssh_key
    assert clone_args.read_text().splitlines() == ["clone", repos[0], "clone", repos[1]]
    assert not (tmp_path / "injected").exists()
    assert service.registry.get_cached(workspace_id).status == "running"
    assert log.warning.call_count == (0 if clone_exit_code == 0 else len(repos))
    if clone_exit_code != 0:
        assert log.warning.call_args.kwargs["output"] == "clone-output\nclone-error\n"


@pytest.mark.asyncio
async def test_inject_workspace_credentials_materializes_file_credentials() -> None:
    class DummyRuntime:
        def __init__(self) -> None:
            self.archives: list[tuple[str, str, bytes]] = []
            self.commands: list[list[str]] = []

        async def put_archive(self, instance_id, target_dir, archive_data):
            self.archives.append((instance_id, target_dir, archive_data))

        async def exec_command_wait(self, instance_id, command, workdir):
            self.commands.append(command)
            return 0, ""

    runtime = DummyRuntime()
    service = WorkspaceService(runtime, RunnerSettings())
    service.credentials.remove_workspace_credentials = AsyncMock()

    present = await service.credentials.inject_workspace_credentials(
        runtime,
        "instance-1",
        {},
        [
            {
                "target_path": "~/.codex/auth.json",
                "content": '{"access_token":"abc"}',
                "mode": 0o600,
            }
        ],
        [],
        log=Mock(),
    )

    assert present is True
    assert runtime.archives
    assert runtime.archives[0][1] == WORKSPACE_CREDENTIAL_DIR
    archive = runtime.archives[0][2]
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
        assert tar.extractfile("files/credential_1").read() == b'{"access_token":"abc"}'
        install = tar.extractfile("install.sh").read().decode("utf-8")
        env_file = tar.extractfile("env.sh").read().decode("utf-8")
        manifest = tar.extractfile("manifest").read().decode("utf-8")

    assert "~/.codex/auth.json" in install
    assert "install -m 600" in install
    assert (
        WORKSPACE_CREDENTIAL_ENV_FILE.split("/")[-1] in env_file or "PATH=" in env_file
    )
    assert "~/.codex/auth.json" in manifest
    assert any("install.sh" in " ".join(cmd) for cmd in runtime.commands)


@pytest.mark.asyncio
async def test_inject_workspace_credentials_empty_set_returns_false() -> None:
    runtime = Mock()
    runtime.exec_command_wait = AsyncMock(return_value=(0, ""))
    service = WorkspaceService(runtime, RunnerSettings())
    service.credentials.remove_workspace_credentials = AsyncMock()

    present = await service.credentials.inject_workspace_credentials(
        runtime,
        "instance-1",
        {},
        [],
        [],
        log=Mock(),
    )

    assert present is False
    service.credentials.remove_workspace_credentials.assert_awaited_once()
    runtime.exec_command_wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_workspace_removes_credentials_before_runtime_stop(tmp_path) -> None:
    runtime = Mock()
    runtime.stop_workspace = AsyncMock()
    runtime.get_workspace_status = AsyncMock(return_value=Mock(status="running"))
    service = WorkspaceService(runtime, RunnerSettings(state_dir=str(tmp_path)))
    service.configure_desktop_leases(str(tmp_path), "security-test-epoch")
    service.credentials.remove_workspace_credentials = AsyncMock()
    workspace_id = __import__("uuid").uuid4()
    from src.models import WorkspaceInfo

    service.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
    )

    present = await service.lifecycle.stop_workspace(workspace_id)

    assert present is False
    service.credentials.remove_workspace_credentials.assert_awaited_once()
    runtime.stop_workspace.assert_awaited_once_with("instance-1")
    assert (
        service.credentials.remove_workspace_credentials.await_args.args[1]
        == "instance-1"
    )


@pytest.mark.asyncio
async def test_start_terminal_sources_persistent_env() -> None:
    runtime = Mock()
    runtime.exec_pty = AsyncMock(return_value=Mock())
    runtime.workspace_exists = AsyncMock(return_value=True)

    service = WorkspaceService(runtime, RunnerSettings())
    workspace_id = __import__("uuid").uuid4()
    from src.models import WorkspaceInfo

    service.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
    )

    await service.terminal_manager.start_terminal(workspace_id=workspace_id)

    runtime.exec_pty.assert_awaited_once()
    call = runtime.exec_pty.await_args
    assert call.args[0] == "instance-1"
    command = call.kwargs["command"]
    assert command[0] == "/bin/bash"
    assert WORKSPACE_CREDENTIAL_ENV_FILE in command[2]
    assert "TERM" in call.kwargs["env"]
