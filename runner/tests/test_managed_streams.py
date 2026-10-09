"""Real guest-script subprocesses exercise restart/late-start kill fencing."""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime.base import ProcessHandle
from src.runtime.managed_process import (
    ISOLATED_AGENT_CONFIG_ROOT,
    _isolated_agent_env,
    isolated_agent_env_preamble,
    managed_argv,
    managed_close_argv,
)
from src.services.sessions.streams import StreamManager


def current_token():
    return {
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "init_starttime": Path("/proc/1/stat")
        .read_text()
        .rsplit(")", 1)[1]
        .split()[19],
    }


@pytest.fixture(autouse=True)
def local_control_root(tmp_path, monkeypatch):
    """Use real scripts without requiring root guest-filesystem permissions."""
    monkeypatch.setattr(
        "src.services.sessions.streams.STREAM_CONTROL_ROOT",
        str(tmp_path / "guest-streams"),
    )


class LocalRuntime:
    runtime_type = "local"
    supports_managed_process = True

    def __init__(self):
        self.fail_close = False
        self.gate = None
        self.last_argv = None
        self.config_root = ISOLATED_AGENT_CONFIG_ROOT

    async def probe_managed_token(self, instance_id):
        return current_token()

    async def spawn_process(
        self,
        instance_id,
        command,
        workdir=None,
        env=None,
        *,
        control_path=None,
        expected_token=None,
        isolated_env=False,
        isolated_home=None,
    ):
        if self.gate:
            await self.gate.wait()
        argv = managed_argv(
            control_path,
            workdir,
            env,
            command,
            expected_token=expected_token or current_token(),
            isolated_env=isolated_env,
            isolated_home=isolated_home,
            config_root=self.config_root,
        )
        self.last_argv = argv
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if isolated_env:
            from src.runtime.managed_process import isolated_agent_env_preamble

            process.stdin.write(
                isolated_agent_env_preamble(env, config_root=self.config_root)
            )
            await process.stdin.drain()
        return ProcessHandle(instance_id, process, {"control_path": control_path})

    async def close_managed_process(self, instance_id, control_path):
        if self.fail_close:
            raise RuntimeError("offline")
        proc = await asyncio.create_subprocess_exec(
            *managed_close_argv(control_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(error.decode() or "unverified")
        return True

    async def process_close(self, handle):
        await self.close_managed_process(
            handle.instance_id, handle.metadata["control_path"]
        )
        await asyncio.wait_for(handle.handle.wait(), 10)
        return True


async def launch(path, code):
    return await asyncio.create_subprocess_exec(
        *managed_argv(
            str(path),
            None,
            {},
            [sys.executable, "-c", code],
            expected_token=current_token(),
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def published(path):
    for _ in range(200):
        if (path / "identity").exists():
            return json.loads((path / "identity").read_text())
        await asyncio.sleep(0.01)
    raise AssertionError("identity not published")


@pytest.mark.asyncio
async def test_anchor_retains_descendants_and_recovery_close(tmp_path):
    path = tmp_path / "control"
    proc = await launch(
        path,
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
        "'import time; time.sleep(90)']); print('ready',flush=True)",
    )
    await published(path)
    assert await proc.stdout.readline() == b"ready\n"
    await asyncio.sleep(0.1)
    assert proc.returncode is None
    assert await LocalRuntime().close_managed_process("instance", str(path))
    await asyncio.wait_for(proc.wait(), 10)
    late = await launch(path, "raise Exception('must not execute')")
    assert await late.wait() == 125


@pytest.mark.asyncio
async def test_close_before_start_is_durable(tmp_path):
    path = tmp_path / "control"
    runtime = LocalRuntime()
    await runtime.close_managed_process("instance", str(path))
    proc = await launch(path, "raise Exception('late')")
    assert await proc.wait() == 125
    await runtime.close_managed_process("instance", str(path))


@pytest.mark.asyncio
async def test_pid_reuse_is_unknown_not_killed(tmp_path):
    path = tmp_path / "control"
    proc = await launch(path, "import time; time.sleep(90)")
    identity = await published(path)
    identity["starttime"] = "wrong"
    (path / "identity").write_text(json.dumps(identity))
    with pytest.raises(RuntimeError):
        await LocalRuntime().close_managed_process("instance", str(path))
    assert proc.returncode is None
    stat = await asyncio.to_thread(Path(f"/proc/{identity['pid']}/stat").read_text)
    identity["starttime"] = str(int(stat.rsplit(")", 1)[1].split()[19]))
    (path / "identity").write_text(json.dumps(identity))
    await LocalRuntime().close_managed_process("instance", str(path))
    await proc.wait()


@pytest.mark.asyncio
async def test_old_guest_boot_never_signals_new_group(tmp_path):
    path = tmp_path / "control"
    path.mkdir()
    (path / "identity").write_text(json.dumps({"boot_id": "old-boot", "pid": 1}))
    assert await LocalRuntime().close_managed_process("instance", str(path))
    proc = await launch(path, "raise Exception()")
    assert await proc.wait() == 125


def manager(tmp_path, runtime, workspace_id, owner, info):
    lease = dict(
        owner,
        workspace_id=str(workspace_id),
        instance_id="instance",
        kind="mcp",
        expires_at=time.time() + 180,
        state="reserved",
    )

    class Leases:
        async def get(self, lease_id):
            return lease

    return StreamManager(
        get_cached=lambda _: info,
        get_runtime=lambda _: runtime,
        state_dir=tmp_path,
        lease_store=Leases(),
    )


@pytest.mark.asyncio
async def test_restart_intents_no_secrets_and_failure_retained(tmp_path):
    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    runtime = LocalRuntime()
    info = SimpleNamespace(instance_id="instance")
    first = manager(tmp_path, runtime, workspace, owner, info)
    session = await first.stream_start_process(
        workspace,
        "stdio",
        [sys.executable, "-c", "import time; time.sleep(90)"],
        workdir="/tmp",
        env={"TOKEN": "secret-value"},
        owner=owner,
    )
    row = await first.stream_record("stdio")
    await published(Path(row["control_path"]))
    assert "secret-value" not in first.intent_store.path.read_bytes().decode(
        errors="ignore"
    )
    restarted = manager(tmp_path, runtime, workspace, owner, info)
    runtime.fail_close = True
    assert not await restarted.close_owner_streams(owner["lease_id"])
    assert (await restarted.stream_record("stdio"))["state"] == "closing"
    runtime.fail_close = False
    info.instance_id = "replacement"
    assert not await restarted.close_owner_streams(owner["lease_id"])
    info.instance_id = "instance"
    assert await restarted.close_owner_streams(owner["lease_id"])
    assert await restarted.close_owner_streams(owner["lease_id"])
    assert (await restarted.stream_record("stdio"))["workspace_id"] == str(workspace)
    await session.handle.handle.wait()
    with pytest.raises(ValueError):
        await restarted.stream_start_process(workspace, "other", ["true"], owner=owner)


@pytest.mark.asyncio
async def test_close_joins_spawn_and_multiple_callers(tmp_path):
    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    runtime = LocalRuntime()
    runtime.gate = asyncio.Event()
    m = manager(
        tmp_path, runtime, workspace, owner, SimpleNamespace(instance_id="instance")
    )
    start = asyncio.create_task(
        m.stream_start_process(
            workspace,
            uuid.uuid4().hex,
            [sys.executable, "-c", 'raise Exception("late")'],
            workdir="/tmp",
            owner=owner,
        )
    )
    while not m._streams:
        await asyncio.sleep(0.01)
    conn = next(iter(m._streams))
    a = asyncio.create_task(m.stream_close(conn))
    b = asyncio.create_task(m.stream_close(conn))
    await asyncio.sleep(0.4)
    assert not a.done() and not b.done()
    runtime.gate.set()
    with pytest.raises(ValueError):
        await start
    assert (await a)["closed"] and (await b)["closed"]


@pytest.mark.asyncio
async def test_cancelled_spawn_remains_joinable(tmp_path):
    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    runtime = LocalRuntime()
    runtime.gate = asyncio.Event()
    m = manager(
        tmp_path, runtime, workspace, owner, SimpleNamespace(instance_id="instance")
    )
    conn = uuid.uuid4().hex
    start = asyncio.create_task(
        m.stream_start_process(
            workspace, conn, ["sleep", "90"], workdir="/tmp", owner=owner
        )
    )
    while conn not in m._streams:
        await asyncio.sleep(0.01)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    close = asyncio.create_task(m.close_owner_streams(owner["lease_id"]))
    await asyncio.sleep(0.4)
    assert not close.done()
    runtime.gate.set()
    assert await close
    assert (await m.stream_record(conn))["state"] == "closed"


@pytest.mark.asyncio
async def test_persisted_starting_intent_without_transport(tmp_path):
    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    runtime = LocalRuntime()
    m = manager(
        tmp_path, runtime, workspace, owner, SimpleNamespace(instance_id="instance")
    )
    path = str(tmp_path / "guest-control")
    await m.intent_store.reserve(
        dict(
            owner,
            connection_id="crashed",
            workspace_id=str(workspace),
            instance_id="instance",
            runtime_type="local",
            control_path=path,
            state="starting",
        )
    )
    restarted = manager(
        tmp_path, runtime, workspace, owner, SimpleNamespace(instance_id="instance")
    )
    assert await restarted.close_owner_streams(owner["lease_id"])
    proc = await launch(Path(path), "raise Exception()")
    assert await proc.wait() == 125


@pytest.mark.asyncio
@pytest.mark.parametrize("same_instance", [True, False])
@pytest.mark.parametrize(
    "status,closed",
    [
        ("stopped", True),
        ("exited", True),
        ("removed", True),
        ("running", False),
        ("paused", False),
        ("suspended", False),
        ("stopping", False),
        ("unknown", False),
    ],
)
async def test_old_incarnation_status_proof(tmp_path, same_instance, status, closed):
    from src.runtime.base import RuntimeStatus

    class EvidenceRuntime(LocalRuntime):
        async def get_workspace_status(self, instance_id):
            assert instance_id == "instance"
            return RuntimeStatus(instance_id, status)

        async def close_managed_process(self, instance_id, control_path):
            assert same_instance  # Never exec into the mismatched incarnation.
            raise RuntimeError("guest offline")

    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    runtime = EvidenceRuntime()
    info = SimpleNamespace(instance_id="instance" if same_instance else "replacement")
    m = manager(tmp_path, runtime, workspace, owner, info)
    await m.intent_store.reserve(
        dict(
            owner,
            connection_id="proof",
            workspace_id=str(workspace),
            instance_id="instance",
            runtime_type="local",
            control_path=str(tmp_path / "unused"),
            state="starting",
        )
    )
    assert await m.close_owner_streams(owner["lease_id"]) is closed
    assert (await m.stream_record("proof"))["state"] == (
        "closed" if closed else "closing"
    )


@pytest.mark.asyncio
async def test_recovery_bound_runtime_not_replacement_status(tmp_path):
    from src.runtime.base import RuntimeStatus

    class OldRuntime(LocalRuntime):
        async def get_workspace_status(self, instance_id):
            assert instance_id == "instance"
            return RuntimeStatus(instance_id, "running")

    class ReplacementRuntime(LocalRuntime):
        runtime_type = "replacement"

        async def get_workspace_status(self, instance_id):
            raise AssertionError("replacement status is not old death evidence")

    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    old = OldRuntime()
    m = manager(
        tmp_path,
        ReplacementRuntime(),
        workspace,
        owner,
        SimpleNamespace(instance_id="replacement"),
    )
    m._runtimes["local"] = old
    await m.intent_store.reserve(
        dict(
            owner,
            connection_id="proof",
            workspace_id=str(workspace),
            instance_id="instance",
            runtime_type="local",
            control_path=str(tmp_path / "unused"),
            state="starting",
        )
    )
    assert not await m.close_owner_streams(owner["lease_id"])


@pytest.mark.asyncio
async def test_status_error_unknown_not_removed(tmp_path):
    class UnknownRuntime(LocalRuntime):
        async def get_workspace_status(self, instance_id):
            raise RuntimeError("libvirt disconnected")

        async def workspace_exists(self, instance_id):
            raise RuntimeError("libvirt disconnected")

    assert not await StreamManager._incarnation_gone(UnknownRuntime(), "instance")


@pytest.mark.asyncio
async def test_lease_expiry_capability_and_epoch_rejected(tmp_path):
    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    runtime = LocalRuntime()
    info = SimpleNamespace(instance_id="instance")
    m = manager(tmp_path, runtime, workspace, owner, info)
    original = await m.lease_store.get(owner["lease_id"])
    original["expires_at"] = time.time() - 1
    with pytest.raises(ValueError):
        await m.stream_start_process(workspace, "expired", ["true"], owner=owner)
    original["expires_at"] = time.time() + 180
    runtime.supports_managed_process = False
    with pytest.raises(ValueError):
        await m.stream_start_process(workspace, "unsupported", ["true"], owner=owner)
    runtime.supports_managed_process = True
    m.epoch = "new-runner-epoch"
    with pytest.raises(ValueError):
        await m.stream_start_process(workspace, "old-epoch", ["true"], owner=owner)
    assert not m._streams
    assert await m.stream_record("expired") is None


def test_intent_storage_private_modes(tmp_path):
    from src.services.sessions.stream_intents import StreamIntentStore

    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    store = StreamIntentStore(state)
    assert state.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_legacy_fixture_close_without_owner(tmp_path):
    from unittest.mock import AsyncMock

    workspace = uuid.uuid4()
    m = StreamManager()
    assert await m.close_owner_streams("unrelated")
    for operation in ("close_workspace_streams", "close_all_streams"):
        runtime = SimpleNamespace(process_close=AsyncMock())
        m._streams["legacy"] = SimpleNamespace(
            connection_id="legacy",
            workspace_id=workspace,
            runtime=runtime,
            handle=object(),
            closed=False,
        )
        assert await m.close_owner_streams("unrelated")
        result = (
            await m.close_workspace_streams(workspace)
            if operation == "close_workspace_streams"
            else await m.close_all_streams()
        )
        assert result == 1
        runtime.process_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_child_launch_identity_not_false_live(tmp_path):
    path = tmp_path / "failed-child"
    proc = await asyncio.create_subprocess_exec(
        *managed_argv(
            str(path),
            None,
            {},
            ["/definitely/not/an/executable"],
            expected_token=current_token(),
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await published(path)
    await proc.communicate()
    assert proc.returncode != 0
    assert await LocalRuntime().close_managed_process("instance", str(path))
    late = await launch(path, 'raise Exception("late")')
    assert await late.wait() == 125


@pytest.mark.asyncio
async def test_supervisor_drops_stdin_while_descendant_lives(tmp_path):
    path = tmp_path / "stdin"
    proc = await asyncio.create_subprocess_exec(
        *managed_argv(
            str(path),
            None,
            {},
            [
                sys.executable,
                "-c",
                ("import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(90)'],stdin=subprocess.DEVNULL); "
                "print('ready',flush=True)"),
            ],
            expected_token=current_token(),
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    identity = await published(path)
    assert await proc.stdout.readline() == b"ready\n"
    for _ in range(100):
        if not Path(f"/proc/{identity['pid']}/fd/0").exists():
            break
        await asyncio.sleep(0.01)
    assert not Path(f"/proc/{identity['pid']}/fd/0").exists()
    assert proc.returncode is None
    await LocalRuntime().close_managed_process("instance", str(path))
    await proc.wait()


@pytest.mark.asyncio
async def test_qemu_connection_error_not_absence():
    from unittest.mock import Mock

    from src.runtime.qemu_runtime import QemuRuntime

    runtime = object.__new__(QemuRuntime)
    runtime._domain_name = lambda instance_id: instance_id
    runtime._get_domain = Mock(side_effect=RuntimeError("connection unavailable"))
    with pytest.raises(RuntimeError):
        await runtime.get_workspace_status("old-instance")
    with pytest.raises(RuntimeError):
        await runtime.workspace_exists("old-instance")
    assert not await StreamManager._incarnation_gone(runtime, "old-instance")


@pytest.mark.asyncio
async def test_qemu_paused_and_shutting_down_not_dead():
    from unittest.mock import Mock

    import libvirt

    from src.runtime.qemu_runtime import QemuRuntime

    runtime = object.__new__(QemuRuntime)
    runtime._domain_name = lambda instance_id: instance_id
    for state in (
        libvirt.VIR_DOMAIN_PAUSED,
        libvirt.VIR_DOMAIN_SHUTDOWN,
        libvirt.VIR_DOMAIN_PMSUSPENDED,
    ):
        runtime._get_domain = Mock(
            return_value=Mock(state=lambda state=state: (state, 0))
        )
        assert not await StreamManager._incarnation_gone(runtime, "old-instance")
    runtime._get_domain = Mock(
        return_value=Mock(state=lambda: (libvirt.VIR_DOMAIN_SHUTOFF, 0))
    )
    assert await StreamManager._incarnation_gone(runtime, "old-instance")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [("stopped", True), ("removed", True), ("unknown", False), ("running", False)],
)
async def test_confirm_workspace_ended_durable_without_cache(
    tmp_path, status, expected
):
    from src.runtime.base import RuntimeStatus

    class ProofRuntime(LocalRuntime):
        async def get_workspace_status(self, instance_id):
            assert instance_id == "instance"
            return RuntimeStatus(instance_id, status)

        async def close_managed_process(self, *args):
            raise AssertionError("must not exec")

    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    m = manager(
        tmp_path,
        ProofRuntime(),
        workspace,
        owner,
        SimpleNamespace(instance_id="replacement"),
    )
    m._runtimes["local"] = ProofRuntime()
    m._get_cached = lambda _: (_ for _ in ()).throw(ValueError("cache removed"))
    await m.intent_store.reserve(
        dict(
            owner,
            connection_id="ended",
            workspace_id=str(workspace),
            instance_id="instance",
            runtime_type="local",
            control_path="unused",
            state="closing",
        )
    )
    assert await m.confirm_workspace_ended(workspace, "instance") is expected
    assert (await m.stream_record("ended"))["state"] == (
        "closed" if expected else "closing"
    )
    if expected:
        with pytest.raises(ValueError):
            await m.intent_store.reserve(
                dict(
                    owner,
                    connection_id="late",
                    workspace_id=str(workspace),
                    instance_id="instance",
                    runtime_type="local",
                    control_path="unused",
                    state="starting",
                )
            )


@pytest.mark.asyncio
async def test_confirmation_pending_spawn_bounded_and_retained(tmp_path, monkeypatch):
    from src.runtime.base import RuntimeStatus

    monkeypatch.setattr("src.services.sessions.streams.STREAM_SETTLE_TIMEOUT", 0.05)

    class ProofRuntime(LocalRuntime):
        async def get_workspace_status(self, instance_id):
            return RuntimeStatus(instance_id, "stopped")

    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    m = manager(
        tmp_path,
        ProofRuntime(),
        workspace,
        owner,
        SimpleNamespace(instance_id="instance"),
    )
    await m.intent_store.reserve(
        dict(
            owner,
            connection_id="pending",
            workspace_id=str(workspace),
            instance_id="instance",
            runtime_type="local",
            control_path="unused",
            state="starting",
        )
    )
    from src.services.sessions.streams import StreamSession

    m._streams["pending"] = StreamSession(
        "pending", workspace, "process", None, ProofRuntime(), owner=owner
    )
    assert not await m.confirm_workspace_ended(workspace, "instance")
    assert (await m.stream_record("pending"))["state"] == "starting"
    m._streams["pending"].spawn_settled.set()
    assert await m.confirm_workspace_ended(workspace, "instance")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["boot_id", "init_starttime"])
async def test_prepared_deferred_launch_rejects_old_guest_token(tmp_path, field):
    token = current_token()
    token[field] = "old-boot" if field == "boot_id" else str(int(token[field]) + 1)
    path = tmp_path / "deferred"
    marker = tmp_path / "must-not-run"
    argv = managed_argv(
        str(path),
        None,
        {},
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ],
        expected_token=token,
    )
    # Transport executes a previously prepared argv in a different incarnation.
    await asyncio.sleep(0.01)
    proc = await asyncio.create_subprocess_exec(*argv)
    assert await proc.wait() == 125
    assert not marker.exists()
    assert not (path / "identity").exists()


@pytest.mark.asyncio
async def test_token_persisted_before_runtime_spawn(tmp_path):
    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}

    class CheckingRuntime(LocalRuntime):
        async def spawn_process(
            self,
            instance_id,
            command,
            workdir=None,
            env=None,
            *,
            control_path=None,
            expected_token=None,
        ):
            row = await m.stream_record("token-stream")
            assert json.loads(row["guest_token"]) == expected_token == current_token()
            return await super().spawn_process(
                instance_id,
                command,
                workdir,
                env,
                control_path=control_path,
                expected_token=expected_token,
            )

    m = manager(
        tmp_path,
        CheckingRuntime(),
        workspace,
        owner,
        SimpleNamespace(instance_id="instance"),
    )
    await m.stream_start_process(
        workspace, "token-stream", ["sleep", "90"], workdir="/tmp", owner=owner
    )
    assert (await m.stream_close("token-stream"))["closed"]


@pytest.mark.asyncio
async def test_probe_completion_rechecks_ended_lease(tmp_path):
    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}

    class ProbeRuntime(LocalRuntime):
        async def probe_managed_token(self, instance_id):
            row = await m.lease_store.get(owner["lease_id"])
            row["state"] = "closing"
            return current_token()

        async def spawn_process(self, *args, **kwargs):
            raise AssertionError("must not launch after queued probe")

    m = manager(
        tmp_path,
        ProbeRuntime(),
        workspace,
        owner,
        SimpleNamespace(instance_id="instance"),
    )
    with pytest.raises(ValueError, match="during guest probe"):
        await m.stream_start_process(workspace, "ended-probe", ["true"], owner=owner)
    assert await m.stream_record("ended-probe") is None


@pytest.mark.asyncio
async def test_agent_stream_private_environment_ignores_workspace_credentials(
    tmp_path, monkeypatch
):
    """Agent child accepts SDK env, excludes ambient auth and keeps stdin open."""
    workspace = uuid.uuid4()
    lease_id = str(uuid.uuid4())
    epoch = str(uuid.uuid4())
    runtime = LocalRuntime()
    runtime.config_root = str(tmp_path / "claude-config")
    (Path(runtime.config_root) / str(workspace) / "config").mkdir(parents=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "workspace-api-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "workspace-oauth")
    info = SimpleNamespace(instance_id="instance")

    class AgentLeases:
        async def get(self, _lease_id):
            return {
                "kind": "agent",
                "epoch": epoch,
                "expires_at": time.time() + 180,
                "state": "reserved",
                "workspace_id": str(workspace),
                "instance_id": "instance",
            }

    manager = StreamManager(
        get_cached=lambda _: info,
        get_runtime=lambda _: runtime,
        state_dir=tmp_path,
        lease_store=AgentLeases(),
        epoch=epoch,
    )
    session = await manager.stream_start_process(
        workspace,
        "agent-private-env",
        [
            sys.executable,
            "-c",
            (
                "import json,os,sys; "
                "first=sys.stdin.readline().strip(); "
                "second=sys.stdin.readline().strip(); "
                "print(json.dumps({'first':first,'second':second,"
                "'env':{k:v for k,v in os.environ.items() "
                "if k.startswith('ANTHROPIC_') or k.startswith('CLAUDE_') "
                "or k.startswith('DISABLE_') or k.startswith('ENABLE_') "
                "or k.startswith('MAX_')}}))"
            ),
        ],
        workdir="/workspace",
        env={
            "ANTHROPIC_API_KEY": "subscription-key",
            "CLAUDE_CONFIG_DIR": f"{runtime.config_root}/{workspace}/config",
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": "0.2.164",
            "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
            "DISABLE_UPDATES": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NON_ESSENTIAL_MODEL_CALLS": "0",
            "CLAUDE_CODE_DISABLE_NON_ESSENTIAL_TRAFFIC": "1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "0",
            "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING": "1",
            "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "4",
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "8",
            "MAX_CONCURRENT_SUBAGENTS": "8",
        },
        owner={"lease_id": lease_id, "epoch": epoch},
    )
    handle = session.handle.handle
    config = runtime.last_argv[runtime.last_argv.index("start") + 1]
    assert "subscription-key" not in config
    assert "workspace-api-key" not in config
    handle.stdin.write(b'{"type":"sdk-initialize"}\n')
    await handle.stdin.drain()
    handle.stdin.write(b'{"type":"sdk-next-turn"}\n')
    await handle.stdin.drain()
    output = await asyncio.wait_for(handle.stdout.readline(), 10)
    values = json.loads(output)
    assert values["env"]["ANTHROPIC_API_KEY"] == "subscription-key"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in values["env"]
    assert values["env"]["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"
    assert values["env"]["CLAUDE_AGENT_SDK_VERSION"] == "0.2.164"
    assert values["env"]["CLAUDE_CONFIG_DIR"] == (
        f"{runtime.config_root}/{workspace}/config"
    )
    assert values["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert values["env"]["CLAUDE_CODE_DISABLE_NON_ESSENTIAL_MODEL_CALLS"] == "0"
    assert values["env"]["CLAUDE_CODE_DISABLE_NON_ESSENTIAL_TRAFFIC"] == "1"
    assert values["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "0"
    assert values["env"]["CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"] == "1"
    assert values["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "4"
    assert values["env"]["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "8"
    assert values["env"]["MAX_CONCURRENT_SUBAGENTS"] == "8"
    assert values["first"] == '{"type":"sdk-initialize"}'
    assert values["second"] == '{"type":"sdk-next-turn"}'
    await manager.stream_close("agent-private-env")


@pytest.mark.parametrize("key", ["HOME", "PATH"])
def test_isolated_agent_env_rejects_runner_owned_environment(key):
    env = {
        "ANTHROPIC_API_KEY": "private-key",
        "CLAUDE_CONFIG_DIR": f"{ISOLATED_AGENT_CONFIG_ROOT}/{uuid.uuid4()}/config",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": "0.2.164",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
        "DISABLE_UPDATES": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        key: "/attacker/controlled",
    }
    with pytest.raises(ValueError, match="Unsupported environment variable"):
        _isolated_agent_env(env)


def test_isolated_agent_env_rejects_unmanaged_config_dir():
    env = {
        "ANTHROPIC_API_KEY": "private-key",
        "CLAUDE_CONFIG_DIR": "/tmp/claude-config",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": "0.2.164",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
        "DISABLE_UPDATES": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }
    with pytest.raises(ValueError, match="Claude config directory"):
        _isolated_agent_env(env)


def test_isolated_agent_env_requires_exactly_one_authentication_value():
    env = {
        "ANTHROPIC_API_KEY": "key",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
        "CLAUDE_CONFIG_DIR": f"{ISOLATED_AGENT_CONFIG_ROOT}/{uuid.uuid4()}/config",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": "0.2.164",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
        "DISABLE_UPDATES": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }
    with pytest.raises(ValueError, match="Exactly one Claude authentication"):
        _isolated_agent_env(env)


def test_isolated_agent_config_root_is_testable_without_internal_workspace_state():
    temp_root = "/tmp/opencuria-test-state/claude"
    env = {
        "ANTHROPIC_API_KEY": "private-key",
        "CLAUDE_CONFIG_DIR": f"{temp_root}/{uuid.uuid4()}/config",
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_AGENT_SDK_VERSION": "0.2.164",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
        "DISABLE_UPDATES": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }
    assert _isolated_agent_env(env, config_root=temp_root) == env
    assert len(isolated_agent_env_preamble(env, config_root=temp_root)) > 4
