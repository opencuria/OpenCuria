"""Real HTTP acceptance of independent API and production execution lifetimes."""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import sys
import textwrap
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from asgiref.sync import sync_to_async
from cryptography.fernet import Fernet
from django.db import connection

from apps.accounts.services import APIKeyService
from apps.harness.repositories import HarnessSessionRepository
from common.tests.test_runner_reply_relay import relay_redis as relay_redis


async def _wait_until(predicate: Callable[[], Awaitable[bool]]) -> None:
    async def wait() -> None:
        while not await predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait(), 20)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


@pytest.mark.django_db(transaction=True)
async def test_real_http_native_run_survives_api_restart_with_production_jobs(
    harness_workspace, tmp_path, relay_redis
):
    """HTTP auth, ASGI, Redis and real jobs retain a native answer across restart."""
    redis_url, _ = relay_redis
    database = tmp_path / "http-acceptance.sqlite3"
    backend = Path(__file__).resolve().parents[3]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        api_port = reservation.getsockname()[1]
    base_url = f"http://127.0.0.1:{api_port}"

    def seed_and_snapshot():
        user = harness_workspace.created_by
        session = HarnessSessionRepository.create(
            workspace_id=harness_workspace.id,
            organization_id=harness_workspace.runner.organization_id,
            created_by_id=user.id,
            title="HTTP acceptance",
            agent_name="build",
            model="acceptance-model",
        )
        _, token = APIKeyService().create_key(
            user=user,
            name="HTTP acceptance",
            expires_at=None,
            permissions=["harness:read", "harness:run", "workspaces:read"],
        )
        connection.ensure_connection()
        with sqlite3.connect(database) as target:
            connection.connection.backup(target)
        return session, token

    session, token = await sync_to_async(seed_and_snapshot)()
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "DJANGO_ALLOW_ASYNC_UNSAFE"
    }
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.development",
            "DJANGO_ENV": "development",
            "SQLITE_PATH": str(database),
            "REDIS_URL": redis_url,
            "CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "EXECUTION_PROCESS_LOCK_PATH": str(tmp_path / "execution.lock"),
            "TEST_STATE_DIR": str(tmp_path),
            "APNS_TEAM_ID": "",
            "APNS_KEY_ID": "",
            "APNS_PRIVATE_KEY_FILE": "",
            "FCM_SERVICE_ACCOUNT_FILE": "",
        }
    )
    worker_code = textwrap.dedent("""
        import asyncio, os
        from pathlib import Path
        import django
        django.setup()
        from apps.harness.harness_service import (
            HarnessService, configure_execution_service,
        )
        from apps.harness.providers.base import ProviderAdapter, Delta, Usage
        from config.execution import create_execution_worker

        state = Path(os.environ["TEST_STATE_DIR"])
        class HeldProvider(ProviderAdapter):
            name = "acceptance"
            async def chat_stream(self, *args, **kwargs):
                (state / "provider-entered").touch()
                while not (state / "release-provider").exists():
                    await asyncio.sleep(0.01)
                yield Delta(
                    text="Native reply completed after HTTP restart",
                    usage=Usage(1, 1, 2),
                )

        async def main():
            assert "DJANGO_ALLOW_ASYNC_UNSAFE" not in os.environ
            service = HarnessService(provider_factory=lambda _: HeldProvider())
            configure_execution_service(service)
            worker = create_execution_worker(service)
            try:
                await worker.start()
                # These are the production scheduler, notifications and recovery,
                # with actual leases, startup repair and Redis reply subscriptions.
                for job in (worker.scheduler, worker.notifications, worker.recovery):
                    assert job._task is not None and not job._task.done()
                assert worker.scheduler._lease is not None
                (state / "worker-ready").touch()
                while not (state / "stop-worker").exists():
                    await asyncio.sleep(0.01)
            finally:
                await worker.stop()
        asyncio.run(main())
    """)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(session.organization_id),
    }
    timeline_path = f"/api/v1/harness/sessions/{session.id}/timeline"
    message_path = f"/api/v1/harness/sessions/{session.id}/message"
    processes = []
    logs = []
    final_snapshot = None

    async def spawn(name, *command):
        log = (tmp_path / f"{name}.log").open("wb")
        logs.append(log)
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            cwd=backend,
            stdout=log,
            stderr=log,
        )
        processes.append(process)
        return process

    async def start_api(name):
        api = await spawn(
            name,
            sys.executable,
            "-m",
            "uvicorn",
            "config.asgi:application",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
            "--workers",
            "1",
            "--lifespan",
            "on",
            "--log-level",
            "info",
        )
        async with httpx.AsyncClient(base_url=base_url, trust_env=False) as probe:

            async def healthy():
                assert api.returncode is None, f"{name} exited {api.returncode}"
                try:
                    return (await probe.get("/api/v1/health/")).status_code == 200
                except httpx.HTTPError:
                    return False

            await _wait_until(healthy)
        return api

    try:
        worker = await spawn("worker", sys.executable, "-c", worker_code)

        async def worker_ready():
            assert worker.returncode is None, f"worker exited {worker.returncode}"
            return (tmp_path / "worker-ready").exists()

        await _wait_until(worker_ready)
        api = await start_api("api-before")
        async with httpx.AsyncClient(
            base_url=base_url, trust_env=False, timeout=20
        ) as client:
            unauthenticated = await client.get(timeline_path)
            assert unauthenticated.status_code == 401
            response = await client.post(
                message_path,
                headers=headers,
                json={
                    "prompt": "Complete this answer after the API process restarts",
                },
            )
            assert response.status_code == 202, response.text[:1200]
            assert response.json()["status"] == "busy"

            async def provider_busy():
                assert worker.returncode is None
                return (tmp_path / "provider-entered").exists()

            await _wait_until(provider_busy)
            response = await client.get(timeline_path, headers=headers)
            assert response.status_code == 200, response.text[:1200]
            before = response.json()
            assert before["session"]["status"] == "busy"
            assistants = [
                row for row in before["messages"] if row["role"] == "assistant"
            ]
            assert len(assistants) == 1 and assistants[0]["completed_at"] is None
            assistant_id = assistants[0]["id"]

        # This is an actual ASGI lifespan shutdown and fresh Uvicorn process.
        await _stop_process(api)
        assert worker.returncode is None
        restarted = await start_api("api-after")
        assert restarted.pid != api.pid
        async with httpx.AsyncClient(
            base_url=base_url, trust_env=False, timeout=20
        ) as fresh:
            response = await fresh.get(timeline_path, headers=headers)
            assert response.status_code == 200, response.text[:1200]
            assert response.json()["session"]["status"] == "busy"
            assert worker.returncode is None
            (tmp_path / "release-provider").touch()

            async def completed():
                nonlocal final_snapshot
                response = await fresh.get(timeline_path, headers=headers)
                assert response.status_code == 200, response.text[:1200]
                final_snapshot = response.json()
                return final_snapshot["session"]["status"] == "idle" and any(
                    row["id"] == assistant_id and row["completed_at"] is not None
                    for row in final_snapshot["messages"]
                )

            await _wait_until(completed)
        assistant = next(
            row for row in final_snapshot["messages"] if row["id"] == assistant_id
        )
        assert assistant["finish"] == "stop" and assistant["error"] == ""
        assert (
            "".join(
                part["output"] for part in assistant["parts"] if part["type"] == "text"
            )
            == "Native reply completed after HTTP restart"
        )
        assert worker.returncode is None
    except Exception as exc:
        diagnostics = "\n".join(
            f"{path.name}:\n{path.read_text()[-12000:]}"
            for path in sorted(tmp_path.glob("*.log"))
        )
        pytest.fail(
            f"HTTP execution acceptance failed: {exc}\n{diagnostics}", pytrace=False
        )
    finally:
        (tmp_path / "release-provider").touch()
        (tmp_path / "stop-worker").touch()
        for process in reversed(processes):
            await _stop_process(process)
        for log in logs:
            log.close()
