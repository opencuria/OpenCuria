"""Real independent processes sharing only durable command/conversation storage."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest
from asgiref.sync import sync_to_async
from django.db import connection

from apps.harness.tests.test_harness_service import _session


@pytest.mark.django_db(transaction=True)
async def test_native_execution_survives_request_process_exit(
    harness_workspace, tmp_path
):
    """A request exits, the provider continues in another process, then settles."""
    session = await sync_to_async(_session)(harness_workspace)
    database = tmp_path / "process-test.sqlite3"

    def snapshot_database():
        connection.ensure_connection()
        with sqlite3.connect(database) as target:
            connection.connection.backup(target)

    await sync_to_async(snapshot_database)()
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.development",
        "SQLITE_PATH": str(database),
        "TEST_SESSION_ID": str(session.id),
        "TEST_STATE_DIR": str(tmp_path),
    }
    backend = Path(__file__).resolve().parents[3]
    worker_code = textwrap.dedent("""
        import asyncio, os, uuid
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import django
        django.setup()
        from apps.harness.harness_service import HarnessService
        from apps.harness.execution_worker import ExecutionWorker
        from apps.harness.tests.test_harness_service import FakeProvider
        from common.process_ownership import ExecutionProcessGuard
        state = Path(os.environ["TEST_STATE_DIR"])
        class Provider(FakeProvider):
            async def chat_stream(self, *args, **kwargs):
                (state / "provider-entered").touch()
                while not (state / "release").exists():
                    await asyncio.sleep(0.01)
                async for delta in super().chat_stream(*args, **kwargs):
                    yield delta
        async def main():
            service = HarnessService(provider_factory=lambda _: Provider([]), emit=AsyncMock())
            async def scheduler_start(*, before_dispatch):
                await before_dispatch()
            worker = ExecutionWorker(service,
                guard=ExecutionProcessGuard(state / "worker.lock"),
                scheduler=SimpleNamespace(start=AsyncMock(side_effect=scheduler_start), stop=AsyncMock()),
                recovery=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
                notifications=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
                start_transport=AsyncMock(), stop_transport=AsyncMock(), interval=0.005)
            await worker.start()
            (state / "ready").touch()
            try:
                while not (state / "stop").exists():
                    await asyncio.sleep(0.01)
            finally:
                await worker.stop()
        asyncio.run(main())
    """)
    api_code = textwrap.dedent("""
        import asyncio, os, uuid
        from pathlib import Path
        import django
        django.setup()
        from apps.harness.harness_service import HarnessService
        from apps.harness.command_service import RemoteHarnessService
        from apps.harness.repositories import HarnessSessionRepository
        session = HarnessSessionRepository.get_by_id(uuid.UUID(os.environ["TEST_SESSION_ID"]))
        async def main():
            service = RemoteHarnessService(HarnessService())
            message = await service.start_run(session, "continue after my API exits", organization_id=session.organization_id)
            (Path(os.environ["TEST_STATE_DIR"]) / "assistant-id").write_text(str(message.id))
        asyncio.run(main())
    """)

    async def wait_for(predicate):
        async def wait():
            while not await asyncio.to_thread(predicate):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 10)

    def persisted():
        with sqlite3.connect(database) as db:
            return db.execute(
                "SELECT status FROM harness_session WHERE id=?", (session.id.hex,)
            ).fetchone()[0]

    log = (tmp_path / "worker.log").open("wb")
    worker = await asyncio.create_subprocess_exec(
        sys.executable, "-c", worker_code, env=env, cwd=backend, stdout=log, stderr=log
    )
    try:
        await wait_for((tmp_path / "ready").exists)
        request = await asyncio.create_subprocess_exec(
            sys.executable, "-c", api_code, env=env, cwd=backend, stdout=log, stderr=log
        )
        assert await asyncio.wait_for(request.wait(), 10) == 0, (
            tmp_path / "worker.log"
        ).read_text()
        await wait_for((tmp_path / "provider-entered").exists)
        assert worker.returncode is None
        assert await asyncio.to_thread(persisted) == "busy"
        # No request process remains. The independent owner completes the turn.
        (tmp_path / "release").touch()
        await wait_for(lambda: persisted() == "idle")
        assistant_id = (tmp_path / "assistant-id").read_text().replace("-", "")
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT finish,completed_at FROM harness_message WHERE id=?",
                (assistant_id,),
            ).fetchone()
        assert row[0] == "stop" and row[1] is not None
    finally:
        (tmp_path / "release").touch()
        (tmp_path / "stop").touch()
        try:
            await asyncio.wait_for(worker.wait(), 10)
        except TimeoutError:
            worker.kill()
            await worker.wait()
        log.close()
