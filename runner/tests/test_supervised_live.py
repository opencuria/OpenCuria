"""Opt-in, real Docker/PTY/two-generation regression (isolated daemon only).

Run with OPENCURIA_RUNNER_LIVE_DOCKER_URL=unix:///tmp/test-docker.sock pytest -q
 tests/test_supervised_live.py. Never use a production daemon for this test.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import docker
import pytest
import socketio
from aiohttp import web

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENCURIA_RUNNER_LIVE_DOCKER_URL"),
    reason="Requires an explicitly selected isolated Docker daemon",
)


async def until(predicate, timeout=30):
    """Wait on a real process/transport observation, not an arbitrary sleep."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Live runner observation timed out")


async def test_actual_pty_retains_pid_and_input_once_across_code_generation(tmp_path):
    """Launch actual runner children and use Docker exec PTY through Socket.IO."""
    runtime_url = os.environ["OPENCURIA_RUNNER_LIVE_DOCKER_URL"]
    client = docker.DockerClient(base_url=runtime_url)
    workspace_id, terminal_id = str(uuid.uuid4()), str(uuid.uuid4())
    client.images.pull("python:3.12-slim")
    container = client.containers.run(
        "python:3.12-slim",
        ["bash", "-c", "mkdir -p /workspace; exec sleep infinity"],
        detach=True,
        network_mode="none",
        labels={"opencuria.workspace-id": workspace_id},
    )
    runner_root = Path(__file__).resolve().parents[1]
    state = tmp_path / "state"
    server = socketio.AsyncServer(async_mode="aiohttp")
    app = web.Application()
    server.attach(app, socketio_path="/ws/runner")
    connected = []
    output = []
    started = []
    receipts = []

    @server.event
    async def connect(sid, environ, auth):
        if auth != {"token": "isolated-live-test"}:
            raise ConnectionRefusedError()
        connected.append(sid)

    @server.on("terminal:started")
    async def terminal_started(sid, data):
        started.append(data)
        return {"ok": True}

    @server.on("terminal:output")
    async def terminal_output(sid, data):
        output.append(data)
        return {"ok": True}

    @server.on("terminal:closed")
    async def terminal_closed(sid, data):
        return {"ok": True}

    @server.on("terminal:input_ack")
    async def terminal_ack(sid, data):
        receipts.append(data)
        return {"ok": True}

    @server.on("runner:authorize_request")
    async def authorize(sid, data):
        return {"ok": data.get("workspace_id") == workspace_id}

    @server.on("runner:run_states")
    async def run_states(sid, data):
        return {"terminal_run_ids": []}

    listener = web.AppRunner(app)
    await listener.setup()
    site = web.TCPSite(listener, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("RUNNER_")
    }
    env.update(
        {
            "RUNNER_STATE_DIR": str(state),
            "RUNNER_SUPERVISED_MODE": "true",
            "RUNNER_API_TOKEN": "isolated-live-test",
            "RUNNER_BACKEND_URL": f"http://127.0.0.1:{port}",
            "RUNNER_DOCKER_SOCKET": runtime_url,
            "RUNNER_ENABLED_RUNTIMES": "docker",
            "RUNNER_HEARTBEAT_INTERVAL": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    log = (tmp_path / "runner.log").open("wb")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src",
        "serve",
        cwd=runner_root,
        env=env,
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await until(lambda: connected or process.returncode is not None)
        assert process.returncode is None, (tmp_path / "runner.log").read_text()
        sid = connected[-1]
        await server.emit(
            "task:start_terminal",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "task_id": str(uuid.uuid4()),
                "cols": 100,
                "rows": 30,
            },
            to=sid,
        )
        await until(lambda: started)
        input_id = str(uuid.uuid4())
        request = {
            "workspace_id": workspace_id,
            "terminal_id": terminal_id,
            "input_id": input_id,
            "data": base64.b64encode(
                b"export LIVE_MARKER=retained; echo BEFORE_PID=$$\n"
            ).decode(),
        }
        await server.call("terminal:input", request, to=sid, timeout=10)

        def text():
            return b"".join(base64.b64decode(row["data"]) for row in output).decode(
                errors="replace"
            )

        await until(
            lambda: (
                "BEFORE_PID=" in text()
                and any(row.get("input_id") == input_id for row in receipts)
            )
        )
        import re

        await until(lambda: re.search(r"BEFORE_PID=(\d+)", text()))
        old_pid = re.search(r"BEFORE_PID=(\d+)", text()).group(1)
        old_status = json.loads((state / "supervisor-status.json").read_text())
        old_generation = old_status["active_generation"]
        # Distinct code snapshot, not a second full runner/socket/journal owner.
        source = tmp_path / "candidate"
        shutil.copytree(
            runner_root / "src",
            source / "src",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (source / "src" / "release_marker.py").write_text("VERSION = 'live-v2'\n")
        from src.supervisor import stage_generation

        stage_generation(source, str(state), "live-v2")
        await until(
            lambda: (
                json.loads((state / "supervisor-status.json").read_text())[
                    "active_generation"
                ]
                == "live-v2"
            )
        )
        status = json.loads((state / "supervisor-status.json").read_text())
        old = next(
            row for row in status["generations"] if row["generation"] == old_generation
        )
        assert old["state"] == "draining" and old["owned_sessions"] >= 1
        assert connected == [sid]  # no runner identity/socket replacement
        cursor = max(row["sequence"] for row in output)
        response = await server.call(
            "terminal:attach",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "after_seq": cursor,
            },
            to=sid,
            timeout=10,
        )
        assert response["ok"]
        after = {
            **request,
            "input_id": str(uuid.uuid4()),
            "data": base64.b64encode(
                b"echo AFTER_PID=$$ MARKER=$LIVE_MARKER; echo effect >> /workspace/live-effects\n"
            ).decode(),
        }
        await server.call("terminal:input", after, to=sid, timeout=10)
        await server.call("terminal:input", after, to=sid, timeout=10)
        await until(lambda: f"AFTER_PID={old_pid} MARKER=retained" in text())
        await until(lambda: any(row.get("duplicate") for row in receipts))
        result = await asyncio.to_thread(
            container.exec_run, ["bash", "-c", "wc -l < /workspace/live-effects"]
        )
        assert result.output.strip() == b"1"
        await server.call(
            "terminal:close",
            {"workspace_id": workspace_id, "terminal_id": terminal_id},
            to=sid,
            timeout=10,
        )
    finally:
        # Test fixture teardown only; never a deployment implementation.
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        if (state / "supervisor-status.json").exists():
            for generation in json.loads(
                (state / "supervisor-status.json").read_text()
            ).get("generations", []):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(generation["pid"], 15)
        await listener.cleanup()
        await asyncio.to_thread(container.remove, force=True)
        client.close()
        log.close()
