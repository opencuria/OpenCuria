import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, call

from src.config import RunnerSettings
from src.models import DesktopSession, WorkspaceInfo
from src.service import WorkspaceService


class DummyRuntime:
    def __init__(self) -> None:
        self.exec_command_wait = AsyncMock()
        self.remove_workspace = AsyncMock()

    def get_container_ip(self, instance_id: str, workspace_id: str) -> str:
        return "172.22.0.2"

    def get_workspace_network_name(self, workspace_id: str) -> str:
        return f"opencuria-ws-{workspace_id}"


class WorkspaceServiceDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runtime = DummyRuntime()
        self.service = WorkspaceService(
            runtimes={"docker": self.runtime},
            settings=RunnerSettings(),
        )
        self.workspace_id = uuid.uuid4()
        self.service.registry._cache[self.workspace_id] = WorkspaceInfo(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
            status="running",
            runtime_type="docker",
        )

    async def test_heartbeat_false_probe_then_true_does_not_prune(self) -> None:
        session = DesktopSession(self.workspace_id, "instance-1", generation=5)
        self.service.desktop._desktop_sessions[self.workspace_id] = session
        self.service.desktop._desktop_generations[self.workspace_id] = 5
        probes = AsyncMock(side_effect=[False, True])
        self.service.registry.desktop_live = probes

        payload = await self.service.registry.get_workspace_heartbeat_statuses()

        self.assertIsNotNone(payload[0]["desktop"])
        self.assertTrue(payload[0]["desktop_session_tracked"])
        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], session)
        probes.assert_has_awaits([call(self.workspace_id)] * 2)

    async def test_heartbeat_stale_negative_racing_acquire_reuses_session(self) -> None:
        session = DesktopSession(self.workspace_id, "instance-1", generation=5)
        self.service.desktop._desktop_sessions[self.workspace_id] = session
        self.service.desktop._desktop_generations[self.workspace_id] = 5
        initial_probe_started = asyncio.Event()
        finish_initial_probe = asyncio.Event()
        heartbeat_probe_count = 0

        async def heartbeat_probe(workspace_id: uuid.UUID) -> bool:
            nonlocal heartbeat_probe_count
            self.assertEqual(workspace_id, self.workspace_id)
            heartbeat_probe_count += 1
            if heartbeat_probe_count == 1:
                initial_probe_started.set()
                await finish_initial_probe.wait()
                return False
            return True

        self.service.desktop._is_desktop_session_live = AsyncMock(return_value=True)
        self.service.registry.desktop_live = heartbeat_probe
        heartbeat = asyncio.create_task(self.service.registry.get_workspace_heartbeat_statuses())
        await asyncio.wait_for(initial_probe_started.wait(), timeout=1)

        acquired = await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="viewer"
        )
        self.assertIs(acquired, session)
        self.assertTrue(acquired.viewer_held)

        # The first negative was captured before a valid acquire reused the
        # same session. Locked re-probing must observe the healthy process.
        finish_initial_probe.set()
        payload = await heartbeat

        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], session)
        self.assertIsNotNone(payload[0]["desktop"])
        self.assertTrue(payload[0]["desktop_session_tracked"])
        self.assertTrue(payload[0]["desktop"]["viewer"])
        self.assertEqual(heartbeat_probe_count, 2)

    async def test_heartbeat_stale_negative_racing_successful_acquire_replacement(
        self,
    ) -> None:
        stale = DesktopSession(self.workspace_id, "instance-1", generation=5)
        self.service.desktop._desktop_sessions[self.workspace_id] = stale
        self.service.desktop._desktop_generations[self.workspace_id] = 5
        initial_probe_started = asyncio.Event()
        finish_initial_probe = asyncio.Event()
        heartbeat_probe_count = 0

        async def heartbeat_probe(workspace_id: uuid.UUID) -> bool:
            nonlocal heartbeat_probe_count
            self.assertEqual(workspace_id, self.workspace_id)
            heartbeat_probe_count += 1
            if heartbeat_probe_count == 1:
                initial_probe_started.set()
                await finish_initial_probe.wait()
                return False
            return True

        # The acquire observes the cached incarnation as stale, then finds
        # the replacement process live and installs a fresh session object.
        self.service.desktop._is_desktop_session_live = AsyncMock(
            side_effect=[False, True]
        )
        self.service.registry.desktop_live = heartbeat_probe
        heartbeat = asyncio.create_task(self.service.registry.get_workspace_heartbeat_statuses())
        await asyncio.wait_for(initial_probe_started.wait(), timeout=1)

        acquired = await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="viewer"
        )
        self.assertIsNot(acquired, stale)
        self.assertEqual(acquired.generation, 6)
        self.assertTrue(acquired.viewer_held)

        # Release the stale negative only after acquire has replaced the cache.
        # Reconciliation must recognize that identity change and probe the
        # new session, not apply the old negative to it.
        finish_initial_probe.set()
        payload = await heartbeat

        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], acquired)
        self.assertIsNotNone(payload[0]["desktop"])
        self.assertTrue(payload[0]["desktop_session_tracked"])
        self.assertTrue(payload[0]["desktop"]["viewer"])
        self.assertEqual(heartbeat_probe_count, 2)

    async def test_heartbeat_probe_exception_preserves_cached_session(self) -> None:
        session = DesktopSession(self.workspace_id, "instance-1", generation=5)
        self.service.desktop._desktop_sessions[self.workspace_id] = session
        self.service.desktop._desktop_generations[self.workspace_id] = 5
        probes = AsyncMock(side_effect=RuntimeError("transient runtime error"))
        self.service.registry.desktop_live = probes

        payload = await self.service.registry.get_workspace_heartbeat_statuses()

        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], session)
        self.assertTrue(payload[0]["desktop_session_tracked"])
        self.assertIsNone(payload[0]["desktop"])
        probes.assert_awaited_once_with(self.workspace_id)

    async def test_heartbeat_confirmed_stop_prunes_stale_session(self) -> None:
        session = DesktopSession(self.workspace_id, "instance-1", generation=5)
        self.service.desktop._desktop_sessions[self.workspace_id] = session
        self.service.desktop._desktop_generations[self.workspace_id] = 5
        self.service.desktop._is_desktop_session_live = AsyncMock(
            side_effect=[False, False]
        )
        self.service.registry.desktop_live = (
            self.service.desktop._is_desktop_session_live
        )

        payload = await self.service.registry.get_workspace_heartbeat_statuses()

        self.assertIsNone(payload[0]["desktop"])
        self.assertFalse(payload[0]["desktop_session_tracked"])
        self.assertEqual(payload[0]["desktop_generation"], 5)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)

    async def test_start_desktop_restarts_stale_cached_session(self) -> None:
        self.service.desktop._desktop_sessions[self.workspace_id] = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
        )
        self.runtime.exec_command_wait.side_effect = [
            (1, "dead"),
            (0, "started"),
        ]

        session = await self.service.desktop.start_desktop(self.workspace_id)

        self.assertEqual(session.workspace_id, self.workspace_id)
        self.assertEqual(self.runtime.exec_command_wait.await_count, 2)
        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], session)

    async def test_ensure_desktop_starts_xvnc_without_remote_resize(self) -> None:
        """Fresh Xvnc starts at a fixed geometry and rejects SetDesktopSize."""
        self.runtime.exec_command_wait.side_effect = [
            (1, "dead"),
            (0, ""),
            (0, "started"),
        ]

        await self.service.desktop.ensure_desktop_process(
            self.workspace_id,
            width=1280,
            height=720,
        )

        commands = [
            call.args[1] for call in self.runtime.exec_command_wait.await_args_list
        ]
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[1][0], "bash")
        self.assertIn("opencuria-desktop-stop", commands[1][2])
        self.assertNotIn("Xvnc", commands[1][2])
        start_command = commands[2]
        self.assertEqual(start_command[0], "bash")
        self.assertIn("-geometry 1280x720", start_command[2])
        self.assertIn("-AcceptSetDesktopSize=0", start_command[2])
        self.assertNotIn("opencuria-desktop-start", start_command)
        self.assertNotIn("opencuria-desktop-stop", start_command[2])

    async def test_start_command_waits_for_x11_socket_and_kasm_port(self) -> None:
        """Readiness needs the X11 socket and TCP 6901; xstartup runs once."""
        command = self.service.desktop._desktop_start_command(1920, 1080)
        self.assertIn("/tmp/.X11-unix/X1", command)
        self.assertIn("/dev/tcp/127.0.0.1/6901", command)
        self.assertIn(".xstartup-started", command)
        self.assertEqual(command.count("/root/.vnc/xstartup >>"), 1)
        # The marker is per-start: it must be cleared before Xvnc launches
        # so a restart re-runs xstartup instead of showing an empty desktop.
        marker_clear = command.index("rm -f /root/.vnc/.xstartup-started")
        xvnc_launch = command.index("/usr/bin/Xvnc :1")
        xstartup_launch = command.index("/root/.vnc/xstartup >>")
        self.assertLess(marker_clear, xvnc_launch)
        self.assertLess(xvnc_launch, xstartup_launch)

    async def test_start_command_creates_xauthority_for_xlib_clients(
        self,
    ) -> None:
        """The start command touches .Xauthority before Xvnc launches.

        Regression: Xvnc uses ``-SecurityTypes None``, but python-Xlib
        unconditionally opens ``$XAUTHORITY``/``~/.Xauthority`` — without
        the file every PyAutoGUI ``execute`` snippet dies with
        ``FileNotFoundError`` before even connecting.
        """
        command = self.service.desktop._desktop_start_command(1920, 1080)
        self.assertIn("touch /root/.Xauthority", command)
        xauth_touch = command.index("touch /root/.Xauthority")
        xvnc_launch = command.index("/usr/bin/Xvnc :1")
        self.assertLess(xauth_touch, xvnc_launch)

    async def test_concurrent_viewer_and_computeruse_start_xvnc_once(
        self,
    ) -> None:
        """Concurrent viewer start + computer-use hold single-flight one start."""
        started = asyncio.Event()
        entered_start = asyncio.Event()
        release_start = asyncio.Event()

        live = {"up": False}

        async def _exec(instance_id, command, **kwargs):
            text = command[2] if len(command) > 2 else " ".join(command)
            if "connect_ex" in text or "pgrep" in text:
                return (0, "alive") if live["up"] else (1, "dead")
            if "opencuria-desktop-stop" in text:
                return (0, "")
            if "Xvnc :1" in text:
                entered_start.set()
                started.set()
                await asyncio.wait_for(release_start.wait(), timeout=5)
                live["up"] = True
                return (0, "started")
            return (0, "")

        self.runtime.exec_command_wait.side_effect = _exec
        viewer_task = asyncio.create_task(self.service.desktop.start_desktop(self.workspace_id))
        await asyncio.wait_for(entered_start.wait(), timeout=5)
        hold_task = asyncio.create_task(
            self.service.desktop.acquire_desktop(
                self.workspace_id, holder="computeruse", run_id="run-1"
            )
        )
        # The hold must block on the starter's lock before the start runs.
        await asyncio.sleep(0.05)
        self.assertFalse(hold_task.done())
        release_start.set()
        viewer_session, hold_session = await asyncio.gather(viewer_task, hold_task)
        # Single-flight: exactly one Xvnc start serves both holders, and
        # both leases land on the same shared session object.
        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], viewer_session)
        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], hold_session)
        self.assertIs(viewer_session, hold_session)
        self.assertTrue(viewer_session.viewer_held)
        self.assertIn("run-1", viewer_session.computeruse_run_ids)
        # Only the real Xvnc launcher counts: liveness probes carry the
        # pgrep pattern as data, never as an executed server start.
        start_calls = [
            call
            for call in self.runtime.exec_command_wait.await_args_list
            if len(call.args[1]) > 2 and "/usr/bin/Xvnc :1 -geometry" in call.args[1][2]
        ]
        self.assertEqual(len(start_calls), 1)

    async def test_release_racing_start_does_not_stop_new_process(self) -> None:
        """A release for an old session must not stop a concurrently started one."""
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        old_session = self.service.desktop._desktop_sessions[self.workspace_id]
        # A concurrent start replaced the cached session (e.g. after the old
        # process died); the stale release must leave the fresh start alone.
        fresh = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
            viewer_held=True,
        )
        self.service.desktop._desktop_sessions[self.workspace_id] = fresh
        self.runtime.exec_command_wait.reset_mock()
        self.runtime.exec_command_wait.return_value = (0, "")

        await self.service.desktop._stop_desktop_process(
            self.workspace_id,
            interrupt_recordings=False,
            expected_session=old_session,
        )

        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], fresh)
        stop_calls = [
            call
            for call in self.runtime.exec_command_wait.await_args_list
            if call.args[1] == ["/usr/local/bin/opencuria-desktop-stop"]
        ]
        self.assertEqual(stop_calls, [])

    async def test_remove_workspace_reuses_desktop_lock(self) -> None:
        """Workspace removal keeps the per-workspace lock (never dropped).

        The lock entry is retained for the runner process lifetime:
        dropping after release cannot observe queued waiters via the
        public asyncio.Lock API (release only schedules the first
        waiter's wakeup), so a drop hands a third caller a different
        lock object and lifecycle ops run in parallel. One small entry
        per ever-seen workspace (cleared on restart) is the trade-off.
        """
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(self.workspace_id, holder="viewer")
        lock_before = await self.service.desktop._desktop_lock(self.workspace_id)

        await self.service.remove_workspace(self.workspace_id)

        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        # Never dropped: the same object survives removal.
        self.assertIs(self.service.desktop._desktop_locks.get(self.workspace_id), lock_before)
        self.assertIs(await self.service.desktop._desktop_lock(self.workspace_id), lock_before)

    async def test_remove_workspace_clears_state_atomically_under_lock(
        self,
    ) -> None:
        """Remove pops session+recordings together under the desktop lock."""
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        self.service.desktop._desktop_recordings[(self.workspace_id, "run-1")] = (
            4242,
            "/workspace/.opencuria/computeruse/run-1/session.mp4",
        )
        self.service.desktop._desktop_recordings[(self.workspace_id, "run-2")] = (
            4243,
            "/workspace/.opencuria/computeruse/run-2/session.mp4",
        )
        other_id = uuid.uuid4()
        self.service.desktop._desktop_recordings[(other_id, "run-9")] = (
            9999,
            "/workspace/.opencuria/computeruse/run-9/session.mp4",
        )
        lock_before = await self.service.desktop._desktop_lock(self.workspace_id)

        await self.service.remove_workspace(self.workspace_id)

        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        self.assertNotIn((self.workspace_id, "run-1"), self.service.desktop._desktop_recordings)
        self.assertNotIn((self.workspace_id, "run-2"), self.service.desktop._desktop_recordings)
        # Other workspaces are untouched.
        self.assertIn((other_id, "run-9"), self.service.desktop._desktop_recordings)
        # The lock entry is retained (never dropped): the same object
        # survives cleanup and is reused by later callers.
        self.assertIs(self.service.desktop._desktop_locks.get(self.workspace_id), lock_before)
        self.assertIs(await self.service.desktop._desktop_lock(self.workspace_id), lock_before)

    async def test_held_lock_blocks_remove_cache_pop(self) -> None:
        """Remove pops the cache only after a lock-holding ensure finishes.

        Regression: remove used to pop ``_cache`` before acquiring the
        desktop lock, sabotaging a concurrent ensure mid-critical-section
        (ensure needs ``_get_cached`` for the Xvnc start). Now remove
        waits on the lock first and pops only inside the hold.
        """
        self.runtime.exec_command_wait.return_value = (0, "alive")
        lock = await self.service.desktop._desktop_lock(self.workspace_id)
        order: list[str] = []
        ensure_done = asyncio.Event()

        async def _blocking_ensure(workspace_id, **kwargs):
            order.append("ensure-enter")
            await asyncio.sleep(0.1)
            order.append("ensure-exit")
            ensure_done.set()
            return await orig_ensure(workspace_id, **kwargs)

        async def _racing_remove() -> None:
            await self.service.remove_workspace(self.workspace_id)
            order.append("remove-done")

        # Hold the manager's desktop lock while remove tries to enter.
        orig_ensure = self.service.desktop._ensure_desktop_process_locked
        self.service.desktop._ensure_desktop_process_locked = _blocking_ensure  # type: ignore[method-assign]
        try:
            ensure_task = asyncio.create_task(
                self.service.desktop.ensure_desktop_process(self.workspace_id)
            )
            await asyncio.sleep(0.02)
            remove_task = asyncio.create_task(_racing_remove())
            await asyncio.sleep(0.02)
            # Remove must wait for the lock: cache still present mid-ensure.
            self.assertFalse(remove_task.done())
            self.assertIn(self.workspace_id, self.service.registry._cache)
            await asyncio.wait_for(asyncio.gather(ensure_task, remove_task), timeout=5)
        finally:
            self.service.desktop._ensure_desktop_process_locked = orig_ensure  # type: ignore[method-assign]
        # Ensure completed before remove popped the cache and cleared state.
        self.assertEqual(order, ["ensure-enter", "ensure-exit", "remove-done"])
        self.assertNotIn(self.workspace_id, self.service.registry._cache)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        self.assertTrue(self.runtime.remove_workspace.awaited)
        # The lock object itself is retained and reused (never dropped).
        self.assertIs(await self.service.desktop._desktop_lock(self.workspace_id), lock)

    async def test_start_after_remove_fails_without_resurrect(self) -> None:
        """A queued/new start after remove fails cleanly, no session revived."""
        self.runtime.exec_command_wait.return_value = (0, "alive")
        lock = await self.service.desktop._desktop_lock(self.workspace_id)
        ensure_started = asyncio.Event()
        release_ensure = asyncio.Event()

        orig_ensure_locked = self.service.desktop._ensure_desktop_process_locked

        async def _blocking_ensure(workspace_id, **kwargs):
            ensure_started.set()
            await asyncio.wait_for(release_ensure.wait(), timeout=5)
            return await orig_ensure_locked(workspace_id, **kwargs)

        self.service.desktop._ensure_desktop_process_locked = _blocking_ensure  # type: ignore[method-assign]
        try:
            ensure_task = asyncio.create_task(
                self.service.desktop.ensure_desktop_process(self.workspace_id)
            )
            await asyncio.wait_for(ensure_started.wait(), timeout=5)
            remove_task = asyncio.create_task(
                self.service.remove_workspace(self.workspace_id)
            )
            # Queued start/hold: arrives while ensure holds the lock and
            # remove is waiting behind it.
            queued_task = asyncio.create_task(
                self.service.desktop.acquire_desktop(
                    self.workspace_id, holder="computeruse", run_id="run-q"
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(ensure_task.done())
            self.assertFalse(remove_task.done())
            self.assertFalse(queued_task.done())
            release_ensure.set()
            await asyncio.wait_for(ensure_task, timeout=5)
            await asyncio.wait_for(remove_task, timeout=5)
            with self.assertRaisesRegex(ValueError, "not found"):
                await asyncio.wait_for(queued_task, timeout=5)
        finally:
            self.service.desktop._ensure_desktop_process_locked = orig_ensure_locked  # type: ignore[method-assign]
        self.assertNotIn(self.workspace_id, self.service.registry._cache)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        # Same retained lock object throughout.
        self.assertIs(await self.service.desktop._desktop_lock(self.workspace_id), lock)
        # A brand-new start after remove fails the same way.
        with self.assertRaisesRegex(ValueError, "not found"):
            await self.service.desktop.start_desktop(self.workspace_id)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)

    async def test_remove_interrupts_recordings_atomically_under_lock(
        self,
    ) -> None:
        """Recording interrupt + state clear hold the lock with no gap.

        No waiter can publish a new session between the recording SIGINT
        and the final sweep: remove holds the desktop lock across both.
        """
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        self.service.desktop._desktop_recordings[(self.workspace_id, "run-1")] = (
            4242,
            "/workspace/.opencuria/computeruse/run-1/session.mp4",
        )
        lock = await self.service.desktop._desktop_lock(self.workspace_id)
        observed_locked_during_interrupt: list[bool] = []
        interrupt_reached = asyncio.Event()

        orig_exec = self.service.desktop._exec_desktop_shell

        async def _spying_exec(workspace_id, command):
            if "kill -INT 4242" in command:
                observed_locked_during_interrupt.append(lock.locked())
                interrupt_reached.set()
                # A concurrent publisher trying to sneak a session in
                # between snapshot and sweep: it must block on the lock.
                publish_task = asyncio.create_task(
                    self.service.desktop.acquire_desktop(
                        workspace_id, holder="computeruse", run_id="run-sneak"
                    )
                )
                await asyncio.sleep(0.05)
                self.assertFalse(publish_task.done())
                self.assertIn(workspace_id, self.service.desktop._desktop_sessions)
                publish_task.cancel()
                await asyncio.gather(publish_task, return_exceptions=True)
            return await orig_exec(workspace_id, command)

        self.service.desktop._exec_desktop_shell = _spying_exec  # type: ignore[method-assign]
        try:
            await self.service.remove_workspace(self.workspace_id)
        finally:
            self.service.desktop._exec_desktop_shell = orig_exec  # type: ignore[method-assign]
        self.assertTrue(interrupt_reached.is_set())
        # The interrupt ran while remove held the desktop lock.
        self.assertEqual(observed_locked_during_interrupt, [True])
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        self.assertFalse(
            [
                key
                for key in self.service.desktop._desktop_recordings
                if key[0] == self.workspace_id
            ]
        )

    async def test_remove_interrupt_failure_still_clears_state(self) -> None:
        """Recording-interrupt errors still clear state + attempt runtime remove."""
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        self.service.desktop._desktop_recordings[(self.workspace_id, "run-1")] = (
            4242,
            "/workspace/.opencuria/computeruse/run-1/session.mp4",
        )

        async def _failing_interrupt(workspace_id):
            raise RuntimeError("boom")

        self.service.desktop._interrupt_desktop_recordings = _failing_interrupt  # type: ignore[method-assign]
        await self.service.remove_workspace(self.workspace_id)

        self.assertNotIn(self.workspace_id, self.service.registry._cache)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        self.assertFalse(
            [
                key
                for key in self.service.desktop._desktop_recordings
                if key[0] == self.workspace_id
            ]
        )
        self.assertTrue(self.runtime.remove_workspace.awaited)

    async def test_cleanup_unknown_holds_lock_and_keeps_identity(self) -> None:
        """cleanup_unknown serialises behind ensure and retains the lock."""
        self.runtime.exec_command_wait.return_value = (0, "alive")
        lock = await self.service.desktop._desktop_lock(self.workspace_id)
        order: list[str] = []

        async def _blocking_ensure(workspace_id, **kwargs):
            order.append("ensure-enter")
            await asyncio.sleep(0.1)
            order.append("ensure-exit")
            return await orig_ensure(workspace_id, **kwargs)

        # Patch the manager (canonical owner since Step 6a); see
        # ``test_held_lock_blocks_remove_cache_pop`` for the rationale.
        orig_ensure = self.service.desktop._ensure_desktop_process_locked
        self.service.desktop._ensure_desktop_process_locked = _blocking_ensure  # type: ignore[method-assign]
        try:
            ensure_task = asyncio.create_task(
                self.service.desktop.ensure_desktop_process(self.workspace_id)
            )
            await asyncio.sleep(0.02)
            cleanup_task = asyncio.create_task(
                self.service.cleanup_unknown_workspace(self.workspace_id)
            )
            await asyncio.sleep(0.02)
            self.assertFalse(cleanup_task.done())
            self.assertIn(self.workspace_id, self.service.registry._cache)
            results = await asyncio.wait_for(
                asyncio.gather(ensure_task, cleanup_task), timeout=5
            )
        finally:
            self.service.desktop._ensure_desktop_process_locked = orig_ensure  # type: ignore[method-assign]
        self.assertEqual(order, ["ensure-enter", "ensure-exit"])
        self.assertTrue(results[1])
        self.assertNotIn(self.workspace_id, self.service.registry._cache)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        self.assertNotIn(self.workspace_id, self.service.lifecycle._unreachable_since)
        self.assertTrue(self.runtime.remove_workspace.awaited)
        self.assertIs(await self.service.desktop._desktop_lock(self.workspace_id), lock)
        with self.assertRaisesRegex(ValueError, "not found"):
            await self.service.desktop.start_desktop(self.workspace_id)

    async def test_held_lock_blocks_remove_state_clearing(self) -> None:
        """A start holding the lock serialises remove cleanup behind it."""
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(self.workspace_id, holder="viewer")
        lock = await self.service.desktop._desktop_lock(self.workspace_id)
        order: list[str] = []

        async def _racing_remove() -> None:
            await self.service.remove_workspace(self.workspace_id)
            order.append("remove-done")

        await lock.acquire()
        try:
            remove_task = asyncio.create_task(_racing_remove())
            await asyncio.sleep(0.05)
            # Remove must wait for the lock: neither done nor state cleared.
            self.assertFalse(remove_task.done())
            self.assertIn(self.workspace_id, self.service.desktop._desktop_sessions)
            order.append("release-lock")
        finally:
            lock.release()
        await asyncio.wait_for(remove_task, timeout=5)
        self.assertEqual(order, ["release-lock", "remove-done"])
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        # The race-critical property is that while the lock was held,
        # remove waited instead of clearing state past it (asserted via
        # order + session above). The lock object itself is retained and
        # reused (never dropped), so later callers share one lock.
        self.assertIs(await self.service.desktop._desktop_lock(self.workspace_id), lock)

    async def test_queued_waiter_and_third_caller_share_one_lock(self) -> None:
        """Queued waiter + third caller never run on two lock objects.

        Regression pin for the unsafe drop-after-release: ``release()``
        only schedules the first waiter's wakeup (the waiter sets its
        locked state later), so any dict removal in that window hands a
        third caller a fresh lock while the woken waiter still holds the
        old one. The fix never drops entries, so the queued waiter, the
        third caller, and every later acquire share one lock object.
        """
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        lock = await self.service.desktop._desktop_lock(self.workspace_id)
        entered: list[str] = []
        release_first: tuple[asyncio.Event, asyncio.Event] = (
            asyncio.Event(),
            asyncio.Event(),
        )
        entered_first, proceed_first = release_first
        release_second: tuple[asyncio.Event, asyncio.Event] = (
            asyncio.Event(),
            asyncio.Event(),
        )
        entered_second, proceed_second = release_second

        orig_ensure = self.service.desktop._ensure_desktop_process_locked

        async def _blocking_ensure(workspace_id, **kwargs):
            entry = await self.service.desktop._desktop_lock(workspace_id)
            entered.append(f"ensure:{id(entry)}")
            if len(entered) == 1:
                entered_first.set()
                await asyncio.wait_for(proceed_first.wait(), timeout=5)
            else:
                entered_second.set()
                await asyncio.wait_for(proceed_second.wait(), timeout=5)
            return await orig_ensure(workspace_id, **kwargs)

        self.service.desktop._ensure_desktop_process_locked = _blocking_ensure  # type: ignore[method-assign]
        try:
            first = asyncio.create_task(
                self.service.desktop.acquire_desktop(self.workspace_id, holder="viewer")
            )
            await asyncio.wait_for(entered_first.wait(), timeout=5)
            # First waiter is queued inside the lock; a third caller must
            # observe the SAME lock object (never a recreated one).
            third_lock = await self.service.desktop._desktop_lock(self.workspace_id)
            self.assertIs(third_lock, lock)
            second = asyncio.create_task(
                self.service.desktop.acquire_desktop(
                    self.workspace_id, holder="computeruse", run_id="run-2"
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            proceed_first.set()
            await asyncio.wait_for(entered_second.wait(), timeout=5)
            # The second waiter also runs under the same lock object.
            self.assertEqual(len(entered), 2)
            self.assertEqual(entered[0], entered[1])
            proceed_second.set()
            await asyncio.gather(first, second)
        finally:
            self.service.desktop._ensure_desktop_process_locked = orig_ensure  # type: ignore[method-assign]
        self.assertIs(await self.service.desktop._desktop_lock(self.workspace_id), lock)
        session = self.service.desktop._desktop_sessions[self.workspace_id]
        self.assertTrue(session.viewer_held)
        self.assertIn("run-1", session.computeruse_run_ids)
        self.assertIn("run-2", session.computeruse_run_ids)

    async def test_concurrent_acquires_serialise_through_one_lock(self) -> None:
        """Release/start ordering is pinned by the shared lock object."""
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        lock = await self.service.desktop._desktop_lock(self.workspace_id)
        seen_locks: list = []

        orig_ensure = self.service.desktop._ensure_desktop_process_locked

        async def _spying_ensure(workspace_id, **kwargs):
            seen_locks.append(await self.service.desktop._desktop_lock(workspace_id))
            return await orig_ensure(workspace_id, **kwargs)

        self.service.desktop._ensure_desktop_process_locked = _spying_ensure  # type: ignore[method-assign]
        try:
            await asyncio.gather(
                self.service.desktop.acquire_desktop(self.workspace_id, holder="viewer"),
                self.service.desktop.acquire_desktop(
                    self.workspace_id, holder="computeruse", run_id="run-2"
                ),
            )
        finally:
            self.service.desktop._ensure_desktop_process_locked = orig_ensure  # type: ignore[method-assign]
        self.assertTrue(seen_locks)
        for entry in seen_locks:
            self.assertIs(entry, lock)
        session = self.service.desktop._desktop_sessions[self.workspace_id]
        self.assertTrue(session.viewer_held)
        self.assertIn("run-1", session.computeruse_run_ids)
        self.assertIn("run-2", session.computeruse_run_ids)

    async def test_lifecycle_payload_reuses_single_helper(self) -> None:
        """State announcements share one payload builder (no duplication)."""
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )

        payload = self.service.desktop.get_desktop_state_payload(self.workspace_id)

        self.assertEqual(
            payload,
            {
                "workspace_id": str(self.workspace_id),
                "port": 6901,
                "container_ip": "172.22.0.2",
                "network_name": f"opencuria-ws-{self.workspace_id}",
                "viewer": False,
                "computer_use": True,
                "generation": payload["generation"],
                "epoch": payload["epoch"],
                "mcp": False,
                "holder_count": 1,
            },
        )
        self.assertEqual(payload["generation"], 1)

    async def test_generation_increments_on_stale_restart(self) -> None:
        """A stale cache entry yields a new incarnation, not the same object."""
        stale = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
            generation=3,
        )
        self.service.desktop._desktop_sessions[self.workspace_id] = stale
        # First liveness probe (cached session) dead, second probe clean,
        # then stop exec + start exec.
        self.runtime.exec_command_wait.side_effect = [
            (1, "dead"),
            (1, "dead"),
            (0, ""),
            (0, "started"),
        ]

        session = await self.service.desktop.ensure_desktop_process(self.workspace_id)

        self.assertIsNot(session, stale)
        self.assertEqual(session.generation, 4)
        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], session)

    async def test_idempotent_ensure_keeps_generation_and_object(self) -> None:
        """A live cached session is reused untouched (same object+generation)."""
        live = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
            generation=7,
        )
        self.service.desktop._desktop_sessions[self.workspace_id] = live
        self.runtime.exec_command_wait.return_value = (0, "alive")

        session = await self.service.desktop.ensure_desktop_process(self.workspace_id)

        self.assertIs(session, live)
        self.assertEqual(session.generation, 7)

    async def test_heartbeat_payload_prunes_stale_desktop_sessions(self) -> None:
        self.service.desktop._desktop_sessions[self.workspace_id] = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
        )
        self.service.registry.desktop_manager = self.service.desktop
        self.service.desktop._desktop_generations[self.workspace_id] = 0
        self.service.registry.desktop_live = AsyncMock(return_value=False)
        self.service.registry.get_cached(self.workspace_id).status = "stopped"

        payload = await self.service.registry.get_workspace_heartbeat_statuses()

        self.assertEqual(
            payload,
            [
                {
                    "workspace_id": str(self.workspace_id),
                    "status": "stopped",
                    "runtime_type": "docker",
                    "desktop": None,
                    "desktop_epoch": self.service.desktop.epoch,
                    "desktop_generation": 0,
                    "desktop_session_tracked": False,
                    "processes": [],
                }
            ],
        )
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)

    async def test_recover_desktop_sessions_from_runtime_does_not_probe_or_prune(
        self,
    ) -> None:
        # Observation/reconciliation belongs to the heartbeat path; reconnect
        # recovery only repairs durable lease bookkeeping and does not infer cache
        # session removal from a one-shot liveness probe.
        session = DesktopSession(self.workspace_id, "instance-1", generation=5)
        self.service.desktop._desktop_sessions[self.workspace_id] = session
        self.service.desktop._desktop_generations[self.workspace_id] = 5
        self.runtime.exec_command_wait.return_value = (1, "transient refusal")

        await self.service.registry.recover_desktop_sessions_from_runtime()

        self.assertIs(self.service.desktop._desktop_sessions[self.workspace_id], session)
        self.assertEqual(
            self.service.desktop.get_desktop_generation(self.workspace_id), 5
        )

    async def test_start_desktop_recovers_live_session_missing_from_cache(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")

        session = await self.service.desktop.start_desktop(self.workspace_id)

        self.assertEqual(session.workspace_id, self.workspace_id)
        self.assertEqual(self.runtime.exec_command_wait.await_count, 1)
        self.assertTrue(session.viewer_held)

    async def test_viewer_stop_keeps_process_when_computer_use_holds(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(self.workspace_id, holder="viewer")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        self.runtime.exec_command_wait.reset_mock()
        self.runtime.exec_command_wait.return_value = (0, "alive")

        result = await self.service.desktop.stop_desktop(self.workspace_id)

        self.assertFalse(result.stopped)
        self.assertTrue(result.process_alive)
        self.assertTrue(result.computer_use_active)
        self.assertFalse(result.viewer_held)
        self.assertIn(self.workspace_id, self.service.desktop._desktop_sessions)
        stop_calls = [
            call
            for call in self.runtime.exec_command_wait.await_args_list
            if call.args[1] == ["/usr/local/bin/opencuria-desktop-stop"]
        ]
        self.assertEqual(stop_calls, [])

    async def test_computer_use_release_stops_process_without_viewer(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        self.runtime.exec_command_wait.reset_mock()
        self.runtime.exec_command_wait.return_value = (0, "")

        result = await self.service.desktop.release_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )

        self.assertTrue(result.stopped)
        self.assertFalse(result.process_alive)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        # The lock entry is retained (never dropped): later callers reuse
        # the same object so no parallel lifecycle ops are possible.
        lock = await self.service.desktop._desktop_lock(self.workspace_id)
        self.assertIs(self.service.desktop._desktop_locks.get(self.workspace_id), lock)
        self.assertEqual(
            self.runtime.exec_command_wait.await_args.args[1],
            ["/usr/local/bin/opencuria-desktop-stop"],
        )

    async def test_computer_use_release_keeps_process_with_viewer(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(self.workspace_id, holder="viewer")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )

        result = await self.service.desktop.release_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )

        self.assertFalse(result.stopped)
        self.assertTrue(result.process_alive)
        self.assertTrue(result.viewer_held)
        self.assertIn(self.workspace_id, self.service.desktop._desktop_sessions)

    async def test_parallel_computer_use_runs_stop_after_last_release(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-a"
        )
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-b"
        )

        first = await self.service.desktop.release_desktop(
            self.workspace_id, holder="computeruse", run_id="run-a"
        )
        self.assertFalse(first.stopped)
        self.assertTrue(first.computer_use_active)

        self.runtime.exec_command_wait.return_value = (0, "")
        second = await self.service.desktop.release_desktop(
            self.workspace_id, holder="computeruse", run_id="run-b"
        )
        self.assertTrue(second.stopped)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)

    async def test_force_stop_kills_process_and_recordings(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        await self.service.desktop.acquire_desktop(self.workspace_id, holder="viewer")
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )
        self.service.desktop._desktop_recordings[(self.workspace_id, "run-1")] = (
            4242,
            "/workspace/.opencuria/computeruse/run-1/session.mp4",
        )
        self.runtime.exec_command_wait.reset_mock()
        self.runtime.exec_command_wait.return_value = (0, "")

        result = await self.service.desktop.release_desktop(
            self.workspace_id, holder="viewer", force=True
        )

        self.assertTrue(result.stopped)
        self.assertNotIn(self.workspace_id, self.service.desktop._desktop_sessions)
        self.assertEqual(self.service.desktop._desktop_recordings, {})
        commands = [
            call.args[1] for call in self.runtime.exec_command_wait.await_args_list
        ]
        self.assertTrue(
            any(
                isinstance(cmd, list) and len(cmd) == 3 and "kill -INT 4242" in cmd[2]
                for cmd in commands
            )
        )
        self.assertIn(
            ["/usr/local/bin/opencuria-desktop-stop"],
            commands,
        )

    async def test_ensure_does_not_acquire_viewer_lease(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")

        session = await self.service.desktop.ensure_desktop_process(self.workspace_id)

        self.assertFalse(session.viewer_held)
        self.assertEqual(session.computeruse_run_ids, set())

    async def test_heartbeat_includes_lease_flags(self) -> None:
        self.runtime.exec_command_wait.return_value = (0, "alive")
        self.service.registry.desktop_manager = self.service.desktop
        await self.service.desktop.acquire_desktop(
            self.workspace_id, holder="computeruse", run_id="run-1"
        )

        payload = await self.service.registry.get_workspace_heartbeat_statuses()

        desktop = payload[0]["desktop"]
        self.assertEqual(desktop["port"], 6901)
        self.assertFalse(desktop["viewer"])
        self.assertTrue(desktop["computer_use"])
