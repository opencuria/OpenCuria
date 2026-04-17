import io
import os
import shutil
import subprocess
import tarfile
from unittest.mock import AsyncMock, Mock

import pytest

from src.config import RunnerSettings
from src.service import OperationCredentialContext, PreparedOperation, WorkspaceService


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
        WorkspaceService._convert_archive_to_tar(source.getvalue())


@pytest.mark.asyncio
async def test_create_workspace_uses_operation_credentials_for_clone_and_configure() -> None:
    class DummyRuntime:
        async def create_workspace(self, config):  # noqa: ANN001
            return "instance-1"

    runtime = DummyRuntime()
    service = WorkspaceService({"docker": runtime}, RunnerSettings())
    service._cleanup_legacy_workspace_credentials = AsyncMock()
    credential_context = object()
    service._create_operation_credential_context = AsyncMock(
        return_value=credential_context
    )
    service._cleanup_operation_credential_context = AsyncMock()
    service._exec_command = AsyncMock(side_effect=[(0, ""), (0, "")])

    await service.create_workspace(
        repos=["git@github.com:example/private-repo.git"],
        configure_commands=[
            {
                "args": ["echo", "configured"],
                "workdir": "/workspace",
                "env": {},
                "description": "configure",
            }
        ],
        env_vars={"GITHUB_TOKEN": "secret"},
        ssh_keys=["-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----"],
        runtime_type="docker",
        image_tag="opencuria/workspace:test",
    )

    create_ctx_args = service._create_operation_credential_context.await_args.args
    assert create_ctx_args[0] is runtime
    assert create_ctx_args[1] == "instance-1"
    assert create_ctx_args[2] == {"GITHUB_TOKEN": "secret"}
    assert create_ctx_args[3] is None
    assert create_ctx_args[4] == [
        "-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----"
    ]
    assert service._exec_command.await_count == 2
    clone_call = service._exec_command.await_args_list[0]
    assert clone_call.kwargs["credential_context"] is credential_context
    assert clone_call.args[2]["args"] == [
        "git",
        "clone",
        "git@github.com:example/private-repo.git",
    ]
    configure_call = service._exec_command.await_args_list[1]
    assert configure_call.kwargs["credential_context"] is credential_context
    service._cleanup_operation_credential_context.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_operation_credential_context_materializes_and_cleans_file_credentials() -> None:
    class DummyRuntime:
        def __init__(self) -> None:
            self.archives: list[tuple[str, str, bytes]] = []
            self.commands: list[list[str]] = []

        async def put_archive(self, instance_id, target_dir, archive_data):  # noqa: ANN001
            self.archives.append((instance_id, target_dir, archive_data))

        async def exec_command_wait(self, instance_id, command, workdir):  # noqa: ANN001
            self.commands.append(command)
            return 0, ""

    runtime = DummyRuntime()
    service = WorkspaceService({"docker": runtime}, RunnerSettings())

    context = await service._create_operation_credential_context(
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

    assert context is not None
    assert runtime.archives
    assert runtime.commands[0] == ["mkdir", "-p", context.directory]
    assert runtime.commands[1][:2] == ["sh", "-lc"]
    assert "bootstrap.sh" in runtime.commands[1][2]

    archive = runtime.archives[0][2]
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
        assert tar.extractfile("files/credential_1").read() == b'{"access_token":"abc"}'
        bootstrap = tar.extractfile("bootstrap.sh").read().decode("utf-8")
        cleanup = tar.extractfile("cleanup.sh").read().decode("utf-8")

    assert "~/.codex/auth.json" in bootstrap
    assert "install -m 600" in bootstrap
    assert "~/.codex/auth.json" in cleanup

    home_dir = "/tmp/opencuria-test-home"
    shutil.rmtree(context.directory, ignore_errors=True)
    shutil.rmtree(home_dir, ignore_errors=True)
    os.makedirs(context.directory, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
        tar.extractall(context.directory, filter="data")

    result = subprocess.run(
        [
            "sh",
            "-lc",
            f"HOME={home_dir} . {context.bootstrap_script} && cat {home_dir}/.codex/auth.json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == '{"access_token":"abc"}'

    await service._cleanup_operation_credential_context(
        runtime,
        "instance-1",
        context,
        log=Mock(),
    )
    assert "cleanup.sh" in runtime.commands[2][2]
    shutil.rmtree(context.directory, ignore_errors=True)
    shutil.rmtree(home_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_start_terminal_bootstraps_credential_context_before_login_shell() -> None:
    runtime = Mock()
    runtime.exec_pty = AsyncMock(return_value=Mock())

    service = WorkspaceService({"docker": runtime}, RunnerSettings())
    credential_context = OperationCredentialContext(
        directory="/tmp/opencuria-op-test",
        bootstrap_script="/tmp/opencuria-op-test/bootstrap.sh",
        cleanup_script="/tmp/opencuria-op-test/cleanup.sh",
        environment={"OPENCURIA_CREDENTIAL_CONTEXT_DIR": "/tmp/opencuria-op-test"},
    )
    prepared = PreparedOperation(
        workspace_id=Mock(),
        instance_id="instance-1",
        runtime=runtime,
        log=Mock(),
        credential_context=credential_context,
    )

    await service.start_terminal(
        workspace_id=prepared.workspace_id,
        prepared=prepared,
    )

    runtime.exec_pty.assert_awaited_once()
    call = runtime.exec_pty.await_args
    assert call.args[0] == "instance-1"
    assert call.kwargs["command"] == [
        "/bin/bash",
        "-lc",
        ". /tmp/opencuria-op-test/bootstrap.sh >/dev/null 2>&1; exec /bin/bash -l",
    ]
    assert call.kwargs["env"]["OPENCURIA_CREDENTIAL_CONTEXT_DIR"] == (
        "/tmp/opencuria-op-test"
    )
