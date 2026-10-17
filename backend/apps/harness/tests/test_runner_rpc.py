"""Redis worker/gateway runner RPC transport and durable ownership fences."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.harness.models import (
    HarnessMessage,
    HarnessRun,
    HarnessRunStatus,
    HarnessSession,
)
from apps.runners.enums import RunnerStatus
from apps.harness.runner_rpc import HarnessRunnerRpcGateway, _authorize_runner_rpc
from apps.harness.runtime_authority import RunAuthority
from apps.harness.worker_protocol import HarnessWorkerRepository


@pytest.mark.django_db(transaction=True)
def test_gateway_runner_rpc_authorizes_exact_run_workspace_and_worker(
    harness_workspace,
):
    """A valid owner passes; token/workspace/root/version collisions fail."""
    worker = HarnessWorkerRepository.register("rpc-worker", "release-rpc", pid=42)
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        harness_id="native",
        status="busy",
    )
    assistant = HarnessMessage.objects.create(
        session=session, role="assistant", harness_id="native", position=0
    )
    run = HarnessRun.objects.create(
        session=session,
        assistant_message=assistant,
        organization_id=session.organization_id,
        harness_id="native",
        owner_worker_id=worker.worker_id,
        deployment_version=worker.deployment_version,
        status=HarnessRunStatus.RUNNING,
    )
    authority = RunAuthority(
        run.id, run.owner_token, worker.worker_id, worker.deployment_version, run.id
    )
    payload = {"workspace_id": str(harness_workspace.id)}
    runner_id = str(harness_workspace.runner_id)
    workspace, denied = _authorize_runner_rpc(
        authority, "harness:exec_wait", payload, runner_id
    )
    assert not denied
    assert workspace.id == harness_workspace.id

    wrong_token = RunAuthority(
        run.id, uuid.uuid4(), worker.worker_id, worker.deployment_version, run.id
    )
    wrong_root = RunAuthority(
        run.id,
        run.owner_token,
        worker.worker_id,
        worker.deployment_version,
        uuid.uuid4(),
    )
    wrong_release = RunAuthority(
        run.id, run.owner_token, worker.worker_id, "other-release", run.id
    )
    for candidate in (wrong_token, wrong_root, wrong_release):
        assert _authorize_runner_rpc(
            candidate, "harness:exec_wait", payload, runner_id
        ) == (None, True)
    assert _authorize_runner_rpc(
        authority, "harness:exec_wait", {"workspace_id": str(uuid.uuid4())}, runner_id
    ) == (None, True)
    assert _authorize_runner_rpc(
        authority, "harness:exec_wait", payload, str(uuid.uuid4())
    ) == (None, True)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_actual_worker_process_roundtrip_through_gateway_redis(
    monkeypatch, harness_workspace
):
    """A separate worker process receives accepted runner replies via Redis."""
    redis_server = shutil_which("redis-server")
    if not redis_server:
        pytest.skip("redis-server is required for cross-process RPC integration")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [
            redis_server,
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    redis_url = f"redis://127.0.0.1:{port}/0"
    try:
        for _ in range(100):
            try:
                import redis

                client = redis.Redis.from_url(redis_url)
                if client.ping():
                    break
            except Exception:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("temporary Redis did not start")
        client.close()

        authority = RunAuthority(
            uuid.uuid4(), uuid.uuid4(), "rpc-child", "release-rpc", uuid.uuid4()
        )
        worker = HarnessWorkerRepository.register(
            authority.worker_id, authority.deployment_version, pid=42
        )
        session = HarnessSession.objects.create(
            workspace=harness_workspace,
            organization_id=harness_workspace.runner.organization_id,
            harness_id="native",
            status="busy",
        )
        assistant = HarnessMessage.objects.create(
            session=session, role="assistant", harness_id="native", position=0
        )
        run = HarnessRun.objects.create(
            id=authority.run_id,
            owner_token=authority.owner_token,
            session=session,
            assistant_message=assistant,
            organization_id=session.organization_id,
            harness_id="native",
            owner_worker_id=authority.worker_id,
            deployment_version=authority.deployment_version,
            status=HarnessRunStatus.RUNNING,
        )
        authority = RunAuthority(
            run.id,
            run.owner_token,
            worker.worker_id,
            worker.deployment_version,
            run.id,
        )
        workspace = harness_workspace
        workspace.runner.status = RunnerStatus.ONLINE
        workspace.runner.sid = "runner-sid"
        workspace.runner.save(update_fields=["status", "sid"])
        runner_id = str(workspace.runner_id)

        monkeypatch.setenv("OPENCURIA_HARNESS_MODE", "gateway")
        monkeypatch.setenv("OPENCURIA_DEPLOYMENT_VERSION", "release-rpc")
        monkeypatch.setenv("OPENCURIA_GATEWAY_WORKER_VERSIONS", "release-rpc")
        monkeypatch.setattr(
            "django.conf.settings.DEPLOYMENT_RELEASE",
            {"version": "release-rpc"},
            raising=False,
        )
        broker = HarnessRunnerRpcGateway(redis_url)

        class FakeSocketServer:
            async def emit(self, event, data, *, to):
                assert event == "harness:exec_wait"
                assert to == "runner-sid"
                reply = {
                    "workspace_id": data["workspace_id"],
                    "request_id": data["request_id"],
                    "exit_code": 0,
                    "stdout": "worker-roundtrip",
                    "stderr": "",
                }
                asyncio.create_task(
                    broker.route_reply(
                        "harness:exec_wait_result", reply, runner_id=runner_id
                    )
                )

        await broker.start(FakeSocketServer())
        from apps.runners.sio_server import get_runner_service

        service = get_runner_service()
        monkeypatch.setattr(service, "_supervised_owner", True, raising=False)

        async def emit_runner_event(runner, event, payload):
            await broker._sio.emit(event, payload, to=runner.sid)

        monkeypatch.setattr(service, "_emit_to_runner", emit_runner_event)
        broker.configure_runner_service(service)
        script = r"""import asyncio, os, uuid
from apps.harness.runtime_authority import RunAuthority, use_run_authority
from apps.harness.runner_rpc import HarnessRunnerRpcClient
async def main():
    authority = RunAuthority(
        uuid.UUID(os.environ["RUN_ID"]),
        uuid.UUID(os.environ["OWNER_TOKEN"]),
        os.environ["WORKER_ID"],
        os.environ["VERSION"],
        uuid.UUID(os.environ["ROOT_ID"]),
    )
    received = asyncio.Event()
    replies = []
    async def handler(event, payload, runner_id):
        replies.append((event, payload, runner_id))
        received.set()
        return True
    client = HarnessRunnerRpcClient(
        authority.worker_id, os.environ["REDIS_URL"], reply_handler=handler
    )
    await client.start()
    with use_run_authority(authority):
        await client.emit(
            os.environ["RUNNER_ID"],
            "harness:exec_wait",
            {
                "workspace_id": os.environ["WORKSPACE_ID"],
                "request_id": "request-collide-safe",
                "command": "echo test",
            },
            timeout=8,
        )
    await asyncio.wait_for(received.wait(), 8)
    assert replies[0][1]["stdout"] == "worker-roundtrip"
    print("accepted-worker-reply")
    await client.close()
asyncio.run(main())"""
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                "REDIS_URL": redis_url,
                "RUN_ID": str(authority.run_id),
                "OWNER_TOKEN": str(authority.owner_token),
                "ROOT_ID": str(authority.root_run_id),
                "WORKER_ID": authority.worker_id,
                "VERSION": authority.deployment_version,
                "RUNNER_ID": runner_id,
                "WORKSPACE_ID": str(workspace.id),
            }
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            cwd=str(Path(__file__).resolve().parents[3]),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
        assert child.returncode == 0, stderr.decode(errors="replace")
        assert b"accepted-worker-reply" in stdout
        await broker.close()
    finally:
        from apps.runners import sio_server

        sio_server._runner_service = None
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def shutil_which(command: str) -> str | None:
    """Resolve a test executable without adding a test dependency."""
    from shutil import which

    return which(command)
