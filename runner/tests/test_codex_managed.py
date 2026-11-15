"""Codex managed stdin isolation without subscription auth in the environment."""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.runtime.managed_process import (
    ISOLATED_CODEX_CONFIG_ROOT,
    ISOLATED_CODEX_PATH,
    _isolated_agent_env,
    isolated_agent_env_preamble,
    managed_isolated_argv,
)
from src.services.sessions.streams import StreamManager

from tests.test_managed_streams import LocalRuntime, current_token


def codex_env():
    return {"CODEX_HOME": f"{ISOLATED_CODEX_CONFIG_ROOT}/{uuid.uuid4()}/config"}


@pytest.mark.parametrize("api_key", [False, True])
def test_codex_profile_requires_only_explicit_config_and_optional_openrouter_key(
    api_key,
):
    env = codex_env()
    if api_key:
        env["OPENCURIA_CODEX_API_KEY"] = "private-openrouter-key"
    assert _isolated_agent_env(env) == env
    preamble = isolated_agent_env_preamble(env)
    assert struct.unpack("!I", preamble[:4])[0] == len(preamble) - 4
    assert json.loads(preamble[4:]) == env
    argv = managed_isolated_argv(
        "/tmp/control",
        "/workspace",
        ["/bin/true"],
        env=env,
        isolated_home="/tmp/opencuria-agent-home-" + uuid.uuid4().hex,
        expected_token=current_token(),
    )
    assert "private-openrouter-key" not in " ".join(argv)
    config = json.loads(argv[argv.index("start") + 1])
    assert config["env"] == {}
    assert config["isolated_agent"]["profile"] == "codex"
    assert config["isolated_agent"]["config_root"] == ISOLATED_CODEX_CONFIG_ROOT
    assert config["isolated_agent"]["path"] == ISOLATED_CODEX_PATH


@pytest.mark.parametrize(
    "key",
    [
        "HOME",
        "PATH",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "OPENAI_API_KEY",
        "OPENCURIA_ISOLATED_AGENT",
        "LD_PRELOAD",
        "CODEX_API_KEY",
        "DISABLE_UPDATES",
    ],
)
def test_codex_profile_rejects_other_env_and_cannot_disable_claude_auth(key):
    env = codex_env()
    env[key] = "untrusted"
    with pytest.raises(ValueError):
        isolated_agent_env_preamble(env)


@pytest.mark.parametrize(
    "suffix",
    [
        "session/config",
        "../config",
        f"{uuid.uuid4()}/config/../../escape",
        f"{uuid.uuid4()}/config/",
        f"{uuid.uuid4()}/config/resume-not-valid",
    ],
)
def test_codex_profile_requires_uuid_session_and_clean_config_path(suffix):
    with pytest.raises(ValueError):
        isolated_agent_env_preamble(
            {"CODEX_HOME": ISOLATED_CODEX_CONFIG_ROOT + "/" + suffix}
        )


@pytest.mark.parametrize("value", ["/tmp/config", "", None, "\x00"])
def test_codex_profile_rejects_wrong_config_root(value):
    with pytest.raises(ValueError):
        isolated_agent_env_preamble({"CODEX_HOME": value})


@pytest.mark.parametrize("api_key", [False, True])
@pytest.mark.asyncio
async def test_codex_agent_owner_stream_minimal_env_private_stdin_and_cleanup(
    tmp_path, monkeypatch, api_key
):
    monkeypatch.setattr(
        "src.services.sessions.streams.STREAM_CONTROL_ROOT", str(tmp_path / "control")
    )
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENCURIA_CODEX_API_KEY",
    ):
        monkeypatch.setenv(key, "ambient-secret")
    runtime = LocalRuntime()
    runtime.config_root = str(tmp_path / "codex-config")
    session_id = uuid.uuid4()
    config_dir = Path(runtime.config_root) / str(session_id) / "config"
    config_dir.mkdir(parents=True)
    env = {"CODEX_HOME": str(config_dir)}
    if api_key:
        env["OPENCURIA_CODEX_API_KEY"] = "private-openrouter"
    workspace, lease_id, epoch = uuid.uuid4(), str(uuid.uuid4()), str(uuid.uuid4())

    class AgentLease:
        async def get(self, _):
            return {
                "kind": "agent",
                "epoch": epoch,
                "expires_at": time.time() + 60,
                "state": "reserved",
                "workspace_id": str(workspace),
                "instance_id": "instance",
            }

    manager = StreamManager(
        get_cached=lambda _: SimpleNamespace(instance_id="instance"),
        get_runtime=lambda _: runtime,
        state_dir=tmp_path,
        lease_store=AgentLease(),
        epoch=epoch,
    )
    session = await manager.stream_start_process(
        workspace,
        "codex",
        [
            sys.executable,
            "-c",
            (
                "import json,os,sys; first=sys.stdin.readline(); "
                "print(json.dumps({'env':dict(os.environ),'first':first}),flush=True); "
                "sys.stdin.readline()"
            ),
        ],
        workdir=str(tmp_path),
        env=env,
        owner={"lease_id": lease_id, "epoch": epoch},
    )
    process = session.handle.handle
    # Subscription access token arrives only after start, via ordinary app-server stdin.
    process.stdin.write(
        b'{"method":"account/login/start","accessToken":"private-chatgpt"}\n'
    )
    await process.stdin.drain()
    output = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
    child_env = output["env"]
    # CPython may add LC_CTYPE to an otherwise minimal environment.
    assert set(child_env) <= {
        "HOME",
        "PATH",
        "CODEX_HOME",
        "OPENCURIA_CODEX_API_KEY",
        "LC_CTYPE",
    }
    assert child_env["CODEX_HOME"] == str(config_dir)
    assert child_env["PATH"] == ISOLATED_CODEX_PATH
    assert "ambient-secret" not in str(output)
    assert "private-chatgpt" in output["first"]
    home = Path(child_env["HOME"])
    assert home.is_dir()
    assert "private-openrouter" not in " ".join(runtime.last_argv)
    for path in (tmp_path / "control").rglob("*"):
        if path.is_file():
            assert "private-openrouter" not in path.read_text()
            assert "private-chatgpt" not in path.read_text()
    assert "private-chatgpt" not in manager.intent_store.path.read_bytes().decode(
        errors="replace"
    )
    assert "private-openrouter" not in manager.intent_store.path.read_bytes().decode(
        errors="replace"
    )
    process.stdin.write(b"exit\n")
    await process.stdin.drain()
    assert await asyncio.wait_for(process.wait(), 5) == 0
    assert not home.exists()
    await manager.stream_close("codex")


async def guest_process(tmp_path, explicit, *, actual=None, modify=None):
    root = str(tmp_path / "codex-root")
    config = Path(root) / str(uuid.uuid4()) / "config"
    config.mkdir(parents=True)
    env = {"CODEX_HOME": str(config), **explicit}
    home = "/tmp/opencuria-agent-home-" + uuid.uuid4().hex
    marker = tmp_path / "child-ran"
    argv = managed_isolated_argv(
        str(tmp_path / "control"),
        str(tmp_path),
        [sys.executable, "-c", f"open({str(marker)!r},'w').write('executed')"],
        env=env,
        config_root=root,
        isolated_home=home,
        expected_token=current_token(),
    )
    if modify:
        modify(argv, config)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = json.dumps(actual if actual is not None else env).encode()
    await proc.communicate(struct.pack("!I", len(payload)) + payload)
    return proc.returncode, marker, Path(home)


@pytest.mark.parametrize(
    "key", ["HOME", "PATH", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR", "OPENAI_API_KEY"]
)
@pytest.mark.asyncio
async def test_guest_supervisor_independently_rejects_poisoned_preamble(tmp_path, key):
    code, marker, home = await guest_process(
        tmp_path, {}, actual={key: "poison", "CODEX_HOME": "/tmp/escape"}
    )
    assert code == 125 and not marker.exists() and not home.exists()


@pytest.mark.asyncio
async def test_guest_rejects_codex_env_under_claude_profile(tmp_path):
    def modify(argv, config):
        index = argv.index("start") + 1
        data = json.loads(argv[index])
        data["isolated_agent"]["profile"] = "claude"
        argv[index] = json.dumps(data)

    code, marker, _ = await guest_process(tmp_path, {}, modify=modify)
    assert code == 125 and not marker.exists()


@pytest.mark.parametrize("component", ["root", "session", "config"])
@pytest.mark.asyncio
async def test_guest_rejects_each_symlinked_config_component(tmp_path, component):
    def modify(argv, config):
        target = (
            config
            if component == "config"
            else config.parent
            if component == "session"
            else config.parent.parent
        )
        moved = tmp_path / "moved"
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=True)

    code, marker, home = await guest_process(tmp_path, {}, modify=modify)
    assert code == 125 and not marker.exists() and not home.exists()


@pytest.mark.asyncio
async def test_missing_executable_cleans_isolated_home(tmp_path):
    def modify(argv, config):
        argv[argv.index("start") + 2 :] = ["/no/such/executable"]

    code, marker, home = await guest_process(tmp_path, {}, modify=modify)
    assert code == 125 and not marker.exists() and not home.exists()




@pytest.mark.asyncio
@pytest.mark.parametrize("process_profile", ["agent", "helper"])
async def test_qemu_selects_profile_and_transmits_only_private_preamble(process_profile):
    from unittest.mock import AsyncMock, MagicMock

    from src.runtime.qemu_runtime import QemuRuntime

    runtime = object.__new__(QemuRuntime)
    stdin = MagicMock()
    stdin.drain = AsyncMock()
    process = SimpleNamespace(stdin=stdin, close=MagicMock())
    ssh = SimpleNamespace(create_process=AsyncMock(return_value=process))
    runtime._get_ssh = AsyncMock(return_value=ssh)
    runtime._stream_pidfile = MagicMock(return_value="/tmp/stream.pid")
    env = {}
    if process_profile == "agent":
        env = codex_env()
        env["OPENCURIA_CODEX_API_KEY"] = "private-codex-key"
    await runtime.spawn_process(
        "guest",
        ["/opt/opencuria/runtimes/codex-agent/0.166.0/bin/codex-app-server"],
        workdir="/workspace",
        env=env,
        control_path="/tmp/control",
        expected_token=current_token(),
        process_profile=process_profile,
        isolated_home="/tmp/opencuria-agent-home-" + uuid.uuid4().hex,
    )
    import shlex

    argv = shlex.split(ssh.create_process.call_args.args[0])
    data = json.loads(argv[argv.index("start") + 1])
    assert data["isolated_agent"]["profile"] == (
        "codex" if process_profile == "agent" else "helper"
    )
    if process_profile == "agent":
        assert data["isolated_agent"]["config_root"] == ISOLATED_CODEX_CONFIG_ROOT
    assert "private-codex-key" not in str(ssh.create_process.call_args)
    stdin.write.assert_called_once_with(
        isolated_agent_env_preamble(env, process_profile=process_profile)
    )
    stdin.write_eof.assert_not_called()


@pytest.mark.asyncio
async def test_guest_rejects_caller_path_before_creating_home(tmp_path):
    def modify(argv, config):
        index = argv.index("start") + 1
        data = json.loads(argv[index])
        data["isolated_agent"]["path"] = "/attacker/bin"
        argv[index] = json.dumps(data)

    code, marker, home = await guest_process(tmp_path, {}, modify=modify)
    assert code == 125 and not marker.exists() and not home.exists()


@pytest.mark.parametrize("provider", ["chatgpt", "openrouter"])
@pytest.mark.parametrize("readonly", [False, True])
def test_actual_backend_codex_argv_passes_production_stream_boundary(
    provider, readonly
):
    """Exercise the real backend launch builder against runner RPC validation."""
    import runpy

    root = Path(__file__).resolve().parents[2]
    configuration = runpy.run_path(
        str(root / "backend/apps/harness/engines/codex/configuration.py")
    )
    auth = SimpleNamespace(provider_id=provider, base_url="")
    settings, _, _ = configuration["runtime_settings"](
        auth, f"{provider}/test-model", readonly=readonly
    )
    settings["projects"] = {"/workspace": {"trust_level": "untrusted"}}
    binary = "/opt/opencuria/runtimes/codex-agent/0.166.0/bin/codex-app-server"
    argv = configuration["runtime_argv"](binary, settings)
    assert len(argv) <= 64
    assert StreamManager._sanitize_stream_command(argv) == argv
    features = [part for part in argv if part.startswith("features=")]
    assert len(features) == 1
    # Native agents keep their CLI defaults; compacting the feature table must
    # retain the host required for exec/wait and the read-only shell policy.
    assert '"multi_agent"' not in features[0]
    assert '"code_mode_host" = true' in features[0]
    assert f'"shell_tool" = {str(not readonly).lower()}' in features[0]
