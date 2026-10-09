"""Durable ownership fencing and manager cleanup ordering."""

import asyncio
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.models import DesktopSession
from src.services.sessions.desktop import DesktopManager
from src.services.sessions.desktop_leases import DesktopLeaseStore


class DesktopLeaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = DesktopLeaseStore(self.directory.name)
        self.ws = uuid.uuid4()

    async def reserve(self, **kwargs):
        values = {
            "workspace_id": str(self.ws),
            "instance_id": "guest",
            "lease_id": "one",
            "kind": "mcp",
            "owner_id": "owner",
            "epoch": "epoch",
            "revision": 1,
            "ttl": 180,
        }
        values.update(kwargs)
        return await self.store.reserve(**values)

    async def test_reserve_durable_unactivated_and_tombstone(self):
        row = await self.reserve()
        self.assertFalse(row["activated"])
        reopened = DesktopLeaseStore(self.directory.name)
        self.assertEqual(await reopened.get("one"), row)
        await reopened.mark_closing("one")
        await reopened.finish("one")
        with self.assertRaises(ValueError):
            await self.reserve()
        with self.assertRaises(ValueError):
            await self.store.renew("one", "epoch", 1, 180)

    async def test_agent_lease_can_reserve_renew_release_without_desktop_stop(self):
        manager = self.manager()
        intent = {
            "lease_id": "agent-owner",
            "kind": "agent",
            "owner_id": "agent-run-1",
            "epoch": "epoch",
            "revision": 1,
        }
        reserved = await manager.desktop_action(self.ws, "reserve", intent)
        self.assertEqual(reserved["lease_state"], "reserved")
        renewed = await manager.desktop_action(self.ws, "renew", intent)
        self.assertEqual(renewed["lease_state"], "reserved")
        with self.assertRaisesRegex(ValueError, "process ownership only"):
            await manager.desktop_action(self.ws, "hold", intent)
        with self.assertRaisesRegex(ValueError, "cannot be activated"):
            await self.store.activate("agent-owner", "epoch", 1)

        result = await manager.desktop_action(self.ws, "release", intent)
        self.assertTrue(result["ok"])
        self.assertEqual(result["lease_state"], "released")
        self.assertFalse(result["stopped"])
        manager._stop_desktop_process.assert_not_awaited()
        manager._ensure_desktop_process_locked.assert_not_awaited()

    async def test_agent_release_does_not_require_live_workspace_lookup(self):
        manager = self.manager()
        intent = {
            "lease_id": "agent-gone",
            "kind": "agent",
            "owner_id": "agent-run",
            "epoch": "epoch",
            "revision": 1,
        }
        await manager.desktop_action(self.ws, "reserve", intent)
        manager._get_cached = lambda _: self.fail("Agent release looked up workspace")
        result = await manager.desktop_action(self.ws, "release", intent)
        self.assertTrue(result["ok"])
        self.assertEqual(result["lease_state"], "released")
        manager._stop_desktop_process.assert_not_awaited()

    async def test_mcp_release_preserves_last_desktop_stop(self):
        manager = self.manager()
        intent = {**self.intent(), "lease_id": "mcp-owner"}
        await manager.desktop_action(self.ws, "hold", intent)
        result = await manager.desktop_action(self.ws, "release", intent)
        self.assertTrue(result["ok"])
        manager._stop_desktop_process.assert_awaited_once()

    async def test_epoch_and_identity_fences(self):
        await self.reserve()
        for kwargs in (
            {"epoch": "other"},
            {"owner_id": "other"},
            {"revision": 2},
            {"workspace_id": "other"},
        ):
            with self.assertRaises(ValueError):
                await self.reserve(**kwargs)

    async def test_viewer_revision_cannot_replace_closing(self):
        await self.reserve(kind="viewer")
        await self.store.activate("one", "epoch", 1)
        await self.store.mark_closing("one")
        with self.assertRaises(ValueError):
            await self.reserve(kind="viewer", revision=2)
        await self.store.finish("one")
        self.assertEqual((await self.reserve(kind="viewer", revision=2))["revision"], 2)

    async def test_concurrent_reserves_single_identity(self):
        results = await asyncio.gather(*[self.reserve() for _ in range(20)])
        self.assertTrue(all(row == results[0] for row in results))
        self.assertEqual(len(await self.store.list_workspace(str(self.ws))), 1)

    def manager(self, close=None):
        manager = DesktopManager(
            get_cached=lambda _: SimpleNamespace(instance_id="guest"),
            lease_store=self.store,
            epoch="epoch",
            close_owner_streams=close or AsyncMock(return_value=True),
        )
        manager._ensure_desktop_process_locked = AsyncMock(
            side_effect=lambda *a, **kw: manager._desktop_sessions.setdefault(
                self.ws, DesktopSession(self.ws, "guest")
            )
        )
        manager._verify_binding = AsyncMock()
        manager._stop_desktop_process = AsyncMock(
            side_effect=lambda *a, **kw: manager._desktop_sessions.pop(self.ws, None)
        )
        return manager

    def intent(self):
        return {
            "lease_id": "one",
            "kind": "mcp",
            "owner_id": "owner",
            "epoch": "epoch",
            "revision": 1,
        }

    async def test_binding_and_reserve_never_start_guest(self):
        manager = self.manager()
        self.assertEqual(
            (await manager.desktop_action(self.ws, "binding"))["epoch"], "epoch"
        )
        await manager.desktop_action(self.ws, "reserve", self.intent())
        manager._ensure_desktop_process_locked.assert_not_awaited()

    async def test_failed_close_keeps_protective_membership_then_retry(self):
        close = AsyncMock(side_effect=[False, True])
        manager = self.manager(close)
        await manager.desktop_action(self.ws, "hold", self.intent())
        result = await manager.desktop_action(self.ws, "release", self.intent())
        self.assertEqual(result["lease_state"], "closing")
        self.assertTrue((await self.store.get("one"))["activated"])
        self.assertEqual(result["holder_count"], 1)
        manager._stop_desktop_process.assert_not_awaited()
        await manager.reap_expired()
        self.assertEqual((await self.store.get("one"))["state"], "expired")
        self.assertFalse((await self.store.get("one"))["activated"])

    async def test_close_hook_outside_lock_and_reserve_release_race(self):
        manager = self.manager()

        async def close(lease_id):
            self.assertFalse((await manager._desktop_lock(self.ws)).locked())
            return True

        manager.close_owner_streams = close
        result = await manager.desktop_action(self.ws, "release", self.intent())
        self.assertEqual(result["lease_state"], "released")
        with self.assertRaises(ValueError):
            await manager.desktop_action(self.ws, "hold", self.intent())
        manager._ensure_desktop_process_locked.assert_not_awaited()

    async def test_recovery_old_epoch_never_resumes(self):
        await self.reserve(epoch="old")
        manager = self.manager()
        await manager.recover_workspace(self.ws)
        self.assertEqual((await self.store.get("one"))["state"], "expired")
        manager._ensure_desktop_process_locked.assert_not_awaited()

    async def test_readiness_failure_retains_cleanup_owner(self):
        manager = self.manager()
        manager._verify_binding.side_effect = RuntimeError("bad X11")
        with self.assertRaises(RuntimeError):
            await manager.desktop_action(self.ws, "hold", self.intent())
        self.assertTrue((await self.store.get("one"))["activated"])
        await manager.desktop_action(self.ws, "release", self.intent())
        self.assertEqual((await self.store.get("one"))["state"], "released")

    async def recording_manager(self):
        async def wait_forever(_):
            await asyncio.sleep(3600)

        runtime = SimpleNamespace(
            probe_managed_token=AsyncMock(
                return_value={"boot_id": "boot-1", "init_starttime": "123"}
            ),
            spawn_process=AsyncMock(return_value=object()),
            close_managed_process=AsyncMock(return_value=True),
            process_close=AsyncMock(return_value=True),
            process_detach=AsyncMock(),
            process_wait=AsyncMock(side_effect=wait_forever),
        )
        manager = self.manager()
        manager._get_runtime = lambda _: runtime
        manager._is_desktop_session_live = AsyncMock(return_value=True)
        manager._get_desktop_geometry = AsyncMock(return_value=(1920, 1080))
        intent = {**self.intent(), "kind": "computeruse", "owner_id": "run-1"}
        await manager.desktop_action(self.ws, "hold", intent)
        return manager, runtime, intent

    async def test_recording_crash_restart_closes_durable_group_without_handle(self):
        manager, runtime, _intent = await self.recording_manager()
        await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        row = (await self.store.recordings("one"))[0]
        runtime.spawn_process.assert_awaited_once()
        self.assertEqual(
            runtime.spawn_process.call_args.kwargs["control_path"], row["control_path"]
        )
        restarted = self.manager()
        restarted.epoch = "restart"
        restarted._get_runtime = lambda _: runtime
        restarted._is_desktop_session_live = AsyncMock(return_value=True)
        self.assertFalse(restarted._recording_handles)
        await restarted.recover_workspace(self.ws)
        runtime.close_managed_process.assert_awaited_with("guest", row["control_path"])
        self.assertEqual((await self.store.recordings("one"))[0]["state"], "closed")
        self.assertEqual((await self.store.get("one"))["state"], "expired")
        # Discard original transport just as a process crash would.
        for waiter in manager._recording_waiters.values():
            waiter.cancel()
        await asyncio.gather(
            *manager._recording_waiters.values(), return_exceptions=True
        )

    async def test_record_ack_loss_cleanup_failure_retains_owner(self):
        manager, runtime, intent = await self.recording_manager()
        runtime.spawn_process.side_effect = RuntimeError("ACK lost after spawn")
        with self.assertRaises(RuntimeError):
            await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        runtime.close_managed_process.return_value = False
        result = await manager.desktop_action(self.ws, "release", intent)
        self.assertFalse(result["ok"])
        self.assertEqual(result["holder_count"], 1)
        self.assertEqual((await self.store.get("one"))["state"], "closing")
        manager._stop_desktop_process.assert_not_awaited()
        runtime.close_managed_process.return_value = True
        await manager.reap_expired()
        self.assertEqual((await self.store.recordings("one"))[0]["state"], "closed")
        self.assertEqual((await self.store.get("one"))["state"], "expired")

    async def test_hold_ack_loss_expiry_closes_record_attempt(self):
        manager, runtime, _intent = await self.recording_manager()
        # Client never sees hold/record acknowledgements, but guest effects exist.
        await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        await asyncio.to_thread(
            self.store._run,
            lambda db: db.execute(
                "UPDATE desktop_leases SET expires_at=0 WHERE lease_id='one'"
            ),
        )
        await manager.reap_expired()
        runtime.close_managed_process.assert_awaited_once()
        self.assertEqual((await self.store.get("one"))["state"], "expired")

    async def test_record_start_idempotent_and_stop_fences_delayed_launch(self):
        manager, runtime, intent = await self.recording_manager()
        first = await manager.desktop_action(
            self.ws, "record_start", {"run_id": "run-1"}
        )
        second = await manager.desktop_action(
            self.ws, "record_start", {"run_id": "run-1"}
        )
        self.assertEqual(first, second)
        runtime.spawn_process.assert_awaited_once()
        await manager.desktop_action(self.ws, "record_stop", {"run_id": "run-1"})
        with self.assertRaises(ValueError):
            await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        await manager.desktop_action(self.ws, "release", intent)
        await self.store.clear_workspace(str(self.ws))
        self.assertEqual(await self.store.recordings(), [])

    async def test_clear_workspace_refuses_live_owners(self):
        await self.reserve()
        with self.assertRaises(ValueError):
            await self.store.clear_workspace(str(self.ws))

    async def test_record_stop_before_delayed_start_is_durable_fence(self):
        manager, runtime, intent = await self.recording_manager()
        await manager.desktop_action(self.ws, "record_stop", {"run_id": "run-1"})
        with self.assertRaises(ValueError):
            await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        runtime.spawn_process.assert_not_awaited()
        await manager.desktop_action(self.ws, "release", intent)

    async def test_periodic_recorder_exit_cleanup_closes_intent(self):
        manager, runtime, intent = await self.recording_manager()
        runtime.process_wait = AsyncMock(return_value=0)
        await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        await asyncio.sleep(0)
        await manager.reap_expired()
        self.assertEqual((await self.store.recordings("one"))[0]["state"], "closed")
        self.assertEqual((await self.store.get("one"))["state"], "held")
        await manager.desktop_action(self.ws, "release", intent)

    async def test_stale_epoch_blocks_fresh_reserve_without_guest_effects(self):
        await self.reserve(epoch="old")
        await self.store.activate("one", "old", 1)
        manager = self.manager()
        with self.assertRaisesRegex(RuntimeError, "cleanup pending"):
            await manager.desktop_action(
                self.ws, "reserve", {**self.intent(), "lease_id": "new"}
            )
        with self.assertRaisesRegex(RuntimeError, "cleanup pending"):
            await manager.desktop_action(
                self.ws, "hold", {**self.intent(), "lease_id": "new"}
            )
        manager._ensure_desktop_process_locked.assert_not_awaited()
        self.assertIsNone(await self.store.get("new"))

    async def test_old_instance_stopped_proof_never_stops_replacement(self):
        await self.reserve(instance_id="old", epoch="old-epoch")
        await self.store.activate("one", "old-epoch", 1)
        manager = self.manager()
        runtime = SimpleNamespace(
            get_workspace_status=AsyncMock(
                return_value=SimpleNamespace(instance_id="old", status="exited")
            )
        )
        manager._get_runtime = lambda _: runtime
        replacement = DesktopSession(self.ws, "guest")
        manager._desktop_sessions[self.ws] = replacement
        await manager.recover_workspace(self.ws)
        self.assertEqual((await self.store.get("one"))["state"], "expired")
        self.assertIs(manager._desktop_sessions[self.ws], replacement)
        manager._stop_desktop_process.assert_not_awaited()

    async def test_old_instance_unknown_status_retains_membership(self):
        await self.reserve(instance_id="old", epoch="old-epoch")
        await self.store.activate("one", "old-epoch", 1)
        manager = self.manager()
        runtime = SimpleNamespace(
            get_workspace_status=AsyncMock(side_effect=RuntimeError("unreachable"))
        )
        manager._get_runtime = lambda _: runtime
        manager._desktop_sessions[self.ws] = DesktopSession(self.ws, "guest")
        await manager.recover_workspace(self.ws)
        self.assertEqual((await self.store.get("one"))["state"], "closing")
        self.assertTrue((await self.store.get("one"))["activated"])
        manager._stop_desktop_process.assert_not_awaited()

    async def test_stopped_guest_closes_recording_without_guest_exec(self):
        manager, runtime, intent = await self.recording_manager()
        await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        runtime.get_workspace_status = AsyncMock(
            return_value=SimpleNamespace(instance_id="guest", status="stopped")
        )
        runtime.close_managed_process.side_effect = RuntimeError("no guest exec")
        manager._is_desktop_session_live.side_effect = RuntimeError("unreachable")
        result = await manager.desktop_action(self.ws, "release", intent)
        self.assertTrue(result["ok"])
        self.assertEqual((await self.store.recordings("one"))[0]["state"], "closed")
        runtime.close_managed_process.assert_not_awaited()
        manager._stop_desktop_process.assert_not_awaited()

    async def test_terminal_viewer_can_advance_instance_only_at_higher_revision(self):
        await self.reserve(kind="viewer", instance_id="old")
        with self.assertRaises(ValueError):
            await self.reserve(kind="viewer", instance_id="new", revision=2)
        await self.store.mark_closing("one")
        await self.store.finish("one")
        with self.assertRaises(ValueError):
            await self.reserve(kind="viewer", instance_id="new")
        row = await self.reserve(kind="viewer", instance_id="new", revision=2)
        self.assertEqual(row["instance_id"], "new")
        self.assertFalse(row["activated"])

    async def test_lease_status_never_calls_guest(self):
        manager = self.manager()
        manager._get_runtime = lambda _: self.fail("Read status invoked guest runtime")
        await manager.desktop_action(self.ws, "lease_status", {"lease_id": "unknown"})

    async def test_stopped_status_is_proof_even_when_stream_hook_cannot_exec(self):
        await self.reserve(instance_id="old", epoch="old-epoch")
        await self.store.activate("one", "old-epoch", 1)
        manager = self.manager(AsyncMock(return_value=False))
        manager._get_runtime = lambda _: SimpleNamespace(
            get_workspace_status=AsyncMock(
                return_value=SimpleNamespace(instance_id="old", status="removed")
            )
        )
        await manager.recover_workspace(self.ws)
        self.assertEqual((await self.store.get("one"))["state"], "expired")

    async def test_recovery_without_ledger_does_not_touch_unknown_desktop(self):
        manager = self.manager()
        manager._get_runtime = lambda _: self.fail("Unknown desktop must not be killed")
        await manager.recover_workspace(self.ws)
        manager._stop_desktop_process.assert_not_awaited()

    async def test_database_file_private(self):
        await self.reserve()
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    async def lifecycle_manager(self):
        from src.models import WorkspaceInfo
        from src.services.workspace_lifecycle import WorkspaceLifecycle
        from src.services.workspace_registry import WorkspaceRegistry

        manager, runtime, intent = await self.recording_manager()
        info = WorkspaceInfo(self.ws, "guest", "running", runtime_type="qemu")
        registry = WorkspaceRegistry(runtimes={"qemu": runtime})
        registry._cache[self.ws] = info
        manager._get_cached = registry.get_cached
        runtime.stop_workspace = AsyncMock()
        runtime.remove_workspace = AsyncMock()
        runtime.get_workspace_status = AsyncMock(
            return_value=SimpleNamespace(instance_id="guest", status="running")
        )
        streams = SimpleNamespace(confirm_workspace_ended=AsyncMock(return_value=True))
        lifecycle = WorkspaceLifecycle(
            registry, runtimes={"qemu": runtime}, desktop=manager, streams=streams
        )
        lifecycle.remove_hook = AsyncMock()
        lifecycle.kill_all_hook = AsyncMock()
        lifecycle.close_streams_hook = AsyncMock(return_value=0)
        lifecycle.release_hook = AsyncMock(return_value=False)
        await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        self.addAsyncCleanup(self.cancel_record_watchers, manager)
        viewer = {**self.intent(), "lease_id": "viewer", "kind": "viewer"}
        await manager.desktop_action(self.ws, "hold", viewer)
        return manager, runtime, lifecycle, registry, streams

    async def cancel_record_watchers(self, manager):
        for task in manager._recording_waiters.values():
            task.cancel()
        await asyncio.gather(
            *manager._recording_waiters.values(), return_exceptions=True
        )

    async def test_remove_finalizes_recorders_viewers_before_cache_loss(self):
        manager, runtime, lifecycle, registry, streams = await self.lifecycle_manager()

        async def confirmed(ws, instance):
            self.assertIn(ws, registry._cache)
            runtime.remove_workspace.assert_awaited_once_with("guest")
            return True

        streams.confirm_workspace_ended.side_effect = confirmed
        await lifecycle.remove_workspace(self.ws)
        self.assertNotIn(self.ws, registry._cache)
        self.assertEqual(await self.store.list_unfinished(str(self.ws)), [])
        self.assertEqual(await self.store.unfinished_recordings(str(self.ws)), [])
        self.assertEqual(len(await self.store.list_workspace(str(self.ws))), 2)
        runtime.close_managed_process.assert_not_awaited()
        self.assertFalse(manager._recording_waiters)
        runtime.process_detach.assert_awaited_once()

    async def test_same_id_remove_allows_a_new_desktop_lease(self):
        manager, runtime, lifecycle, registry, streams = await self.lifecycle_manager()
        await lifecycle.remove_workspace(self.ws)
        self.assertEqual(await self.store.list_unfinished(str(self.ws)), [])
        from src.models import WorkspaceInfo

        registry._cache[self.ws] = WorkspaceInfo(
            self.ws, "guest-2", "running", runtime_type="qemu"
        )
        result = await manager.desktop_action(
            self.ws,
            "hold",
            {**self.intent(), "lease_id": "after-recreate", "kind": "viewer"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["lease_state"], "held")
        self.assertEqual(
            (await self.store.get("after-recreate"))["instance_id"], "guest-2"
        )

    async def test_stop_failed_preclose_final_proof_finishes_owners(self):
        manager, runtime, lifecycle, registry, streams = await self.lifecycle_manager()
        await lifecycle.stop_workspace(self.ws)
        self.assertEqual(registry._cache[self.ws].status, "exited")
        lifecycle.release_hook.assert_awaited_once()
        self.assertEqual(await self.store.list_unfinished(str(self.ws)), [])
        self.assertEqual(await self.store.unfinished_recordings(str(self.ws)), [])
        streams.confirm_workspace_ended.assert_awaited_once_with(self.ws, "guest")

    async def test_remove_failure_retains_registry_and_ownership(self):
        manager, runtime, lifecycle, registry, streams = await self.lifecycle_manager()
        runtime.remove_workspace.side_effect = RuntimeError("remove failed")
        with self.assertRaises(RuntimeError):
            await lifecycle.remove_workspace(self.ws)
        streams.confirm_workspace_ended.assert_not_awaited()
        self.assertIn(self.ws, registry._cache)
        self.assertEqual(len(await self.store.list_unfinished(str(self.ws))), 2)
        self.assertTrue(await self.store.unfinished_recordings(str(self.ws)))

    async def test_workspace_ended_mismatched_evidence_rejected(self):
        from src.services.sessions.desktop import WorkspaceEndedEvidence

        manager, runtime, lifecycle, registry, streams = await self.lifecycle_manager()
        with self.assertRaises(ValueError):
            await manager.workspace_ended(
                self.ws,
                "guest",
                "qemu",
                WorkspaceEndedEvidence(self.ws, "wrong", "qemu", "remove"),
            )
        self.assertEqual(len(await self.store.list_unfinished(str(self.ws))), 2)

    async def test_cleanup_unknown_confirms_before_forgetting_identity(self):
        manager, runtime, lifecycle, registry, streams = await self.lifecycle_manager()
        self.assertTrue(await lifecycle.cleanup_unknown_workspace(self.ws))
        streams.confirm_workspace_ended.assert_awaited_once_with(self.ws, "guest")
        self.assertNotIn(self.ws, registry._cache)
        self.assertFalse(await self.store.list_unfinished(str(self.ws)))

    async def test_stop_preclose_errors_do_not_skip_physical_proof(self):
        manager, runtime, lifecycle, registry, streams = await self.lifecycle_manager()
        lifecycle.close_streams_hook.side_effect = RuntimeError("streams unreachable")
        lifecycle.release_hook.side_effect = RuntimeError("desktop unreachable")
        await lifecycle.stop_workspace(self.ws)
        runtime.stop_workspace.assert_awaited_once_with("guest")
        self.assertFalse(await self.store.list_unfinished(str(self.ws)))

    async def test_failed_post_remove_confirmation_keeps_lookup(self):
        manager, runtime, lifecycle, registry, streams = await self.lifecycle_manager()
        streams.confirm_workspace_ended.return_value = False
        with self.assertRaisesRegex(RuntimeError, "confirmation failed"):
            await lifecycle.remove_workspace(self.ws)
        self.assertIn(self.ws, registry._cache)
        self.assertTrue(await self.store.list_unfinished(str(self.ws)))

    async def test_public_maintenance_duplicate_ticks_false_cleanup_no_restart(self):
        manager = self.manager(AsyncMock(return_value=False))
        await manager.desktop_action(self.ws, "hold", self.intent())
        await self.store.mark_closing("one")
        await asyncio.gather(
            manager.maintain_workspace(self.ws), manager.maintain_workspace(self.ws)
        )
        self.assertEqual((await self.store.get("one"))["state"], "closing")
        manager._stop_desktop_process.assert_not_awaited()
        self.assertEqual(manager._ensure_desktop_process_locked.await_count, 1)
        await manager.maintain_workspace(self.ws)
        self.assertTrue((await self.store.get("one"))["activated"])

    async def test_maintenance_terminal_history_never_probes_guest_or_cache(self):
        await self.reserve()
        await self.store.mark_closing("one")
        await self.store.finish("one")
        self.assertEqual(await self.store.unfinished_workspace_ids(), [])
        self.assertEqual(await self.store.list_expired(str(self.ws)), [])
        manager = self.manager()
        manager._get_cached = lambda _: self.fail("Terminal history accessed cache")
        manager._get_runtime = lambda _: self.fail("Terminal history probed guest")
        await manager.maintain_workspace(self.ws)
        await manager.recover()
        manager._ensure_desktop_process_locked.assert_not_awaited()

    async def test_maintenance_other_owner_progresses_while_first_hangs(self):
        await self.reserve(lease_id="first")
        await self.reserve(lease_id="second")
        await self.store.mark_closing("first")
        await self.store.mark_closing("second")
        blocked = asyncio.Event()
        second_closed = asyncio.Event()

        async def close(lease_id):
            if lease_id == "first":
                await blocked.wait()
            else:
                second_closed.set()
            return True

        manager = self.manager(close)
        tick = asyncio.create_task(manager.maintain_workspace(self.ws))
        await asyncio.wait_for(second_closed.wait(), 1)
        blocked.set()
        await tick
        self.assertEqual((await self.store.get("second"))["state"], "expired")

    async def test_scoped_expiry_does_not_clean_other_workspace(self):
        await self.reserve()
        await self.store.mark_closing("one")
        self.assertEqual(await self.store.list_expired(str(uuid.uuid4())), [])
        self.assertEqual(len(await self.store.list_expired(str(self.ws))), 1)

    async def test_recording_token_committed_before_spawn_and_passed_unchanged(self):
        import json

        manager, runtime, intent = await self.recording_manager()
        token = {"boot_id": "immutable-boot", "init_starttime": "987"}
        runtime.probe_managed_token.return_value = token

        async def spawn(*args, **kwargs):
            row = (await self.store.recordings("one"))[0]
            self.assertEqual(json.loads(row["guest_token"]), token)
            self.assertEqual(kwargs["expected_token"], token)
            return object()

        runtime.spawn_process.side_effect = spawn
        await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        runtime.probe_managed_token.assert_awaited_once_with("guest")
        await manager.desktop_action(self.ws, "release", intent)

    async def test_delayed_recording_probe_revalidates_ended_owner(self):
        manager, runtime, intent = await self.recording_manager()
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def probe(instance):
            entered.set()
            await proceed.wait()
            return {"boot_id": "boot", "init_starttime": "1"}

        runtime.probe_managed_token.side_effect = probe
        start = asyncio.create_task(
            manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        )
        await entered.wait()
        # Simulate independently persisted owner closure while probe I/O awaits.
        await self.store.mark_closing("one")
        proceed.set()
        with self.assertRaisesRegex(ValueError, "ended during guest token probe"):
            await start
        self.assertEqual(await self.store.recordings("one"), [])
        runtime.spawn_process.assert_not_awaited()
        await manager.desktop_action(self.ws, "release", intent)

    async def test_invalid_recording_token_never_publishes_or_spawns(self):
        manager, runtime, intent = await self.recording_manager()
        runtime.probe_managed_token.return_value = {"boot_id": "partial"}
        with self.assertRaises(ValueError):
            await manager.desktop_action(self.ws, "record_start", {"run_id": "run-1"})
        self.assertEqual(await self.store.recordings("one"), [])
        runtime.spawn_process.assert_not_awaited()
        await manager.desktop_action(self.ws, "release", intent)

    async def test_recording_schema_upgrade_leaves_legacy_token_unknown(self):
        import sqlite3

        with sqlite3.connect(self.store.path) as db:
            db.execute(
                "CREATE TABLE desktop_recordings (recording_id TEXT PRIMARY KEY, "
                "lease_id TEXT, workspace_id TEXT, instance_id TEXT, owner_id TEXT, "
                "epoch TEXT, control_path TEXT, path TEXT, state TEXT)"
            )
            db.execute(
                "INSERT INTO desktop_recordings VALUES "
                "('old','one',?,'guest','run','old','control','output','closing')",
                (str(self.ws),),
            )
        rows = await self.store.recordings()
        self.assertIsNone(rows[0]["guest_token"])

    def guest_manager(self, *, stop_kills_xvnc: bool = True):
        """Manager with the real stop path against a pre-a001a88 guest.

        The guest's stop script kills every process whose argv matches
        ``Xvnc.*:1`` (including its own wrapper shell) as old images did.
        """
        import re

        stop_script = "/usr/local/bin/opencuria-desktop-stop"
        guest = {"xvnc": True, "kills": stop_kills_xvnc}
        commands: list[list[str]] = []

        async def exec_command_wait(instance_id, command, workdir=None, env=None):
            commands.append(list(command))
            argv = " ".join(command)
            if command == [stop_script]:
                if guest["kills"]:
                    guest["xvnc"] = False
                return 0, ""
            if stop_script in argv and re.search(r"Xvnc.*:1", argv):
                guest["xvnc"] = False
                return -1, ""
            if "pgrep" in argv:
                return (1, "") if guest["xvnc"] else (0, "")
            return 0, ""

        runtime = SimpleNamespace(
            exec_command_wait=exec_command_wait,
            get_workspace_status=AsyncMock(
                return_value=SimpleNamespace(instance_id="guest", status="running")
            ),
        )
        manager = self.manager()
        del manager._stop_desktop_process
        manager._get_runtime = lambda _: runtime
        return manager, guest, commands

    def viewer(self, revision: int) -> dict:
        return {
            "lease_id": "viewer-tab",
            "kind": "viewer",
            "owner_id": "user",
            "epoch": "epoch",
            "revision": revision,
        }

    async def test_viewer_release_confirms_stop_on_old_guest_and_restarts(self):
        manager, guest, commands = self.guest_manager()
        await manager.desktop_action(self.ws, "hold", self.viewer(1))
        result = await manager.desktop_action(self.ws, "release", self.viewer(1))
        self.assertTrue(result["ok"])
        self.assertEqual(result["lease_state"], "released")
        self.assertFalse(guest["xvnc"])
        self.assertEqual(len(commands), 2)
        for argv in commands:
            joined = " ".join(argv)
            self.assertNotIn("Xvnc", joined)
            self.assertNotIn("Xtigervnc", joined)
        self.assertNotIn(self.ws, manager._desktop_sessions)
        held = await manager.desktop_action(self.ws, "hold", self.viewer(2))
        self.assertEqual((held["lease_state"], held["revision"]), ("held", 2))

    async def test_unconfirmed_stop_keeps_lease_closing_until_xvnc_exits(self):
        manager, guest, _commands = self.guest_manager(stop_kills_xvnc=False)
        await manager.desktop_action(self.ws, "hold", self.viewer(1))
        result = await manager.desktop_action(self.ws, "release", self.viewer(1))
        self.assertFalse(result["ok"])
        self.assertEqual(result["lease_state"], "closing")
        with self.assertRaisesRegex(ValueError, "cannot advance revision"):
            await manager.desktop_action(self.ws, "hold", self.viewer(2))
        guest["kills"] = True
        await manager.reap_expired()
        self.assertEqual((await self.store.get("viewer-tab"))["state"], "expired")
        held = await manager.desktop_action(self.ws, "hold", self.viewer(2))
        self.assertEqual(held["lease_state"], "held")
