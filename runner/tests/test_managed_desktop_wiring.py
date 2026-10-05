"""Production ownership composition and websocket lifecycle regression tests."""

import asyncio
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.models import DesktopSession, WorkspaceInfo
from src.service import WorkspaceService


class ManagedDesktopWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = RunnerSettings(state_dir=self.directory.name)
        self.service = WorkspaceService({}, self.settings)
        self.assertTrue(self.service.desktop._legacy_mode)
        self.interface = WebSocketInterface(self.service, self.settings)
        self.interface._sio.emit = AsyncMock()
        self.ws = uuid.uuid4()
        self.service._cache[self.ws] = WorkspaceInfo(
            workspace_id=self.ws,
            instance_id="guest",
            status="running",
            runtime_type="docker",
        )
        self.desktop = self.service.desktop
        self.desktop._ensure_desktop_process_locked = AsyncMock(
            side_effect=lambda *a, **kw: self.desktop._desktop_sessions.setdefault(
                self.ws, DesktopSession(self.ws, "guest")
            )
        )
        self.desktop._verify_binding = AsyncMock()
        self.desktop._stop_desktop_process = AsyncMock(
            side_effect=lambda *a, **kw: self.desktop._desktop_sessions.pop(
                self.ws, None
            )
        )
        self.desktop.get_desktop_container_ip = lambda _: "127.0.0.1"
        self.desktop.get_desktop_network_name = lambda _: "network"
        self.handlers = self.interface._sio.handlers["/"]

    def intent(self, lease="one", **extra):
        return {
            "lease_id": lease,
            "kind": "viewer",
            "owner_id": "owner",
            "revision": 1,
            "epoch": self.interface._inventory_epoch,
            **extra,
        }

    async def test_production_composition_and_reserve_no_start(self):
        self.assertFalse(self.desktop._legacy_mode)
        self.assertIs(self.service._registry.desktop_manager, self.desktop)
        self.assertIs(self.service.streams.lease_store, self.desktop.lease_store)
        self.assertEqual(self.desktop.epoch, self.interface._inventory_epoch)
        self.assertEqual(
            self.service.streams.intent_store.path.parent,
            self.desktop.lease_store.path.parent,
        )
        await self.desktop.desktop_action(self.ws, "reserve", self.intent())
        await self.desktop.desktop_action(self.ws, "renew", self.intent())
        self.desktop._ensure_desktop_process_locked.assert_not_awaited()

    async def test_distinct_viewer_tasks_release_only_their_identity(self):
        for lease in ("one", "two"):
            await self.handlers["task:start_desktop"](
                {
                    "task_id": lease,
                    "workspace_id": str(self.ws),
                    **self.intent(lease),
                }
            )
        await self.handlers["task:stop_desktop"](
            {
                "task_id": "stop",
                "workspace_id": str(self.ws),
                **self.intent(),
            }
        )
        self.desktop._stop_desktop_process.assert_not_awaited()
        self.assertEqual((await self.desktop.lease_store.get("two"))["state"], "held")
        self.assertNotIn(
            "desktop:stopped",
            [call.args[0] for call in self.interface._sio.emit.await_args_list],
        )

    async def test_stream_rpc_owner_is_forwarded_without_rewriting(self):
        self.service.streams.stream_start_process = AsyncMock()
        self.service.streams.stream_read = AsyncMock()
        owner = {"lease_id": "one", "epoch": self.desktop.epoch}
        response = await self.handlers["workspace:stream_start"](
            {
                "workspace_id": str(self.ws),
                "connection_id": "stream",
                "kind": "process",
                "command": ["true"],
                "owner": owner,
            }
        )
        self.assertTrue(response["ok"])
        self.assertEqual(
            self.service.streams.stream_start_process.await_args.kwargs["owner"],
            owner,
        )
        for task in self.interface._running_tasks.values():
            task.cancel()
        await asyncio.gather(
            *self.interface._running_tasks.values(), return_exceptions=True
        )

    async def test_persisted_close_authorization_and_false_ack(self):
        await self.service.streams.intent_store.reserve(
            {
                "connection_id": "persisted",
                "workspace_id": str(self.ws),
                "instance_id": "guest",
                "runtime_type": "docker",
                "lease_id": "one",
                "epoch": self.desktop.epoch,
                "control_path": "/control",
                "state": "closing",
            }
        )
        self.service.streams.stream_close = AsyncMock(return_value={"closed": False})
        response = await self.handlers["workspace:stream_close"](
            {
                "workspace_id": str(uuid.uuid4()),
                "connection_id": "persisted",
            }
        )
        self.assertFalse(response["ok"])
        self.service.streams.stream_close.assert_not_awaited()
        response = await self.handlers["workspace:stream_close"](
            {
                "workspace_id": str(self.ws),
                "connection_id": "persisted",
            }
        )
        self.assertFalse(response["closed"])
        self.assertIsNotNone(await self.service.streams.stream_record("persisted"))

    async def test_offline_expiry_and_connected_lifecycle_notification(self):
        await self.desktop.desktop_action(self.ws, "hold", self.intent())
        await self.desktop.lease_store.mark_closing("one")
        self.interface._sio.connected = False
        await self.interface._desktop_lease_tick()
        self.assertEqual(
            (await self.desktop.lease_store.get("one"))["state"], "expired"
        )
        await self.desktop.desktop_action(self.ws, "hold", self.intent("two"))
        await self.desktop.lease_store.mark_closing("two")
        self.interface._sio.connected = True
        await self.interface._desktop_lease_tick()
        self.interface._sio.emit.assert_any_await(
            "desktop:stopped", {"workspace_id": str(self.ws), "stopped": True}
        )

    async def test_harness_reserve_does_not_announce_lifecycle(self):
        await self.handlers["harness:desktop_action"](
            {
                "workspace_id": str(self.ws),
                "request_id": "reserve",
                "action": "reserve",
                "args": self.intent(),
            }
        )
        await asyncio.gather(*self.interface._running_tasks.values())
        self.assertNotIn(
            "desktop:process",
            [call.args[0] for call in self.interface._sio.emit.await_args_list],
        )
        self.desktop._ensure_desktop_process_locked.assert_not_awaited()

    async def test_facade_preserves_managed_owner(self):
        self.service.streams.stream_start_process = AsyncMock()
        owner = {"lease_id": "one", "epoch": self.desktop.epoch}
        await self.service.stream_start_process(self.ws, "stdio", ["true"], owner=owner)
        self.assertEqual(
            self.service.streams.stream_start_process.await_args.kwargs["owner"],
            owner,
        )
        await self.service.stream_start_process(self.ws, "legacy", ["true"])
        self.assertNotIn(
            "owner", self.service.streams.stream_start_process.await_args.kwargs
        )

    async def test_hung_recovery_is_cancelled_without_accumulating_tasks(self):
        await self.desktop.desktop_action(self.ws, "reserve", self.intent())
        self.settings.desktop_lease_timeout = 0.01
        cancelled = asyncio.Event()

        async def hang(_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.desktop.maintain_workspace = hang
        await asyncio.wait_for(self.interface._desktop_lease_tick(), 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(
            (await self.desktop.lease_store.get("one"))["state"], "reserved"
        )

    async def test_stop_cancels_and_joins_lease_loop(self):
        self.interface._desktop_lease_task = asyncio.create_task(
            self.interface._desktop_lease_loop()
        )
        task = self.interface._desktop_lease_task
        await self.interface.stop()
        self.assertTrue(task.done())
        self.assertIsNone(self.interface._desktop_lease_task)

    async def test_hung_first_workspace_does_not_delay_next(self):
        next_ws = uuid.uuid4()
        entered = asyncio.Event()
        completed = asyncio.Event()
        cancelled = asyncio.Event()
        self.settings.desktop_lease_timeout = 5

        async def maintain(workspace_id):
            if workspace_id == self.ws:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            else:
                completed.set()

        workers = asyncio.create_task(
            self.service.run_desktop_workers([self.ws, next_ws], maintain)
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(completed.wait(), 1)
            self.assertFalse(workers.done())
        finally:
            workers.cancel()
            await asyncio.gather(workers, return_exceptions=True)
        self.assertTrue(cancelled.is_set())

    async def test_stop_joins_active_workspace_cleanup(self):
        await self.desktop.desktop_action(self.ws, "reserve", self.intent())
        entered = asyncio.Event()
        joined = asyncio.Event()

        async def maintain(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                joined.set()

        self.desktop.maintain_workspace = maintain
        self.interface._desktop_lease_task = asyncio.create_task(
            self.interface._desktop_lease_loop()
        )
        await asyncio.wait_for(entered.wait(), 1)
        await self.interface.stop()
        self.assertTrue(joined.is_set())
        self.assertIsNone(self.interface._desktop_lease_task)

    async def test_shutdown_close_timeout_keeps_protective_intent(self):
        await self.desktop.desktop_action(self.ws, "reserve", self.intent())
        await self.desktop.lease_store.mark_closing("one")
        self.settings.stream_shutdown_timeout = 0.01
        cancelled = asyncio.Event()

        async def close(**_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.service.streams.close_all_streams = close
        await asyncio.wait_for(self.interface.stop(), 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(
            (await self.desktop.lease_store.get("one"))["state"], "closing"
        )
        self.interface._sio.emit.assert_not_awaited()

    async def test_tick_schedules_next_guest_while_first_is_unresponsive(self):
        other = uuid.uuid4()
        self.desktop.maintenance_workspace_ids = AsyncMock(
            return_value=[self.ws, other]
        )
        entered = asyncio.Event()
        completed = asyncio.Event()
        joined = asyncio.Event()

        async def maintain(workspace_id):
            if workspace_id == self.ws:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    joined.set()
            else:
                completed.set()

        self.desktop.maintain_workspace = maintain
        tick = asyncio.create_task(self.interface._desktop_lease_tick())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(completed.wait(), 1)
            self.assertFalse(tick.done())
        finally:
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
        self.assertTrue(joined.is_set())

    async def test_worker_count_is_bounded(self):
        self.settings.desktop_lease_workers = 2
        active = 0
        peak = 0

        async def maintain(_):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            try:
                await asyncio.sleep(0.01)
            finally:
                active -= 1

        await self.service.run_desktop_workers(
            [uuid.uuid4() for _ in range(12)], maintain
        )
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
