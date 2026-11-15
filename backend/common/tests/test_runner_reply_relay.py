"""Real Redis transport tests, including an independent execution process."""

from __future__ import annotations

import asyncio
import shutil
import socket
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from common.runner_reply_relay import RunnerReplyRelay


@pytest.fixture
async def relay_redis():
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is required for cross-process relay tests")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        executable,
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--save",
        "",
        "--appendonly",
        "no",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    redis = Redis.from_url(url)
    try:
        for _ in range(100):
            try:
                await redis.ping()
                break
            except ConnectionError:
                await asyncio.sleep(0.02)
        else:
            pytest.fail("isolated Redis did not start")
        yield url, redis
    finally:
        await redis.aclose()
        if process.returncode is None:
            process.terminate()
        await process.wait()


def peer(url, channel, *, dispatch=None, owns=None, disconnected=None, timeout=1):
    return RunnerReplyRelay(
        url,
        events={
            "git:operation_result",
            "harness:exec_wait_result",
            "harness:read_file_chunk",
            "harness:read_file_result",
            "workspace:stream_output",
            "workspace:stream_closed",
        },
        dispatch=dispatch or AsyncMock(return_value=False),
        owns=owns or (lambda event, data: False),
        disconnected=disconnected or AsyncMock(),
        channel=channel,
        timeout=timeout,
    )


async def test_chunks_cross_process_without_persistence(relay_redis):
    url, redis = relay_redis
    channel = uuid.uuid4().hex
    script = r"""
import asyncio, sys
from common.runner_reply_relay import RunnerReplyRelay
async def main():
    async def dispatch(event, data, runner):
        return runner == "trusted" and data.get("content") == "x" * (256 * 1024)
    async def disconnected(runner): pass
    relay = RunnerReplyRelay(sys.argv[1], dispatch=dispatch,
        events={"harness:read_file_chunk", "harness:read_file_result"},
        owns=lambda event, data: data.get("request_id") == "owned",
        disconnected=disconnected, channel=sys.argv[2])
    await relay.start()
    sys.stdout.write("ready\n"); sys.stdout.flush()
    await asyncio.to_thread(sys.stdin.readline)
    await relay.stop()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        url,
        channel,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[2],
    )
    sender = peer(url, channel)
    try:
        await sender.start()
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        payload = {"request_id": "owned", "content": "x" * (256 * 1024)}
        assert await sender.route("harness:read_file_chunk", payload, "trusted")
        assert not await sender.route("harness:read_file_result", payload, "foreign")
        assert await redis.keys("*") == []
    finally:
        await sender.stop()
        if process.returncode is None:
            process.stdin.write(b"stop\n")
            await process.stdin.drain()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        assert process.returncode == 0, (await process.stderr.read()).decode()


async def test_matches_owner_and_preserves_stream_acceptance(relay_redis):
    url, _ = relay_redis
    channel = uuid.uuid4().hex
    dispatch, unrelated = AsyncMock(return_value=True), AsyncMock(return_value=True)
    sender = peer(url, channel)
    owner = peer(
        url,
        channel,
        dispatch=dispatch,
        owns=lambda event, data: data.get("connection_id") == "stream",
    )
    other = peer(url, channel, dispatch=unrelated)
    try:
        for instance in (sender, owner, other):
            await instance.start()
        payload = {
            "connection_id": "stream",
            "workspace_id": "ws",
            "data": "YQ==",
            "stream": "stdout",
        }
        assert await sender.route("workspace:stream_output", payload, "runner")
        dispatch.assert_awaited_once_with("workspace:stream_output", payload, "runner")
        unrelated.assert_not_awaited()
        dispatch.return_value = False
        assert not await sender.route("workspace:stream_closed", payload, "runner")
    finally:
        for instance in (sender, owner, other):
            await instance.stop()


async def test_local_fast_path_and_authentication_boundary(relay_redis):
    url, _ = relay_redis
    dispatch = AsyncMock(return_value=True)
    relay = peer(url, uuid.uuid4().hex, dispatch=dispatch)
    try:
        assert await relay.route(
            "git:operation_result", {"request_id": "local"}, "runner"
        )
        assert not await relay.route("unrecognized", {}, "runner")
        assert not await relay.route("git:operation_result", {}, "")
        dispatch.assert_awaited_once()
    finally:
        await relay.stop()


async def test_disconnect_notifies_remote_stream_owners_once(relay_redis):
    url, _ = relay_redis
    channel = uuid.uuid4().hex
    local, remote = AsyncMock(), AsyncMock()
    sender = peer(url, channel, disconnected=local)
    receiver = peer(url, channel, disconnected=remote)
    try:
        await sender.start()
        await receiver.start()
        await sender.runner_disconnected("runner")
        for _ in range(100):
            if remote.await_count:
                break
            await asyncio.sleep(0.01)
        remote.assert_awaited_once_with("runner")
        local.assert_not_awaited()
    finally:
        await sender.stop()
        await receiver.stop()


async def test_shutdown_wakes_pending_and_unknown_owner_times_out(relay_redis):
    url, _ = relay_redis
    channel = uuid.uuid4().hex
    sender, other = peer(url, channel, timeout=0.05), peer(url, channel)
    try:
        await sender.start()
        await other.start()
        assert not await sender.route(
            "git:operation_result", {"request_id": "unknown"}, "runner"
        )
        pending = asyncio.create_task(
            sender.route("git:operation_result", {"request_id": "pending"}, "runner")
        )
        await asyncio.sleep(0)
        await sender.stop()
        assert await pending is False
    finally:
        await other.stop()


async def test_malformed_internal_envelopes_never_dispatch(relay_redis):
    url, _ = relay_redis
    dispatch = AsyncMock()
    relay = peer(url, uuid.uuid4().hex, dispatch=dispatch, owns=lambda *_: True)
    try:
        for envelope in (
            None,
            [],
            {},
            {"kind": "reply", "origin": "other"},
            {
                "kind": "reply",
                "origin": "other",
                "runner_id": "runner",
                "event": "evil",
                "data": {},
                "correlation": "x",
            },
        ):
            await relay._receive(envelope)
        dispatch.assert_not_awaited()
    finally:
        await relay.stop()


async def test_remote_git_reply_resolves_real_registry_after_domain_checks(
    relay_redis,
    monkeypatch,
):
    """Relay acceptance means an actual waiter passed workspace/operation fencing."""
    from asgiref.sync import sync_to_async

    from apps.runners.services import RunnerService
    from apps.runners.services.infra.rpc_registry import PendingGitRequest

    url, _ = relay_redis
    channel = uuid.uuid4().hex
    workspace, runner = uuid.uuid4(), str(uuid.uuid4())
    service = RunnerService()
    monkeypatch.setattr(
        service.ownership,
        "validate_harness_workspace_runner",
        lambda claimed_workspace, claimed_runner: (
            claimed_workspace == workspace and claimed_runner == runner
        ),
    )
    future = asyncio.get_running_loop().create_future()
    service.rpc_registry.git_pending["owned-git"] = PendingGitRequest(
        future=future,
        workspace_id=str(workspace),
        operation="repo_snapshot",
    )

    async def dispatch(event, data, runner_id):
        return await sync_to_async(service.git_operations.handle_git_reply)(
            event,
            data,
            runner_id=runner_id,
        )

    sender = peer(url, channel)
    owner = peer(
        url,
        channel,
        dispatch=dispatch,
        owns=lambda event, data: service.rpc_registry.owns_request(
            "git",
            str(data.get("request_id", "")),
        ),
    )
    try:
        await sender.start()
        await owner.start()
        payload = {
            "request_id": "owned-git",
            "workspace_id": str(workspace),
            "operation": "repo_snapshot",
            "result": {"branch": "main"},
        }
        assert not await sender.route(
            "git:operation_result",
            {**payload, "operation": "push"},
            runner,
        )
        assert not future.done()
        assert not await sender.route(
            "git:operation_result", payload, str(uuid.uuid4())
        )
        assert not future.done()
        assert await sender.route("git:operation_result", payload, runner)
        assert await asyncio.wait_for(future, 1) == payload
        assert not service.rpc_registry.owns_request("git", "owned-git")
    finally:
        await sender.stop()
        await owner.stop()
        if not future.done():
            future.cancel()


async def test_remote_harness_reply_resolves_real_accessor(relay_redis, monkeypatch):
    """An execution accessor stays live while its response arrives via another API."""
    from asgiref.sync import sync_to_async

    from apps.harness.access.runner_accessor import (
        RunnerWorkspaceAccessor,
        owns_runner_reply,
    )
    from apps.runners.services import RunnerService

    url, _ = relay_redis
    channel = uuid.uuid4().hex
    workspace, runner = uuid.uuid4(), str(uuid.uuid4())
    service = RunnerService()
    monkeypatch.setattr(
        service.ownership,
        "validate_harness_workspace_runner",
        lambda claimed_workspace, claimed_runner: (
            claimed_workspace == workspace and claimed_runner == runner
        ),
    )
    emitted = asyncio.Event()
    request = {}

    async def emit(event, payload):
        request.update(payload)
        emitted.set()

    accessor = RunnerWorkspaceAccessor(str(workspace), emit=emit)
    command = asyncio.create_task(accessor.exec_wait("echo hello"))

    async def dispatch(event, data, runner_id):
        return await sync_to_async(service.runner_transport.handle_harness_reply)(
            event,
            data,
            runner_id=runner_id,
        )

    sender = peer(url, channel)
    owner = peer(url, channel, dispatch=dispatch, owns=owns_runner_reply)
    try:
        await sender.start()
        await owner.start()
        await asyncio.wait_for(emitted.wait(), 1)
        payload = {
            "request_id": request["request_id"],
            "workspace_id": str(workspace),
            "exit_code": 0,
            "stdout": "hello\n",
            "stderr": "",
        }
        assert not await sender.route(
            "harness:exec_wait_result",
            payload,
            str(uuid.uuid4()),
        )
        assert not command.done()
        assert await sender.route("harness:exec_wait_result", payload, runner)
        result = await asyncio.wait_for(command, 1)
        assert result.stdout == "hello\n"
        assert result.exit_code == 0
        assert not owns_runner_reply("harness:exec_wait_result", payload)
    finally:
        await sender.stop()
        await owner.stop()
        if not command.done():
            command.cancel()
        await asyncio.gather(command, return_exceptions=True)
