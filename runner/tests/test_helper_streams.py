"""Credential-free helpers share agent ownership and the real guest supervisor."""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.runtime.base import ProcessHandle
from src.runtime.managed_process import (
    ISOLATED_AGENT_PATH,
    isolated_agent_env_preamble,
    managed_isolated_argv,
)
from src.services.sessions.desktop_leases import DesktopLeaseStore
from src.services.sessions.streams import StreamManager

from tests.test_managed_streams import LocalRuntime, current_token


class HelperRuntime(LocalRuntime):
    """Run the production supervisor locally, with ordinary stream I/O."""

    async def process_read(
        self, handle: ProcessHandle, stream: str = "stdout", size: int = 65536
    ) -> bytes:
        return await getattr(handle.handle, stream).read(size)

    async def process_write(self, handle: ProcessHandle, data: bytes) -> None:
        handle.handle.stdin.write(data)
        await handle.handle.stdin.drain()

    async def process_write_eof(self, handle: ProcessHandle) -> None:
        handle.handle.stdin.write_eof()

    async def process_wait(self, handle: ProcessHandle) -> int:
        return await handle.handle.wait()


@dataclass
class HelperCase:
    """One durable agent owner and its local guest runtime."""

    manager: StreamManager
    runtime: HelperRuntime
    leases: DesktopLeaseStore
    workspace: uuid.UUID
    owner: dict[str, str]


@pytest.fixture
async def helper_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[HelperCase]:
    monkeypatch.setattr(
        "src.services.sessions.streams.STREAM_CONTROL_ROOT", str(tmp_path / "control")
    )
    workspace = uuid.uuid4()
    owner = {"lease_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
    leases = DesktopLeaseStore(tmp_path)
    await leases.reserve(
        str(workspace),
        "instance",
        owner["lease_id"],
        "agent",
        "session",
        owner["epoch"],
        revision=1,
        ttl=180,
    )
    runtime = HelperRuntime()
    manager = StreamManager(
        get_cached=lambda _: SimpleNamespace(instance_id="instance"),
        get_runtime=lambda _: runtime,
        state_dir=tmp_path,
        lease_store=leases,
        epoch=owner["epoch"],
    )
    try:
        yield HelperCase(manager, runtime, leases, workspace, owner)
    finally:
        await manager.close_owner_streams(owner["lease_id"])


async def test_helper_isolates_environment_preserves_stdin_and_closes_with_owner(
    helper_case: HelperCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME"):
        monkeypatch.setenv(key, "ambient-private-value")
    case = helper_case
    session = await case.manager.stream_start_process(
        case.workspace,
        "helper",
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import json,os,sys; first=sys.stdin.readline(); "
                "print(json.dumps({'env':dict(os.environ),'first':first}),flush=True); "
                "sys.stdin.readline()"
            ),
        ],
        workdir=str(tmp_path),
        env={},
        owner=case.owner,
        process_profile="helper",
    )
    process = session.handle.handle
    await case.manager.stream_write("helper", b"protocol request\n")
    output = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
    assert output["first"] == "protocol request\n"
    assert set(output["env"]) <= {"HOME", "PATH", "LC_CTYPE"}
    assert output["env"]["PATH"] == ISOLATED_AGENT_PATH
    home = Path(output["env"]["HOME"])
    assert home.is_dir()
    record = await case.manager.stream_record("helper")
    assert record["lease_id"] == case.owner["lease_id"]
    assert "ambient-private-value" not in str(case.runtime.last_argv)
    assert b"ambient-private-value" not in case.manager.intent_store.path.read_bytes()

    await case.leases.mark_closing(case.owner["lease_id"])
    assert await case.manager.close_owner_streams(case.owner["lease_id"])
    assert process.returncode is not None
    assert not home.exists()
    assert (await case.manager.stream_record("helper"))["state"] == "closed"


@pytest.mark.parametrize("profile", [None, "agent"])
async def test_empty_agent_environment_still_requires_authentication(
    helper_case: HelperCase, tmp_path: Path, profile: str | None
) -> None:
    case = helper_case
    marker = tmp_path / "child-ran"
    with pytest.raises(ValueError, match="Claude authentication"):
        await case.manager.stream_start_process(
            case.workspace,
            "missing-auth",
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
            workdir=str(tmp_path),
            env={},
            owner=case.owner,
            process_profile=profile,
        )
    assert not marker.exists()


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"process_profile": "unknown"}, "Invalid process profile"),
        ({"process_profile": ""}, "Invalid process profile"),
        ({"owner": None}, "managed owner"),
        ({"env": {"CODEX_HOME": "/tmp/foreign"}}, "empty environment"),
        ({"env": {"ANTHROPIC_API_KEY": "secret"}}, "empty environment"),
        ({"env": {"HOME": "/tmp/foreign"}}, "blocked"),
    ],
)
async def test_helper_rejects_invalid_profile_owner_and_environment(
    helper_case: HelperCase, kwargs: dict, match: str
) -> None:
    case = helper_case
    args = {"owner": case.owner, "process_profile": "helper", "env": {}, **kwargs}
    with pytest.raises(ValueError, match=match):
        await case.manager.stream_start_process(
            case.workspace, "invalid", [sys.executable], **args
        )
    assert case.runtime.last_argv is None
    assert await case.manager.stream_record("invalid") is None


@pytest.mark.parametrize("ended", ["closing", "expired", "stale", "foreign"])
async def test_helper_rejects_ended_or_mismatched_lease(
    helper_case: HelperCase, monkeypatch: pytest.MonkeyPatch, ended: str
) -> None:
    case = helper_case
    if ended == "closing":
        await case.leases.mark_closing(case.owner["lease_id"])
    elif ended == "expired":
        monkeypatch.setattr("src.services.sessions.streams.time.time", lambda: 1e12)
    owner = dict(case.owner)
    if ended == "stale":
        owner["epoch"] = "old-epoch"
    workspace = uuid.uuid4() if ended == "foreign" else case.workspace
    with pytest.raises(ValueError, match="Invalid or ended stream lease"):
        await case.manager.stream_start_process(
            workspace,
            "ended",
            [sys.executable],
            owner=owner,
            process_profile="helper",
        )
    assert case.runtime.last_argv is None


@pytest.mark.parametrize("kind", ["mcp", "computeruse"])
async def test_helper_profile_requires_agent_lease(
    helper_case: HelperCase, kind: str
) -> None:
    case = helper_case
    lease_id = str(uuid.uuid4())
    await case.leases.reserve(
        str(case.workspace),
        "instance",
        lease_id,
        kind,
        "other-owner",
        case.owner["epoch"],
        revision=1,
        ttl=180,
    )
    with pytest.raises(ValueError, match="require an agent lease"):
        await case.manager.stream_start_process(
            case.workspace,
            "wrong-kind",
            [sys.executable],
            owner={**case.owner, "lease_id": lease_id},
            process_profile="helper",
        )
    assert case.runtime.last_argv is None


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "CODEX_HOME", "PATH"])
async def test_guest_helper_rejects_poisoned_preamble(tmp_path: Path, key: str) -> None:
    marker = tmp_path / "child-ran"
    home = Path("/tmp/opencuria-agent-home-" + uuid.uuid4().hex)
    argv = managed_isolated_argv(
        str(tmp_path / "control"),
        str(tmp_path),
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
        env={},
        expected_token=current_token(),
        isolated_home=str(home),
        process_profile="helper",
    )
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = json.dumps({key: "poison"}).encode()
    await asyncio.wait_for(
        proc.communicate(struct.pack("!I", len(payload)) + payload), 5
    )
    assert proc.returncode == 125
    assert not marker.exists()
    assert not home.exists()
    with pytest.raises(ValueError, match="empty environment"):
        isolated_agent_env_preamble({key: "poison"}, process_profile="helper")


async def test_real_codex_mcp_bridge_uses_helper_and_ends_with_agent_lease(
    helper_case: HelperCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Import the production bridge without requiring a running Django backend.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "backend"))
    from apps.harness.engines.codex.mcp_bridge import ManagedMcpBridge

    case = helper_case

    class Stream:
        def __init__(self) -> None:
            self.process = case.manager.get_stream("mcp-helper").handle.handle

        async def receive(self) -> bytes:
            return await case.manager.stream_read_once("mcp-helper")

        async def send(self, data: bytes) -> None:
            await case.manager.stream_write("mcp-helper", data)

        async def send_eof(self) -> None:
            if self.process.returncode is None:
                await case.manager.stream_write_eof("mcp-helper")

        async def wait_closed(self) -> int | None:
            return await self.process.wait()

        async def aclose(self) -> None:
            await case.manager.stream_close("mcp-helper")

    class Accessor:
        async def open_process(
            self, command: list[str], cwd: str, env: dict[str, str], **kwargs
        ) -> Stream:
            await case.manager.stream_start_process(
                case.workspace, "mcp-helper", command, workdir=cwd, env=env, **kwargs
            )
            return Stream()

    invoke = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
    bridge = ManagedMcpBridge(
        [{"name": "allowed", "inputSchema": {"type": "object"}}], invoke
    )
    client = None
    socket_path = None
    try:
        config = await asyncio.wait_for(
            bridge.start(Accessor(), str(tmp_path), case.owner), 5
        )
        socket_path = Path(config["args"][-1])
        client = await asyncio.create_subprocess_exec(
            config["command"],
            *config["args"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for identifier, name in enumerate(["allowed", "forbidden"], 1):
            request = {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            }
            client.stdin.write((json.dumps(request) + "\n").encode())
            await client.stdin.drain()
            reply = json.loads(await asyncio.wait_for(client.stdout.readline(), 5))
            assert reply["id"] == identifier
            if name == "allowed":
                assert reply["result"]["content"][0]["text"] == "ok"
            else:
                assert reply["result"]["isError"]
        invoke.assert_awaited_once()
        assert invoke.call_args.args[0]["tool"] == "allowed"
        session = case.manager.get_stream("mcp-helper")
        record = await case.manager.stream_record("mcp-helper")
        identity = json.loads((Path(record["control_path"]) / "identity").read_text())
        home = Path(identity["isolated_home"])
        await case.leases.mark_closing(case.owner["lease_id"])
        assert await case.manager.close_owner_streams(case.owner["lease_id"])
        assert session.handle.handle.returncode is not None
        assert not home.exists()
        assert await asyncio.wait_for(client.wait(), 5) == 0
    finally:
        await bridge.close()
        if client is not None:
            if client.returncode is None:
                client.kill()
            await client.communicate()
        if socket_path is not None and socket_path.parent.exists():
            socket_path.unlink(missing_ok=True)
            socket_path.parent.rmdir()
